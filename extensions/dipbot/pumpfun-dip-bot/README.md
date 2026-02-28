# PumpFun Dip Bot — Full Port Edition

Post-migration dip buyer for pump.fun → Raydium tokens.
100% portfolio per trade. 20% TP. 10% SL. No mercy.

## Strategy
1. **Scan** for tokens that recently migrated from pump.fun to Raydium
2. **Filter** by volume, holder count, dev wallet status
3. **Confirm** with smart wallet activity + volume flip detection
4. **Execute** via Jupiter with Jito MEV protection
5. **Manage** position with hard TP/SL

## Setup

```bash
# Clone to VPS alongside Ouroboros
cd ~
git clone <this-repo> pumpfun-dip-bot
cd pumpfun-dip-bot

# Create venv
python3 -m venv venv
source venv/bin/activate

# Install deps
pip install -r requirements.txt

# Configure
cp config/.env.template config/.env
nano config/.env
# Fill in: SOLANA_PRIVATE_KEY, HELIUS_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# Add smart wallets to track
nano config/smart_wallets.txt
# One address per line — curate from GMGN.ai, Birdeye leaderboards, Cielo

# Run
python -m src.main
```

## Systemd (run alongside Ouroboros)

```bash
sudo cp config/dipbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable dipbot
sudo systemctl start dipbot
```

## Rules (hardcoded, not configurable)
- One trade at a time
- 2 consecutive stops = done for the day
- No manual override on TP/SL
