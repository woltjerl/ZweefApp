#!/usr/bin/env python3
"""
Backfill hourly convection weather for EHHV into table `weather_conv`.

Source: Open-Meteo historical-forecast API (same models as the live forecast, so
training and prediction use the same kind of data). Boundary-layer height is
missing there before 2024-09; it is filled from the reanalysis archive where
the archive has it. Hours without a value stay NULL (the models handle that).

Usage:
    python scripts/backfill_convection_weather.py               # incremental
    python scripts/backfill_convection_weather.py --from 2024-03-01 --to 2026-11-20
"""

import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DB_PATH  # noqa: E402
from src.weather.open_meteo_forecast import CONVECTION_COLUMNS, fetch_convection  # noqa: E402

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS weather_conv (
    date TEXT, hour_utc INTEGER,
    {', '.join(f'{c} REAL' for c in CONVECTION_COLUMNS)},
    blh_source TEXT,
    PRIMARY KEY (date, hour_utc)
)"""


def chunks(start: date, end: date, days: int = 365):
    cur = start
    while cur <= end:
        stop = min(end, cur + timedelta(days=days - 1))
        yield cur, stop
        cur = stop + timedelta(days=1)


def backfill(db_path: Path, start: date, end: date) -> int:
    conn = sqlite3.connect(db_path)
    conn.execute(SCHEMA)
    total = 0
    for a, b in chunks(start, end):
        print(f"  {a} → {b}: historical forecast...", flush=True)
        rows = fetch_convection('historical_forecast', a, b)
        print(f"  {a} → {b}: archive boundary-layer height...", flush=True)
        arch = fetch_convection('archive', a, b, variables={'boundary_layer_height': 'blh'})
        arch_blh = {(r['date'], r['hour_utc']): r['blh'] for r in arch}

        out = []
        for r in rows:
            src = 'historical_forecast'
            if r['blh'] is None:
                r['blh'] = arch_blh.get((r['date'], r['hour_utc']))
                src = 'archive' if r['blh'] is not None else None
            out.append([r['date'], r['hour_utc']] + [r[c] for c in CONVECTION_COLUMNS] + [src])

        cols = ['date', 'hour_utc'] + CONVECTION_COLUMNS + ['blh_source']
        conn.executemany(
            f"INSERT OR REPLACE INTO weather_conv ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            out)
        conn.commit()
        total += len(out)
    conn.close()
    return total


def main():
    p = argparse.ArgumentParser(description='Backfill convection weather (Open-Meteo)')
    p.add_argument('--from', dest='from_date')
    p.add_argument('--to', dest='to_date')
    p.add_argument('--db-path', default=str(DB_PATH))
    args = p.parse_args()

    db = Path(args.db_path)
    end = date.fromisoformat(args.to_date) if args.to_date else date.today() - timedelta(days=1)
    if args.from_date:
        start = date.fromisoformat(args.from_date)
    else:
        conn = sqlite3.connect(db)
        conn.execute(SCHEMA)
        last = conn.execute("SELECT MAX(date) FROM weather_conv").fetchone()[0]
        first_flight = conn.execute("SELECT MIN(datum) FROM flights").fetchone()[0]
        conn.close()
        # re-fetch the last few days: the historical-forecast archive fills in with a delay
        start = (date.fromisoformat(last) - timedelta(days=5)) if last else date.fromisoformat(first_flight[:10])

    if start > end:
        print("weather_conv is up to date.")
        return 0
    print(f"Backfilling convection weather {start} → {end}")
    n = backfill(db, start, end)
    print(f"✓ {n} hourly rows written to weather_conv")
    return 0


if __name__ == '__main__':
    sys.exit(main())
