"""
Position Manager — monitors open position and triggers exits.

Rules (hardcoded, non-overridable):
- TP: +20% from entry → sell 100%
- SL: -10% from entry → sell 100%
- Polls price every 5 seconds
- No manual override
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

import aiohttp

from .config import cfg

log = logging.getLogger("dipbot.position")

DEXSCREENER_BASE = "https://api.dexscreener.com"
PRICE_POLL_INTERVAL = 5  # seconds


class ExitType(Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    ERROR = "ERROR"
    MANUAL = "MANUAL"


@dataclass
class Position:
    token_address: str
    pair_address: str
    symbol: str
    entry_price: float  # USD price at entry
    entry_sol: float  # SOL spent
    token_amount_raw: int  # raw token amount received
    token_decimals: int
    entry_time: float = field(default_factory=time.time)

    # Computed
    tp_price: float = 0.0
    sl_price: float = 0.0
    current_price: float = 0.0
    highest_price: float = 0.0

    def __post_init__(self):
        self.tp_price = self.entry_price * (1 + cfg.take_profit_pct)
        self.sl_price = self.entry_price * (1 - cfg.stop_loss_pct)
        self.highest_price = self.entry_price
        self.current_price = self.entry_price

    @property
    def pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return (self.current_price - self.entry_price) / self.entry_price

    @property
    def pnl_sol(self) -> float:
        return self.entry_sol * self.pnl_pct

    @property
    def duration_minutes(self) -> float:
        return (time.time() - self.entry_time) / 60

    @property
    def status_line(self) -> str:
        return (
            f"${self.symbol} | "
            f"Entry: ${self.entry_price:.10f} | "
            f"Now: ${self.current_price:.10f} | "
            f"PnL: {self.pnl_pct:+.1%} | "
            f"High: ${self.highest_price:.10f} | "
            f"Time: {self.duration_minutes:.0f}m"
        )


async def get_current_price(
    session: aiohttp.ClientSession, pair_address: str
) -> float | None:
    """Fetch current price from DexScreener."""
    try:
        url = f"{DEXSCREENER_BASE}/pairs/solana/{pair_address}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

        pair = data.get("pair") or (
            data.get("pairs", [None])[0]
            if isinstance(data.get("pairs"), list)
            else None
        )
        if not pair:
            return None

        return float(pair.get("priceUsd", 0) or 0)
    except Exception as e:
        log.debug(f"Price fetch failed: {e}")
        return None


async def monitor_position(
    session: aiohttp.ClientSession, position: Position
) -> tuple[ExitType, Position]:
    """
    Monitor a position until an exit condition is met.
    Returns (exit_type, updated_position).

    This is a blocking loop — call it and it runs until the position is closed.
    """
    log.info(
        f"Monitoring ${position.symbol} | "
        f"TP: ${position.tp_price:.10f} | SL: ${position.sl_price:.10f}"
    )

    consecutive_failures = 0
    max_failures = 20  # 20 failures * 5s = ~100s of no data

    while True:
        price = await get_current_price(session, position.pair_address)

        if price is None or price == 0:
            consecutive_failures += 1
            if consecutive_failures >= max_failures:
                log.error(
                    f"${position.symbol} — {max_failures} consecutive price failures, "
                    f"emergency exit"
                )
                return ExitType.ERROR, position
            await asyncio.sleep(PRICE_POLL_INTERVAL)
            continue

        consecutive_failures = 0
        position.current_price = price

        # Track highest price (for logging/future trailing stop)
        if price > position.highest_price:
            position.highest_price = price

        # Check TP
        if price >= position.tp_price:
            log.info(
                f"🟢 TP HIT ${position.symbol} at ${price:.10f} "
                f"(+{position.pnl_pct:.1%})"
            )
            return ExitType.TAKE_PROFIT, position

        # Check SL
        if price <= position.sl_price:
            log.info(
                f"🔴 SL HIT ${position.symbol} at ${price:.10f} "
                f"({position.pnl_pct:.1%})"
            )
            return ExitType.STOP_LOSS, position

        # Log every ~30s
        if int(time.time()) % 30 < PRICE_POLL_INTERVAL:
            log.info(position.status_line)

        await asyncio.sleep(PRICE_POLL_INTERVAL)
