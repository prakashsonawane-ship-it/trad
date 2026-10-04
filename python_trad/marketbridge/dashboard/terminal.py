"""
Rich Terminal Display — Live session status in the console.

Shows:
- Session header (index, date, mode)
- Current LTP + day range
- Active indicators snapshot
- Open positions with P&L
- Recent agent decisions
- System health (WS status, candle count)
"""

import logging
from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich import box

from data.state import MarketState
import config

logger = logging.getLogger(__name__)
console = Console()


class TerminalDisplay:
    """Rich console display for live session monitoring."""

    def __init__(self, state: MarketState):
        self._state = state
        self._agent_stats: dict = {}
        self._order_stats: dict = {}

    def set_agent_stats(self, stats: dict) -> None:
        self._agent_stats = stats

    def set_order_stats(self, stats: dict) -> None:
        self._order_stats = stats

    def print_session_banner(self, index: str) -> None:
        """Print the session startup banner."""
        lot = config.NIFTY_LOT if index == "NIFTY" else config.SENSEX_LOT
        mode = "PAPER" if config.PAPER_MODE else "LIVE"
        mode_color = "yellow" if config.PAPER_MODE else "red"

        banner = Text()
        banner.append("MARKETBRIDGE AI v2.1\n", style="bold cyan")
        banner.append(f"{index} EXPIRY SESSION\n", style="bold white")
        banner.append(f"{datetime.now().strftime('%A, %d %B %Y')}\n", style="dim")
        banner.append(f"Lot: {lot} | Capital: Rs.{config.TOTAL_CAPITAL} | ", style="white")
        banner.append(f"Mode: {mode}", style=f"bold {mode_color}")

        console.print(Panel(banner, title="[bold green]Session Start[/]", border_style="green"))

    def print_status(self, index: str) -> None:
        """Print a compact status update."""
        idx_data = self._state.get("index", index) or {}
        ltp = idx_data.get("ltp", 0)
        day_high = idx_data.get("high", 0)
        day_low = idx_data.get("low", 0)

        indicators = self._state.get("indicators", index) or {}
        positions = self._state.positions
        candle_count = len(self._state.get_candles(index, "1m"))

        ws_icon = "[green]WS[/]" if self._state.ws_connected else "[red]WS[/]"
        time_str = datetime.now().strftime("%H:%M:%S")

        # Main status line
        console.print(
            f"  {ws_icon} {time_str} | "
            f"[bold]{index}[/]: [cyan]Rs.{ltp:.2f}[/] "
            f"(H:{day_high:.0f} L:{day_low:.0f}) | "
            f"Candles: {candle_count} | "
            f"Pos: {len(positions)} | "
            f"PnL: Rs.{self._state.daily_pnl:.0f}"
        )

    def print_indicators(self, index: str) -> None:
        """Print current indicator snapshot."""
        ind = self._state.get("indicators", index) or {}
        if not ind:
            return

        table = Table(title="Indicators", box=box.SIMPLE, show_header=False, padding=(0, 1))
        table.add_column("Name", style="dim")
        table.add_column("Value", style="cyan")

        table.add_row("EMA", f"9={ind.get('ema_9', 0):.0f} 21={ind.get('ema_21', 0):.0f} 50={ind.get('ema_50', 0):.0f} [{ind.get('ema_alignment', '?')}]")
        table.add_row("RSI", f"{ind.get('rsi', 0):.1f} [{ind.get('rsi_zone', '?')}]")
        table.add_row("MACD", f"{ind.get('macd_histogram', 0):+.1f} [{ind.get('macd_cross', 'NONE')}]")
        table.add_row("VWAP", f"{ind.get('vwap', 0):.0f} [{ind.get('vwap_position', '?')}]")
        table.add_row("ATR", f"{ind.get('atr', 0):.1f}")

        console.print(table)

    def print_positions(self) -> None:
        """Print open positions table."""
        positions = self._state.positions
        if not positions:
            return

        table = Table(title="Open Positions", box=box.ROUNDED)
        table.add_column("Instrument", style="white")
        table.add_column("Dir", style="cyan")
        table.add_column("Entry", style="green")
        table.add_column("Current", style="yellow")
        table.add_column("P&L", style="bold")
        table.add_column("Stop", style="red")

        for pos in positions:
            curr = pos.current_premium or pos.entry_premium
            pnl = (curr - pos.entry_premium) * pos.quantity
            pnl_style = "green" if pnl >= 0 else "red"

            table.add_row(
                pos.instrument_name,
                pos.direction,
                f"Rs.{pos.entry_premium:.2f}",
                f"Rs.{curr:.2f}",
                f"[{pnl_style}]Rs.{pnl:+.0f}[/]",
                f"Rs.{pos.stop:.2f}",
            )

        console.print(table)

    def print_session_summary(self, index: str) -> None:
        """Print end-of-session summary."""
        candle_count = len(self._state.get_candles(index, "1m"))
        pnl = self._state.daily_pnl
        pnl_style = "green" if pnl >= 0 else "red"

        summary = Text()
        summary.append("SESSION COMPLETE\n\n", style="bold")
        summary.append(f"Candles recorded: {candle_count}\n")
        summary.append(f"Daily P&L: ", style="white")
        summary.append(f"Rs.{pnl:+.0f}\n", style=f"bold {pnl_style}")
        summary.append(f"Agent calls: {self._agent_stats.get('total_calls', 0)}\n")
        summary.append(f"Orders placed: {self._order_stats.get('total_orders', 0)}\n")
        summary.append(f"Paper mode: {'Yes' if config.PAPER_MODE else 'No'}\n")

        border_color = "green" if pnl >= 0 else "red"
        console.print(Panel(summary, title=f"[bold]{index} Summary[/]", border_style=border_color))
