# DipBot — Pump.fun Dip Buying Module

Standalone async Python trading bot. Buys pump.fun tokens after migration to Raydium in the 60-85% dip zone.

## Files
- scanner.py — polls DexScreener for pump.fun → Raydium migrations
- analyzer.py — 3-signal confirmation (smart wallets, volume flip, dev sold), needs 2/3
- executor.py — Jupiter swap via Jito MEV protection
- position.py — monitors TP (+20%) / SL (-10%)
- telegram.py — notifications
- main.py — orchestration loop, 2-loss daily limit
- config.py — configuration loader
- requirements.txt — dependencies

## How To Run
cd extensions/dipbot && pip install -r requirements.txt && python -m main

## Integration Ideas
- Manage smart_wallets.txt by scraping GMGN/Birdeye
- Start/stop dipbot as a subprocess
- Analyze trade logs and tune parameters
- Add new signal confirmations
