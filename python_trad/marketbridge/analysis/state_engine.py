"""
State Engine — Technical indicators computed on every 1m candle close.

Computes: EMA (9,21,50), VWAP, RSI(14), MACD(12,26,9),
Bollinger Bands(20,2), ATR(14) using the `ta` library.

Results stored in state["indicators"][index].
Runs on the primary timeframe (1m) after each candle close.
"""

import logging
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional

from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands, AverageTrueRange

from data.state import MarketState, Candle
import config

logger = logging.getLogger(__name__)


class StateEngine:
    """
    Computes technical indicators after each 1m candle close.

    Usage:
        engine = StateEngine(state)
        # Called automatically via candle_builder callback:
        await engine.on_candle_close("NIFTY", candle)
    """

    def __init__(self, state: MarketState):
        self._state = state
        self._warmup_logged: dict[str, bool] = {"NIFTY": False, "SENSEX": False}
        logger.info("StateEngine initialized")

    async def on_candle_close(self, index: str, candle: Candle) -> Optional[dict]:
        """
        Recompute all indicators after a 1m candle close.

        Args:
            index: "NIFTY" or "SENSEX"
            candle: The just-closed 1m candle

        Returns:
            Dict of all computed indicators, or None if warmup incomplete.
        """
        candles = self._state.get_candles(index, "1m")

        if len(candles) < config.INDICATOR_WARMUP_CANDLES:
            if not self._warmup_logged.get(index):
                logger.info(
                    f"{index} indicator warmup: {len(candles)}/{config.INDICATOR_WARMUP_CANDLES} candles"
                )
                if len(candles) % 10 == 0:
                    logger.info(f"{index} warmup progress: {len(candles)} candles")
            return None

        if not self._warmup_logged.get(index):
            self._warmup_logged[index] = True
            logger.info(f"{index} indicator warmup complete. Computing indicators.")

        # Build DataFrame from candles
        df = self._candles_to_df(candles)

        # Compute all indicators
        indicators = self._compute_all(df, index)

        # Store in state
        await self._state.update("indicators", index, value=indicators)

        return indicators

    @staticmethod
    def _candles_to_df(candles: list[Candle]) -> pd.DataFrame:
        """Convert candle list to pandas DataFrame."""
        data = {
            "timestamp": [c.timestamp for c in candles],
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "volume": [c.volume for c in candles],
        }
        df = pd.DataFrame(data)
        df.set_index("timestamp", inplace=True)
        return df

    def _compute_all(self, df: pd.DataFrame, index: str) -> dict:
        """Compute all technical indicators from the candle DataFrame."""
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        indicators = {}

        try:
            # ── EMA (9, 21, 50) ────────────────────────────────────
            ema9 = EMAIndicator(close=close, window=9)
            ema21 = EMAIndicator(close=close, window=21)
            ema50 = EMAIndicator(close=close, window=50)

            indicators["ema_9"] = round(ema9.ema_indicator().iloc[-1], 2)
            indicators["ema_21"] = round(ema21.ema_indicator().iloc[-1], 2)
            indicators["ema_50"] = round(ema50.ema_indicator().iloc[-1], 2)

            # EMA alignment (bullish: 9 > 21 > 50, bearish: reverse)
            if indicators["ema_9"] > indicators["ema_21"] > indicators["ema_50"]:
                indicators["ema_alignment"] = "BULLISH"
            elif indicators["ema_9"] < indicators["ema_21"] < indicators["ema_50"]:
                indicators["ema_alignment"] = "BEARISH"
            else:
                indicators["ema_alignment"] = "MIXED"

        except Exception as e:
            logger.error(f"EMA computation failed: {e}")
            indicators["ema_9"] = 0
            indicators["ema_21"] = 0
            indicators["ema_50"] = 0
            indicators["ema_alignment"] = "UNKNOWN"

        try:
            # ── RSI (14) ───────────────────────────────────────────
            rsi = RSIIndicator(close=close, window=14)
            indicators["rsi"] = round(rsi.rsi().iloc[-1], 2)

            if indicators["rsi"] > 70:
                indicators["rsi_zone"] = "OVERBOUGHT"
            elif indicators["rsi"] < 30:
                indicators["rsi_zone"] = "OVERSOLD"
            else:
                indicators["rsi_zone"] = "NEUTRAL"

        except Exception as e:
            logger.error(f"RSI computation failed: {e}")
            indicators["rsi"] = 50
            indicators["rsi_zone"] = "UNKNOWN"

        try:
            # ── MACD (12, 26, 9) ───────────────────────────────────
            macd = MACD(close=close, window_slow=26, window_fast=12, window_sign=9)
            indicators["macd_line"] = round(macd.macd().iloc[-1], 2)
            indicators["macd_signal"] = round(macd.macd_signal().iloc[-1], 2)
            indicators["macd_histogram"] = round(macd.macd_diff().iloc[-1], 2)

            # MACD crossover detection
            macd_series = macd.macd()
            signal_series = macd.macd_signal()
            if len(macd_series) >= 2:
                prev_diff = macd_series.iloc[-2] - signal_series.iloc[-2]
                curr_diff = macd_series.iloc[-1] - signal_series.iloc[-1]
                if prev_diff < 0 and curr_diff > 0:
                    indicators["macd_cross"] = "BULLISH_CROSS"
                elif prev_diff > 0 and curr_diff < 0:
                    indicators["macd_cross"] = "BEARISH_CROSS"
                else:
                    indicators["macd_cross"] = "NONE"
            else:
                indicators["macd_cross"] = "NONE"

        except Exception as e:
            logger.error(f"MACD computation failed: {e}")
            indicators["macd_line"] = 0
            indicators["macd_signal"] = 0
            indicators["macd_histogram"] = 0
            indicators["macd_cross"] = "UNKNOWN"

        try:
            # ── Bollinger Bands (20, 2) ────────────────────────────
            bb = BollingerBands(close=close, window=20, window_dev=2)
            indicators["bb_upper"] = round(bb.bollinger_hband().iloc[-1], 2)
            indicators["bb_middle"] = round(bb.bollinger_mavg().iloc[-1], 2)
            indicators["bb_lower"] = round(bb.bollinger_lband().iloc[-1], 2)
            indicators["bb_width"] = round(
                bb.bollinger_wband().iloc[-1], 4
            )

            # Price position relative to bands
            last_close = close.iloc[-1]
            if last_close > indicators["bb_upper"]:
                indicators["bb_position"] = "ABOVE_UPPER"
            elif last_close < indicators["bb_lower"]:
                indicators["bb_position"] = "BELOW_LOWER"
            elif last_close > indicators["bb_middle"]:
                indicators["bb_position"] = "UPPER_HALF"
            else:
                indicators["bb_position"] = "LOWER_HALF"

        except Exception as e:
            logger.error(f"Bollinger Bands computation failed: {e}")
            indicators["bb_upper"] = 0
            indicators["bb_middle"] = 0
            indicators["bb_lower"] = 0
            indicators["bb_width"] = 0
            indicators["bb_position"] = "UNKNOWN"

        try:
            # ── ATR (14) ───────────────────────────────────────────
            atr = AverageTrueRange(high=high, low=low, close=close, window=14)
            indicators["atr"] = round(atr.average_true_range().iloc[-1], 2)

        except Exception as e:
            logger.error(f"ATR computation failed: {e}")
            indicators["atr"] = 0

        try:
            # ── VWAP ───────────────────────────────────────────────
            # VWAP = cumulative(typical_price * volume) / cumulative(volume)
            typical_price = (high + low + close) / 3
            cum_tp_vol = (typical_price * volume).cumsum()
            cum_vol = volume.cumsum()
            vwap_series = cum_tp_vol / cum_vol.replace(0, np.nan)
            indicators["vwap"] = round(vwap_series.iloc[-1], 2) if not pd.isna(vwap_series.iloc[-1]) else 0

            # Price vs VWAP
            last_close = close.iloc[-1]
            if indicators["vwap"] > 0:
                if last_close > indicators["vwap"]:
                    indicators["vwap_position"] = "ABOVE"
                else:
                    indicators["vwap_position"] = "BELOW"
            else:
                indicators["vwap_position"] = "UNKNOWN"

        except Exception as e:
            logger.error(f"VWAP computation failed: {e}")
            indicators["vwap"] = 0
            indicators["vwap_position"] = "UNKNOWN"

        # ── Current Price Context ──────────────────────────────────
        indicators["last_close"] = round(close.iloc[-1], 2)
        indicators["candle_count"] = len(df)
        indicators["computed_at"] = datetime.now().isoformat()

        return indicators
