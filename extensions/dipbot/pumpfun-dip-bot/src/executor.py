"""
Executor — buys and sells tokens via Jupiter aggregator with Jito MEV protection.

Flow:
1. Get quote from Jupiter
2. Build swap transaction
3. Send via Jito bundle for frontrun protection
4. Confirm transaction
"""

import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp
import base58
from solders.keypair import Keypair  # type: ignore
from solders.transaction import VersionedTransaction  # type: ignore

from .config import cfg

log = logging.getLogger("dipbot.executor")

JUPITER_QUOTE = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP = "https://quote-api.jup.ag/v6/swap"
JITO_BUNDLE = "https://mainnet.block-engine.jito.wtf/api/v1/bundles"
JITO_TXN = "https://mainnet.block-engine.jito.wtf/api/v1/transactions"

# SOL mint address
SOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass
class TradeResult:
    success: bool
    tx_hash: str = ""
    error: str = ""
    amount_in: float = 0.0  # SOL spent or tokens sold
    amount_out: float = 0.0  # tokens received or SOL received
    price: float = 0.0  # execution price in USD


def _load_keypair() -> Keypair:
    """Load Solana keypair from private key."""
    secret = base58.b58decode(cfg.solana_private_key)
    return Keypair.from_bytes(secret)


async def get_sol_balance(session: aiohttp.ClientSession) -> float:
    """Get SOL balance for the bot wallet."""
    keypair = _load_keypair()
    pubkey = str(keypair.pubkey())

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [pubkey],
    }
    try:
        async with session.post(cfg.helius_rpc_url, json=body, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            lamports = data.get("result", {}).get("value", 0)
            return lamports / 1e9
    except Exception as e:
        log.error(f"Failed to get SOL balance: {e}")
        return 0.0


async def get_token_balance(session: aiohttp.ClientSession, token_mint: str) -> float:
    """Get token balance for the bot wallet."""
    keypair = _load_keypair()
    pubkey = str(keypair.pubkey())

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            pubkey,
            {"mint": token_mint},
            {"encoding": "jsonParsed"},
        ],
    }
    try:
        async with session.post(cfg.helius_rpc_url, json=body, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            accounts = data.get("result", {}).get("value", [])
            total = 0.0
            for acc in accounts:
                parsed = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
                total += float(parsed.get("uiAmount", 0) or 0)
            return total
    except Exception as e:
        log.error(f"Failed to get token balance: {e}")
        return 0.0


async def _jupiter_quote(
    session: aiohttp.ClientSession,
    input_mint: str,
    output_mint: str,
    amount: int,  # in smallest unit (lamports for SOL)
) -> dict | None:
    """Get a swap quote from Jupiter."""
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(cfg.slippage_bps),
        "onlyDirectRoutes": "false",
        "asLegacyTransaction": "false",
    }
    try:
        async with session.get(JUPITER_QUOTE, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                text = await resp.text()
                log.error(f"Jupiter quote failed {resp.status}: {text}")
                return None
            return await resp.json()
    except Exception as e:
        log.error(f"Jupiter quote error: {e}")
        return None


async def _jupiter_swap_tx(
    session: aiohttp.ClientSession, quote: dict, pubkey: str
) -> bytes | None:
    """Build a swap transaction from Jupiter quote."""
    body = {
        "quoteResponse": quote,
        "userPublicKey": pubkey,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": cfg.jito_tip_lamports,
    }
    try:
        async with session.post(JUPITER_SWAP, json=body, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                text = await resp.text()
                log.error(f"Jupiter swap failed {resp.status}: {text}")
                return None
            data = await resp.json()
            swap_tx = data.get("swapTransaction")
            if not swap_tx:
                log.error("No swapTransaction in Jupiter response")
                return None
            return base58.b58decode(swap_tx) if len(swap_tx) < 2000 else __import__("base64").b64decode(swap_tx)
    except Exception as e:
        log.error(f"Jupiter swap tx error: {e}")
        return None


async def _send_via_jito(session: aiohttp.ClientSession, signed_tx_bytes: bytes) -> str | None:
    """Send a signed transaction via Jito for MEV protection."""
    import base64
    tx_b64 = base64.b64encode(signed_tx_bytes).decode()

    # Send as single transaction (simpler than bundle)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [
            tx_b64,
            {"encoding": "base64", "maxRetries": 3},
        ],
    }
    try:
        async with session.post(JITO_TXN, json=body, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
            if "result" in data:
                return data["result"]
            log.error(f"Jito send failed: {data}")
            return None
    except Exception as e:
        log.error(f"Jito send error: {e}")
        return None


async def _send_via_rpc(session: aiohttp.ClientSession, signed_tx_bytes: bytes) -> str | None:
    """Fallback: send via Helius RPC."""
    import base64
    tx_b64 = base64.b64encode(signed_tx_bytes).decode()

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [
            tx_b64,
            {
                "encoding": "base64",
                "skipPreflight": True,
                "maxRetries": 3,
            },
        ],
    }
    try:
        async with session.post(cfg.helius_rpc_url, json=body, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
            if "result" in data:
                return data["result"]
            log.error(f"RPC send failed: {data}")
            return None
    except Exception as e:
        log.error(f"RPC send error: {e}")
        return None


async def _confirm_tx(session: aiohttp.ClientSession, tx_hash: str, timeout: int = 60) -> bool:
    """Wait for transaction confirmation."""
    start = time.time()
    while time.time() - start < timeout:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignatureStatuses",
            "params": [[tx_hash], {"searchTransactionHistory": True}],
        }
        try:
            async with session.post(cfg.helius_rpc_url, json=body, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                statuses = data.get("result", {}).get("value", [])
                if statuses and statuses[0]:
                    status = statuses[0]
                    if status.get("err"):
                        log.error(f"TX failed: {status['err']}")
                        return False
                    conf = status.get("confirmationStatus")
                    if conf in ("confirmed", "finalized"):
                        return True
        except Exception:
            pass
        await asyncio.sleep(2)

    log.error(f"TX confirmation timeout: {tx_hash}")
    return False


async def buy_token(
    session: aiohttp.ClientSession,
    token_mint: str,
    sol_amount: float,
) -> TradeResult:
    """
    Buy a token with SOL via Jupiter + Jito.
    sol_amount: how much SOL to spend (full portfolio).
    """
    keypair = _load_keypair()
    pubkey = str(keypair.pubkey())

    # Reserve 0.01 SOL for fees
    lamports_to_spend = int((sol_amount - 0.01) * 1e9)
    if lamports_to_spend <= 0:
        return TradeResult(success=False, error="Insufficient SOL (need > 0.01)")

    log.info(f"Buying token {token_mint[:12]}... with {sol_amount:.4f} SOL")

    # 1. Get quote
    quote = await _jupiter_quote(session, SOL_MINT, token_mint, lamports_to_spend)
    if not quote:
        return TradeResult(success=False, error="Failed to get Jupiter quote")

    out_amount = int(quote.get("outAmount", 0))
    log.info(f"Quote: {lamports_to_spend} lamports → {out_amount} tokens")

    # 2. Build swap transaction
    tx_bytes = await _jupiter_swap_tx(session, quote, pubkey)
    if not tx_bytes:
        return TradeResult(success=False, error="Failed to build swap transaction")

    # 3. Sign transaction
    try:
        tx = VersionedTransaction.from_bytes(tx_bytes)
        signed_tx = VersionedTransaction(tx.message, [keypair])
        signed_bytes = bytes(signed_tx)
    except Exception as e:
        return TradeResult(success=False, error=f"Failed to sign: {e}")

    # 4. Send via Jito first, fallback to RPC
    tx_hash = await _send_via_jito(session, signed_bytes)
    if not tx_hash:
        log.warning("Jito failed, falling back to RPC")
        tx_hash = await _send_via_rpc(session, signed_bytes)

    if not tx_hash:
        return TradeResult(success=False, error="Failed to send transaction")

    # 5. Confirm
    confirmed = await _confirm_tx(session, tx_hash)
    if not confirmed:
        return TradeResult(success=False, error=f"TX not confirmed: {tx_hash}", tx_hash=tx_hash)

    return TradeResult(
        success=True,
        tx_hash=tx_hash,
        amount_in=sol_amount,
        amount_out=out_amount,
    )


async def sell_token(
    session: aiohttp.ClientSession,
    token_mint: str,
    token_amount_raw: int,  # raw amount in smallest unit
    decimals: int = 6,
) -> TradeResult:
    """
    Sell tokens for SOL via Jupiter + Jito.
    """
    keypair = _load_keypair()
    pubkey = str(keypair.pubkey())

    if token_amount_raw <= 0:
        return TradeResult(success=False, error="No tokens to sell")

    log.info(f"Selling {token_amount_raw} of {token_mint[:12]}...")

    # 1. Quote
    quote = await _jupiter_quote(session, token_mint, SOL_MINT, token_amount_raw)
    if not quote:
        return TradeResult(success=False, error="Failed to get sell quote")

    out_lamports = int(quote.get("outAmount", 0))
    log.info(f"Quote: {token_amount_raw} tokens → {out_lamports} lamports")

    # 2. Build swap
    tx_bytes = await _jupiter_swap_tx(session, quote, pubkey)
    if not tx_bytes:
        return TradeResult(success=False, error="Failed to build sell transaction")

    # 3. Sign
    try:
        tx = VersionedTransaction.from_bytes(tx_bytes)
        signed_tx = VersionedTransaction(tx.message, [keypair])
        signed_bytes = bytes(signed_tx)
    except Exception as e:
        return TradeResult(success=False, error=f"Failed to sign: {e}")

    # 4. Send via Jito, fallback RPC
    tx_hash = await _send_via_jito(session, signed_bytes)
    if not tx_hash:
        log.warning("Jito failed, falling back to RPC")
        tx_hash = await _send_via_rpc(session, signed_bytes)

    if not tx_hash:
        return TradeResult(success=False, error="Failed to send sell transaction")

    # 5. Confirm
    confirmed = await _confirm_tx(session, tx_hash)
    if not confirmed:
        return TradeResult(success=False, error=f"Sell TX not confirmed: {tx_hash}", tx_hash=tx_hash)

    return TradeResult(
        success=True,
        tx_hash=tx_hash,
        amount_in=token_amount_raw,
        amount_out=out_lamports / 1e9,
    )
