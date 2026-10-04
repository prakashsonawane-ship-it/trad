"""
Option Selector — Maps agent decision to exact option strike + budget check.

Logic:
1. Direction → CE (BUY) or PE (SELL)
2. ATM strike: NIFTY round(LTP/50)*50, SENSEX round(LTP/100)*100
3. OTM depth by confidence + time:
   - SENSEX: conf >= 8 & before 11am → ATM; 6-7 or 11-13:30 → 1-OTM; after 13:30 → 2-OTM
   - NIFTY: always 2-3 OTM, only if premium <= Rs.26
4. OI wall check: highest OI strike → move 1 further OTM
5. Budget gate: 400 <= premium * lot_size <= 2000
"""

import logging
from datetime import datetime, time
from typing import Optional

from data.state import MarketState
import config

logger = logging.getLogger(__name__)


class OptionSelector:
    """
    Converts a directional decision + confidence into an exact option contract.

    Returns None if no affordable strike is found (system waits).
    """

    def __init__(self, state: MarketState):
        self._state = state
        logger.info("OptionSelector initialized")

    def select(
        self,
        index: str,
        direction: str,
        confidence: int,
        chain: Optional[dict] = None,
    ) -> Optional[dict]:
        """
        Select the best option contract for the trade.

        Args:
            index: "NIFTY" or "SENSEX"
            direction: "BUY" or "SELL" (market direction, not option action)
            confidence: 1-10 from agent
            chain: Option chain dict {strike: {"CE": {...}, "PE": {...}}}

        Returns:
            {
                "instrument_key": str,
                "instrument_name": str,
                "option_type": "CE" | "PE",
                "strike": int,
                "premium": float,
                "lot_size": int,
                "total_cost": float,
                "otm_depth": int,
                "atm_strike": int,
            }
            or None if no affordable strike found.
        """
        if not chain:
            chain = self._state.get("option_chain", index) or {}

        if not chain:
            logger.warning(f"No option chain available for {index}")
            return None

        # Step 1: Direction → option type
        option_type = "CE" if direction == "BUY" else "PE"

        # Step 2: Calculate ATM strike
        ltp = self._state.get("index", index, "ltp") or 0
        if ltp == 0:
            logger.warning(f"No LTP available for {index}")
            return None

        strike_interval = (
            config.NIFTY_STRIKE_INTERVAL if index == "NIFTY"
            else config.SENSEX_STRIKE_INTERVAL
        )
        lot_size = config.NIFTY_LOT if index == "NIFTY" else config.SENSEX_LOT
        max_premium = (
            config.NIFTY_MAX_PREMIUM if index == "NIFTY"
            else config.SENSEX_MAX_PREMIUM
        )

        atm_strike = round(ltp / strike_interval) * strike_interval

        # Step 3: Determine OTM depth
        otm_depth = self._get_otm_depth(index, confidence)

        # Step 4: Calculate target strike
        if option_type == "CE":
            target_strike = atm_strike + (otm_depth * strike_interval)
        else:
            target_strike = atm_strike - (otm_depth * strike_interval)

        # Step 5: OI wall check — if target has highest OI, move 1 further
        target_strike = self._oi_wall_check(
            chain, target_strike, option_type, strike_interval
        )

        # Step 6: Find the best affordable strike
        result = self._find_affordable_strike(
            chain, index, target_strike, option_type,
            strike_interval, lot_size, max_premium, atm_strike
        )

        if result:
            logger.info(
                f"Selected: {index} {result['strike']} {option_type} "
                f"premium=Rs.{result['premium']:.2f} "
                f"cost=Rs.{result['total_cost']:.2f} "
                f"OTM depth={result['otm_depth']}"
            )
        else:
            logger.warning(
                f"No affordable {option_type} found for {index}. "
                f"ATM={atm_strike}, target OTM depth={otm_depth}"
            )

        return result

    def _get_otm_depth(self, index: str, confidence: int) -> int:
        """
        Determine how many strikes OTM to go.

        SENSEX:
        - conf >= 8 & before 11am → ATM (0)
        - conf 6-7 or 11-13:30 → 1-OTM
        - after 13:30 → 2-OTM

        NIFTY:
        - Always 2-3 OTM (premium cap of Rs.26 enforces this naturally)
        """
        now = datetime.now().time()

        if index == "SENSEX":
            if confidence >= 8 and now < time(11, 0):
                return 0  # ATM
            elif confidence >= 6 or now < time(13, 30):
                return 1  # 1-OTM
            else:
                return 2  # 2-OTM

        else:  # NIFTY
            # Always 2-3 OTM due to premium cap
            if confidence >= 8:
                return 2
            else:
                return 3

    def _oi_wall_check(
        self,
        chain: dict,
        target_strike: int,
        option_type: str,
        strike_interval: int,
    ) -> int:
        """
        If target strike has highest OI for that option type,
        move 1 strike further OTM (the OI wall acts as resistance/support).
        """
        # Find highest OI strike for this option type
        max_oi = 0
        max_oi_strike = 0

        for strike, opts in chain.items():
            oi = opts.get(option_type, {}).get("oi", 0)
            if oi > max_oi:
                max_oi = oi
                max_oi_strike = strike

        if max_oi_strike == target_strike and max_oi > 0:
            # Move 1 further OTM
            if option_type == "CE":
                new_strike = target_strike + strike_interval
            else:
                new_strike = target_strike - strike_interval

            logger.info(
                f"OI wall at {target_strike} ({max_oi} OI). "
                f"Moving to {new_strike}"
            )
            return new_strike

        return target_strike

    def _find_affordable_strike(
        self,
        chain: dict,
        index: str,
        target_strike: int,
        option_type: str,
        strike_interval: int,
        lot_size: int,
        max_premium: float,
        atm_strike: int,
    ) -> Optional[dict]:
        """
        Find the best affordable strike near the target.

        Checks target strike, then 1 OTM on each side.
        Applies budget gate: MIN_TRADE_COST <= cost <= MAX_TRADE_COST.
        """
        # Try strikes in order: target, target+1 OTM, target-1 OTM
        candidates = [target_strike]

        if option_type == "CE":
            candidates.append(target_strike + strike_interval)
            candidates.append(target_strike - strike_interval)
        else:
            candidates.append(target_strike - strike_interval)
            candidates.append(target_strike + strike_interval)

        for strike in candidates:
            opts = chain.get(strike, {})
            option_data = opts.get(option_type, {})

            premium = option_data.get("ltp", 0)
            if premium <= 0:
                continue

            total_cost = premium * lot_size

            # ── NIFTY premium cap: hard reject above max ──
            if index == "NIFTY" and premium > max_premium:
                logger.debug(
                    f"NIFTY {strike}{option_type}: premium Rs.{premium:.2f} "
                    f"> cap Rs.{max_premium} — REJECTED"
                )
                continue

            # ── Budget gate ──
            if total_cost > config.MAX_TRADE_COST:
                logger.debug(
                    f"{strike}{option_type}: cost Rs.{total_cost:.2f} "
                    f"> max Rs.{config.MAX_TRADE_COST} — too expensive"
                )
                continue

            if total_cost < config.MIN_TRADE_COST:
                logger.debug(
                    f"{strike}{option_type}: cost Rs.{total_cost:.2f} "
                    f"< min Rs.{config.MIN_TRADE_COST} — too far OTM"
                )
                continue

            # Calculate OTM depth
            if option_type == "CE":
                otm_depth = (strike - atm_strike) // strike_interval
            else:
                otm_depth = (atm_strike - strike) // strike_interval

            instrument_key = option_data.get("instrument_key", f"{index}|{strike}{option_type}")

            return {
                "instrument_key": instrument_key,
                "instrument_name": f"{index} {strike} {option_type}",
                "option_type": option_type,
                "strike": strike,
                "premium": premium,
                "lot_size": lot_size,
                "total_cost": round(total_cost, 2),
                "otm_depth": max(otm_depth, 0),
                "atm_strike": atm_strike,
            }

        return None  # No affordable strike found
