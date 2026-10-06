"""SQLite storage and delayed outcome evaluation for Alpha Radar scans."""

from __future__ import annotations

import json
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any

HORIZONS: dict[str, int] = {
    "1h": 60 * 60,
    "6h": 6 * 60 * 60,
    "24h": 24 * 60 * 60,
    "3d": 3 * 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
}


def _connect(db_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY,
            observed_at REAL NOT NULL,
            symbol TEXT NOT NULL,
            price REAL NOT NULL,
            score INTEGER NOT NULL,
            features_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS observations_symbol_time
            ON observations(symbol, observed_at);
        CREATE TABLE IF NOT EXISTS outcomes (
            observation_id INTEGER NOT NULL REFERENCES observations(id),
            horizon TEXT NOT NULL,
            target_at REAL NOT NULL,
            exit_observed_at REAL NOT NULL,
            exit_price REAL NOT NULL,
            return_pct REAL NOT NULL,
            PRIMARY KEY (observation_id, horizon)
        );
        """
    )
    return connection


def record_observations(
    rows: list[dict[str, Any]], db_path: str | Path, observed_at: float | None = None
) -> int:
    """Persist a scan's scored markets and measured feature values."""
    timestamp = time.time() if observed_at is None else observed_at
    with _connect(db_path) as connection:
        connection.executemany(
            """INSERT INTO observations
               (observed_at, symbol, price, score, features_json)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (
                    timestamp,
                    row["symbol"],
                    float(row["price"]),
                    int(row["score"]),
                    json.dumps(row, sort_keys=True),
                )
                for row in rows
            ],
        )
    return len(rows)


def evaluate_due_outcomes(db_path: str | Path, now: float | None = None) -> int:
    """Attach the first valid recorded price at/after each target horizon.

    The exit sample can be at most max(20 minutes, 5% of the horizon) late.
    This prevents a long scanner outage from being mislabeled as an exact
    1-hour or 6-hour result. The exact sample time is stored with the return.
    """
    current_time = time.time() if now is None else now
    completed = 0
    with _connect(db_path) as connection:
        pending = connection.execute(
            """SELECT o.id, o.symbol, o.price, o.observed_at, h.name, h.seconds
               FROM observations AS o
               CROSS JOIN (
                   SELECT '1h' AS name, 3600 AS seconds UNION ALL
                   SELECT '6h', 21600 UNION ALL
                   SELECT '24h', 86400 UNION ALL
                   SELECT '3d', 259200 UNION ALL
                   SELECT '7d', 604800
               ) AS h
               LEFT JOIN outcomes AS done
                 ON done.observation_id = o.id AND done.horizon = h.name
               WHERE done.observation_id IS NULL
                 AND o.observed_at + h.seconds <= ?""",
            (current_time,),
        ).fetchall()

        for observation_id, symbol, entry_price, entry_at, horizon, seconds in pending:
            target_at = entry_at + seconds
            exit_sample = connection.execute(
                """SELECT observed_at, price FROM observations
                   WHERE symbol = ? AND observed_at >= ?
                   ORDER BY observed_at ASC LIMIT 1""",
                (symbol, target_at),
            ).fetchone()
            if exit_sample is None:
                continue
            exit_at, exit_price = exit_sample
            allowed_lag = max(20 * 60, seconds * 0.05)
            if exit_at - target_at > allowed_lag or entry_price <= 0:
                continue
            return_pct = (exit_price / entry_price - 1) * 100
            connection.execute(
                """INSERT OR IGNORE INTO outcomes
                   (observation_id, horizon, target_at, exit_observed_at,
                    exit_price, return_pct)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (observation_id, horizon, target_at, exit_at, exit_price, return_pct),
            )
            completed += 1
    return completed


def outcome_summary(db_path: str | Path) -> dict[str, dict[str, float | int]]:
    """Summarize completed forward returns by horizon."""
    summary: dict[str, dict[str, float | int]] = {}
    with _connect(db_path) as connection:
        for horizon in HORIZONS:
            values = [
                row[0]
                for row in connection.execute(
                    "SELECT return_pct FROM outcomes WHERE horizon = ? ORDER BY return_pct",
                    (horizon,),
                )
            ]
            if not values:
                continue
            summary[horizon] = {
                "count": len(values),
                "hit_rate_pct": round(sum(value > 0 for value in values) / len(values) * 100, 1),
                "mean_return_pct": round(statistics.fmean(values), 3),
                "median_return_pct": round(statistics.median(values), 3),
            }
    return summary


def print_outcome_report(db_path: str | Path) -> None:
    summary = outcome_summary(db_path)
    if not summary:
        print("No completed outcomes yet. Keep the scanner running to collect future prices.")
        return
    print("Forward outcomes from recorded scans (descriptive, not a prediction):")
    for horizon, values in summary.items():
        print(
            f"{horizon:>3} | n={values['count']:>5} | "
            f"positive={values['hit_rate_pct']:>5.1f}% | "
            f"mean={values['mean_return_pct']:+.3f}% | "
            f"median={values['median_return_pct']:+.3f}%"
        )
