"""
News Bias Generator — Translates macro context into agent instruction strings.

Takes the classified macro_context and produces a plain-English instruction
that gets prepended to every Claude agent call. This ensures the agent's
trading decisions account for the current macro environment.

Bias rules (from spec):
- RBI day -> block trading until announcement, then confluence >= 8 only
- RISK_OFF -> bearish bias, extra delta confirmation for bullish
- SGX gap > 0.5% -> skip first 15 min, expect gap fill
- Earnings today -> watch for index-level impact
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def get_bias_instruction(macro_context: dict) -> str:
    """
    Translate macro context into a plain-English agent instruction string.

    This string is prepended to every agent call so the AI knows the
    macro backdrop before interpreting any technical signals.

    Args:
        macro_context: Classified context dict from scanner + classifier

    Returns:
        Pipe-delimited instruction string, or default message if no events.
    """
    instructions = []

    # ── HIGH IMPACT: RBI Event Day ─────────────────────────────────
    if macro_context.get("rbi_event_today"):
        instructions.append(
            "RBI POLICY DAY. Do NOT trade before the announcement. "
            "After announcement: trade only WITH the announced direction, "
            "confluence >= 8 required. Max Pain is irrelevant today. "
            "Expect 200-500 point NIFTY moves post-announcement."
        )

    # ── HIGH IMPACT: Budget Day ────────────────────────────────────
    if macro_context.get("budget_day"):
        instructions.append(
            "UNION BUDGET DAY. Extreme volatility expected. "
            "Do NOT trade before Budget speech concludes (~1:00 PM). "
            "After speech: trade only WITH the announced direction, "
            "confluence >= 8 required. All technical signals unreliable "
            "until market digests the announcements."
        )

    # ── MEDIUM: US Fed Decision ────────────────────────────────────
    if macro_context.get("fed_day"):
        instructions.append(
            "US Fed FOMC decision day. Announcement at ~11:30 PM IST (last night). "
            "Market may gap based on outcome. Watch for opening volatility. "
            "If gap > 1%, expect gap-fill attempt by 11:00 AM. "
            "Confluence >= 7 recommended."
        )

    # ── Global Sentiment ───────────────────────────────────────────
    sentiment = macro_context.get("global_sentiment", "NEUTRAL")

    if sentiment == "RISK_OFF":
        instructions.append(
            "Global RISK-OFF environment. Bearish bias. "
            "Be more aggressive on SELL setups, more cautious on BUY setups. "
            "Require extra delta confirmation for any bullish entry. "
            "Expect nervous selling and stop-hunts below support."
        )
    elif sentiment == "RISK_ON":
        instructions.append(
            "Global RISK-ON environment. Bullish bias. "
            "Be more aggressive on BUY setups. "
            "Mean-reversion shorts have lower probability today."
        )
    elif sentiment == "CAUTIOUS":
        instructions.append(
            "Global sentiment cautious (US markets slightly negative). "
            "No strong directional bias, but lean slightly bearish. "
            "Require standard confluence for all setups."
        )

    # ── SGX Nifty Gap ──────────────────────────────────────────────
    sgx_gap_str = macro_context.get("sgx_nifty_gap", "0.0%")
    try:
        sgx_gap = float(sgx_gap_str.replace("%", "").replace("+", "").strip())
    except (ValueError, AttributeError):
        sgx_gap = 0.0

    if abs(sgx_gap) > 0.5:
        direction = "gap-up" if sgx_gap > 0 else "gap-down"
        instructions.append(
            f"SGX shows {direction} of {abs(sgx_gap):.1f}%. "
            "Expect opening momentum in gap direction for 15-20 mins, "
            "then high probability of gap fill. Do NOT trade first 15 minutes. "
            "Watch for stop-hunt above/below opening gap level."
        )
    elif abs(sgx_gap) > 0.2:
        direction = "slightly positive" if sgx_gap > 0 else "slightly negative"
        instructions.append(
            f"SGX shows {direction} opening ({sgx_gap:+.1f}%). "
            "Minor gap — likely absorbed in first 15 minutes. "
            "No special gap-trade setup."
        )

    # ── Crude Oil ──────────────────────────────────────────────────
    crude_str = macro_context.get("crude_change", "0.0%")
    try:
        crude_change = float(crude_str.replace("%", "").replace("+", "").strip())
    except (ValueError, AttributeError):
        crude_change = 0.0

    if abs(crude_change) > 2.0:
        direction = "surging" if crude_change > 0 else "crashing"
        instructions.append(
            f"Crude oil {direction} ({crude_change:+.1f}%). "
            "This impacts ONGC, Reliance, and index sentiment. "
            f"{'Bearish for market if sustained.' if crude_change > 2 else 'Bullish for market if sustained.'}"
        )

    # ── Earnings Today ─────────────────────────────────────────────
    earnings = macro_context.get("earnings_today", [])
    if earnings:
        stocks = ", ".join(earnings[:5])
        instructions.append(
            f"Heavyweight earnings this month: {stocks}. "
            "Watch for index-level impact from beats/misses. "
            "Large results can override normal OI signals. "
            "Check if any report before market today."
        )

    # ── USD/INR ────────────────────────────────────────────────────
    usd_inr = macro_context.get("usd_inr", "")
    if "volatile" in usd_inr.lower():
        instructions.append(
            f"USD/INR is volatile ({usd_inr}). "
            "FII flows may be impacted. Watch for sudden INR moves "
            "that could trigger FII selling."
        )

    # ── Build Final String ─────────────────────────────────────────
    if instructions:
        bias_string = " | ".join(instructions)
    else:
        bias_string = (
            "No major macro events. Normal trading conditions. "
            "Trade setups as per standard confluence thresholds."
        )

    # Also set the agent_bias_note in context
    macro_context["agent_bias_note"] = bias_string

    logger.info(f"Bias instruction generated ({len(instructions)} rules applied)")
    logger.debug(f"Bias: {bias_string[:200]}...")

    return bias_string


def get_confluence_adjustment(macro_context: dict) -> int:
    """
    Returns the minimum confluence threshold adjustment based on macro context.

    This is ADDED to the base time-of-day threshold.
    For example, if base threshold is 5 and this returns 3,
    the effective threshold is 8.

    Returns:
        0 = no adjustment (normal conditions)
        1 = slightly elevated (medium impact events)
        2 = significantly elevated (crude spike, large gap)
        3 = maximum elevation (RBI, Budget — effectively requires 8+)
    """
    if macro_context.get("rbi_event_today") or macro_context.get("budget_day"):
        return 3  # Base 5 + 3 = 8 minimum

    adjustment = 0

    if macro_context.get("has_high_impact_event"):
        adjustment = max(adjustment, 3)

    if macro_context.get("global_sentiment") == "RISK_OFF":
        adjustment = max(adjustment, 1)

    sgx_gap_str = macro_context.get("sgx_nifty_gap", "0.0%")
    try:
        sgx_gap = abs(float(sgx_gap_str.replace("%", "").replace("+", "").strip()))
    except (ValueError, AttributeError):
        sgx_gap = 0.0

    if sgx_gap > 1.0:
        adjustment = max(adjustment, 2)
    elif sgx_gap > 0.5:
        adjustment = max(adjustment, 1)

    crude_str = macro_context.get("crude_change", "0.0%")
    try:
        crude_change = abs(float(crude_str.replace("%", "").replace("+", "").strip()))
    except (ValueError, AttributeError):
        crude_change = 0.0

    if crude_change > 3.0:
        adjustment = max(adjustment, 2)

    if macro_context.get("fed_day"):
        adjustment = max(adjustment, 1)

    return adjustment
