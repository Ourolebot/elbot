"""
Smart wallet discovery — finds consistently profitable pump.fun traders to follow.

Criteria for a "smart wallet":
  - Win rate >= 60% across >= 10 completed trades (buy + sell pairs)
  - Average profit per winning trade >= 30%
  - No single trade > 30% of total volume (not a whale manipulator)
  - Active in last 7 days
  - Realized PnL positive overall
  - Not a known bot pattern (no two trades < 5s apart)

Discovery flow:
  1. Fetch recently graduated pump.fun tokens (hit Raydium bonding curve)
  2. For each token, get all trade history from pump.fun API
  3. Group trades by wallet
  4. Score each wallet against criteria
  5. Save qualifying wallets to config/smart_wallets.txt
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent / "config"
WALLETS_FILE = CONFIG_DIR / "smart_wallets.txt"

# Qualification thresholds
MIN_COMPLETED_TRADES = 10       # must have at least 10 full buy+sell cycles
MIN_WIN_RATE = 0.60             # 60% win rate
MIN_AVG_WIN_PCT = 0.30          # average winning trade = +30%
MAX_WHALE_SHARE = 0.30          # single trade can't be >30% of total volume
MAX_INACTIVE_DAYS = 7           # must have traded in last 7 days
BOT_INTERVAL_SECS = 5           # two trades this close = bot pattern


@dataclass
class WalletStats:
    address: str
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl_sol: float = 0.0
    avg_win_pct: float = 0.0
    max_single_trade_share: float = 0.0
    last_active_days_ago: int = 999
    is_bot_pattern: bool = False

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades

    def qualifies(self) -> tuple[bool, str]:
        """Returns (passes, rejection_reason). reason is empty string on pass."""
        if self.total_trades < MIN_COMPLETED_TRADES:
            return False, f"too few trades ({self.total_trades})"
        if self.win_rate < MIN_WIN_RATE:
            return False, f"win rate {self.win_rate:.0%} < {MIN_WIN_RATE:.0%}"
        if self.avg_win_pct < MIN_AVG_WIN_PCT:
            return False, f"avg win {self.avg_win_pct:.0%} < {MIN_AVG_WIN_PCT:.0%}"
        if self.max_single_trade_share > MAX_WHALE_SHARE:
            return False, "whale/manipulator pattern"
        if self.last_active_days_ago > MAX_INACTIVE_DAYS:
            return False, f"inactive {self.last_active_days_ago}d"
        if self.total_pnl_sol < 0:
            return False, "net negative PnL"
        if self.is_bot_pattern:
            return False, "bot pattern"
        return True, ""

    def summary(self) -> str:
        return (
            f"trades={self.total_trades} win={self.win_rate:.0%} "
            f"avg_win={self.avg_win_pct:.0%} pnl={self.total_pnl_sol:+.3f}SOL "
            f"active={self.last_active_days_ago}d"
        )


class WalletDiscovery:
    """Discovers smart wallets by analyzing pump.fun trade history."""

    PUMP_API = "https://frontend-api.pump.fun"

    def __init__(self, helius_api_key: str, helius_rpc_url: str):
        self.helius_api_key = helius_api_key
        self.helius_rpc_url = helius_rpc_url
        self._session: Optional[aiohttp.ClientSession] = None

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "Mozilla/5.0 (compatible; dipbot/1.0)"},
                timeout=aiohttp.ClientTimeout(total=20),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # -------------------------------------------------------------------------
    # Pump.fun API calls
    # -------------------------------------------------------------------------

    async def fetch_recent_graduates(self, limit: int = 50) -> list[str]:
        """
        Fetch recently graduated pump.fun tokens.
        Graduated = bonding curve completed = now on Raydium.
        These are the tokens where all the interesting action happened.
        """
        session = await self._session_get()
        try:
            async with session.get(
                f"{self.PUMP_API}/coins",
                params={
                    "offset": 0,
                    "limit": limit,
                    "sort": "last_trade_timestamp",
                    "order": "DESC",
                    "includeNsfw": "false",
                }
            ) as r:
                if r.status != 200:
                    logger.warning(f"fetch_recent_graduates HTTP {r.status}")
                    return []
                data = await r.json()
                # Only graduated tokens have a raydium_pool set
                mints = [c["mint"] for c in data if c.get("raydium_pool")]
                logger.info(f"Found {len(mints)}/{len(data)} graduated tokens")
                return mints
        except Exception as e:
            logger.warning(f"fetch_recent_graduates error: {e}")
            return []

    async def fetch_token_trades(self, mint: str, limit: int = 500) -> list[dict]:
        """Fetch trade history for a token from pump.fun."""
        session = await self._session_get()
        try:
            async with session.get(
                f"{self.PUMP_API}/trades/all/{mint}",
                params={"limit": limit, "offset": 0, "minimumSize": 0},
            ) as r:
                if r.status != 200:
                    logger.debug(f"fetch_token_trades({mint[:8]}) HTTP {r.status}")
                    return []
                return await r.json()
        except Exception as e:
            logger.debug(f"fetch_token_trades({mint[:8]}) error: {e}")
            return []

    # -------------------------------------------------------------------------
    # Wallet analysis
    # -------------------------------------------------------------------------

    def _analyze_wallet(self, wallet: str, trades: list[dict]) -> WalletStats:
        """
        Build WalletStats for one wallet from its trade list.
        
        A "completed trade" = one or more buys followed by a sell on the same token.
        We track SOL in/out to compute PnL.
        """
        stats = WalletStats(address=wallet)
        if not trades:
            return stats

        # Sort chronologically
        sorted_trades = sorted(trades, key=lambda t: t.get("timestamp", 0))

        # Detect bot pattern: any two consecutive trades < BOT_INTERVAL_SECS apart
        timestamps = [t.get("timestamp", 0) for t in sorted_trades]
        for i in range(1, len(timestamps)):
            if timestamps[i] - timestamps[i - 1] < BOT_INTERVAL_SECS:
                stats.is_bot_pattern = True
                break

        # Last activity
        if timestamps:
            stats.last_active_days_ago = int((time.time() - max(timestamps)) / 86400)

        # Group by token mint, track SOL flows
        positions: dict[str, dict] = {}  # mint -> {sol_in, sol_out}
        total_volume_sol = 0.0

        for t in sorted_trades:
            mint = t.get("mint", "")
            if not mint:
                continue
            sol = t.get("sol_amount", 0) / 1e9  # lamports → SOL
            is_buy = t.get("is_buy", False)

            if mint not in positions:
                positions[mint] = {"sol_in": 0.0, "sol_out": 0.0}

            if is_buy:
                positions[mint]["sol_in"] += sol
            else:
                positions[mint]["sol_out"] += sol
            total_volume_sol += sol

        # Score completed trades (both buy and sell happened)
        completed = [
            pos for pos in positions.values()
            if pos["sol_in"] > 0 and pos["sol_out"] > 0
        ]

        stats.total_trades = len(completed)
        if not completed:
            return stats

        pnl_list = []
        for pos in completed:
            pnl_sol = pos["sol_out"] - pos["sol_in"]
            pnl_pct = pnl_sol / pos["sol_in"]
            pnl_list.append({"pnl_sol": pnl_sol, "pnl_pct": pnl_pct, "vol": pos["sol_in"] + pos["sol_out"]})

        wins = [p for p in pnl_list if p["pnl_sol"] > 0]
        stats.winning_trades = len(wins)
        stats.total_pnl_sol = sum(p["pnl_sol"] for p in pnl_list)
        stats.avg_win_pct = (
            sum(p["pnl_pct"] for p in wins) / len(wins) if wins else 0.0
        )

        if total_volume_sol > 0:
            max_trade_vol = max(p["vol"] for p in pnl_list)
            stats.max_single_trade_share = max_trade_vol / total_volume_sol

        return stats

    async def discover_from_token(self, mint: str) -> list[WalletStats]:
        """
        Analyze all wallets that traded a specific token.
        Returns list of WalletStats that qualify.
        """
        trades = await self.fetch_token_trades(mint)
        if not trades:
            return []

        # Group trades by wallet
        by_wallet: dict[str, list[dict]] = {}
        for t in trades:
            w = t.get("user", "")
            if w:
                by_wallet.setdefault(w, []).append(t)

        qualified = []
        for wallet, wallet_trades in by_wallet.items():
            stats = self._analyze_wallet(wallet, wallet_trades)
            passes, reason = stats.qualifies()
            if passes:
                qualified.append(stats)
                logger.debug(f"  ✅ {wallet[:8]}... {stats.summary()}")
            else:
                logger.debug(f"  ❌ {wallet[:8]}... rejected: {reason}")

        return qualified

    # -------------------------------------------------------------------------
    # Main discovery runner
    # -------------------------------------------------------------------------

    async def run_discovery(self, tokens_to_scan: int = 20) -> list[str]:
        """
        Full discovery run:
        1. Fetch recently graduated tokens
        2. Analyze traders on each token
        3. Add qualifying wallets to smart_wallets.txt
        Returns list of newly added wallet addresses.
        """
        logger.info(f"🔍 Wallet discovery: scanning {tokens_to_scan} graduated tokens...")

        mints = await self.fetch_recent_graduates(limit=tokens_to_scan)
        if not mints:
            logger.warning("No graduated tokens found — pump.fun API might be down")
            return []

        all_qualifying: dict[str, WalletStats] = {}

        for i, mint in enumerate(mints):
            found = await self.discover_from_token(mint)
            if found:
                logger.info(f"  [{i+1}/{len(mints)}] {mint[:8]}...: {len(found)} qualified")
                for s in found:
                    # Keep the best stats if wallet appears across multiple tokens
                    if s.address not in all_qualifying or s.win_rate > all_qualifying[s.address].win_rate:
                        all_qualifying[s.address] = s
            else:
                logger.debug(f"  [{i+1}/{len(mints)}] {mint[:8]}...: none qualified")

            # Small delay to avoid rate limiting
            await asyncio.sleep(0.5)

        # Load existing wallets
        existing: set[str] = set()
        if WALLETS_FILE.exists():
            for line in WALLETS_FILE.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    existing.add(line)

        new_wallets = {addr: s for addr, s in all_qualifying.items() if addr not in existing}

        if new_wallets:
            WALLETS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with WALLETS_FILE.open("a") as f:
                for addr, stats in sorted(new_wallets.items()):
                    # Write with stats as comment for transparency
                    f.write(f"{addr}  # {stats.summary()}\n")

            logger.info(
                f"✅ Added {len(new_wallets)} wallets "
                f"(total: {len(existing) + len(new_wallets)})"
            )
        else:
            logger.info(f"No new wallets found (existing: {len(existing)})")

        return list(new_wallets.keys())


async def run_once(helius_api_key: str, helius_rpc_url: str, tokens: int = 20) -> list[str]:
    """Convenience function for one-shot discovery."""
    d = WalletDiscovery(helius_api_key, helius_rpc_url)
    try:
        return await d.run_discovery(tokens_to_scan=tokens)
    finally:
        await d.close()


if __name__ == "__main__":
    # Can run directly for testing:
    # python -m src.wallet_discovery
    import os
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / "config" / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    results = asyncio.run(run_once(
        os.getenv("HELIUS_API_KEY", ""),
        os.getenv("HELIUS_RPC_URL", ""),
    ))
    print(f"\nDiscovered {len(results)} new wallets:")
    for w in results:
        print(f"  {w}")
