"""
Smart wallet discovery for pump.fun tokens.

Strategy:
  1. Get recently graduated/active pump.fun tokens from DexScreener
  2. For each token, fetch 100 SWAP transactions via Helius
  3. For each wallet that traded, record buy/sell events
  4. A wallet's PROFIT on a token = sum(sell_sol) - sum(buy_sol)
  5. After scanning N tokens, rank wallets by:
       - win_rate  >= 60%  (won money on >= 60% of tokens they traded)
       - avg_pnl   >= 0.05 SOL per token (profitable on average)
       - min_tokens >= 3   (traded at least 3 different tokens)
       - no bot pattern    (not trading same micro-amounts on every tx)
  6. Qualified wallets are saved to smart_wallets.txt

Design:
  - wallet_db: dict[wallet_str, WalletProfile]
  - Each WalletProfile tracks per-token profit
  - run_once() scans N tokens and returns newly-qualified wallets
"""

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import aiohttp

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Wallet qualification criteria
# ──────────────────────────────────────────────────────────────────────────────
MIN_WIN_RATE     = 0.55   # ≥55% of tokens traded were profitable
MIN_AVG_PNL_SOL  = 0.02   # ≥0.02 SOL average profit per token
MIN_TOKENS       = 3      # traded at least 3 different tokens
MAX_BOT_STDDEV   = 0.0001 # if std(buy_amounts) < this → bot pattern, reject


@dataclass
class TokenPnl:
    token: str
    buy_sol: float = 0.0
    sell_sol: float = 0.0

    @property
    def pnl(self) -> float:
        return self.sell_sol - self.buy_sol

    @property
    def is_win(self) -> bool:
        return self.pnl > 0


@dataclass
class WalletProfile:
    address: str
    token_trades: dict = field(default_factory=dict)  # token -> TokenPnl

    def record_buy(self, token: str, sol: float):
        if token not in self.token_trades:
            self.token_trades[token] = TokenPnl(token)
        self.token_trades[token].buy_sol += sol

    def record_sell(self, token: str, sol: float):
        if token not in self.token_trades:
            self.token_trades[token] = TokenPnl(token)
        self.token_trades[token].sell_sol += sol

    @property
    def tokens_traded(self) -> int:
        return len(self.token_trades)

    @property
    def win_rate(self) -> float:
        trades = list(self.token_trades.values())
        if not trades:
            return 0.0
        wins = sum(1 for t in trades if t.is_win)
        return wins / len(trades)

    @property
    def avg_pnl(self) -> float:
        trades = list(self.token_trades.values())
        if not trades:
            return 0.0
        return sum(t.pnl for t in trades) / len(trades)

    def is_bot(self) -> bool:
        """Reject wallets that buy exact same micro-amounts (bot pattern)."""
        all_buys = []
        for tp in self.token_trades.values():
            if tp.buy_sol > 0:
                all_buys.append(tp.buy_sol)
        if len(all_buys) < 3:
            return False
        mean = sum(all_buys) / len(all_buys)
        if mean < 0.001:  # micro-trader, ignore
            return True
        stddev = (sum((x - mean) ** 2 for x in all_buys) / len(all_buys)) ** 0.5
        return stddev < MAX_BOT_STDDEV

    def is_qualified(self) -> bool:
        if self.tokens_traded < MIN_TOKENS:
            return False
        if self.win_rate < MIN_WIN_RATE:
            return False
        if self.avg_pnl < MIN_AVG_PNL_SOL:
            return False
        if self.is_bot():
            return False
        return True

    def summary(self) -> str:
        return (
            f"tokens={self.tokens_traded} win_rate={self.win_rate:.0%} "
            f"avg_pnl={self.avg_pnl:+.4f}SOL"
        )


# ──────────────────────────────────────────────────────────────────────────────
# API helpers
# ──────────────────────────────────────────────────────────────────────────────
DEXSCREENER_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_BOOSTS   = "https://api.dexscreener.com/token-boosts/latest/v1"


async def _fetch_pump_tokens(session: aiohttp.ClientSession, limit: int) -> list[str]:
    """Get recent pump.fun token addresses from DexScreener."""
    tokens = []
    for url in [DEXSCREENER_PROFILES, DEXSCREENER_BOOSTS]:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    continue
                data = await r.json(content_type=None)
                if not isinstance(data, list):
                    continue
                for item in data:
                    addr = item.get("tokenAddress", "")
                    chain = item.get("chainId", "")
                    if chain == "solana" and addr.endswith("pump") and addr not in tokens:
                        tokens.append(addr)
                    if len(tokens) >= limit:
                        break
        except Exception as e:
            log.debug(f"DexScreener fetch error: {e}")
        if len(tokens) >= limit:
            break
    log.info(f"DexScreener: found {len(tokens)} pump.fun tokens")
    return tokens[:limit]


async def _fetch_token_swaps(
    session: aiohttp.ClientSession,
    token_address: str,
    helius_api_key: str,
    limit: int = 100,
) -> list[dict]:
    """Fetch swap transactions for a token address via Helius."""
    url = (
        f"https://api.helius.xyz/v0/addresses/{token_address}/transactions"
        f"?api-key={helius_api_key}&limit={limit}&type=SWAP"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                log.debug(f"Helius {r.status} for {token_address[:16]}...")
                return []
            data = await r.json(content_type=None)
            return data if isinstance(data, list) else []
    except Exception as e:
        log.debug(f"Helius fetch error for {token_address[:16]}: {e}")
        return []


def _parse_swaps(txs: list[dict], token_address: str) -> dict[str, list[tuple[str, float]]]:
    """
    Parse Helius swap events.
    Returns {wallet_address: [('buy'|'sell', sol_amount), ...]}
    """
    wallet_events: dict[str, list] = {}

    for tx in txs:
        swap = tx.get("events", {}).get("swap", {})
        if not swap:
            continue

        # BUY: native SOL in → token out
        ni = swap.get("nativeInput")
        to_list = swap.get("tokenOutputs", [])
        if ni and to_list:
            wallet = ni.get("account", "")
            sol = int(ni.get("amount", 0)) / 1e9
            if wallet and sol > 0:
                wallet_events.setdefault(wallet, []).append(("buy", sol))

        # SELL: token in → native SOL out
        no = swap.get("nativeOutput")
        ti_list = swap.get("tokenInputs", [])
        if no and ti_list:
            wallet = no.get("account", "")
            sol = int(no.get("amount", 0)) / 1e9
            if wallet and sol > 0:
                wallet_events.setdefault(wallet, []).append(("sell", sol))

    return wallet_events


# ──────────────────────────────────────────────────────────────────────────────
# Main discovery logic
# ──────────────────────────────────────────────────────────────────────────────
_wallet_db: dict[str, WalletProfile] = {}


def _load_existing_wallets(wallets_file: str) -> set[str]:
    """Load existing qualified wallets from file."""
    p = Path(wallets_file)
    if not p.exists():
        return set()
    existing = set()
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            existing.add(line.split()[0])
    return existing


def _save_wallets(wallets_file: str, new_wallets: list[WalletProfile]) -> None:
    """Append newly qualified wallets to smart_wallets.txt."""
    p = Path(wallets_file)
    existing = _load_existing_wallets(wallets_file)
    lines = p.read_text().splitlines() if p.exists() else [
        "# Smart wallets — auto-populated by wallet_discovery.py",
        f"# Criteria: win_rate>={MIN_WIN_RATE:.0%}, avg_pnl>={MIN_AVG_PNL_SOL}SOL, "
        f"min_tokens>={MIN_TOKENS}, no bot pattern",
        "# Format: <wallet_address>  # stats",
    ]
    added = 0
    for wp in new_wallets:
        if wp.address not in existing:
            lines.append(f"{wp.address}  # {wp.summary()}")
            added += 1
    if added:
        p.write_text("\n".join(lines) + "\n")
        log.info(f"Saved {added} new wallets to {wallets_file}")


async def run_once(
    helius_api_key: str,
    tokens: int = 20,
    wallets_file: Optional[str] = None,
) -> list[str]:
    """
    Scan `tokens` recent pump.fun tokens, update wallet profiles,
    and return newly-qualified wallet addresses.

    Args:
        helius_api_key: Helius API key
        tokens: number of pump.fun tokens to scan
        wallets_file: path to smart_wallets.txt (None = don't save)
    """
    global _wallet_db

    log.info(f"🔍 Wallet discovery: scanning {tokens} pump.fun tokens...")

    existing_wallets = _load_existing_wallets(wallets_file) if wallets_file else set()

    async with aiohttp.ClientSession() as session:
        token_addresses = await _fetch_pump_tokens(session, limit=tokens)
        if not token_addresses:
            log.warning("No pump.fun tokens found from DexScreener")
            return []

        # Process tokens concurrently (max 5 at a time to avoid rate limits)
        sem = asyncio.Semaphore(5)

        async def process_token(addr: str) -> None:
            async with sem:
                txs = await _fetch_token_swaps(session, addr, helius_api_key)
                if not txs:
                    return
                events = _parse_swaps(txs, addr)
                for wallet, trade_list in events.items():
                    if wallet not in _wallet_db:
                        _wallet_db[wallet] = WalletProfile(wallet)
                    for event_type, sol in trade_list:
                        if event_type == "buy":
                            _wallet_db[wallet].record_buy(addr, sol)
                        else:
                            _wallet_db[wallet].record_sell(addr, sol)

        tasks = [process_token(addr) for addr in token_addresses]
        for i, coro in enumerate(asyncio.as_completed(tasks), 1):
            await coro
            if i % 5 == 0 or i == len(tasks):
                qualified_so_far = sum(
                    1 for wp in _wallet_db.values()
                    if wp.is_qualified() and wp.address not in existing_wallets
                )
                log.info(f"  Progress: {i}/{len(tasks)}, qualified so far: {qualified_so_far}")

    # Find newly qualified wallets
    new_qualified = [
        wp for wp in _wallet_db.values()
        if wp.is_qualified() and wp.address not in existing_wallets
    ]

    if new_qualified:
        log.info(f"✅ Found {len(new_qualified)} new qualifying wallets:")
        for wp in new_qualified[:10]:
            log.info(f"  {wp.address[:20]}... {wp.summary()}")
        if wallets_file:
            _save_wallets(wallets_file, new_qualified)
    else:
        total = len(_wallet_db)
        log.info(
            f"No new qualifying wallets found "
            f"(tracked {total} wallets across {len(token_addresses)} tokens)"
        )

    return [wp.address for wp in new_qualified]
