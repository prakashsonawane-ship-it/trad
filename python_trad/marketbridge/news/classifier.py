"""
Impact Classifier — Tags news events and market conditions as HIGH/MEDIUM/LOW.

Takes the raw macro_context from scanner.py and:
1. Assigns an impact_level to the overall session
2. Sets trading_blocked flag for HIGH impact pre-event scenarios
3. Generates a structured impact assessment for the agent

HIGH impact = can override all technical signals (RBI, Budget, Fed)
MEDIUM impact = sector moves, earnings, large global gaps
LOW impact = routine data, normal conditions
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Keyword Patterns for Headline Classification ─────────────────────
HIGH_IMPACT_KEYWORDS = [
    r"\brbi\b", r"\breserve bank\b", r"\bmonetary policy\b",
    r"\brate cut\b", r"\brate hike\b", r"\binterest rate\b",
    r"\bunion budget\b", r"\bbudget 202\d\b",
    r"\bfed\b.*\b(cut|hike|decision|hold)\b",
    r"\bfomc\b",
    r"\bwar\b", r"\bmilitary\b", r"\bsanction\b",
    r"\bdefault\b.*\b(debt|sovereign)\b",
    r"\bcrisis\b",
]

MEDIUM_IMPACT_KEYWORDS = [
    r"\bcpi\b", r"\binflation\b", r"\bgdp\b",
    r"\bfii\b", r"\bdii\b",
    r"\bearnings\b", r"\bresults\b", r"\bquarterly\b",
    r"\breliance\b", r"\btcs\b", r"\binfosys\b", r"\binfy\b",
    r"\bhdfc\b", r"\bicici\b", r"\bsbi\b",
    r"\bcrude\b.*\b(surge|crash|spike)\b",
    r"\brupee\b.*\b(fall|crash|surge|rise)\b",
    r"\bsebi\b.*\b(ban|circular|new rule)\b",
    r"\bitr\b", r"\btax\b.*\b(change|new|reform)\b",
]

LOW_IMPACT_KEYWORDS = [
    r"\brollover\b", r"\bf&o\b.*\bdata\b",
    r"\bipo\b", r"\bsme\b",
    r"\bmutual fund\b",
]


class ImpactClassifier:
    """
    Classifies the macro context into impact levels and sets trading flags.

    Usage:
        classifier = ImpactClassifier()
        classified = classifier.classify(macro_context)
        # classified["impact_level"] = "HIGH" | "MEDIUM" | "LOW"
        # classified["trading_blocked"] = True | False
    """

    def __init__(self):
        # Pre-compile regex patterns
        self._high_patterns = [re.compile(p, re.IGNORECASE) for p in HIGH_IMPACT_KEYWORDS]
        self._medium_patterns = [re.compile(p, re.IGNORECASE) for p in MEDIUM_IMPACT_KEYWORDS]
        self._low_patterns = [re.compile(p, re.IGNORECASE) for p in LOW_IMPACT_KEYWORDS]
        logger.info("ImpactClassifier initialized")

    def classify(self, macro_context: dict) -> dict:
        """
        Classify the macro context and set impact flags.

        Modifies macro_context in place and returns it.

        Classification logic:
        1. Calendar events (hardcoded dates) — highest priority
        2. Headline keyword matching — secondary
        3. Market data thresholds — tertiary
        """
        impact_level = "LOW"
        trading_blocked = False
        impact_reasons = []

        # ── 1. Calendar-Based Classification (highest priority) ────
        if macro_context.get("rbi_event_today"):
            impact_level = "HIGH"
            trading_blocked = True  # Block until announcement
            impact_reasons.append("RBI Monetary Policy day")

        if macro_context.get("budget_day"):
            impact_level = "HIGH"
            trading_blocked = True
            impact_reasons.append("Union Budget day")

        if macro_context.get("fed_day"):
            # Fed decisions are announced at night IST,
            # but the day after can be volatile
            if impact_level != "HIGH":
                impact_level = "MEDIUM"
            impact_reasons.append("US Fed FOMC decision day")

        # ── 2. Headline-Based Classification ───────────────────────
        headlines = macro_context.get("headlines", [])
        headline_texts = " ".join(
            h.get("title", "") + " " + h.get("summary", "")
            for h in headlines
        )

        high_matches = self._match_keywords(headline_texts, self._high_patterns)
        medium_matches = self._match_keywords(headline_texts, self._medium_patterns)

        if high_matches and impact_level != "HIGH":
            impact_level = "HIGH"
            trading_blocked = True
            impact_reasons.append(f"High-impact headlines: {', '.join(high_matches[:3])}")

        if medium_matches and impact_level == "LOW":
            impact_level = "MEDIUM"
            impact_reasons.append(f"Medium-impact headlines: {', '.join(medium_matches[:3])}")

        # ── 3. Market Data Classification ──────────────────────────
        sgx_gap = self._parse_percentage(macro_context.get("sgx_nifty_gap", "0.0%"))
        crude_change = self._parse_percentage(macro_context.get("crude_change", "0.0%"))

        # Large SGX gap = medium impact at minimum
        if abs(sgx_gap) > 1.0:
            if impact_level == "LOW":
                impact_level = "MEDIUM"
            impact_reasons.append(f"Large SGX Nifty gap: {sgx_gap:+.1f}%")

        # Crude oil spike > 3%
        if abs(crude_change) > 3.0:
            if impact_level == "LOW":
                impact_level = "MEDIUM"
            impact_reasons.append(f"Crude oil spike: {crude_change:+.1f}%")

        # Global risk-off
        if macro_context.get("global_sentiment") == "RISK_OFF":
            if impact_level == "LOW":
                impact_level = "MEDIUM"
            impact_reasons.append("Global risk-off sentiment")

        # USD/INR volatility
        usd_inr_str = macro_context.get("usd_inr", "")
        if "volatile" in usd_inr_str.lower():
            impact_reasons.append("USD/INR volatile")

        # ── Earnings impact ────────────────────────────────────────
        earnings = macro_context.get("earnings_today", [])
        if earnings:
            if impact_level == "LOW":
                impact_level = "MEDIUM"
            impact_reasons.append(f"Heavyweight earnings: {', '.join(earnings[:3])}")

        # ── Set results ────────────────────────────────────────────
        macro_context["impact_level"] = impact_level
        macro_context["trading_blocked"] = trading_blocked
        macro_context["impact_reasons"] = impact_reasons

        logger.info(
            f"Impact classification: {impact_level} | "
            f"Trading blocked: {trading_blocked} | "
            f"Reasons: {impact_reasons or ['Normal session']}"
        )

        return macro_context

    @staticmethod
    def _match_keywords(text: str, patterns: list) -> list[str]:
        """Find all keyword pattern matches in text."""
        matches = []
        for pattern in patterns:
            if pattern.search(text):
                matches.append(pattern.pattern)
        return matches

    @staticmethod
    def _parse_percentage(pct_str: str) -> float:
        """Parse a percentage string like '+0.3%' or '-1.2%' to float."""
        try:
            return float(pct_str.replace("%", "").replace("+", "").strip())
        except (ValueError, AttributeError):
            return 0.0
