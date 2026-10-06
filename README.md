# ALPHA RADAR

A research scanner for liquid USDT crypto markets. It ranks unusual combinations of spot momentum, spot-volume expansion, futures open-interest change, and funding.

It reports observable market conditions; it does not predict returns, place trades, or promise profitable signals.

## What it scans

- Highest-volume Binance USDT spot pairs that also have a USDT perpetual contract.
- Recent 1-hour price change.
- Latest closed 5-minute spot quote volume versus the average of the preceding 20 candles.
- Futures open-interest change since the previous scan.
- Current perpetual funding rate.

Signals use a transparent rules-based score. Data comes from public Binance market-data endpoints. No exchange keys are needed.

## Run locally

Requires Python 3.11+. The scanner uses only the Python standard library.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
cp .env.example .env
python alpha_radar.py --once
```

Set `MAX_SYMBOLS` (default 50, maximum 100), `MIN_24H_QUOTE_VOLUME`, and `ALERT_SCORE` in `.env`. Telegram alerts are optional: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Keep secrets out of Git.

Run continuously with `python alpha_radar.py`; `SCAN_INTERVAL_SECONDS` sets the interval (default 600 seconds).

## Reading a score

The score (0–100) is a ranking aid, not a probability or trade instruction. Each result shows its component measurements. High positive funding reduces the score and is treated as a crowded-long warning. The first scan records open interest as a baseline; OI-change scoring starts after the next scan.

## Limitations

- Binance-only market data in this starter version.
- Exchange-flow and on-chain data are not included yet.
- A scan does not establish predictive edge. Validate ideas with historical data, fees, slippage, and out-of-sample testing before using it.
- Public endpoints can rate-limit or be unavailable. Failed symbols are skipped and logged.

See `.env.example` for configuration. A local `radar_state.json` stores prior open-interest and alert state and is ignored by Git.
