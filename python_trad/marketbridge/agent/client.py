"""
Claude Agent Client — Direct Anthropic API for trading decisions.

Uses:
- claude-sonnet-4-6 for confluence 5-7 (speed + cost)
- claude-opus-4-5 for confluence 8-9 (Opus 4.6 — max accuracy)

JSON mode enforced. Retry with exponential backoff on 429/500.
Falls back when MCP bridge routes here.
"""

import json
import logging
import asyncio
from datetime import datetime
from typing import Optional

import anthropic

from agent.prompts import get_system_prompt
import config

logger = logging.getLogger(__name__)


class AgentClient:
    """
    Direct Anthropic API client for Claude trading decisions.

    Usage:
        client = AgentClient()
        decision = await client.decide(context, scenario_type, confluence_score)
    """

    def __init__(self):
        self._client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self._call_count = 0
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        logger.info("AgentClient initialized (Anthropic API)")

    async def decide(
        self,
        context: dict,
        scenario_type: str = "GENERIC",
        confluence_score: float = 5,
    ) -> Optional[dict]:
        """
        Send context to Claude and get a structured trading decision.

        Args:
            context: Full context JSON from ContextBuilder
            scenario_type: Scenario type for prompt selection
            confluence_score: Used to select model tier

        Returns:
            Parsed decision dict, or None if all retries fail.
        """
        # Select model based on confluence score
        model = self._select_model(confluence_score)

        # Get system prompt with scenario addendum
        system_prompt = get_system_prompt(scenario_type)

        # Build user message
        user_message = (
            f"Analyze this market situation and respond with a JSON trading decision.\n\n"
            f"```json\n{json.dumps(context, indent=2)}\n```"
        )

        # Retry with backoff
        max_retries = 3
        for attempt in range(max_retries):
            try:
                decision = await self._call_api(model, system_prompt, user_message)

                if decision:
                    self._call_count += 1
                    logger.info(
                        f"Agent call #{self._call_count}: "
                        f"model={model.split('-')[1]}, "
                        f"decision={decision.get('decision')}, "
                        f"confidence={decision.get('confidence')}"
                    )
                    return decision

            except anthropic.RateLimitError:
                delay = (2 ** attempt) * 5  # 5, 10, 20 seconds
                logger.warning(f"Rate limited. Retrying in {delay}s...")
                await asyncio.sleep(delay)

            except anthropic.APIStatusError as e:
                if e.status_code >= 500:
                    delay = (2 ** attempt) * 3
                    logger.warning(f"API error {e.status_code}. Retrying in {delay}s...")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"API error: {e}")
                    break

            except Exception as e:
                logger.error(f"Agent call failed: {e}", exc_info=True)
                break

        logger.error("All agent call retries exhausted")
        return None

    async def _call_api(
        self, model: str, system_prompt: str, user_message: str
    ) -> Optional[dict]:
        """Make the actual API call and parse the JSON response."""
        loop = asyncio.get_event_loop()

        # Run synchronous Anthropic call in executor
        response = await loop.run_in_executor(
            None,
            lambda: self._client.messages.create(
                model=model,
                max_tokens=1024,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            ),
        )

        # Track usage
        self._total_input_tokens += response.usage.input_tokens
        self._total_output_tokens += response.usage.output_tokens

        # Extract text content
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text

        if not text:
            logger.error("Empty response from agent")
            return None

        # Parse JSON from response
        return self._parse_response(text)

    def _parse_response(self, text: str) -> Optional[dict]:
        """
        Parse JSON from the agent's response.

        Handles both raw JSON and JSON embedded in markdown code blocks.
        """
        # Try direct parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try extracting from code block
        import re
        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        # Try finding JSON object in text
        brace_start = text.find("{")
        brace_end = text.rfind("}") + 1
        if brace_start >= 0 and brace_end > brace_start:
            try:
                return json.loads(text[brace_start:brace_end])
            except json.JSONDecodeError:
                pass

        logger.error(f"Could not parse JSON from agent response: {text[:200]}...")
        return None

    @staticmethod
    def _select_model(confluence_score: float) -> str:
        """Select model based on confluence score."""
        if confluence_score >= config.AGENT_HIGH_CONVICTION_THRESHOLD:
            return config.AGENT_MODEL_HIGH_CONVICTION  # claude-opus-4-5 (Opus 4.6)
        return config.AGENT_MODEL_DEFAULT  # claude-sonnet-4-6

    @property
    def stats(self) -> dict:
        """Get usage statistics."""
        return {
            "total_calls": self._call_count,
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "estimated_cost_usd": round(
                (self._total_input_tokens * 3 + self._total_output_tokens * 15) / 1_000_000,
                4,
            ),
        }
