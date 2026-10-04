"""
Economic Calendar — Hardcoded + fetched schedule of market-moving events.

Contains known dates for:
- RBI Monetary Policy decisions
- Union Budget
- GDP, CPI/WPI releases
- US Fed decisions (that affect Indian markets)
- Major F&O expiry anomalies

Returns flags like is_rbi_day, is_budget_day for the current date.
Updated manually each quarter or fetched from public sources.
"""

import logging
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Known High-Impact Dates (Update quarterly) ───────────────────────
# Format: (month, day) or exact date objects
# Source: RBI website, Ministry of Finance, US Fed schedule

RBI_POLICY_DATES_2026 = [
    date(2026, 2, 5),
    date(2026, 2, 6),
    date(2026, 2, 7),
    date(2026, 4, 8),
    date(2026, 4, 9),
    date(2026, 4, 10),
    date(2026, 6, 4),
    date(2026, 6, 5),
    date(2026, 6, 6),
    date(2026, 8, 5),
    date(2026, 8, 6),
    date(2026, 8, 7),
    date(2026, 10, 7),
    date(2026, 10, 8),
    date(2026, 10, 9),
    date(2026, 12, 3),
    date(2026, 12, 4),
    date(2026, 12, 5),
]

# Union Budget is typically Feb 1
BUDGET_DATES_2026 = [
    date(2026, 2, 1),
]

# US Fed FOMC meeting dates (2-day meetings, second day is decision day)
US_FED_DATES_2026 = [
    date(2026, 1, 29),
    date(2026, 3, 19),
    date(2026, 5, 7),
    date(2026, 6, 18),
    date(2026, 7, 30),
    date(2026, 9, 17),
    date(2026, 11, 5),
    date(2026, 12, 17),
]

# India CPI release dates (typically 12th-14th of each month)
# These are approximate — exact dates announced by MOSPI
INDIA_CPI_DATES_2026 = [
    date(2026, 1, 13),
    date(2026, 2, 12),
    date(2026, 3, 12),
    date(2026, 4, 14),
    date(2026, 5, 12),
    date(2026, 6, 12),
    date(2026, 7, 14),
    date(2026, 8, 12),
    date(2026, 9, 14),
    date(2026, 10, 13),
    date(2026, 11, 12),
    date(2026, 12, 14),
]

# India GDP release dates (quarterly, ~2 months after quarter end)
INDIA_GDP_DATES_2026 = [
    date(2026, 2, 28),   # Q3 FY26
    date(2026, 5, 29),   # Q4 FY26
    date(2026, 8, 29),   # Q1 FY27
    date(2026, 11, 28),  # Q2 FY27
]

# Nifty50 heavyweight earnings — approximate result dates per quarter
# These are stocks whose results can move the index 100+ points
NIFTY50_HEAVYWEIGHT_EARNINGS = {
    "RELIANCE": {"quarters": [(1, "Jan"), (4, "Apr"), (7, "Jul"), (10, "Oct")]},
    "TCS": {"quarters": [(1, "Jan"), (4, "Apr"), (7, "Jul"), (10, "Oct")]},
    "INFY": {"quarters": [(1, "Jan"), (4, "Apr"), (7, "Jul"), (10, "Oct")]},
    "HDFCBANK": {"quarters": [(1, "Jan"), (4, "Apr"), (7, "Jul"), (10, "Oct")]},
    "ICICIBANK": {"quarters": [(1, "Jan"), (4, "Apr"), (7, "Jul"), (10, "Oct")]},
    "HINDUNILVR": {"quarters": [(1, "Jan"), (4, "Apr"), (7, "Jul"), (10, "Oct")]},
    "ITC": {"quarters": [(1, "Jan"), (4, "Apr"), (7, "Jul"), (10, "Oct")]},
    "BHARTIARTL": {"quarters": [(1, "Jan"), (4, "Apr"), (7, "Jul"), (10, "Oct")]},
    "SBIN": {"quarters": [(1, "Jan"), (5, "May"), (8, "Aug"), (11, "Nov")]},
    "LT": {"quarters": [(1, "Jan"), (4, "Apr"), (7, "Jul"), (10, "Oct")]},
}


class EconomicCalendar:
    """
    Provides date-based flags for high-impact economic events.

    Usage:
        cal = EconomicCalendar()
        flags = cal.get_today_flags()
        # flags = {"is_rbi_day": True, "is_budget_day": False, ...}
    """

    def __init__(self):
        self._rbi_dates = set(RBI_POLICY_DATES_2026)
        self._budget_dates = set(BUDGET_DATES_2026)
        self._fed_dates = set(US_FED_DATES_2026)
        self._cpi_dates = set(INDIA_CPI_DATES_2026)
        self._gdp_dates = set(INDIA_GDP_DATES_2026)
        logger.info("EconomicCalendar initialized")

    def get_today_flags(self, today: Optional[date] = None) -> dict:
        """
        Get event flags for a specific date (default: today).

        Returns:
            Dict with boolean flags and event descriptions.
        """
        if today is None:
            today = date.today()

        flags = {
            "is_rbi_day": today in self._rbi_dates,
            "is_budget_day": today in self._budget_dates,
            "is_fed_day": today in self._fed_dates,
            "is_cpi_day": today in self._cpi_dates,
            "is_gdp_day": today in self._gdp_dates,
            "events": [],
        }

        if flags["is_rbi_day"]:
            flags["events"].append("RBI Monetary Policy Decision")
        if flags["is_budget_day"]:
            flags["events"].append("Union Budget")
        if flags["is_fed_day"]:
            flags["events"].append("US Fed FOMC Decision")
        if flags["is_cpi_day"]:
            flags["events"].append("India CPI Data Release")
        if flags["is_gdp_day"]:
            flags["events"].append("India GDP Data Release")

        # Check if any high-impact event
        flags["has_high_impact_event"] = (
            flags["is_rbi_day"]
            or flags["is_budget_day"]
            or flags["is_fed_day"]
        )

        # Medium impact
        flags["has_medium_impact_event"] = (
            flags["is_cpi_day"]
            or flags["is_gdp_day"]
        )

        if flags["events"]:
            logger.info(f"Calendar events today: {', '.join(flags['events'])}")
        else:
            logger.debug("No scheduled calendar events today")

        return flags

    def get_next_event(self, from_date: Optional[date] = None) -> Optional[dict]:
        """Get the next upcoming high-impact event."""
        if from_date is None:
            from_date = date.today()

        all_events = []
        for d in self._rbi_dates:
            if d >= from_date:
                all_events.append({"date": d, "event": "RBI Policy"})
        for d in self._fed_dates:
            if d >= from_date:
                all_events.append({"date": d, "event": "US Fed FOMC"})
        for d in self._budget_dates:
            if d >= from_date:
                all_events.append({"date": d, "event": "Union Budget"})

        if all_events:
            all_events.sort(key=lambda x: x["date"])
            return all_events[0]
        return None

    def get_earnings_this_month(self) -> list[str]:
        """
        Get Nifty50 heavyweights that typically report this month.
        This is approximate — actual dates come from exchange filings.
        """
        current_month = date.today().month
        reporting = []
        for stock, info in NIFTY50_HEAVYWEIGHT_EARNINGS.items():
            for month_num, _ in info["quarters"]:
                if month_num == current_month:
                    reporting.append(stock)
        return reporting
