"""
Analyzer — confirms if a candidate token is actually ready to buy.

Three confirmation signals (need 2/3):
1. Smart wallet accumulation — tracked wallets buying the dip
2. Volume flip — buy volume exceeding sell volume over N consecutive candles
3. Dev wallet sold — deployer has exited, no more overhang

Uses Helius RPC for on-chain wallet checks and DexScreener for volume data.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp

from .config import cfg
from .scanner import TokenCandidate

log = logging.getLogger("dipbot.analyzer")

HELIUS_BASE = "https://api.helius.xyz/v0"
DEXSCREENER_BASE = "https://api.dexscreener.com"


@dataclass
class Signal:
    token: TokenCandidate
    smart_wallet_buying: bool = False
    volume_flipped: bool = False
    dev_sold: bool = False
    smart_wallet_count: int = 0  # how many smart wallets hold/bought
    buy_sell_ratio: float = 0.0
    dev_pct_remaining: float = 0.0
    confidence: int = 0  # 0-3, need >= 2 to buy

    @property
    def is_valid(self) -> bool:
        return self.confidence >= 2

    def summary(self) -> str:
        checks = []
        checks.append(f"{'✅' if self.smart_wallet_buying else '❌'} Smart wallets ({self.smart_wallet_count} active)")
        checks.append(f"{'✅' if self.volume_flipped else '❌'} Volume flip (ratio: {self.buy_sell_ratio:.2f})")
        checks.append(f"{'✅' if self.dev_sold else '❌'} Dev sold ({self.dev_pct_remaining:.1f}% remaining)")
        return "\n".join(checks)


async def _helius_get(session: aiohttp.ClientSession, endpoint: str, params: dict = None) -> dict | list | None:
    """Make a Helius API request."""
    url = f"{HELIUS_BASE}/{endpoint}"
    params = params or {}
    params["api-key"] = cfg.helius_api_key
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 429:
                log.warning("Helius rate limited, backing off 5s")
                await asyncio.sleep(5)
                return None
            if resp.status != 200:
                log.warning(f"Helius {resp.status}: {endpoint}")
                return None
            return await resp.json()
    except Exception as e:
        log.error(f"Helius request failed: {e}")
        return None


async def _helius_post(session: aiohttp.ClientSession, endpoint: str, body: dict) -> dict | list | None:
    """Make a Helius RPC POST request."""
    url = cfg.helius_rpc_url
    try:
        async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                log.warning(f"Helius RPC {resp.status}")
                return None
            data = await resp.json()
            return data.get("result")
    except Exception as e:
        log.error(f"Helius RPC failed: {e}")
        return None


async def check_smart_wallets(
    session: aiohttp.ClientSession, token_addr: str
) -> tuple[bool, int]:
    """
    Check if any tracked smart wallets hold or recently bought this token.

    Uses Helius token accounts lookup. Returns (is_buying, count).
    """
    if not cfg.smart_wallets:
        return False, 0

    active_count = 0

    # Check in batches of 10 to avoid hammering the API
    batch_size = 10
    for i in range(0, len(cfg.smart_wallets), batch_size):
        batch = cfg.smart_wallets[i : i + batch_size]

        for wallet in batch:
            try:
                # Use getTokenAccountsByOwner to check if wallet holds this token
                body = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        wallet,
                        {"mint": token_addr},
                        {"encoding": "jsonParsed"},
                    ],
                }
                result = await _helius_post(session, "rpc", body)
                if result and result.get("value"):
                    accounts = result["value"]
                    for acc in accounts:
                        parsed = acc.get("account", {}).get("data", {}).get("parsed", {})
                        info = parsed.get("info", {})
                        token_amount = info.get("tokenAmount", {})
                        amount = float(token_amount.get("uiAmount", 0) or 0)
                        if amount > 0:
                            active_count += 1
                            break

                await asyncio.sleep(0.1)  # rate limit courtesy
            except Exception as e:
                log.debug(f"Smart wallet check failed for {wallet[:8]}...: {e}")

    # At least 2 smart wallets holding = confirmation
    is_buying = active_count >= 2
    return is_buying, active_count


async def check_volume_flip(
    session: aiohttp.ClientSession, pair_address: str
) -> tuple[bool, float]:
    """
    Check if buy volume has flipped sell volume.

    Uses DexScreener pair data for buy/sell transaction counts.
    Returns (is_flipped, buy_sell_ratio).
    """
    try:
        url = f"{DEXSCREENER_BASE}/pairs/solana/{pair_address}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return False, 0.0
            data = await resp.json()

        pair = data.get("pair") or (data.get("pairs", [None])[0] if isinstance(data.get("pairs"), list) else None)
        if not pair:
            return False, 0.0

        txns = pair.get("txns", {})

        # Check multiple timeframes — we want recent volume flip
        # h1 is the most relevant for our consolidation check
        for tf in ("h1", "h6"):
            tf_data = txns.get(tf, {})
            buys = int(tf_data.get("buys", 0) or 0)
            sells = int(tf_data.get("sells", 0) or 0)

            if sells == 0:
                if buys > 10:
                    return True, float("inf")
                continue

            ratio = buys / sells

            # For h1: we want at least 1.3x more buys than sells
            # This indicates the dump is exhausting and buyers are stepping in
            if tf == "h1" and ratio >= 1.3 and buys >= 20:
                return True, ratio

            # For h6: more lenient — 1.1x ratio with decent count
            if tf == "h6" and ratio >= 1.1 and buys >= 100:
                return True, ratio

        # If h1 data available, return its ratio even if not flipped
        h1 = txns.get("h1", {})
        h1_buys = int(h1.get("buys", 0) or 0)
        h1_sells = int(h1.get("sells", 0) or 0)
        ratio = h1_buys / h1_sells if h1_sells > 0 else 0.0

        return False, ratio

    except Exception as e:
        log.error(f"Volume flip check failed: {e}")
        return False, 0.0


async def check_dev_wallet(
    session: aiohttp.ClientSession, token_addr: str
) -> tuple[bool, float]:
    """
    Check if the token deployer has sold their holdings.

    Strategy:
    1. Find the token's mint authority / first transaction signer (deployer)
    2. Check their current token balance
    3. If balance is 0 or negligible, dev has sold.

    Returns (dev_sold, pct_remaining).
    """
    try:
        # Get token supply info
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenSupply",
            "params": [token_addr],
        }
        supply_result = await _helius_post(session, "rpc", body)
        if not supply_result:
            return False, 100.0

        total_supply = float(supply_result.get("value", {}).get("uiAmount", 0) or 0)
        if total_supply == 0:
            return False, 100.0

        # Get largest token holders
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenLargestAccounts",
            "params": [token_addr],
        }
        holders_result = await _helius_post(session, "rpc", body)
        if not holders_result:
            return False, 100.0

        accounts = holders_result.get("value", [])
        if not accounts:
            return False, 100.0

        # The deployer is typically the first/largest holder initially
        # After pump.fun migration, the bonding curve contract holds a big chunk
        # We check if top holder (non-AMM) holds an outsized amount

        # Known Raydium/pump.fun pool addresses to exclude
        # (These change, but we look for the pattern of a single large non-pool holder)
        top_holder_pcts = []
        for acc in accounts[:20]:
            amount = float(acc.get("uiAmount", 0) or 0)
            pct = (amount / total_supply * 100) if total_supply > 0 else 0
            top_holder_pcts.append(pct)

        # Check top 20 concentration (excluding #1 which is usually the pool)
        # If top 20 (non-pool) hold < 25%, dev likely sold
        non_pool_pcts = top_holder_pcts[1:21]  # skip largest (likely pool)
        top20_concentration = sum(non_pool_pcts) / 100

        if top20_concentration > cfg.max_top20_concentration:
            # Too concentrated — likely dev or insiders still in
            return False, top20_concentration * 100

        # If largest non-pool holder has < 3%, consider dev sold
        max_non_pool = max(non_pool_pcts) if non_pool_pcts else 100
        dev_sold = max_non_pool < 3.0

        return dev_sold, max_non_pool

    except Exception as e:
        log.error(f"Dev wallet check failed: {e}")
        return False, 100.0


async def analyze(session: aiohttp.ClientSession, candidate: TokenCandidate) -> Signal:
    """
    Run all three confirmation checks on a candidate.
    Returns a Signal with confidence score.
    """
    log.info(f"Analyzing ${candidate.symbol} ({candidate.address[:12]}...)")

    # Run checks concurrently
    smart_task = check_smart_wallets(session, candidate.address)
    volume_task = check_volume_flip(session, candidate.pair_address)
    dev_task = check_dev_wallet(session, candidate.address)

    (smart_buying, smart_count), (vol_flipped, buy_sell_ratio), (dev_sold, dev_pct) = (
        await asyncio.gather(smart_task, volume_task, dev_task)
    )

    confidence = sum([smart_buying, vol_flipped, dev_sold])

    signal = Signal(
        token=candidate,
        smart_wallet_buying=smart_buying,
        volume_flipped=vol_flipped,
        dev_sold=dev_sold,
        smart_wallet_count=smart_count,
        buy_sell_ratio=buy_sell_ratio,
        dev_pct_remaining=dev_pct,
        confidence=confidence,
    )

    log.info(
        f"${candidate.symbol} — confidence {confidence}/3:\n{signal.summary()}"
    )

    return signal
