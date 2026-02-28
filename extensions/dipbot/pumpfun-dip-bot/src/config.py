"""Configuration loader — reads .env and smart_wallets.txt"""

import os
from pathlib import Path
from dataclasses import dataclass, field
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

load_dotenv(CONFIG_DIR / ".env")


@dataclass
class Config:
    # Keys
    solana_private_key: str = ""
    helius_api_key: str = ""
    helius_rpc_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Strategy
    take_profit_pct: float = 0.20
    stop_loss_pct: float = 0.10
    min_volume_24h: float = 1_000_000
    min_holders: int = 2000
    max_top20_concentration: float = 0.25
    consolidation_candles: int = 3  # consecutive candles where buy_vol >= sell_vol
    volume_spike_multiplier: float = 3.0
    dip_from_ath_min: float = 0.60  # must be down at least 60%
    dip_from_ath_max: float = 0.85  # but not more than 85% (dead)
    min_dip_age_minutes: int = 240  # 4 hours minimum since ATH
    max_consecutive_losses: int = 2
    slippage_bps: int = 150
    jito_tip_lamports: int = 10_000

    # Smart wallets
    smart_wallets: list[str] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Config":
        wallets = []
        wallet_file = CONFIG_DIR / "smart_wallets.txt"
        if wallet_file.exists():
            for line in wallet_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    wallets.append(line)

        rpc_url = os.getenv("HELIUS_RPC_URL", "")
        api_key = os.getenv("HELIUS_API_KEY", "")
        if not rpc_url and api_key:
            rpc_url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"

        return cls(
            solana_private_key=os.getenv("SOLANA_PRIVATE_KEY", ""),
            helius_api_key=api_key,
            helius_rpc_url=rpc_url,
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "0.20")),
            stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "0.10")),
            min_volume_24h=float(os.getenv("MIN_VOLUME_24H", "1000000")),
            min_holders=int(os.getenv("MIN_HOLDERS", "2000")),
            max_top20_concentration=float(os.getenv("MAX_TOP20_CONCENTRATION", "0.25")),
            consolidation_candles=int(os.getenv("CONSOLIDATION_CANDLES", "3")),
            volume_spike_multiplier=float(os.getenv("VOLUME_SPIKE_MULTIPLIER", "3.0")),
            dip_from_ath_min=float(os.getenv("DIP_FROM_ATH_MIN", "0.60")),
            dip_from_ath_max=float(os.getenv("DIP_FROM_ATH_MAX", "0.85")),
            min_dip_age_minutes=int(os.getenv("MIN_DIP_AGE_MINUTES", "240")),
            max_consecutive_losses=int(os.getenv("MAX_CONSECUTIVE_LOSSES", "2")),
            slippage_bps=int(os.getenv("SLIPPAGE_BPS", "150")),
            jito_tip_lamports=int(os.getenv("JITO_TIP_LAMPORTS", "10000")),
            smart_wallets=wallets,
        )

    def validate(self) -> list[str]:
        errors = []
        if not self.solana_private_key:
            errors.append("SOLANA_PRIVATE_KEY is required")
        if not self.helius_api_key and not self.helius_rpc_url:
            errors.append("HELIUS_API_KEY or HELIUS_RPC_URL is required")
        if not self.smart_wallets:
            errors.append("No smart wallets configured in config/smart_wallets.txt")
        return errors


cfg = Config.load()
