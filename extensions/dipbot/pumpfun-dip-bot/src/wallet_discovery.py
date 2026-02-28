"""
Smart wallet discovery — finds consistently profitable pump.fun traders to follow.

Criteria for a "smart wallet":
  - Win rate >= 60% across >= 10 completed trades (buy + sell pairs)
  - Average profit per winning trade >= 30%
  - No single trade > 30% of total volume (not a whale manipulator)
  - Active in last 7 days
  - Realized PnL positive overall
  - Not a known bot pattern (no two trades < 5s apart)

Discovery flow (Helius-based, no pump.fun direct API needed):
  1. Fetch recently graduated pump.fun tokens via DexScreener (pumpswap → raydium migration)
  2. Fetch all trades for each token via Helius Enhanced Transactions API
  3. Group trades by wallet, compute stats
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

DEXSCREENER_BASE = "https://api.dexscreener.com"
HELIUS_BASE = "https://api.helius.xyz"


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
    """Discovers smart wallets by analyzing pump.fun trade history via Helius."""

    def __init__(self, helius_api_key: str):
        self.helius_api_key = helius_api_key
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # -------------------------------------------------------------------------
    # DexScreener — find recently migrated pump.fun tokens
    # -------------------------------------------------------------------------

    async def fetch_recent_pump_tokens(self, limit: int = 50) -> list[str]:
        """
        Fetch recently active pump.fun tokens via DexScreener.
        We use the pumpswap dex — tokens traded there are on the bonding curve
        OR recently graduated. Filter by recent creation (< 48h) and decent volume.
        """
        session = await self._get_session()
        addresses: list[str] = []
        try:
            # Get token profiles/boosts — these are often fresh pump.fun tokens
            async with session.get(
                f"{DEXSCREENER_BASE}/token-profiles/latest/v1",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    if isinstance(data, list):
                        for item in data:
                            if item.get("chainId") == "solana":
                                addr = item.get("tokenAddress", "")
                                if addr:
                                    addresses.append(addr)

            # Also try token-boosts
            async with session.get(
                f"{DEXSCREENER_BASE}/token-boosts/latest/v1",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    if isinstance(data, list):
                        for item in data:
                            if item.get("chainId") == "solana":
                                addr = item.get("tokenAddress", "")
                                if addr and addr not in addresses:
                                    addresses.append(addr)

        except Exception as e:
            logger.warning(f"DexScreener error: {e}")

        # Deduplicate and limit
        seen = set()
        unique = []
        for addr in addresses:
            if addr not in seen:
                seen.add(addr)
                unique.append(addr)
            if len(unique) >= limit:
                break

        logger.info(f"DexScreener: found {len(unique)} recent pump.fun token addresses")
        return unique

    # -------------------------------------------------------------------------
    # Helius — get transaction history for a token
    # -------------------------------------------------------------------------

    async def fetch_token_transactions(
        self,
        token_address: str,
        limit: int = 100,
    ) -> list[dict]:
        """
        Fetch enhanced transaction history for a token address via Helius.
        Returns parsed transactions with type, source, and account info.
        """
        session = await self._get_session()
        url = f"{HELIUS_BASE}/v0/addresses/{token_address}/transactions"
        params = {
            "api-key": self.helius_api_key,
            "limit": min(limit, 100),  # Helius max per call
            "type": "SWAP",
        }
        try:
            async with session.get(url, params=params) as r:
                if r.status == 429:
                    logger.warning("Helius rate limited, waiting 10s")
                    await asyncio.sleep(10)
                    return []
                if r.status != 200:
                    text = await r.text()
                    logger.debug(f"Helius {r.status} for {token_address[:8]}: {text[:100]}")
                    return []
                data = await r.json()
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug(f"fetch_token_transactions({token_address[:8]}): {e}")
            return []

    # -------------------------------------------------------------------------
    # Extract wallet trades from Helius transactions
    # -------------------------------------------------------------------------

    def _parse_trades(self, txns: list[dict], token_address: str) -> dict[str, list[dict]]:
        """
        Parse Helius enhanced transactions into per-wallet trade records.
        Each trade: {wallet, is_buy, sol_amount, timestamp}
        """
        by_wallet: dict[str, list[dict]] = {}

        for tx in txns:
            if not isinstance(tx, dict):
                continue

            # Helius enhanced tx format
            timestamp = tx.get("timestamp", 0)
            fee_payer = tx.get("feePayer", "")
            account_data = tx.get("accountData", [])
            native_transfers = tx.get("nativeTransfers", [])
            token_transfers = tx.get("tokenTransfers", [])

            # Identify the swapper (usually the fee payer)
            wallet = fee_payer
            if not wallet:
                continue

            # Determine direction: look at SOL flow vs token flow
            sol_in = 0.0
            sol_out = 0.0
            got_token = False
            sent_token = False

            for transfer in native_transfers:
                from_acc = transfer.get("fromUserAccount", "")
                to_acc = transfer.get("toUserAccount", "")
                amount = transfer.get("amount", 0) / 1e9  # lamports → SOL

                if from_acc == wallet:
                    sol_out += amount
                elif to_acc == wallet:
                    sol_in += amount

            for transfer in token_transfers:
                mint = transfer.get("mint", "")
                from_acc = transfer.get("fromUserAccount", "")
                to_acc = transfer.get("toUserAccount", "")

                if mint != token_address:
                    continue
                if to_acc == wallet:
                    got_token = True
                elif from_acc == wallet:
                    sent_token = True

            # Determine trade type
            if got_token and sol_out > 0:
                is_buy = True
                sol_amount = sol_out
            elif sent_token and sol_in > 0:
                is_buy = False
                sol_amount = sol_in
            else:
                # Can't determine direction
                continue

            if sol_amount < 0.001:  # too small to be meaningful
                continue

            trade = {
                "wallet": wallet,
                "is_buy": is_buy,
                "sol_amount": sol_amount,
                "timestamp": timestamp,
            }

            by_wallet.setdefault(wallet, []).append(trade)

        return by_wallet

    # -------------------------------------------------------------------------
    # Wallet analysis
    # -------------------------------------------------------------------------

    def _analyze_wallet(self, wallet: str, trades: list[dict]) -> WalletStats:
        """
        Build WalletStats for one wallet from its trade list.
        A "completed trade" = buys followed by sells on the same token.
        """
        stats = WalletStats(address=wallet)
        if not trades:
            return stats

        sorted_trades = sorted(trades, key=lambda t: t.get("timestamp", 0))

        # Detect bot pattern
        timestamps = [t.get("timestamp", 0) for t in sorted_trades]
        for i in range(1, len(timestamps)):
            if timestamps[i] - timestamps[i - 1] < BOT_INTERVAL_SECS:
                stats.is_bot_pattern = True
                break

        if timestamps:
            stats.last_active_days_ago = int((time.time() - max(timestamps)) / 86400)

        # PnL calculation: simple SOL in vs out
        total_sol_in = sum(t["sol_amount"] for t in sorted_trades if t["is_buy"])
        total_sol_out = sum(t["sol_amount"] for t in sorted_trades if not t["is_buy"])
        total_volume = total_sol_in + total_sol_out

        stats.total_pnl_sol = total_sol_out - total_sol_in

        # Consider each buy→sell pair as one trade
        buys = sorted([t for t in sorted_trades if t["is_buy"]], key=lambda t: t["timestamp"])
        sells = sorted([t for t in sorted_trades if not t["is_buy"]], key=lambda t: t["timestamp"])

        # Match buys to sells greedily
        buy_idx = 0
        sell_idx = 0
        completed = []

        while buy_idx < len(buys) and sell_idx < len(sells):
            buy = buys[buy_idx]
            sell = sells[sell_idx]

            if sell["timestamp"] > buy["timestamp"]:
                pnl = sell["sol_amount"] - buy["sol_amount"]
                pnl_pct = pnl / buy["sol_amount"] if buy["sol_amount"] > 0 else 0
                completed.append({"pnl_sol": pnl, "pnl_pct": pnl_pct, "vol": buy["sol_amount"] + sell["sol_amount"]})
                buy_idx += 1
                sell_idx += 1
            else:
                sell_idx += 1  # skip early sell

        stats.total_trades = len(completed)
        if not completed:
            return stats

        wins = [p for p in completed if p["pnl_sol"] > 0]
        stats.winning_trades = len(wins)
        stats.avg_win_pct = sum(p["pnl_pct"] for p in wins) / len(wins) if wins else 0.0

        if total_volume > 0:
            max_trade_vol = max(p["vol"] for p in completed)
            stats.max_single_trade_share = max_trade_vol / total_volume

        return stats

    # -------------------------------------------------------------------------
    # Main discovery runner
    # -------------------------------------------------------------------------

    async def run_discovery(self, tokens_to_scan: int = 20) -> list[str]:
        """
        Full discovery run.
        Returns list of newly added wallet addresses.
        """
        logger.info(f"🔍 Wallet discovery: scanning {tokens_to_scan} pump.fun tokens...")

        token_addresses = await self.fetch_recent_pump_tokens(limit=tokens_to_scan)
        if not token_addresses:
            logger.warning("No tokens found from DexScreener")
            return []

        all_qualifying: dict[str, WalletStats] = {}

        for i, token_addr in enumerate(token_addresses):
            txns = await self.fetch_token_transactions(token_addr, limit=100)
            if not txns:
                continue

            by_wallet = self._parse_trades(txns, token_addr)
            for wallet, trades in by_wallet.items():
                stats = self._analyze_wallet(wallet, trades)
                passes, reason = stats.qualifies()
                if passes:
                    if wallet not in all_qualifying or stats.win_rate > all_qualifying[wallet].win_rate:
                        all_qualifying[wallet] = stats
                        logger.debug(f"  ✅ {wallet[:8]}... {stats.summary()}")

            if i % 5 == 0:
                logger.info(f"  Progress: {i+1}/{len(token_addresses)}, qualified so far: {len(all_qualifying)}")

            await asyncio.sleep(0.3)  # Helius rate limit: ~10 req/s on free tier

        # Load existing wallets
        existing: set[str] = set()
        if WALLETS_FILE.exists():
            for line in WALLETS_FILE.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    addr = line.split()[0]  # address is first token
                    existing.add(addr)

        new_wallets = {addr: s for addr, s in all_qualifying.items() if addr not in existing}

        if new_wallets:
            WALLETS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with WALLETS_FILE.open("a") as f:
                for addr, stats in sorted(new_wallets.items()):
                    f.write(f"{addr}  # {stats.summary()}\n")

            logger.info(
                f"✅ Added {len(new_wallets)} new wallets "
                f"(total: {len(existing) + len(new_wallets)})"
            )
        else:
            logger.info(f"No new qualifying wallets found (existing: {len(existing)})")

        return list(new_wallets.keys())


async def run_once(helius_api_key: str, tokens: int = 20) -> list[str]:
    """Convenience function for one-shot discovery."""
    d = WalletDiscovery(helius_api_key)
    try:
        return await d.run_discovery(tokens_to_scan=tokens)
    finally:
        await d.close()


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / "config" / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    results = asyncio.run(run_once(
        os.getenv("HELIUS_API_KEY", ""),
    ))
    print(f"\nDiscovered {len(results)} new wallets:")
    for w in results:
        print(f"  {w}")
