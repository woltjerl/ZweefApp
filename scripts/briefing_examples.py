#!/usr/bin/env python3
"""
Example thermal briefings on past days, made exactly as they would have been the evening
before: the day itself is left out of the history, weather comes from the stored forecast
archive (weather_conv).

Usage:
    python scripts/briefing_examples.py --me "Woltjer"
    python scripts/briefing_examples.py --day 2026-07-28 --runway 25 --window midday --me "Woltjer"
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config as C  # noqa: E402
from src.prediction.briefing_map import build_briefing, render_briefing  # noqa: E402
from src.prediction.dataset import connect  # noqa: E402
from src.prediction.first_thermal_map import build_forecast  # noqa: E402

EXAMPLES = [('2026-07-28', '25', 'midday'), ('2026-04-29', '07', 'midday'), ('2026-04-12', '18', 'midday')]
OUT_DIR = C.OUTPUT_DIR / 'briefing_examples'


def main():
    p = argparse.ArgumentParser(description='Example thermal briefings on past days')
    p.add_argument('--day', help='one day (YYYY-MM-DD); default: three example days')
    p.add_argument('--runway', choices=['07', '12', '18', '25', '30', '36'])
    p.add_argument('--window', choices=['morning', 'midday', 'afternoon'], default='midday')
    p.add_argument('--glider', choices=['single', 'two_seat'], default='single')
    p.add_argument('--me', help='your name (or part of it) in the logbook, for the "your first thermals" layer')
    args = p.parse_args()

    conn = connect()
    todo = [(args.day, args.runway, args.window)] if args.day else EXAMPLES
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for d, rw, win in todo:
        day = date.fromisoformat(d)
        rows = pd.read_sql_query("SELECT * FROM weather_conv WHERE date BETWEEN ? AND ?", conn,
                                 params=(str(day - timedelta(days=3)), str(day + timedelta(days=1)))).to_dict('records')
        fc = build_forecast(conn, day, win, rw, args.glider, 0, weather_rows=rows, exclude_day=True)
        br = build_briefing(conn, fc, me=args.me, exclude_day=True)
        out = OUT_DIR / f"briefing_{d}_{win}_rwy{fc['runway']}.html"
        render_briefing(fc, br, out)
        spots = ', '.join(f"{s['label']} {s['dist_m'] / 1000:.1f} km {C.cardinal(s['bearing'])} ({s['n']})"
                          for s in br['spots'])
        print(f"{d} {win} rwy {fc['runway']}: chance {100 * fc['pA']:.0f}%, {br['n_sim']} similar launches, "
              f"{br['n_conn']} connected · spots: {spots}\n  ✓ {out}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
