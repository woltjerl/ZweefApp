#!/usr/bin/env python3
"""
Find flights that entered airspace they should not have been in.

A violation = at least --min-duration seconds continuously inside an airspace of a
checked class, at least --vertical-margin above its floor (and below its ceiling)
and --horizontal-margin inside its boundary, inside the measuring circle: --radius-km,
placed by default where it covers the most flying time (EHHV inside), or --centre.
Altitudes in the logs are GPS altitudes AMSL; flight levels use that day's QNH.

Areas only active when booked (TRA/TSA, NOTAM areas) are "unverified" (check the NOTAMs
of that day). Military TMAs/CTRs on weekends and Dutch public holidays are "possible".
Time inside a gliding sector is reported as "exempt".

Airspace: the Dutch OpenAir file from SoaringWeb (as listed by the XCSoar data
repository), cached in data/airspace/. It is NOT an official source; check the AIP.

Outputs:
  tables airspace_checks / airspace_violations in the database
  output/airspace_violations_report.html    overview: tiles, ceiling map, charts, tables (no names)
  output/airspace_violations_details.html   every event with pilots, aircraft and why; per-pilot summary
  output/airspace_events/event_<id>.html    detailed track of one event (zoomed map, altitude vs floor,
                                            distance inside the boundary, every fix)
  output/airspace_violations_map.html       interactive map (airspace, violation tracks)
  output/airspace_maps/flight_<id>.html     track + barogram page for every flight with an event
  output/airspace_violations.csv            one row per event

Usage:
  python scripts/find_airspace_violations.py
  python scripts/find_airspace_violations.py --min-duration 30 --vertical-margin 50m --radius-km 50
  python scripts/find_airspace_violations.py --centre ehhv               # circle around the field
  python scripts/find_airspace_violations.py --centre 52.15,5.45         # circle at a chosen point
  python scripts/find_airspace_violations.py --vertical-margin 150ft --horizontal-margin 100m
  python scripts/find_airspace_violations.py --classes A,B,C,D,P,R,Q --include-notam-areas
  python scripts/find_airspace_violations.py --from 2026-01-01 --to 2026-12-31
  python scripts/find_airspace_violations.py --map 119385
  python scripts/find_airspace_violations.py --year 2026 --event-pages violation-only   # separate 2026 edition
  python scripts/find_airspace_violations.py --event-top 10 --event-pilot Patrick   # which violations get a detailed track
  python scripts/find_airspace_violations.py --update-airspace
"""

import argparse
import base64
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config as C  # noqa: E402
from src.airspace import openair as OA  # noqa: E402
from src.airspace import report as RP  # noqa: E402
from src.airspace import schedule  # noqa: E402
from src.airspace.violations import Settings, best_circle, check_track, circle_coverage, select_airspaces  # noqa: E402
from src.thermal_detection.climb_detector import (airborne_intervals, clean_track, parse_times,  # noqa: E402
                                                  select_interval)

AIRSPACE_DIR = ROOT / 'data' / 'airspace'
AIRSPACE_FILE = AIRSPACE_DIR / 'NL-ASP.txt'
AIRSPACE_META = AIRSPACE_DIR / 'NL-ASP.json'
MANIFEST_API = ('https://api.github.com/repos/XCSoar/xcsoar-data-content/contents/'
                'data/remote/airspace/country/NL-ASP-National-SoaringWeb.txt.json')
FALLBACK_URI = 'https://soaring.silentflight.ca/Airspace/NL/NL-ASP_20feb2025a.txt'
OUT_CSV = C.OUTPUT_DIR / 'airspace_violations.csv'
OUT_MAP = C.OUTPUT_DIR / 'airspace_violations_map.html'
OUT_DETAILS = C.OUTPUT_DIR / 'airspace_violations_details.html'
MAP_DIR = C.OUTPUT_DIR / 'airspace_maps'
TUG_METHODS = ('sep-a', 'tmg-a', 'sep', 'tmg', 'car')
PATH_STEP = 3          # keep every 3rd fix of a violation segment for the maps


# ═══════════════════════════ airspace file ═══════════════════════════

def update_airspace(log=print):
    """Download the current Dutch OpenAir file (URL from the XCSoar data repository)."""
    uri, listed = FALLBACK_URI, None
    try:
        r = requests.get(MANIFEST_API, timeout=20, headers={'Accept': 'application/vnd.github+json'})
        r.raise_for_status()
        meta = json.loads(base64.b64decode(r.json()['content']))
        uri, listed = meta['uri'], meta.get('update')
    except Exception as e:  # noqa: BLE001
        log(f"  could not read the XCSoar index ({e}); using {FALLBACK_URI}")
    r = requests.get(uri, timeout=60)
    r.raise_for_status()
    AIRSPACE_DIR.mkdir(parents=True, exist_ok=True)
    AIRSPACE_FILE.write_bytes(r.content)
    header = [line.strip('* \t\r') for line in r.text.splitlines()[:6] if line.startswith('*')]
    AIRSPACE_META.write_text(json.dumps({'uri': uri, 'listed': listed, 'downloaded': time.strftime('%Y-%m-%d'),
                                         'header': header}, indent=2))
    log(f"  airspace file: {uri} → {AIRSPACE_FILE}")


def airspace_version(path):
    meta = json.loads(AIRSPACE_META.read_text()) if Path(path) == AIRSPACE_FILE and AIRSPACE_META.exists() else {}
    head = [line.strip('* \t\r') for line in Path(path).read_text(encoding='utf-8', errors='replace').splitlines()[:5]
            if line.startswith('*')]
    return {'file': str(path), 'uri': meta.get('uri'), 'header': ' / '.join(h for h in head if h)}


# ═══════════════════════════ checking ═══════════════════════════

def parse_length(s: str) -> float:
    """'50', '50m', '150ft' → metres."""
    m = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(m|ft|feet)?\s*', str(s).lower())
    if not m:
        raise argparse.ArgumentTypeError(f"length like 50m or 150ft expected, got {s!r}")
    v = float(m.group(1))
    return v * 0.3048 if m.group(2) in ('ft', 'feet') else v


def load_qnh(conn):
    w = pd.read_sql_query("SELECT date, hour, pres FROM weather_data WHERE pres IS NOT NULL", conn)
    return {(d, int(h)): float(p) for d, h, p in zip(w.date, w.hour, w.pres)}


def load_tracks(conn, flight_ids=None, date_from=None, date_to=None, include_tugs=False, log=print):
    """Airborne part of every selected flight (cut to the logbook flight). Returns (tracks, failed check rows)."""
    fl = pd.read_sql_query("""SELECT id, datum, start_tijd, start_methode, registratie, type,
                                     gezagvoerder_naam, tweede_inzittende_naam FROM flights""", conn)
    fl['datum'] = fl.datum.str[:10]
    if flight_ids:
        fl = fl[fl.id.isin(flight_ids)]
    if date_from:
        fl = fl[fl.datum >= date_from]
    if date_to:
        fl = fl[fl.datum <= date_to]
    if not include_tugs and not flight_ids:
        fl = fl[~fl.registratie.isin(C.TUG_REGS) & ~fl.start_methode.isin(TUG_METHODS)]
    if fl.empty:
        return [], []
    ids = ','.join(str(int(i)) for i in fl.id)
    log(f"  loading tracks of {len(fl)} flights...")
    fx = pd.read_sql_query(f"""SELECT flight_id, time, latitude, longitude, altitude FROM igc_data
                               WHERE flight_id IN ({ids}) ORDER BY flight_id, sequence""", conn)
    fx = fx.dropna(subset=['time', 'latitude', 'longitude', 'altitude'])
    fx = fx[fx.time.str.len() >= 8]
    fx['t'] = parse_times(fx.time)
    fx['x'], fx['y'] = C.to_xy(fx.latitude.to_numpy(), fx.longitude.to_numpy())
    groups = {fid: g for fid, g in fx.groupby('flight_id', sort=False)}
    del fx
    tracks, failed = [], []
    for f in fl.itertuples(index=False):
        row = {'flight_id': int(f.id), 'datum': f.datum, 'registration': f.registratie,
               'launch_method': f.start_methode, 'glider_type': f.type, 'pilot': f.gezagvoerder_naam,
               'second_pilot': f.tweede_inzittende_naam,
               'status': 'ok', 'n_events': 0, 'n_possible': 0, 'n_unverified': 0, 'n_exempt': 0,
               'airborne_s': 0.0, 'qnh_hpa': None}
        g = groups.get(f.id)
        if g is None:
            failed.append({**row, 'status': 'no_track'})
            continue
        t, x, y, alt = clean_track(g.t.values, g.x.values, g.y.values, g.altitude.values.astype(float))
        if len(t) < 10:
            failed.append({**row, 'status': 'too_few_fixes'})
            continue
        iv, status = select_interval(airborne_intervals(t, x, y, alt), t,
                                     C.local_to_utc_seconds(f.datum, f.start_tijd))
        if iv is None:
            failed.append({**row, 'status': status})
            continue
        a, b = iv['i_takeoff'], iv['i_landing']
        row['status'] = status
        tracks.append((row, t[a:b + 1], x[a:b + 1], y[a:b + 1], alt[a:b + 1]))
    return tracks, failed


def check_tracks(tracks, failed, checked, sectors, settings, qnh_table, part_time_pattern, count_off_days,
                 intermittent_pattern, count_intermittent, clearance_pattern=schedule.CLEARANCE_DEFAULT):
    checks, events = list(failed), []
    count_key = {'violation': 'n_events', 'possible': 'n_possible', 'unverified': 'n_unverified',
                 'exempt': 'n_exempt'}
    for row, t, x, y, alt in tracks:
        row = dict(row)
        row['airborne_s'] = float(t[-1] - t[0])
        hour = int(np.clip(round(t[0] / 3600.0), 0, 23))
        qnh = qnh_table.get((row['datum'], hour), OA.STD_HPA)
        row['qnh_hpa'] = qnh
        for e in check_track(t, x, y, alt, checked, sectors, settings, qnh,
                             schedule.utc_offset_hours(row['datum']), schedule.is_off_day(row['datum'])):
            i0, i1 = e.pop('_i0'), e.pop('_i1')
            sel = np.r_[np.arange(i0, i1, PATH_STEP), i1]
            la, lo = C.to_latlon(x[sel], y[sel])
            e['path'] = [[round(float(p), 6), round(float(q), 6)] for p, q in zip(la, lo)]
            e.update({k: row[k] for k in ('flight_id', 'datum', 'registration', 'launch_method', 'glider_type',
                                          'pilot', 'second_pilot')})
            ts = C.utc_timestamp(row['datum'], e['start_s'])
            e['start_utc'] = ts.strftime('%H:%M:%S')
            e['start_local'] = ts.astimezone(C.TZ_LOCAL).strftime('%H:%M:%S')
            e['entry_lat'], e['entry_lon'] = e['path'][0]
            e['part_time'] = schedule.is_part_time(e['airspace'], part_time_pattern)
            e['intermittent'] = schedule.is_intermittent(e['airspace'], intermittent_pattern)
            e['off_day'] = schedule.is_off_day(row['datum'])
            e['category'] = schedule.category(e['airspace'], row['datum'], e['exempt_by'], part_time_pattern,
                                              count_off_days, intermittent_pattern, count_intermittent,
                                              timed=e['time_rule'] is not None, cls=e['class'],
                                              local_hour=int(e['start_local'][:2]),
                                              clearance_pattern=clearance_pattern)
            events.append(e)
            row[count_key[e['category']]] += 1
        checks.append(row)
    ev = pd.DataFrame(events)
    if len(ev):
        ev = ev.rename(columns={'class': 'cls'})
    return pd.DataFrame(checks), ev


DB_EVENT_COLS = ['flight_id', 'datum', 'registration', 'launch_method', 'glider_type', 'pilot', 'second_pilot',
                 'airspace', 'cls', 'lower_text',
                 'upper_text', 'lower_m', 'upper_m', 'notam_area', 'part_time', 'intermittent', 'off_day', 'category',
                 'time_rule', 'start_utc',
                 'start_local', 'duration_s', 'n_fixes', 'max_alt_m', 'max_above_lower_m', 'min_below_upper_m',
                 'max_inside_m', 'entry_lat', 'entry_lon', 'qnh_hpa', 'exempt_by']
DB_CHECK_COLS = ['flight_id', 'datum', 'registration', 'launch_method', 'pilot', 'status', 'n_events',
                 'n_possible', 'n_unverified', 'n_exempt', 'airborne_s', 'qnh_hpa']


def _ensure_columns(conn, table, cols):
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for c in cols:
        if c not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {c}")


def save_db(conn, checks, events, settings, version):
    conn.execute("CREATE TABLE IF NOT EXISTS airspace_checks (flight_id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE IF NOT EXISTS airspace_violations (flight_id INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS airspace_meta (key TEXT PRIMARY KEY, value TEXT)")
    _ensure_columns(conn, 'airspace_checks', DB_CHECK_COLS + ['settings', 'checked_at'])
    _ensure_columns(conn, 'airspace_violations', DB_EVENT_COLS + ['settings'])
    conn.execute("CREATE INDEX IF NOT EXISTS idx_asv_flight ON airspace_violations(flight_id)")
    ids = ','.join(str(int(i)) for i in checks.flight_id)
    conn.execute(f"DELETE FROM airspace_checks WHERE flight_id IN ({ids})")
    conn.execute(f"DELETE FROM airspace_violations WHERE flight_id IN ({ids})")
    sj = json.dumps(settings.__dict__, default=list)
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    checks.assign(settings=sj, checked_at=now)[DB_CHECK_COLS + ['settings', 'checked_at']].to_sql(
        'airspace_checks', conn, if_exists='append', index=False)
    if len(events):
        events.assign(settings=sj)[DB_EVENT_COLS + ['settings']].to_sql(
            'airspace_violations', conn, if_exists='append', index=False)
    for k, v in (('settings', sj), ('airspace', json.dumps(version)), ('last_run', now)):
        conn.execute("INSERT OR REPLACE INTO airspace_meta VALUES (?, ?)", (k, v))
    conn.commit()


# ═══════════════════════════ main ═══════════════════════════

def parse_centre(s):
    if s in ('auto', 'ehhv'):
        return s
    lat, lon = (float(v) for v in s.split(','))
    return lat, lon


def main():
    p = argparse.ArgumentParser(description='Find flights that violated airspace',
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument('--min-duration', type=float, default=30, help='seconds continuously inside (default 30)')
    p.add_argument('--vertical-margin', type=parse_length, default=50.0,
                   help='margin above the floor / below the ceiling, e.g. 50m or 150ft (default 50m)')
    p.add_argument('--horizontal-margin', type=parse_length, default=0.0,
                   help='distance inside the boundary, e.g. 100m (default 0)')
    p.add_argument('--radius-km', type=float, default=50.0, help='circle radius (default 50; 0 = everywhere)')
    p.add_argument('--centre', type=parse_centre, default='auto',
                   help="'auto' (default): placed where it covers the most flying time, EHHV inside; "
                        "'ehhv': centred on the field; or 'LAT,LON'")
    p.add_argument('--classes', default='A,B,C,D,P,R',
                   help='airspace classes to check (default A,B,C,D,P,R; others: Q danger, TMZ, RMZ)')
    p.add_argument('--include-notam-areas', action='store_true', help='also check areas tagged NOTAM-only/dates')
    p.add_argument('--intermittent-regex', default=schedule.INTERMITTENT_DEFAULT,
                   help='airspace only active when booked → "unverified" (default: EHTRA, EHTSA, <notam>)')
    p.add_argument('--count-intermittent', action='store_true', help='count those as violations anyway')
    p.add_argument('--clearance-regex', default=schedule.CLEARANCE_DEFAULT,
                   help='control zones a glider may cross with a clearance on request → always "possible" '
                        '(default: CTR Deelen, Dutch Mil); empty string to count them')
    p.add_argument('--part-time-regex', default=schedule.PART_TIME_DEFAULT,
                   help='military weekday airspace → "possible" on weekends/holidays (default: MIL TMAs, mil. CTRs)')
    p.add_argument('--count-part-time-off-days', action='store_true',
                   help='count military airspace on weekends/holidays as violations too')
    p.add_argument('--no-time-rules', action='store_true',
                   help='ignore published time-dependent classes (Lelystad TMA 3-5, Nieuw Milligen TMA B/C2/E)')
    p.add_argument('--no-gliding-sector-exemption', action='store_true',
                   help='count time inside gliding sectors as violations of the controlled airspace around them')
    p.add_argument('--exclude', help='regex of airspace names to ignore')
    p.add_argument('--max-gap', type=float, default=30, help='logging gap (s) that splits an event (default 30)')
    p.add_argument('--year', type=int, help='only this year (same as --from YYYY-01-01 --to YYYY-12-31)')
    p.add_argument('--suffix', help="write a separate edition: report, map, details and CSV named …_<suffix>, event tracks in "
                                    "airspace_events_<suffix>/ (default with --year: the year); leaves the main "
                                    "outputs and the database alone")
    p.add_argument('--from', dest='date_from', help='first date (YYYY-MM-DD)')
    p.add_argument('--to', dest='date_to', help='last date (YYYY-MM-DD)')
    p.add_argument('--flight', type=int, action='append', help='check only this flight id (repeatable)')
    p.add_argument('--include-tugs', action='store_true', help='also check tow planes and motor gliders')
    p.add_argument('--details-only', action='store_true',
                   help='write only the details report, its event tracks and the CSV (no map / overview report)')
    p.add_argument('--anonymise', action='store_true',
                   help='replace pilot names by pilot_1, pilot_2, … (most violations first) in the details report, '
                        'event tracks and CSV')
    p.add_argument('--map', type=int, help='only write the track page of this flight')
    p.add_argument('--event-pages', choices=['selection', 'violation-only', 'violations', 'all', 'none'],
                   default='selection',
                   help="detailed track pages: 'selection' (default: the --event-top longest violations plus the "
                        "(possible) violations of --event-pilot), 'violation-only' (every violation), "
                        "'violations' (+ possible), 'all', or 'none'")
    p.add_argument('--event-top', type=int, default=10, help='longest violations that get a detailed track (default 10)')
    p.add_argument('--event-pilot', action='append', default=[],
                   help='also detailed tracks for the violations and possible violations on flights where this pilot '
                        'was pilot in command (name or part of it, repeatable), e.g. --event-pilot Patrick')
    p.add_argument('--airspace', help='OpenAir file to use instead of the downloaded Dutch file')
    p.add_argument('--update-airspace', action='store_true', help='download the current Dutch airspace file')
    p.add_argument('--no-db', action='store_true', help='do not store results in the database')
    p.add_argument('--db', default=str(C.DB_PATH))
    args = p.parse_args()
    if args.year:
        args.date_from, args.date_to = f"{args.year}-01-01", f"{args.year}-12-31"
        args.suffix = args.suffix or str(args.year)
    edition = bool(args.suffix)
    sfx = f"_{args.suffix}" if edition else ''
    out_csv = C.OUTPUT_DIR / f'airspace_violations{sfx}.csv'
    out_map = C.OUTPUT_DIR / f'airspace_violations_map{sfx}.html'
    out_details = C.OUTPUT_DIR / f'airspace_violations_details{sfx}.html'
    out_report = C.OUTPUT_DIR / f'airspace_violations_report{sfx}.html'
    ev_dir_name = f"{RP.EVENT_DIR}{sfx}"
    if edition:
        args.no_db = True          # an edition never changes the stored results

    if args.update_airspace or (not args.airspace and not AIRSPACE_FILE.exists()):
        print("Downloading airspace...")
        update_airspace()
    asp_path = Path(args.airspace) if args.airspace else AIRSPACE_FILE
    airspaces = OA.load(asp_path)
    version = airspace_version(asp_path)
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA busy_timeout = 20000")
    t0 = time.time()

    # tracks first: the circle is placed from where the flying happens
    tracks, failed = load_tracks(conn, [args.map] if args.map else args.flight, args.date_from, args.date_to,
                                 args.include_tugs, log=(lambda *_: None) if args.map else print)
    if not tracks and not failed:
        print("No flights selected.")
        return 0
    radius = args.radius_km * 1000 if args.radius_km else None
    cov = (np.nan, np.nan)
    if radius is None:
        cx = cy = 0.0
    elif args.centre == 'ehhv':
        cx = cy = 0.0
    elif args.centre == 'auto':
        if args.map or args.flight or args.date_from or args.date_to:
            meta = dict(conn.execute("SELECT key, value FROM airspace_meta").fetchall()) \
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='airspace_meta'").fetchone() else {}
            reg = json.loads(meta.get('region', 'null')) or [0.0, 0.0]
            cx, cy = reg[0], reg[1]            # reuse the circle of the last full run
        else:
            cx, cy, c_auto, c_ehhv = best_circle(tracks, radius)
            cov = (c_auto, c_ehhv)
    else:
        x, y = C.to_xy(*args.centre)
        cx, cy = float(x), float(y)
    region = (float(cx), float(cy), radius)
    if radius is not None and not np.isfinite(cov[0]) and not args.map:
        cov = circle_coverage(tracks, cx, cy, radius)     # coverage of the reused circle for these flights

    s = Settings(min_duration_s=args.min_duration, vertical_margin_m=args.vertical_margin,
                 horizontal_margin_m=args.horizontal_margin,
                 classes=tuple(c.strip().upper() for c in args.classes.split(',') if c.strip()),
                 include_notam_areas=args.include_notam_areas,
                 gliding_sector_exemption=not args.no_gliding_sector_exemption,
                 max_gap_s=args.max_gap, exclude_names=args.exclude, radius_m=radius, centre_x=cx, centre_y=cy,
                 apply_time_rules=not args.no_time_rules)
    checked, sectors = select_airspaces(airspaces, s)
    checked = [a for a in checked if RP.in_region(a, region)]
    sectors = [a for a in sectors if RP.in_region(a, region)]
    names = {a.name for a in checked}
    if not args.map:
        print(f"Airspace: {len(airspaces)} areas from {asp_path}; measuring within a {RP.region_text(region)}"
              + (f" (covers {100 * cov[0]:.1f}% of flying time; around EHHV {100 * cov[1]:.1f}%)" if np.isfinite(cov[0]) else '')
              + f"; {len(checked)} airspaces checked, {len(sectors)} gliding sectors as exemptions")

    checks, events = check_tracks(tracks, failed, checked, sectors, s, load_qnh(conn), args.part_time_regex,
                                  args.count_part_time_off_days, args.intermittent_regex, args.count_intermittent,
                                  args.clearance_regex)
    if events.empty:
        events = pd.DataFrame(columns=DB_EVENT_COLS + ['path', 'start_s', 'entry_x', 'entry_y'])
    events['event_id'] = [RP.event_id(f, u) for f, u in zip(events.flight_id, events.start_utc)]
    if args.anonymise:
        # pilot_1, pilot_2, … numbered by violations (most first), then everyone else; placeholders stay unknown
        placeholder = {'', '-', '--', '?', 'None', 'nan'}
        clean = lambda v: None if pd.isna(v) or str(v).strip() in placeholder else str(v).strip()  # noqa: E731
        for df in (checks, events):
            for col in ('pilot', 'second_pilot'):
                if col in df:
                    df[col] = df[col].map(clean)
        v_ = events[events.category == 'violation']
        rank = (v_.groupby('pilot').agg(f=('flight_id', 'nunique'), e=('flight_id', 'size'))
                .sort_values(['f', 'e'], ascending=False).index.tolist())
        others = [n for n in pd.concat([checks.pilot, checks.second_pilot, events.second_pilot]).dropna().unique()
                  if n not in rank]
        names_map = {n: f"pilot_{k + 1}" for k, n in enumerate(rank + others)}
        for df in (checks, events):
            for col in ('pilot', 'second_pilot'):
                if col in df:
                    df[col] = df[col].map(names_map)

    if args.map:
        out = RP.flight_page(conn, args.map, airspaces, events, region, args.part_time_regex,
                             args.intermittent_regex, names, MAP_DIR / f'flight_{args.map}.html')
        print(f"✓ {out}  ({len(events)} events)")
        return 0

    if not args.no_db:
        save_db(conn, checks, events, s, version)
        conn.execute("INSERT OR REPLACE INTO airspace_meta VALUES ('region', ?)", (json.dumps([cx, cy, radius]),))
        conn.commit()

    C.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    events[[c for c in DB_EVENT_COLS if c in events.columns]].to_csv(out_csv, index=False)

    # a page for every flight with an event (the map and report link to them); an edition only adds/updates
    # its own flights in the shared folder
    if MAP_DIR.exists() and not edition:
        for old in MAP_DIR.glob('flight_*.html'):
            old.unlink()
    fids = events.flight_id.drop_duplicates().tolist()
    for fid in fids:
        RP.flight_page(conn, fid, airspaces, events, region, args.part_time_regex, args.intermittent_regex, names,
                       MAP_DIR / f'flight_{fid}.html')
    label = (str(args.year) if args.year else args.suffix) if edition else ''
    if not args.details_only:
        RP.overview_map(airspaces, events, region, args.part_time_regex, args.intermittent_regex, names, out_map,
                        title=f"Airspace violations {label}" if edition else None)
        RP.write_report(checks, events, s, version, airspaces, checked, args.part_time_regex, args.intermittent_regex,
                        region, cov, out_report, len(fids), title=f"Airspace violations {label} — EHHV flights"
                        if edition else None, map_href=out_map.name)
    # detailed track page per event
    ev_dir = C.OUTPUT_DIR / ev_dir_name
    if ev_dir.exists():
        for old in ev_dir.glob('event_*.html'):
            old.unlink()
    if args.event_pages == 'selection':
        viol = events[events.category == 'violation']
        sel = events.event_id.isin(viol.nlargest(args.event_top, 'duration_s').event_id)
        for name in args.event_pilot:      # that pilot's (possible) violations, as pilot in command
            who = events.pilot.fillna('').str.contains(name, case=False, regex=False)
            sel |= who & events.category.isin(['violation', 'possible'])
        chosen = events[sel]
    else:
        want = {'violation-only': ('violation',), 'violations': ('violation', 'possible'),
                'all': schedule.CATEGORIES, 'none': ()}[args.event_pages]
        chosen = events[events.category.isin(want)]
    event_pages = set()
    for ev in chosen.itertuples():
        if RP.event_page(conn, ev, airspaces, region, args.part_time_regex, args.intermittent_regex, names, s,
                         ev_dir / f'event_{ev.event_id}.html'):
            event_pages.add(ev.event_id)
    RP.write_details(checks, events, s, region, out_details, anonymised=args.anonymise, event_pages=event_pages,
                     event_dir=ev_dir_name,
                     title=(f"Airspace violations {label} — details" + (' (anonymised)' if args.anonymise else ''))
                     if edition else None)
    # every link must point to a page that exists
    linked = set(int(m) for f in ((out_details,) if args.details_only else (out_map, out_report, out_details)) for m in re.findall(r'airspace_maps/flight_(\d+)\.html',
                                                                         f.read_text()))
    missing = [f for f in linked if not (MAP_DIR / f'flight_{f}.html').exists()]
    ev_links = set(re.findall(rf'{ev_dir_name}/event_(\w+)\.html', out_details.read_text()))
    missing += [f"event {e}" for e in ev_links if not (ev_dir / f'event_{e}.html').exists()]

    ok, v = RP.flights_frame(checks, events)
    n_ok, n_v = len(ok), int(ok.violated.sum())
    cnt = events.category.value_counts().to_dict()
    print(f"\nChecked {n_ok} flights in {time.time() - t0:.0f}s "
          f"(not checkable: {int((~checks.status.isin(['ok', 'starts_airborne'])).sum())})")
    print(f"Flights with a violation: {n_v} ({100 * n_v / max(n_ok, 1):.1f}%), violation events: {len(v)}; "
          f"unverified (check NOTAM): {cnt.get('unverified', 0)}; possible: {cnt.get('possible', 0)}; "
          f"exempt: {cnt.get('exempt', 0)}")
    if len(events):
        t = events.assign(airspace=events.airspace.map(RP.short)).groupby(['airspace', 'category']).size() \
            .unstack(fill_value=0)
        print("\nEvents by airspace:")
        print(t.assign(total=t.sum(axis=1)).sort_values('total', ascending=False).head(12).to_string())
        print("\nBy weekday:")
        print(RP.weekday_table(ok, events)[['weekday', 'flights', 'with_violation', 'share']].to_string(index=False))
        print(f"\nPilots with violations: {len(RP.pilot_summary(ok, v))} (see the details report)")
    print(('' if args.details_only else f"\n✓ {out_report}  (overview, no names)")
          + f"\n✓ {out_details}  (every event, {'pilots anonymised' if args.anonymise else 'with pilots'})"
          f"\n✓ {len(event_pages)} detailed event tracks in {ev_dir}"
          + ('' if args.details_only else f"\n✓ {out_map}") + f"\n✓ {len(fids)} flight pages in {MAP_DIR}\n✓ {out_csv}"
          + ('' if args.no_db else '\n✓ tables airspace_checks, airspace_violations')
          + (f"\n✗ links without a page: {missing}" if missing else '\n✓ all flight links resolve'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
