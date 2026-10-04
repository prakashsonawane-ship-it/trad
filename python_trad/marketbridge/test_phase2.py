"""
Phase 2 Validation — Current Affairs Intelligence Layer
"""
import sys
import asyncio
from datetime import date

# --- news/calendar.py ---
from news.calendar import EconomicCalendar

cal = EconomicCalendar()
flags = cal.get_today_flags()
print(f"Today flags: {flags}")
assert "is_rbi_day" in flags
assert "is_budget_day" in flags
assert "has_high_impact_event" in flags

# Test known RBI day
rbi_flags = cal.get_today_flags(date(2026, 10, 7))
assert rbi_flags["is_rbi_day"] == True, "Oct 7 should be RBI day"
assert rbi_flags["has_high_impact_event"] == True

# Test normal day
normal_flags = cal.get_today_flags(date(2026, 10, 1))
assert normal_flags["is_rbi_day"] == False
assert normal_flags["has_high_impact_event"] == False

next_event = cal.get_next_event()
print(f"Next event: {next_event}")

earnings = cal.get_earnings_this_month()
print(f"Earnings this month: {earnings}")
print("[PASS] news/calendar.py\n")

# --- news/classifier.py ---
from news.classifier import ImpactClassifier

clf = ImpactClassifier()

# Test HIGH impact (RBI day)
ctx_high = {
    "rbi_event_today": True,
    "global_sentiment": "NEUTRAL",
    "sgx_nifty_gap": "+0.2%",
    "crude_change": "+0.5%",
    "usd_inr": "83.50 stable",
    "headlines": [],
    "earnings_today": [],
}
result = clf.classify(ctx_high)
assert result["impact_level"] == "HIGH", f"Expected HIGH, got {result['impact_level']}"
assert result["trading_blocked"] == True
print(f"RBI day: impact={result['impact_level']}, blocked={result['trading_blocked']}")

# Test MEDIUM impact (large SGX gap)
ctx_med = {
    "rbi_event_today": False,
    "budget_day": False,
    "fed_day": False,
    "global_sentiment": "RISK_OFF",
    "sgx_nifty_gap": "-1.5%",
    "crude_change": "+0.5%",
    "usd_inr": "84.00 volatile",
    "headlines": [{"title": "FII selling continues", "summary": "Heavy FII outflow"}],
    "earnings_today": ["RELIANCE"],
}
result = clf.classify(ctx_med)
assert result["impact_level"] == "MEDIUM", f"Expected MEDIUM, got {result['impact_level']}"
assert result["trading_blocked"] == False
print(f"Risk-off: impact={result['impact_level']}, blocked={result['trading_blocked']}")

# Test LOW impact (normal day)
ctx_low = {
    "rbi_event_today": False,
    "budget_day": False,
    "fed_day": False,
    "global_sentiment": "NEUTRAL",
    "sgx_nifty_gap": "+0.1%",
    "crude_change": "-0.3%",
    "usd_inr": "83.45 stable",
    "headlines": [{"title": "Markets flat today", "summary": "Quiet session expected"}],
    "earnings_today": [],
}
result = clf.classify(ctx_low)
assert result["impact_level"] == "LOW", f"Expected LOW, got {result['impact_level']}"
assert result["trading_blocked"] == False
print(f"Normal day: impact={result['impact_level']}, blocked={result['trading_blocked']}")

# Test headline-based HIGH (war keyword)
ctx_headline_high = {
    "rbi_event_today": False,
    "budget_day": False,
    "fed_day": False,
    "global_sentiment": "RISK_OFF",
    "sgx_nifty_gap": "-2.0%",
    "crude_change": "+5.0%",
    "usd_inr": "85.00 volatile",
    "headlines": [{"title": "Military crisis escalates at border", "summary": "War fears rise"}],
    "earnings_today": [],
}
result = clf.classify(ctx_headline_high)
assert result["impact_level"] == "HIGH"
assert result["trading_blocked"] == True
print(f"Crisis headline: impact={result['impact_level']}, blocked={result['trading_blocked']}")

print("[PASS] news/classifier.py\n")

# --- news/news_bias.py ---
from news.news_bias import get_bias_instruction, get_confluence_adjustment

# Test RBI day bias
bias = get_bias_instruction(ctx_high)
assert "RBI" in bias
assert "confluence >= 8" in bias.lower() or "confluence" in bias.lower()
print(f"RBI bias: {bias[:80]}...")

# Test risk-off bias
ctx_riskoff = {
    "rbi_event_today": False,
    "budget_day": False,
    "fed_day": False,
    "global_sentiment": "RISK_OFF",
    "sgx_nifty_gap": "-0.8%",
    "crude_change": "+1.0%",
    "usd_inr": "83.50 stable",
    "earnings_today": ["TCS", "INFY"],
}
bias = get_bias_instruction(ctx_riskoff)
assert "RISK-OFF" in bias or "bearish" in bias.lower()
assert "gap-down" in bias.lower()
print(f"Risk-off bias: {bias[:80]}...")

# Test normal day bias
ctx_normal = {
    "rbi_event_today": False,
    "budget_day": False,
    "fed_day": False,
    "global_sentiment": "NEUTRAL",
    "sgx_nifty_gap": "+0.1%",
    "crude_change": "-0.2%",
    "usd_inr": "83.45 stable",
    "earnings_today": [],
}
bias = get_bias_instruction(ctx_normal)
assert "No major macro events" in bias
print(f"Normal bias: {bias[:80]}...")

# Test confluence adjustment
adj_rbi = get_confluence_adjustment({"rbi_event_today": True})
assert adj_rbi == 3, f"Expected 3, got {adj_rbi}"

adj_normal = get_confluence_adjustment({"rbi_event_today": False, "global_sentiment": "NEUTRAL", "sgx_nifty_gap": "0.0%", "crude_change": "0.0%"})
assert adj_normal == 0, f"Expected 0, got {adj_normal}"

adj_riskoff = get_confluence_adjustment({"rbi_event_today": False, "global_sentiment": "RISK_OFF", "sgx_nifty_gap": "-0.8%", "crude_change": "+1.0%"})
assert adj_riskoff >= 1
print(f"Confluence adjustments: RBI={adj_rbi}, normal={adj_normal}, riskoff={adj_riskoff}")

print("[PASS] news/news_bias.py\n")

# --- news/scanner.py (import only, no live fetch) ---
from news.scanner import NewsScanner
scanner = NewsScanner()
print("NewsScanner imports and initializes correctly")
print("[PASS] news/scanner.py\n")

print("=" * 50)
print("  ALL PHASE 2 MODULES VALIDATED SUCCESSFULLY")
print("=" * 50)
