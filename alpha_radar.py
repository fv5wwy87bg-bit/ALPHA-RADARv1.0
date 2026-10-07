"""Transparent market scanner for Binance USDT spot/perpetual pairs."""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from signal_store import evaluate_due_outcomes, print_outcome_report, record_observations

SPOT_API = "https://api.binance.com"
FUTURES_API = "https://fapi.binance.com"
EXCLUDED_BASES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "USDE", "USTC", "EUR"}
log = logging.getLogger("alpha_radar")


def get_json(url: str, *, params: dict[str, Any] | None = None) -> Any:
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "alpha-radar/0.1"})
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.loads(response.read())


def load_env_file() -> None:
    """Load simple KEY=value entries from .env without overriding real env vars."""
    try:
        lines = Path(".env").read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def score_signal(
    price_change_1h_pct: float,
    volume_ratio: float,
    oi_change_pct: float | None,
    funding_rate: float | None,
    taker_buy_imbalance: float | None = None,
) -> dict[str, Any]:
    """Return an explainable 0-100 ranking score and its component points."""
    momentum = clamp(price_change_1h_pct * 10, 0, 30)
    volume = clamp((volume_ratio - 1) * 15, 0, 25)
    oi = 0.0
    if oi_change_pct is not None and price_change_1h_pct > 0:
        oi = clamp(oi_change_pct * 5, 0, 15)
    agreement = 20.0 if price_change_1h_pct > 0 and oi_change_pct is not None and oi_change_pct > 0 else 0.0
    taker_flow = 0.0
    if taker_buy_imbalance is not None:
        # Positive imbalance means taker buys exceeded taker sells. This is
        # spot trade pressure, not exchange wallet inflow/outflow or CVD.
        taker_flow = clamp(taker_buy_imbalance * 20, 0, 10)
    funding_penalty = 0.0
    if funding_rate is not None:
        funding_penalty = clamp((funding_rate - 0.0002) * 20_000, 0, 20)
    score = round(clamp(momentum + volume + oi + agreement + taker_flow - funding_penalty, 0, 100))
    return {
        "score": score,
        "components": {
            "momentum": round(momentum, 1),
            "volume_expansion": round(volume, 1),
            "open_interest": round(oi, 1),
            "price_oi_agreement": round(agreement, 1),
            "spot_taker_buy_pressure": round(taker_flow, 1),
            "funding_penalty": round(funding_penalty, 1),
        },
    }


def load_state() -> dict[str, Any]:
    state_file = Path(os.getenv("STATE_FILE", "radar_state.json"))
    try:
        return json.loads(state_file.read_text())
    except (OSError, json.JSONDecodeError):
        return {"open_interest": {}, "alerts": {}}


def save_state(state: dict[str, Any]) -> None:
    state_file = Path(os.getenv("STATE_FILE", "radar_state.json"))
    temporary = state_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2))
    temporary.replace(state_file)


def eligible_symbols() -> set[str]:
    spot = get_json(f"{SPOT_API}/api/v3/exchangeInfo")
    futures = get_json(f"{FUTURES_API}/fapi/v1/exchangeInfo")
    spot_symbols = {
        item["symbol"] for item in spot["symbols"]
        if item.get("status") == "TRADING"
        and item.get("quoteAsset") == "USDT"
        and item.get("isSpotTradingAllowed", True)
        and item.get("baseAsset") not in EXCLUDED_BASES
    }
    perpetuals = {
        item["symbol"] for item in futures["symbols"]
        if item.get("status") == "TRADING"
        and item.get("contractType") == "PERPETUAL"
        and item.get("quoteAsset") == "USDT"
    }
    return spot_symbols & perpetuals


def scan_symbol(symbol: str, prior_oi: dict[str, float], funding: dict[str, float]) -> dict[str, Any] | None:
    candles = get_json(f"{SPOT_API}/api/v3/klines", params={"symbol": symbol, "interval": "5m", "limit": 22})
    if len(candles) < 22:
        return None
    closed = candles[:-1]  # exclude the still-forming candle
    last_close = float(closed[-1][4])
    close_1h_ago = float(closed[-13][4])
    if close_1h_ago <= 0:
        return None
    price_change = (last_close / close_1h_ago - 1) * 100
    last_quote_volume = float(closed[-1][7])
    baseline = sum(float(candle[7]) for candle in closed[-21:-1]) / 20
    volume_ratio = last_quote_volume / baseline if baseline > 0 else 0.0

    # Binance kline field 10 is quote volume bought by taker orders.
    # Compare it with all quote volume in the latest 12 closed 5m candles.
    recent_hour = closed[-12:]
    recent_quote_volume = sum(float(candle[7]) for candle in recent_hour)
    recent_taker_buy_quote = sum(float(candle[10]) for candle in recent_hour)
    taker_buy_imbalance = (
        2 * recent_taker_buy_quote / recent_quote_volume - 1
        if recent_quote_volume > 0 else 0.0
    )

    oi_payload = get_json(f"{FUTURES_API}/fapi/v1/openInterest", params={"symbol": symbol})
    current_oi = float(oi_payload["openInterest"])
    previous = prior_oi.get(symbol)
    oi_change = ((current_oi / previous) - 1) * 100 if previous and previous > 0 else None
    rate = funding.get(symbol)
    scored = score_signal(price_change, volume_ratio, oi_change, rate, taker_buy_imbalance)
    return {
        "symbol": symbol,
        "price": last_close,
        "price_change_1h_pct": price_change,
        "spot_volume_ratio": volume_ratio,
        "spot_taker_buy_imbalance_1h": taker_buy_imbalance,
        "open_interest": current_oi,
        "oi_change_pct": oi_change,
        "funding_rate": rate,
        **scored,
    }


def telegram_send(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "alpha-radar/0.1"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        response.read()


def format_signal(item: dict[str, Any]) -> str:
    oi = "baseline" if item["oi_change_pct"] is None else f'{item["oi_change_pct"]:+.2f}%'
    funding = "n/a" if item["funding_rate"] is None else f'{item["funding_rate"] * 100:+.4f}%'
    buy_pressure = item.get("spot_taker_buy_imbalance_1h")
    buy_pressure_text = "n/a" if buy_pressure is None else f'{buy_pressure * 100:+.1f}%'
    return (
        f'{item["symbol"]}  score {item["score"]}/100 | '
        f'1h {item["price_change_1h_pct"]:+.2f}% | '
        f'5m spot volume {item["spot_volume_ratio"]:.2f}x baseline | '
        f'OI {oi} | taker buy imbalance 1h {buy_pressure_text} | funding {funding}'
    )


def run_scan() -> list[dict[str, Any]]:
    limit = int(os.getenv("MAX_SYMBOLS", "100"))
    limit = int(clamp(limit, 1, 200))
    min_volume = float(os.getenv("MIN_24H_QUOTE_VOLUME", "10000000"))
    alert_score = int(clamp(int(os.getenv("ALERT_SCORE", "65")), 0, 100))
    state = load_state()
    prior_oi: dict[str, float] = state.get("open_interest", {})

    symbols = eligible_symbols()
    spot_tickers = get_json(f"{SPOT_API}/api/v3/ticker/24hr")
    ranked = sorted(
        (
            ticker for ticker in spot_tickers
            if ticker.get("symbol") in symbols
            and float(ticker.get("quoteVolume", 0)) >= min_volume
        ),
        key=lambda ticker: float(ticker["quoteVolume"]),
        reverse=True,
    )[:limit]
    funding_rows = get_json(f"{FUTURES_API}/fapi/v1/premiumIndex")
    funding = {row["symbol"]: float(row["lastFundingRate"]) for row in funding_rows}

    results: list[dict[str, Any]] = []
    current_oi: dict[str, float] = {}
    workers = int(clamp(int(os.getenv("WORKERS", "8")), 1, 16))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(scan_symbol, row["symbol"], prior_oi, funding): row["symbol"]
            for row in ranked
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                item = future.result()
                if item:
                    results.append(item)
                    current_oi[symbol] = item["open_interest"]
            except (urllib.error.URLError, KeyError, TypeError, ValueError) as exc:
                log.warning("Skipping %s: %s", symbol, exc)

    results.sort(key=lambda item: item["score"], reverse=True)
    print(f"\nScan {datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC — {len(results)} pairs")
    for item in results[:10]:
        print(format_signal(item))

    database = os.getenv("RADAR_DB", "radar_history.sqlite3")
    observed_at = time.time()
    record_observations(results, database, observed_at)
    evaluate_due_outcomes(database, observed_at)

    previous_alerts: dict[str, float] = state.get("alerts", {})
    now = time.time()
    for item in results[:10]:
        symbol = item["symbol"]
        was_above = float(previous_alerts.get(symbol, 0)) >= alert_score
        last_alert_at = float(previous_alerts.get(f"{symbol}:sent_at", 0))
        if item["score"] >= alert_score and (not was_above or now - last_alert_at >= 6 * 3600):
            try:
                telegram_send("ALPHA RADAR — market data alert (not a trade recommendation)\n" + format_signal(item))
            except urllib.error.URLError as exc:
                log.warning("Telegram alert failed for %s: %s", symbol, exc)
            previous_alerts[symbol] = item["score"]
            previous_alerts[f"{symbol}:sent_at"] = now
        else:
            previous_alerts[symbol] = item["score"]
    state["open_interest"] = current_oi
    state["alerts"] = previous_alerts
    save_state(state)
    return results


def main() -> None:
    load_env_file()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Scan liquid Binance USDT spot/perpetual pairs.")
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument("--report", action="store_true", help="summarize completed forward outcomes and exit")
    args = parser.parse_args()
    if args.report:
        print_outcome_report(os.getenv("RADAR_DB", "radar_history.sqlite3"))
        return
    interval = max(60, int(os.getenv("SCAN_INTERVAL_SECONDS", "600")))
    while True:
        try:
            run_scan()
        except (urllib.error.URLError, ValueError) as exc:
            log.exception("Scan failed: %s", exc)
        if args.once:
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
