"""
Sentiment & Options Flow — Stream C: PCR, OI velocity, Max Pain, IV percentile.

Processes option chain data (refreshed every 30 seconds via REST) to compute:
- PCR time series (Put-Call Ratio from OI)
- OI velocity (rate of change per strike — buildup/unwinding)
- Max Pain calculation (price where writers lose least)
- IV percentile (current ATM IV vs. 20-day historical)

Also handles IV history persistence to SQLite for percentile calculations.
"""

import logging
import sqlite3
from datetime import datetime, date, timedelta
from typing import Optional
from pathlib import Path

from data.state import MarketState
import config

logger = logging.getLogger(__name__)


class SentimentEngine:
    """
    Stream C — Options sentiment and flow analysis.

    Processes option chain snapshots to derive sentiment indicators.
    """

    def __init__(self, state: MarketState):
        self._state = state

        # PCR history (tracks intraday trend)
        self._pcr_history: dict[str, list[dict]] = {"NIFTY": [], "SENSEX": []}

        # OI snapshots for velocity calculation
        self._prev_oi: dict[str, dict] = {"NIFTY": {}, "SENSEX": {}}

        # IV history DB
        self._db_path = config.IV_HISTORY_DB
        self._init_db()

        logger.info("SentimentEngine (Stream C) initialized")

    def _init_db(self) -> None:
        """Initialize SQLite DB for IV history."""
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS iv_history (
                    date TEXT NOT NULL,
                    index_name TEXT NOT NULL,
                    atm_iv REAL NOT NULL,
                    timestamp TEXT NOT NULL,
                    PRIMARY KEY (date, index_name)
                )
            """)
            conn.commit()
            conn.close()
            logger.debug("IV history DB ready")
        except Exception as e:
            logger.error(f"IV history DB init failed: {e}")

    async def on_chain_update(self, index: str, chain: dict) -> Optional[dict]:
        """
        Process an option chain snapshot update.

        Called every 30 seconds when REST fetches fresh chain data.

        Args:
            index: "NIFTY" or "SENSEX"
            chain: Parsed chain dict {strike: {"CE": {...}, "PE": {...}}}

        Returns:
            Dict with sentiment analysis output.
        """
        if not chain:
            return None

        # Get current index LTP for ATM calculation
        ltp = self._state.get("index", index, "ltp") or 0
        if ltp == 0:
            return None

        strike_interval = (
            config.NIFTY_STRIKE_INTERVAL if index == "NIFTY"
            else config.SENSEX_STRIKE_INTERVAL
        )

        atm_strike = round(ltp / strike_interval) * strike_interval

        # ── Compute PCR ────────────────────────────────────────────
        pcr_data = self._compute_pcr(chain)

        # Track PCR history
        self._pcr_history[index].append({
            "time": datetime.now().isoformat(),
            "pcr": pcr_data["pcr"],
        })
        if len(self._pcr_history[index]) > 100:
            self._pcr_history[index] = self._pcr_history[index][-100:]

        # ── Compute OI Velocity ────────────────────────────────────
        oi_velocity = self._compute_oi_velocity(index, chain)

        # ── Compute Max Pain ───────────────────────────────────────
        max_pain = self._compute_max_pain(chain, strike_interval)

        # ── Compute IV Percentile ──────────────────────────────────
        atm_iv = self._get_atm_iv(chain, atm_strike, index)
        iv_percentile = self._compute_iv_percentile(index, atm_iv)

        # ── PCR Trend ──────────────────────────────────────────────
        pcr_trend = self._pcr_trend(index)

        # ── Highest OI Strikes ─────────────────────────────────────
        highest_ce_oi = self._highest_oi_strike(chain, "CE")
        highest_pe_oi = self._highest_oi_strike(chain, "PE")

        # Build sentiment output
        sentiment = {
            "pcr": pcr_data["pcr"],
            "pcr_trend": pcr_trend,
            "pcr_interpretation": pcr_data["interpretation"],
            "total_ce_oi": pcr_data["total_ce_oi"],
            "total_pe_oi": pcr_data["total_pe_oi"],
            "max_pain": max_pain,
            "max_pain_distance": round(max_pain - ltp, 2) if max_pain else 0,
            "max_pain_direction": "ABOVE" if max_pain and max_pain > ltp else "BELOW",
            "atm_iv": atm_iv,
            "iv_percentile": iv_percentile,
            "oi_velocity": oi_velocity,
            "highest_ce_oi_strike": highest_ce_oi,
            "highest_pe_oi_strike": highest_pe_oi,
            "atm_strike": atm_strike,
            "computed_at": datetime.now().isoformat(),
        }

        # Store in state
        await self._state.update("analysis", "sentiment", value=sentiment)

        # Save previous OI for velocity calculation
        self._prev_oi[index] = {
            strike: {
                "ce_oi": opts.get("CE", {}).get("oi", 0),
                "pe_oi": opts.get("PE", {}).get("oi", 0),
            }
            for strike, opts in chain.items()
        }

        return sentiment

    # ─── PCR Calculation ───────────────────────────────────────────

    @staticmethod
    def _compute_pcr(chain: dict) -> dict:
        """
        Compute Put-Call Ratio from open interest.

        PCR = Total Put OI / Total Call OI
        """
        total_ce_oi = 0
        total_pe_oi = 0

        for strike, opts in chain.items():
            total_ce_oi += opts.get("CE", {}).get("oi", 0)
            total_pe_oi += opts.get("PE", {}).get("oi", 0)

        pcr = round(total_pe_oi / max(total_ce_oi, 1), 3)

        # Interpretation
        if pcr > 1.5:
            interpretation = "EXTREME_BEARISH_SENTIMENT"
        elif pcr > 1.2:
            interpretation = "BEARISH_SENTIMENT"
        elif pcr > 0.8:
            interpretation = "NEUTRAL"
        elif pcr > 0.5:
            interpretation = "BULLISH_SENTIMENT"
        else:
            interpretation = "EXTREME_BULLISH_SENTIMENT"

        return {
            "pcr": pcr,
            "total_ce_oi": total_ce_oi,
            "total_pe_oi": total_pe_oi,
            "interpretation": interpretation,
        }

    def _pcr_trend(self, index: str) -> str:
        """Determine if PCR is rising, falling, or flat over last 5 readings."""
        history = self._pcr_history[index]
        if len(history) < 3:
            return "INSUFFICIENT_DATA"

        recent = [h["pcr"] for h in history[-5:]]
        if len(recent) < 3:
            return "INSUFFICIENT_DATA"

        # Simple trend: compare first half avg to second half avg
        mid = len(recent) // 2
        first_half = sum(recent[:mid]) / mid
        second_half = sum(recent[mid:]) / (len(recent) - mid)

        diff = second_half - first_half
        if diff > 0.05:
            return "RISING"   # More puts being added = bearish positioning
        elif diff < -0.05:
            return "FALLING"  # More calls being added = bullish positioning
        else:
            return "STABLE"

    # ─── OI Velocity ───────────────────────────────────────────────

    def _compute_oi_velocity(self, index: str, chain: dict) -> list[dict]:
        """
        Compute rate of OI change per strike since last snapshot.

        Returns top 5 strikes with highest absolute OI change.
        """
        prev = self._prev_oi.get(index, {})
        if not prev:
            return []

        velocity = []
        for strike, opts in chain.items():
            ce_oi = opts.get("CE", {}).get("oi", 0)
            pe_oi = opts.get("PE", {}).get("oi", 0)

            prev_data = prev.get(strike, {"ce_oi": 0, "pe_oi": 0})
            ce_change = ce_oi - prev_data.get("ce_oi", 0)
            pe_change = pe_oi - prev_data.get("pe_oi", 0)

            total_change = abs(ce_change) + abs(pe_change)
            if total_change > 0:
                velocity.append({
                    "strike": strike,
                    "ce_oi_change": ce_change,
                    "pe_oi_change": pe_change,
                    "total_change": total_change,
                    "signal": self._interpret_oi_change(ce_change, pe_change),
                })

        # Sort by total change, return top 5
        velocity.sort(key=lambda x: x["total_change"], reverse=True)
        return velocity[:5]

    @staticmethod
    def _interpret_oi_change(ce_change: int, pe_change: int) -> str:
        """Interpret OI changes at a strike."""
        if ce_change > 0 and pe_change > 0:
            return "BOTH_BUILDUP"         # Both sides adding — range bound
        elif ce_change > 0 and pe_change < 0:
            return "CE_BUILDUP_PE_UNWIND"  # Bearish: writers adding calls, closing puts
        elif ce_change < 0 and pe_change > 0:
            return "CE_UNWIND_PE_BUILDUP"  # Bullish: writers closing calls, adding puts
        elif ce_change < 0 and pe_change < 0:
            return "BOTH_UNWINDING"        # Expiry close, both sides exiting
        else:
            return "NEUTRAL"

    # ─── Max Pain ──────────────────────────────────────────────────

    @staticmethod
    def _compute_max_pain(chain: dict, strike_interval: int) -> float:
        """
        Calculate Max Pain — the price at which option writers lose the least.

        For each possible expiry price, calculate total loss for all writers.
        Max Pain = price with minimum total writer loss.
        """
        if not chain:
            return 0

        strikes = sorted(int(k) for k in chain.keys())
        if not strikes:
            return 0

        min_pain = float("inf")
        max_pain_strike = strikes[len(strikes) // 2]  # Default to middle

        for test_price in strikes:
            total_pain = 0

            for strike_key, opts in chain.items():
                strike = int(strike_key)
                ce_oi = opts.get("CE", {}).get("oi", 0)
                pe_oi = opts.get("PE", {}).get("oi", 0)

                # Call writers lose if price > strike
                if test_price > strike:
                    total_pain += (test_price - strike) * ce_oi

                # Put writers lose if price < strike
                if test_price < strike:
                    total_pain += (strike - test_price) * pe_oi

            if total_pain < min_pain:
                min_pain = total_pain
                max_pain_strike = test_price

        return max_pain_strike

    # ─── IV Percentile ─────────────────────────────────────────────

    def _get_atm_iv(self, chain: dict, atm_strike: int, index: str = "SENSEX") -> float:
        """Get ATM implied volatility (average of CE and PE IV)."""
        atm = chain.get(atm_strike, {})
        ce_iv = atm.get("CE", {}).get("iv", 0)
        pe_iv = atm.get("PE", {}).get("iv", 0)

        if ce_iv and pe_iv:
            return round((ce_iv + pe_iv) / 2, 2)
        elif ce_iv:
            return round(ce_iv, 2)
        elif pe_iv:
            return round(pe_iv, 2)

        # Try adjacent strikes
        for offset in [1, -1, 2, -2]:
            strike_interval = (config.NIFTY_STRIKE_INTERVAL if index == "NIFTY"
                               else config.SENSEX_STRIKE_INTERVAL)
            adj_strike = atm_strike + offset * strike_interval
            adj = chain.get(adj_strike, {})
            ce_iv = adj.get("CE", {}).get("iv", 0)
            pe_iv = adj.get("PE", {}).get("iv", 0)
            if ce_iv or pe_iv:
                return round((ce_iv + pe_iv) / max(bool(ce_iv) + bool(pe_iv), 1), 2)

        return 0.0

    def _compute_iv_percentile(self, index: str, current_iv: float) -> int:
        """
        Compute IV percentile vs. last 20 trading sessions.

        IV percentile = % of days where IV was BELOW current IV.
        High percentile = IV is elevated = options are expensive.
        """
        if current_iv == 0:
            return 50  # Default to median

        try:
            conn = sqlite3.connect(str(self._db_path))
            cursor = conn.execute(
                "SELECT atm_iv FROM iv_history WHERE index_name = ? "
                "ORDER BY date DESC LIMIT 20",
                (index,)
            )
            rows = cursor.fetchall()
            conn.close()

            if not rows:
                return 50

            historical_ivs = [r[0] for r in rows]
            below_count = sum(1 for iv in historical_ivs if iv < current_iv)
            percentile = int((below_count / len(historical_ivs)) * 100)

            return percentile

        except Exception as e:
            logger.error(f"IV percentile computation failed: {e}")
            return 50

    def save_session_iv(self, index: str, atm_iv: float) -> None:
        """Save today's ATM IV to the history DB (called at session end)."""
        if atm_iv <= 0:
            return

        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute(
                "INSERT OR REPLACE INTO iv_history (date, index_name, atm_iv, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (date.today().isoformat(), index, atm_iv, datetime.now().isoformat())
            )
            conn.commit()
            conn.close()
            logger.info(f"Saved {index} session IV: {atm_iv:.2f}")
        except Exception as e:
            logger.error(f"Failed to save session IV: {e}")

    # ─── Utility ───────────────────────────────────────────────────

    @staticmethod
    def _highest_oi_strike(chain: dict, option_type: str) -> Optional[int]:
        """Find the strike with highest OI for a given option type (CE/PE)."""
        max_oi = 0
        max_strike = None

        for strike, opts in chain.items():
            oi = opts.get(option_type, {}).get("oi", 0)
            if oi > max_oi:
                max_oi = oi
                max_strike = strike

        return max_strike
