"""Telegram notifications — fire and forget"""

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from .config import cfg

log = logging.getLogger("dipbot.telegram")

API = f"https://api.telegram.org/bot{cfg.telegram_bot_token}"


async def send(text: str, parse_mode: str = "HTML") -> None:
    """Send a message to Telegram. Never raises — logs errors silently."""
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        return
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"{API}/sendMessage",
                json={
                    "chat_id": cfg.telegram_chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            )
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


async def notify_scan(token_symbol: str, token_addr: str, reason: str) -> None:
    await send(
        f"🔍 <b>Scanning:</b> ${token_symbol}\n"
        f"<code>{token_addr}</code>\n"
        f"Reason: {reason}"
    )


async def notify_entry(
    token_symbol: str, token_addr: str, entry_price: float,
    sol_amount: float, tp_price: float, sl_price: float
) -> None:
    await send(
        f"🟢 <b>ENTRY:</b> ${token_symbol}\n"
        f"<code>{token_addr}</code>\n"
        f"Price: ${entry_price:.10f}\n"
        f"Size: {sol_amount:.4f} SOL (100% portfolio)\n"
        f"TP: ${tp_price:.10f} (+20%)\n"
        f"SL: ${sl_price:.10f} (-10%)"
    )


async def notify_exit(
    token_symbol: str, exit_type: str, pnl_pct: float,
    pnl_sol: float, duration_min: float
) -> None:
    emoji = "🟢" if pnl_pct >= 0 else "🔴"
    await send(
        f"{emoji} <b>EXIT ({exit_type}):</b> ${token_symbol}\n"
        f"PnL: {pnl_pct:+.1f}% ({pnl_sol:+.4f} SOL)\n"
        f"Duration: {duration_min:.0f}min"
    )


async def notify_shutdown(reason: str) -> None:
    await send(f"⛔ <b>Bot stopped:</b> {reason}")


async def notify_startup(sol_balance: float) -> None:
    await send(
        f"🚀 <b>DipBot started</b>\n"
        f"Balance: {sol_balance:.4f} SOL\n"
        f"Smart wallets loaded: {len(cfg.smart_wallets)}\n"
        f"TP: +{cfg.take_profit_pct*100:.0f}% | SL: -{cfg.stop_loss_pct*100:.0f}%\n"
        f"Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )


async def notify_daily_stop(losses: int) -> None:
    await send(
        f"🛑 <b>Daily stop hit:</b> {losses} consecutive losses\n"
        f"Bot paused until next UTC day."
    )
