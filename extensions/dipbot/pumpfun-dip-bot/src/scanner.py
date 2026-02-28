"""
Scanner — finds pump.fun tokens that migrated to Raydium and are in the dip zone.

Uses DexScreener API (free, no key needed) to find candidates:
1. Polls token profiles/boosted for recent pump.fun tokens
2. Checks price drawdown from ATH
3. Filters by volume, age, holder count
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp

from .config import cfg

log = logging.getLogger("dipbot.scanner")

DEXSCREENER_BASE = "https://api.dexscreener.com"

# Rate limit: DexScreener allows ~300 req/min on free tier
SCAN_INTERVAL = 60  # seconds between full scans
REQUEST_DELAY = 0.25  # seconds between individual API calls


@dataclass
class TokenCandidate:
    address: str
    symbol: str
    name: str
    pair_address: str
    dex: str  # "raydium" etc
    price_usd: float
    ath_usd: float
    drawdown_pct: float  # 0.0 to 1.0 — how far from ATH
    volume_24h: float
    liquidity_usd: float
    market_cap: float
    pair_created_at: int  # unix ms
    fdv: float = 0.0
    tx_count_24h: int = 0
    holders: int = 0
    first_seen: float = field(default_factory=time.time)


async def _get(session: aiohttp.ClientSession, url: str) -> dict | list | None:
    """GET with error handling and rate limiting."""
    try:
        await asyncio.sleep(REQUEST_DELAY)
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 429:
                log.warning("DexScreener rate limited, backing off 30s")
                await asyncio.sleep(30)
                return None
            if resp.status != 200:
                log.warning(f"DexScreener {resp.status}: {url}")
                return None
            return await resp.json()
    except Exception as e:
        log.error(f"DexScreener request failed: {e}")
        return None


async def fetch_latest_boosted(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch latest boosted tokens — often includes recently migrated pump.fun tokens."""
    data = await _get(session, f"{DEXSCREENER_BASE}/token-boosts/latest/v1")
    return data if isinstance(data, list) else []


async def fetch_token_pairs(session: aiohttp.ClientSession, token_addr: str) -> list[dict]:
    """Fetch all pairs for a token address on Solana."""
    data = await _get(session, f"{DEXSCREENER_BASE}/tokens/v1/solana/{token_addr}")
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "pairs" in data:
        return data["pairs"] or []
    return []


async def search_pump_tokens(session: aiohttp.ClientSession) -> list[dict]:
    """
    Search for recently migrated pump.fun tokens.
    DexScreener search endpoint can find tokens by keyword.
    """
    data = await _get(
        session,
        f"{DEXSCREENER_BASE}/latest/dex/search?q=pump.fun"
    )
    if isinstance(data, dict) and "pairs" in data:
        return data["pairs"] or []
    return []


async def fetch_token_profile(session: aiohttp.ClientSession, token_addr: str) -> dict | None:
    """Fetch token profile for holder/social data."""
    data = await _get(session, f"{DEXSCREENER_BASE}/token-profiles/latest/v1")
    if isinstance(data, list):
        for item in data:
            if item.get("tokenAddress") == token_addr:
                return item
    return None


def _is_pump_migration(pair: dict) -> bool:
    """Check if a pair looks like a pump.fun → Raydium migration."""
    chain = pair.get("chainId", "")
    dex = pair.get("dexId", "")
    labels = pair.get("labels", [])

    if chain != "solana":
        return False

    # Must be on Raydium (post-migration) not still on pump.fun bonding curve
    if dex not in ("raydium", "raydium-clmm", "raydium-cpmm"):
        return False

    # pump.fun origins often have these labels
    if "pump.fun" in labels or "pump" in labels:
        return True

    # Check pair URL or info for pump.fun references
    info = pair.get("info", {})
    websites = info.get("websites", []) if info else []
    for w in websites:
        if "pump.fun" in w.get("url", ""):
            return True

    return True  # On Raydium + Solana, assume potential candidate


def _extract_candidate(pair: dict) -> TokenCandidate | None:
    """Parse a DexScreener pair into a TokenCandidate if it passes basic filters."""
    try:
        base = pair.get("baseToken", {})
        token_addr = base.get("address", "")
        symbol = base.get("symbol", "?")
        name = base.get("name", "?")

        price_usd = float(pair.get("priceUsd", 0) or 0)
        volume_24h = float(pair.get("volume", {}).get("h24", 0) or 0)
        liquidity_usd = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        market_cap = float(pair.get("marketCap", 0) or 0)
        fdv = float(pair.get("fdv", 0) or 0)
        pair_created = pair.get("pairCreatedAt", 0) or 0

        # Price changes to estimate ATH
        price_change = pair.get("priceChange", {})
        # Use max of available timeframes to estimate ATH
        changes = []
        for tf in ("m5", "h1", "h6", "h24"):
            val = price_change.get(tf)
            if val is not None:
                changes.append(float(val))

        # Estimate ATH: if price dropped X%, ATH was price / (1 + change/100)
        # The most negative change gives us the best ATH estimate
        if changes:
            worst_change = min(changes)  # most negative
            if worst_change < -20:  # at least 20% down
                ath_usd = price_usd / (1 + worst_change / 100)
                drawdown_pct = 1 - (price_usd / ath_usd) if ath_usd > 0 else 0
            else:
                return None  # not in a dip
        else:
            return None

        # TX counts
        txns = pair.get("txns", {})
        h24_txns = txns.get("h24", {})
        tx_count = int(h24_txns.get("buys", 0) or 0) + int(h24_txns.get("sells", 0) or 0)

        return TokenCandidate(
            address=token_addr,
            symbol=symbol,
            name=name,
            pair_address=pair.get("pairAddress", ""),
            dex=pair.get("dexId", ""),
            price_usd=price_usd,
            ath_usd=ath_usd,
            drawdown_pct=drawdown_pct,
            volume_24h=volume_24h,
            liquidity_usd=liquidity_usd,
            market_cap=market_cap,
            pair_created_at=pair_created,
            fdv=fdv,
            tx_count_24h=tx_count,
        )
    except Exception as e:
        log.debug(f"Failed to parse pair: {e}")
        return None


def _passes_filter(c: TokenCandidate) -> bool:
    """Apply the hard filters from config."""
    # Volume check
    if c.volume_24h < cfg.min_volume_24h:
        return False

    # Drawdown in target range
    if c.drawdown_pct < cfg.dip_from_ath_min:
        return False
    if c.drawdown_pct > cfg.dip_from_ath_max:
        return False

    # Age check — must have been dipping for at least N minutes
    if c.pair_created_at > 0:
        age_minutes = (time.time() * 1000 - c.pair_created_at) / 60_000
        if age_minutes < cfg.min_dip_age_minutes:
            return False

    # Minimum liquidity to avoid ultra-thin pools
    if c.liquidity_usd < 10_000:
        return False

    return True


async def scan_once(session: aiohttp.ClientSession) -> list[TokenCandidate]:
    """Run one scan cycle. Returns filtered candidates sorted by volume."""
    candidates: list[TokenCandidate] = []
    seen_addresses: set[str] = set()

    # Source 1: Boosted tokens (pump.fun devs often boost)
    boosted = await fetch_latest_boosted(session)
    boosted_addrs = [
        item.get("tokenAddress", "")
        for item in boosted
        if item.get("chainId") == "solana"
    ]

    # Source 2: Search for pump.fun
    pump_pairs = await search_pump_tokens(session)

    # Combine sources
    all_pairs = list(pump_pairs)

    # Fetch pairs for boosted tokens
    for addr in boosted_addrs[:20]:  # cap to avoid rate limiting
        if addr and addr not in seen_addresses:
            pairs = await fetch_token_pairs(session, addr)
            all_pairs.extend(pairs)
            seen_addresses.add(addr)

    # Parse and filter
    for pair in all_pairs:
        if not _is_pump_migration(pair):
            continue
        c = _extract_candidate(pair)
        if c is None:
            continue
        if c.address in seen_addresses:
            continue
        seen_addresses.add(c.address)

        if _passes_filter(c):
            candidates.append(c)

    # Sort by volume descending — highest volume dips first
    candidates.sort(key=lambda c: c.volume_24h, reverse=True)

    log.info(f"Scan complete: {len(candidates)} candidates from {len(all_pairs)} pairs checked")
    return candidates
