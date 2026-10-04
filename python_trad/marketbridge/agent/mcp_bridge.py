"""
MCP Bridge — FastAPI server bridging MarketBridge to Antigravity/direct API.

Runs on localhost:8765. Exposes:
- POST /decide — MarketBridge posts context JSON, bridge routes to
  Antigravity (via MCP tool call) or falls back to direct Anthropic API.
- GET /health — Reports bridge status and Antigravity availability.

Falls back to agent/client.py if Antigravity is offline.
"""

import json
import logging
import asyncio
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agent.client import AgentClient
from agent.memory import DecisionMemory
import config

logger = logging.getLogger(__name__)


# ─── Request/Response Models ──────────────────────────────────────

class DecideRequest(BaseModel):
    context: dict
    scenario_type: str = "GENERIC"
    confluence_score: float = 5.0


class DecideResponse(BaseModel):
    decision: str
    confidence: int
    entry_zone: Optional[dict] = None
    invalidation: Optional[float] = None
    targets: Optional[list] = None
    reasoning: str = ""
    trap_flags: Optional[list] = None
    source: str = "direct_api"  # "antigravity" or "direct_api"


class HealthResponse(BaseModel):
    status: str
    antigravity: bool
    uptime_seconds: float
    agent_calls: int


# ─── MCP Bridge ───────────────────────────────────────────────────

class MCPBridge:
    """
    FastAPI bridge server for AI agent decisions.

    Routes decisions to Antigravity (preferred) or direct API (fallback).
    """

    def __init__(self):
        self._agent_client = AgentClient()
        self._memory = DecisionMemory()
        self._antigravity_available = False
        self._start_time = datetime.now()
        self._app: Optional[FastAPI] = None
        logger.info("MCPBridge initialized")

    @property
    def memory(self) -> DecisionMemory:
        return self._memory

    @property
    def agent_client(self) -> AgentClient:
        return self._agent_client

    def create_app(self) -> FastAPI:
        """Create and configure the FastAPI application."""

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            logger.info("MCP Bridge starting on localhost:8765")
            # Check Antigravity availability on startup
            self._antigravity_available = await self._check_antigravity()
            yield
            logger.info("MCP Bridge shutting down")

        app = FastAPI(
            title="MarketBridge MCP Bridge",
            version="2.1",
            lifespan=lifespan,
        )

        @app.get("/health", response_model=HealthResponse)
        async def health():
            uptime = (datetime.now() - self._start_time).total_seconds()
            return HealthResponse(
                status="ok",
                antigravity=self._antigravity_available,
                uptime_seconds=uptime,
                agent_calls=self._agent_client.stats["total_calls"],
            )

        @app.post("/decide", response_model=DecideResponse)
        async def decide(request: DecideRequest):
            try:
                result = await self.route_decision(
                    context=request.context,
                    scenario_type=request.scenario_type,
                    confluence_score=request.confluence_score,
                )

                if result is None:
                    raise HTTPException(
                        status_code=503,
                        detail="Agent returned no decision"
                    )

                return result

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Decision error: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/stats")
        async def stats():
            return {
                "agent_stats": self._agent_client.stats,
                "memory_stats": self._memory.get_win_rate(),
                "antigravity_available": self._antigravity_available,
            }

        self._app = app
        return app

    async def route_decision(
        self,
        context: dict,
        scenario_type: str = "GENERIC",
        confluence_score: float = 5.0,
    ) -> Optional[DecideResponse]:
        """
        Route a decision request to the best available backend.

        Priority:
        1. Antigravity (if available)
        2. Direct Anthropic API (fallback)
        """
        source = "direct_api"
        decision_dict = None

        # Try Antigravity first
        if self._antigravity_available:
            decision_dict = await self._call_antigravity(context, scenario_type)
            if decision_dict:
                source = "antigravity"

        # Fallback to direct API
        if decision_dict is None:
            decision_dict = await self._agent_client.decide(
                context=context,
                scenario_type=scenario_type,
                confluence_score=confluence_score,
            )

        if decision_dict is None:
            return None

        # Record in memory
        self._memory.add_decision(
            decision=decision_dict.get("decision", "WAIT"),
            confidence=decision_dict.get("confidence", 0),
            scenario_type=scenario_type,
            reasoning=decision_dict.get("reasoning", ""),
            entry_zone=decision_dict.get("entry_zone"),
            invalidation=decision_dict.get("invalidation"),
            targets=decision_dict.get("targets"),
            confluence_score=confluence_score,
            full_response=decision_dict,
        )

        return DecideResponse(
            decision=decision_dict.get("decision", "WAIT"),
            confidence=decision_dict.get("confidence", 0),
            entry_zone=decision_dict.get("entry_zone"),
            invalidation=decision_dict.get("invalidation"),
            targets=decision_dict.get("targets"),
            reasoning=decision_dict.get("reasoning", ""),
            trap_flags=decision_dict.get("trap_flags"),
            source=source,
        )

    async def _call_antigravity(
        self, context: dict, scenario_type: str
    ) -> Optional[dict]:
        """
        Attempt to route decision through Antigravity MCP.

        This is a placeholder — actual MCP tool call integration
        depends on the Antigravity harness configuration.
        """
        # TODO: Implement actual MCP tool call when Antigravity is configured
        # For now, this always returns None (falls back to direct API)
        return None

    async def _check_antigravity(self) -> bool:
        """Check if Antigravity is available for MCP routing."""
        # TODO: Implement actual Antigravity availability check
        # For now, always returns False (direct API mode)
        logger.info("Antigravity check: not configured (using direct API)")
        return False


# ─── Module-level factory ─────────────────────────────────────────

_bridge_instance: Optional[MCPBridge] = None


def get_bridge() -> MCPBridge:
    """Get or create the singleton MCPBridge instance."""
    global _bridge_instance
    if _bridge_instance is None:
        _bridge_instance = MCPBridge()
    return _bridge_instance


def create_bridge_app() -> FastAPI:
    """Create the FastAPI app for the MCP bridge."""
    bridge = get_bridge()
    return bridge.create_app()
