"""
Decision Memory — Rolling store of last N agent decisions with outcomes.

Fed back into agent calls for self-correction. The agent sees its own
past decisions and their outcomes to avoid repeating mistakes.

Persisted per-session to a JSONL file.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)


class DecisionMemory:
    """
    Rolling memory of agent decisions with outcomes.

    Stores the last AGENT_MEMORY_SIZE (5) decisions.
    Each entry tracks: decision, confidence, actual outcome, P&L.
    """

    def __init__(self):
        self._decisions: list[dict] = []
        self._max_size = config.AGENT_MEMORY_SIZE
        self._log_file = self._get_log_path()
        logger.info(f"DecisionMemory initialized (max={self._max_size})")

    @staticmethod
    def _get_log_path() -> Path:
        """Get today's decision log path."""
        today = datetime.now().strftime("%Y%m%d")
        path = config.LOG_DIR / f"agent_calls_{today}.jsonl"
        return path

    def add_decision(
        self,
        decision: str,
        confidence: int,
        scenario_type: str,
        reasoning: str,
        entry_zone: Optional[dict] = None,
        invalidation: Optional[float] = None,
        targets: Optional[list] = None,
        confluence_score: float = 0,
        full_response: Optional[dict] = None,
    ) -> None:
        """
        Record a new agent decision.

        Args:
            decision: "BUY", "SELL", or "WAIT"
            confidence: 1-10
            scenario_type: e.g., "LIQUIDITY_TRAP"
            reasoning: Agent's reasoning text
            entry_zone: {"trigger_price": float, "direction": str}
            invalidation: Price that invalidates the trade
            targets: List of target prices
            confluence_score: The confluence score that triggered this call
            full_response: Complete agent response for logging
        """
        entry = {
            "id": len(self._decisions) + 1,
            "time": datetime.now().strftime("%H:%M"),
            "timestamp": datetime.now().isoformat(),
            "decision": decision,
            "confidence": confidence,
            "scenario_type": scenario_type,
            "reasoning": reasoning,
            "entry_zone": entry_zone,
            "invalidation": invalidation,
            "targets": targets or [],
            "confluence_score": confluence_score,
            "outcome": "pending",
            "pnl": 0,
            "exit_reason": None,
        }

        self._decisions.append(entry)

        # Keep only last N
        if len(self._decisions) > self._max_size:
            self._decisions = self._decisions[-self._max_size:]

        # Log to file
        self._log_to_file(entry, full_response)

        logger.info(
            f"Decision #{entry['id']}: {decision} (conf={confidence}, "
            f"scenario={scenario_type})"
        )

    def update_outcome(
        self,
        decision_id: Optional[int] = None,
        outcome: str = "completed",
        pnl: float = 0,
        exit_reason: str = "",
    ) -> None:
        """
        Update the outcome of a previous decision.

        Args:
            decision_id: ID of decision to update (default: last one)
            outcome: "win", "loss", "breakeven", "timeout", "cancelled"
            pnl: Realized P&L
            exit_reason: Why the position was exited
        """
        if not self._decisions:
            return

        if decision_id:
            for d in self._decisions:
                if d["id"] == decision_id:
                    d["outcome"] = outcome
                    d["pnl"] = pnl
                    d["exit_reason"] = exit_reason
                    break
        else:
            # Update the last decision
            self._decisions[-1]["outcome"] = outcome
            self._decisions[-1]["pnl"] = pnl
            self._decisions[-1]["exit_reason"] = exit_reason

        logger.info(f"Decision outcome updated: {outcome}, PnL=Rs.{pnl:.0f}")

    def get_memory(self) -> list[dict]:
        """Get the decision memory for agent context injection."""
        return [
            {
                "time": d["time"],
                "decision": d["decision"],
                "confidence": d["confidence"],
                "scenario": d["scenario_type"],
                "outcome": d["outcome"],
                "pnl": d["pnl"],
                "reasoning_summary": d["reasoning"][:100],
            }
            for d in self._decisions
        ]

    def get_win_rate(self) -> dict:
        """Get session win rate stats."""
        completed = [d for d in self._decisions if d["outcome"] != "pending"]
        if not completed:
            return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0}

        wins = sum(1 for d in completed if d["outcome"] == "win")
        losses = sum(1 for d in completed if d["outcome"] == "loss")
        total_pnl = sum(d["pnl"] for d in completed)

        return {
            "total": len(completed),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(completed) * 100, 1) if completed else 0,
            "total_pnl": total_pnl,
        }

    @property
    def last_decision(self) -> Optional[dict]:
        """Get the most recent decision."""
        return self._decisions[-1] if self._decisions else None

    @property
    def pending_count(self) -> int:
        """Count of decisions still pending outcome."""
        return sum(1 for d in self._decisions if d["outcome"] == "pending")

    def _log_to_file(self, entry: dict, full_response: Optional[dict]) -> None:
        """Append decision to JSONL log file."""
        try:
            log_entry = {**entry, "full_response": full_response}
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, default=str) + "\n")
        except Exception as e:
            logger.error(f"Failed to log decision: {e}")
