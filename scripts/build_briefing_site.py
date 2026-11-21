#!/usr/bin/env python3
"""
Build the briefing website for one day into a folder (default output/briefing_site).

Usage:
    python scripts/build_briefing_site.py                         # today, live forecast, full database
    python scripts/build_briefing_site.py --day 2026-11-22        # another day in the forecast range
    python scripts/build_briefing_site.py --pack output/briefing_pack   # run from the history pack, as the cloud job does
    python scripts/build_briefing_site.py --day 2026-07-28 --past # a past day: stored forecast, day left out of the history

Open <out>/index.html in a browser. The hosted version is made by cloud/job.py (see cloud/README.md).
"""

import argparse
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    p = argparse.ArgumentParser(description='Build the static briefing website for one day')
    p.add_argument('--day', help='YYYY-MM-DD (default: today, Dutch time)')
    p.add_argument('--out', default=str(ROOT / 'output' / 'briefing_site'))
    p.add_argument('--pack', help='history pack folder (scripts/publish_briefing_pack.py) instead of the full database')
    p.add_argument('--me', help='your name in the logbook, for the "your first thermals" layer (full database only)')
    p.add_argument('--past', action='store_true',
                   help='past day: forecast from weather_conv and the day itself left out of the history')
    p.add_argument('--weather-db', help='database with weather_conv for --past (default: the full database)')
    p.add_argument('--windows', default='morning,midday,afternoon')
    p.add_argument('--runways', default='07,12,18,25,30,36')
    args = p.parse_args()

    if args.pack:
        from src.briefing_site.env import use_pack
        use_pack(args.pack)                               # before src.config is imported
    import pandas as pd
    from src import config as C
    from src.briefing_site.pack import my_pilot_ids
    from src.briefing_site.site import build_site

    day = date.fromisoformat(args.day) if args.day else datetime.now(ZoneInfo('Europe/Amsterdam')).date()
    conn = sqlite3.connect(str(C.DB_PATH))
    me_ids = None
    if args.me:
        if args.pack:
            p.error('--me works with the full database; for a pack, export it with --me')
        me_ids = my_pilot_ids(conn, args.me)

    rows = None
    if args.past:
        wconn = sqlite3.connect(args.weather_db or str(ROOT / 'data' / 'flights.db'))
        rows = pd.read_sql_query("SELECT * FROM weather_conv WHERE date BETWEEN ? AND ?", wconn,
                                 params=(str(day - timedelta(days=3)), str(day + timedelta(days=1)))).to_dict('records')
        if not rows:
            p.error(f'no stored weather for {day}')

    info = build_site(conn, day, args.out, weather_rows=rows, me_ids=me_ids, exclude_day=args.past,
                      windows=args.windows.split(','), runways=args.runways.split(','))
    first = {w: v['odds'][0][0] for w, v in info['windows'].items()}
    print(f"✓ {Path(args.out) / 'index.html'}  (most likely runway: "
          + ', '.join(f'{w} {r}' for w, r in first.items()) + ')')
    return 0


if __name__ == '__main__':
    sys.exit(main())
