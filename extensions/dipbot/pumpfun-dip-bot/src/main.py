"""
Main loop — the brain of the dip bot.

Cycle:
1. Scan for pump.fun migration dip candidates
2. Analyze top candidates for entry signals
3. If signal fires, execute buy (100% portfolio)
4. Monitor position until TP/SL
5. Log result, check daily loss limit
6. Repeat

One trade at a time. 2 consecutive stops = done for the day.

Wallet discovery runs as a background task every 30 minutes,
automatically building the smart_wallets.txt list over time.
"""

import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timezone, timedelta

import aiohttp

from .config import cfg, Config
from .scanner import scan_once, TokenCandidate, SCAN_INTERVAL
from .analyzer import analyze, Signal
from .executor import buy_token, sell_token, get_sol_balance, get_token_balance
from .position import Position, monitor_position, ExitType
from . import telegram

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("dipbot.log", mode="a"),
    ],
)
log = logging.getLogger("dipbot.main")

# State
consecutive_losses = 0
daily_loss_reset_date: str = ""
trades_today: list[dict] = []
running = True


def handle_signal(signum, frame):
    global running
    log.info(f"Received signal {signum}, shutting down gracefully...")
    running = False


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _check_daily_reset():
    """Reset consecutive loss counter at UTC midnight."""
    global consecutive_losses, daily_loss_reset_date, trades_today
    today = _today_utc()
    if daily_loss_reset_date != today:
        if consecutive_losses > 0:
            log.info(f"New UTC day — resetting loss counter (was {consecutive_losses})")
        consecutive_losses = 0
        daily_loss_reset_date = today
        trades_today = []


# ─────────────────────────────────────────────────────────────────────────────
# Background wallet discovery
# ─────────────────────────────────────────────────────────────────────────────

DISCOVERY_INTERVAL = 1800  # 30 minutes between discovery runs
DISCOVERY_TOKENS = 20      # how many graduated tokens to scan each run


async def _wallet_discovery_loop():
    """
    Background task: periodically scan pump.fun for smart wallets.
    Adds qualifying wallets to config/smart_wallets.txt and hot-reloads cfg.
    """
    from .wallet_discovery import WalletDiscovery

    # Stagger first run by 60s so startup completes first
    await asyncio.sleep(60)

    while running:
        try:
            log.info("🔍 Starting wallet discovery run...")
            discoverer = WalletDiscovery(cfg.helius_api_key, cfg.helius_rpc_url)
            new_wallets = await discoverer.run_discovery(tokens_to_scan=DISCOVERY_TOKENS)
            await discoverer.close()

            if new_wallets:
                # Hot-reload the wallet list
                total = cfg.reload_wallets()
                log.info(f"Wallet discovery: +{len(new_wallets)} new (total: {total})")
                await telegram.send(
                    f"🔍 <b>Wallet discovery</b>\n"
                    f"Found {len(new_wallets)} new smart wallets\n"
                    f"Total tracking: {total}"
                )
            else:
                log.info(f"Wallet discovery: no new wallets (tracking: {len(cfg.smart_wallets)})")

        except Exception as e:
            log.error(f"Wallet discovery error: {e}", exc_info=True)

        await asyncio.sleep(DISCOVERY_INTERVAL)


# ─────────────────────────────────────────────────────────────────────────────
# Scan + trade logic
# ─────────────────────────────────────────────────────────────────────────────

async def run_scan_cycle(session: aiohttp.ClientSession) -> Signal | None:
    """Scan and analyze. Returns the first valid signal, or None."""
    candidates = await scan_once(session)

    if not candidates:
        log.info("No candidates found this cycle")
        return None

    log.info(f"Evaluating {len(candidates)} candidates...")

    # Analyze top 5 by volume (don't burn API credits on all of them)
    for candidate in candidates[:5]:
        sig = await analyze(session, candidate)

        if sig.is_valid:
            log.info(f"✅ SIGNAL FIRED: ${candidate.symbol} (confidence {sig.confidence}/3)")
            await telegram.notify_scan(
                candidate.symbol,
                candidate.address,
                f"Confidence {sig.confidence}/3 — {sig.summary()}",
            )
            return sig
        else:
            log.info(f"❌ ${candidate.symbol} — confidence {sig.confidence}/3, skipping")

    return None


async def execute_trade(session: aiohttp.ClientSession, sig: Signal) -> bool:
    """Execute a full trade cycle: buy → monitor → sell. Returns True if profitable."""
    global consecutive_losses, trades_today

    token = sig.token

    # Get current balance
    sol_balance = await get_sol_balance(session)
    if sol_balance < 0.05:
        log.error(f"Insufficient SOL: {sol_balance:.4f}")
        await telegram.send(f"⚠️ Insufficient SOL: {sol_balance:.4f}")
        return False

    log.info(f"Portfolio: {sol_balance:.4f} SOL — going full port on ${token.symbol}")

    # === BUY ===
    buy_result = await buy_token(session, token.address, sol_balance)

    if not buy_result.success:
        log.error(f"Buy failed: {buy_result.error}")
        await telegram.send(f"❌ Buy failed for ${token.symbol}: {buy_result.error}")
        return False

    log.info(f"Bought ${token.symbol} — TX: {buy_result.tx_hash}")

    # Get actual token balance after buy
    await asyncio.sleep(3)  # wait for RPC to catch up
    token_balance = await get_token_balance(session, token.address)
    token_amount_raw = buy_result.amount_out

    if token_amount_raw <= 0 and token_balance > 0:
        token_amount_raw = int(token_balance * 1e6)

    # Create position
    position = Position(
        token_address=token.address,
        pair_address=token.pair_address,
        symbol=token.symbol,
        entry_price=token.price_usd,
        entry_sol=sol_balance,
        token_amount_raw=token_amount_raw,
        token_decimals=6,
    )

    await telegram.notify_entry(
        token.symbol, token.address, position.entry_price,
        sol_balance, position.tp_price, position.sl_price,
    )

    # === MONITOR ===
    exit_type, position = await monitor_position(session, position)

    # === SELL ===
    actual_balance_raw = token_amount_raw
    try:
        bal = await get_token_balance(session, token.address)
        if bal > 0:
            actual_balance_raw = int(bal * (10 ** position.token_decimals))
    except Exception:
        pass

    sell_result = await sell_token(session, token.address, actual_balance_raw)

    if not sell_result.success:
        log.error(f"SELL FAILED: {sell_result.error} — MANUAL INTERVENTION NEEDED")
        await telegram.send(
            f"🚨🚨🚨 <b>SELL FAILED</b> for ${token.symbol}!\n"
            f"Error: {sell_result.error}\n"
            f"Token: <code>{token.address}</code>\n"
            f"You need to sell manually!"
        )
        consecutive_losses += 1
        return False

    log.info(f"Sold ${token.symbol} — TX: {sell_result.tx_hash}")

    # === RESULTS ===
    pnl_pct = position.pnl_pct
    pnl_sol = position.pnl_sol
    is_win = pnl_pct >= 0

    trade_record = {
        "symbol": token.symbol,
        "address": token.address,
        "entry_price": position.entry_price,
        "exit_price": position.current_price,
        "entry_sol": position.entry_sol,
        "pnl_pct": pnl_pct,
        "pnl_sol": pnl_sol,
        "exit_type": exit_type.value,
        "duration_min": position.duration_minutes,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "buy_tx": buy_result.tx_hash,
        "sell_tx": sell_result.tx_hash,
    }
    trades_today.append(trade_record)

    await telegram.notify_exit(
        token.symbol, exit_type.value, pnl_pct * 100, pnl_sol, position.duration_minutes
    )

    if is_win:
        consecutive_losses = 0
        log.info(f"✅ WIN: {pnl_pct:+.1%} ({pnl_sol:+.4f} SOL)")
    else:
        consecutive_losses += 1
        log.info(f"❌ LOSS: {pnl_pct:+.1%} ({pnl_sol:+.4f} SOL) — streak: {consecutive_losses}")

    return is_win


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    """Main event loop."""
    global running, consecutive_losses

    # Validate config (smart_wallets absence is a warning, not an error)
    errors = cfg.validate()
    if errors:
        for e in errors:
            log.error(f"Config error: {e}")
        print("\n⚠️  Fix config errors above, then restart.")
        sys.exit(1)

    log.info("=" * 60)
    log.info("DipBot starting")
    log.info(f"  TP: +{cfg.take_profit_pct*100:.0f}%  |  SL: -{cfg.stop_loss_pct*100:.0f}%")
    log.info(f"  Smart wallets: {len(cfg.smart_wallets)} (auto-discovering more)")
    log.info(f"  Min volume: ${cfg.min_volume_24h:,.0f}")
    log.info(f"  Dip range: {cfg.dip_from_ath_min*100:.0f}%-{cfg.dip_from_ath_max*100:.0f}% from ATH")
    log.info(f"  Min dip age: {cfg.min_dip_age_minutes}min")
    log.info(f"  Max daily losses: {cfg.max_consecutive_losses}")
    log.info("=" * 60)

    async with aiohttp.ClientSession() as session:
        # Startup checks
        sol_balance = await get_sol_balance(session)
        log.info(f"Wallet balance: {sol_balance:.4f} SOL")

        if sol_balance < 0.05:
            log.error("Need at least 0.05 SOL to operate")
            sys.exit(1)

        await telegram.notify_startup(sol_balance)

        # Launch background wallet discovery
        discovery_task = asyncio.create_task(
            _wallet_discovery_loop(),
            name="wallet_discovery",
        )
        log.info("Wallet discovery background task started (first run in 60s)")

        # Main trading loop
        try:
            while running:
                _check_daily_reset()

                # Check daily loss limit
                if consecutive_losses >= cfg.max_consecutive_losses:
                    log.warning(f"Daily loss limit hit ({consecutive_losses} consecutive losses)")
                    await telegram.notify_daily_stop(consecutive_losses)

                    now = datetime.now(timezone.utc)
                    tomorrow = (now + timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    sleep_seconds = (tomorrow - now).total_seconds()
                    log.info(f"Sleeping {sleep_seconds/3600:.1f}h until next UTC day")
                    await asyncio.sleep(min(sleep_seconds, 3600))
                    continue

                try:
                    sig = await run_scan_cycle(session)

                    if sig and sig.is_valid:
                        await execute_trade(session, sig)
                        log.info("Trade complete. Cooling down 30s before next scan...")
                        await asyncio.sleep(30)
                    else:
                        log.info(f"No signal. Next scan in {SCAN_INTERVAL}s...")
                        await asyncio.sleep(SCAN_INTERVAL)

                except Exception as e:
                    log.error(f"Unexpected error in main loop: {e}", exc_info=True)
                    await telegram.send(f"⚠️ Error in main loop: {e}")
                    await asyncio.sleep(30)

        finally:
            discovery_task.cancel()
            try:
                await discovery_task
            except asyncio.CancelledError:
                pass

    log.info("Bot stopped")
    await telegram.notify_shutdown("Graceful shutdown")


def run():
    """Entry point."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Interrupted")


if __name__ == "__main__":
    run()
