"""
News Scanner — Pre-market data fetch from multiple sources.

Priority order (from user's plan):
1. MoneyControl RSS (primary) — free, no key, Indian-market focused
2. NewsAPI (fallback) — activated when MoneyControl fails or returns < 5 items
3. yfinance (always) — SGX Nifty proxy, crude oil, USD/INR
4. Economic calendar (always) — hardcoded RBI, Budget, CPI, GDP dates

Runs at:
- 8:30 AM — full pre-market scan
- 12:00 PM — midday refresh
"""

import logging
import asyncio
from datetime import date, datetime
from typing import Optional

import feedparser
import yfinance as yf

import config
from news.calendar import EconomicCalendar

logger = logging.getLogger(__name__)

# ─── MoneyControl RSS Feeds ───────────────────────────────────────────
MONEYCONTROL_FEEDS = [
    "https://www.moneycontrol.com/rss/marketreports.xml",
    "https://www.moneycontrol.com/rss/economy.xml",
    "https://www.moneycontrol.com/rss/business.xml",
]

# ─── NewsAPI Config ───────────────────────────────────────────────────
NEWSAPI_QUERIES = ["NIFTY", "SENSEX", "RBI", "India economy", "Indian stock market"]
NEWSAPI_MIN_ARTICLES = 5


class NewsScanner:
    """
    Fetches pre-market intelligence from multiple sources.

    Usage:
        scanner = NewsScanner()
        context = await scanner.run_full_scan()
        # context = {"headlines": [...], "sgx_nifty_gap": "+0.3%", ...}
    """

    def __init__(self):
        self._calendar = EconomicCalendar()
        self._newsapi_client = None
        self._seen_urls: set[str] = set()  # Deduplication
        logger.info("NewsScanner initialized")

    def _init_newsapi(self):
        """Lazy-init NewsAPI client (only when needed as fallback)."""
        if self._newsapi_client is None and config.NEWSAPI_KEY:
            try:
                from newsapi import NewsApiClient
                self._newsapi_client = NewsApiClient(api_key=config.NEWSAPI_KEY)
                logger.info("NewsAPI client initialized (fallback)")
            except ImportError:
                logger.warning("newsapi-python not installed")
            except Exception as e:
                logger.error(f"NewsAPI init failed: {e}")

    # ─── Main Scan ─────────────────────────────────────────────────

    async def run_full_scan(self) -> dict:
        """
        Run complete pre-market scan. Returns macro_context dict.

        Fetches in parallel:
        - News headlines (MoneyControl RSS -> NewsAPI fallback)
        - Market data (SGX, crude, USD/INR via yfinance)
        - Economic calendar flags
        """
        logger.info("Starting pre-market scan...")

        # Run fetches concurrently
        headlines_task = asyncio.create_task(self._fetch_headlines())
        market_task = asyncio.create_task(self._fetch_market_data())

        headlines = await headlines_task
        market_data = await market_task
        calendar_flags = self._calendar.get_today_flags()
        earnings = self._calendar.get_earnings_this_month()

        # Build the macro context
        context = {
            "rbi_event_today": calendar_flags.get("is_rbi_day", False),
            "budget_day": calendar_flags.get("is_budget_day", False),
            "fed_day": calendar_flags.get("is_fed_day", False),
            "calendar_events": calendar_flags.get("events", []),
            "has_high_impact_event": calendar_flags.get("has_high_impact_event", False),
            "global_sentiment": market_data.get("global_sentiment", "NEUTRAL"),
            "sgx_nifty_gap": market_data.get("sgx_nifty_gap", "0.0%"),
            "crude_change": market_data.get("crude_change", "0.0%"),
            "usd_inr": market_data.get("usd_inr", "N/A"),
            "us_market_close": market_data.get("us_market_close", {}),
            "earnings_today": earnings,
            "headlines": headlines[:10],  # Top 10
            "key_headline": headlines[0]["title"] if headlines else "No headlines",
            "headline_source": headlines[0].get("source", "N/A") if headlines else "N/A",
            "agent_bias_note": "",  # Set by classifier/bias module
            "impact_level": "LOW",  # Set by classifier
            "scan_time": datetime.now().isoformat(),
        }

        logger.info(
            f"Pre-market scan complete: {len(headlines)} headlines, "
            f"SGX gap={context['sgx_nifty_gap']}, "
            f"events={context['calendar_events'] or 'none'}"
        )

        return context

    # ─── Headlines: MoneyControl RSS (primary) ─────────────────────

    async def _fetch_headlines(self) -> list[dict]:
        """
        Fetch headlines with MoneyControl RSS as primary,
        NewsAPI as automatic fallback.
        """
        headlines = await self._fetch_moneycontrol_rss()

        if len(headlines) < NEWSAPI_MIN_ARTICLES:
            logger.info(
                f"MoneyControl returned {len(headlines)} items "
                f"(< {NEWSAPI_MIN_ARTICLES}). Activating NewsAPI fallback."
            )
            newsapi_headlines = await self._fetch_newsapi()
            headlines.extend(newsapi_headlines)

        # Deduplicate by URL
        seen = set()
        unique = []
        for h in headlines:
            url = h.get("url", h.get("title", ""))
            if url not in seen:
                seen.add(url)
                unique.append(h)

        return unique

    async def _fetch_moneycontrol_rss(self) -> list[dict]:
        """
        Parse MoneyControl RSS feeds.
        Runs in executor since feedparser is synchronous.
        """
        headlines = []
        loop = asyncio.get_event_loop()

        for feed_url in MONEYCONTROL_FEEDS:
            try:
                feed = await loop.run_in_executor(None, feedparser.parse, feed_url)

                if feed.bozo and not feed.entries:
                    logger.warning(f"RSS feed error for {feed_url}: {feed.bozo_exception}")
                    continue

                for entry in feed.entries[:10]:  # Top 10 per feed
                    headlines.append({
                        "title": entry.get("title", "").strip(),
                        "url": entry.get("link", ""),
                        "published": entry.get("published", ""),
                        "summary": entry.get("summary", "")[:200],
                        "source": "MoneyControl",
                    })

                logger.debug(f"RSS {feed_url}: {len(feed.entries)} entries")

            except Exception as e:
                logger.error(f"RSS fetch failed for {feed_url}: {e}")

        logger.info(f"MoneyControl RSS: {len(headlines)} headlines")
        return headlines

    async def _fetch_newsapi(self) -> list[dict]:
        """
        Fetch headlines from NewsAPI (fallback source).
        Uses 1 API call with combined query.
        """
        self._init_newsapi()
        if not self._newsapi_client:
            logger.warning("NewsAPI not available (no key or init failed)")
            return []

        loop = asyncio.get_event_loop()
        headlines = []

        try:
            query = " OR ".join(NEWSAPI_QUERIES)

            response = await loop.run_in_executor(
                None,
                lambda: self._newsapi_client.get_everything(
                    q=query,
                    language="en",
                    sort_by="publishedAt",
                    page_size=20,
                )
            )

            articles = response.get("articles", [])
            for article in articles:
                headlines.append({
                    "title": article.get("title", "").strip(),
                    "url": article.get("url", ""),
                    "published": article.get("publishedAt", ""),
                    "summary": (article.get("description") or "")[:200],
                    "source": article.get("source", {}).get("name", "NewsAPI"),
                })

            logger.info(f"NewsAPI fallback: {len(headlines)} headlines")

        except Exception as e:
            logger.error(f"NewsAPI fetch failed: {e}")

        return headlines

    # ─── Market Data: yfinance ─────────────────────────────────────

    async def _fetch_market_data(self) -> dict:
        """
        Fetch market data via yfinance:
        - SGX Nifty proxy (Nifty futures or ^NSEI previous close)
        - Crude oil (CL=F)
        - USD/INR (USDINR=X)
        - US market close (^GSPC, ^DJI)
        """
        loop = asyncio.get_event_loop()
        data = {
            "global_sentiment": "NEUTRAL",
            "sgx_nifty_gap": "0.0%",
            "crude_change": "0.0%",
            "usd_inr": "N/A",
            "us_market_close": {},
        }

        try:
            result = await loop.run_in_executor(None, self._yfinance_fetch)
            data.update(result)
        except Exception as e:
            logger.error(f"yfinance fetch failed: {e}")

        return data

    def _yfinance_fetch(self) -> dict:
        """Synchronous yfinance data fetch (runs in executor)."""
        data = {}

        try:
            # ── Nifty Previous Close (proxy for SGX gap calculation) ──
            nifty = yf.Ticker("^NSEI")
            nifty_info = nifty.fast_info
            nifty_prev_close = getattr(nifty_info, "previous_close", 0) or 0
            nifty_last = getattr(nifty_info, "last_price", 0) or 0

            if nifty_prev_close and nifty_last:
                gap_pct = ((nifty_last - nifty_prev_close) / nifty_prev_close) * 100
                data["sgx_nifty_gap"] = f"{gap_pct:+.1f}%"
            else:
                logger.warning(
                    f"yfinance returned incomplete Nifty data: "
                    f"prev_close={nifty_prev_close}, last={nifty_last}. "
                    "SGX gap defaulting to 0.0% — agent may miss a real gap."
                )
                data["sgx_nifty_gap"] = "0.0%"

        except Exception as e:
            logger.warning(f"Nifty data fetch failed: {e}")
            data["sgx_nifty_gap"] = "0.0%"

        try:
            # ── Crude Oil ──
            crude = yf.Ticker("CL=F")
            crude_info = crude.fast_info
            crude_prev = getattr(crude_info, "previous_close", 0) or 0
            crude_last = getattr(crude_info, "last_price", 0) or 0

            if crude_prev and crude_last:
                crude_chg = ((crude_last - crude_prev) / crude_prev) * 100
                data["crude_change"] = f"{crude_chg:+.1f}%"
            else:
                logger.warning(
                    f"yfinance returned incomplete crude data: "
                    f"prev_close={crude_prev}, last={crude_last}. "
                    "Crude change defaulting to 0.0%."
                )
                data["crude_change"] = "0.0%"

        except Exception as e:
            logger.warning(f"Crude data fetch failed: {e}")
            data["crude_change"] = "0.0%"

        try:
            # ── USD/INR ──
            usdinr = yf.Ticker("USDINR=X")
            usdinr_info = usdinr.fast_info
            usd_rate = getattr(usdinr_info, "last_price", 0) or 0
            usd_prev = getattr(usdinr_info, "previous_close", 0) or 0

            if usd_rate:
                stability = "stable"
                if usd_prev:
                    usd_chg = abs(((usd_rate - usd_prev) / usd_prev) * 100)
                    if usd_chg > 0.5:
                        stability = "volatile"
                    elif usd_chg > 0.2:
                        stability = "moving"
                data["usd_inr"] = f"{usd_rate:.2f} {stability}"
            else:
                data["usd_inr"] = "N/A"

        except Exception as e:
            logger.warning(f"USD/INR data fetch failed: {e}")
            data["usd_inr"] = "N/A"

        try:
            # ── US Markets ──
            sp500 = yf.Ticker("^GSPC")
            sp_info = sp500.fast_info
            sp_prev = getattr(sp_info, "previous_close", 0) or 0
            sp_last = getattr(sp_info, "last_price", 0) or 0

            dow = yf.Ticker("^DJI")
            dow_info = dow.fast_info
            dow_prev = getattr(dow_info, "previous_close", 0) or 0
            dow_last = getattr(dow_info, "last_price", 0) or 0

            data["us_market_close"] = {}
            if sp_prev and sp_last:
                sp_chg = ((sp_last - sp_prev) / sp_prev) * 100
                data["us_market_close"]["sp500"] = f"{sp_chg:+.1f}%"
            if dow_prev and dow_last:
                dow_chg = ((dow_last - dow_prev) / dow_prev) * 100
                data["us_market_close"]["dow"] = f"{dow_chg:+.1f}%"

            # ── Global Sentiment ──
            if sp_prev and sp_last:
                sp_change = ((sp_last - sp_prev) / sp_prev) * 100
                if sp_change < -1.0:
                    data["global_sentiment"] = "RISK_OFF"
                elif sp_change > 1.0:
                    data["global_sentiment"] = "RISK_ON"
                elif sp_change < -0.3:
                    data["global_sentiment"] = "CAUTIOUS"
                elif sp_change > 0.3:
                    data["global_sentiment"] = "MILDLY_BULLISH"
                else:
                    data["global_sentiment"] = "NEUTRAL"

        except Exception as e:
            logger.warning(f"US market data fetch failed: {e}")
            data["global_sentiment"] = "NEUTRAL"

        return data

    # ─── Midday Refresh ────────────────────────────────────────────

    async def run_midday_refresh(self) -> dict:
        """
        Lighter scan at 12:00 PM — re-fetch headlines only.
        Market data doesn't need refresh (live ticks are running).
        """
        logger.info("Running midday news refresh...")
        headlines = await self._fetch_headlines()

        refresh = {
            "headlines": headlines[:10],
            "key_headline": headlines[0]["title"] if headlines else "No new headlines",
            "headline_source": headlines[0].get("source", "N/A") if headlines else "N/A",
            "scan_time": datetime.now().isoformat(),
        }

        logger.info(f"Midday refresh: {len(headlines)} headlines")
        return refresh
