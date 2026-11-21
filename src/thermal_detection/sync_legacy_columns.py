#!/usr/bin/env python3
"""
Write the climb detector's results into the older label columns, so every script that
still reads them (legacy maps, old ML model, plots) uses the corrected labels.

flights (winch launches):
    flight_outcome              'thermal_found' / 'landed_back' (launch_quality ok), else 'unknown'
    release_lat/lon/alt         release point; release_alt is now height above the field (AGL)
    thermal_entry_lat/lon/alt   start of the first real circling climb (alt = AGL)
    release_to_thermal_*        bearing, distance and time from release to that climb
    flight_duration_after_release, max_altitude (AGL)
flights (all other launch methods): the old columns are cleared — the old extractor applied
    winch logic to them, which produced meaningless labels.
igc_data.first_thermal: 1 for the fixes of the first real climb of connected winch launches,
    up to 600 m AGL (the old meaning: lift reachable from a winch launch); 0 elsewhere.

Before the first overwrite the old values are copied to tables
legacy_labels_backup / legacy_first_thermal_backup (kept, never overwritten).

Usage:
    python -m src.thermal_detection.sync_legacy_columns
    python -m src.thermal_detection.sync_legacy_columns --restore     # put the old labels back
"""

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import config as C  # noqa: E402

COLS = ['flight_outcome', 'release_lat', 'release_lon', 'release_alt', 'thermal_entry_lat',
        'thermal_entry_lon', 'thermal_entry_alt', 'release_to_thermal_bearing',
        'release_to_thermal_distance_m', 'release_to_thermal_seconds', 'flight_duration_after_release',
        'max_altitude', 'thermal_processed', 'release_thermal_processed']
REACHABLE_AGL = 600.0


def _exists(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def backup(conn, verbose=True):
    if not _exists(conn, 'legacy_labels_backup'):
        conn.execute(f"CREATE TABLE legacy_labels_backup AS SELECT id, {', '.join(COLS)} FROM flights")
        if verbose:
            print("  backed up old flight labels → legacy_labels_backup")
    if not _exists(conn, 'legacy_first_thermal_backup'):
        conn.execute("CREATE TABLE legacy_first_thermal_backup AS SELECT id FROM igc_data WHERE first_thermal = 1")
        if verbose:
            n = conn.execute("SELECT COUNT(*) FROM legacy_first_thermal_backup").fetchone()[0]
            print(f"  backed up {n} old first_thermal fixes → legacy_first_thermal_backup")
    conn.commit()


def _seconds(time_str: pd.Series) -> np.ndarray:
    s = time_str.str
    return (s[0:2].astype(int) * 3600 + s[3:5].astype(int) * 60 + s[6:8].astype(int)).to_numpy(float)


def sync(conn, verbose=True):
    backup(conn, verbose)
    L = pd.read_sql_query("SELECT * FROM launches", conn)
    fc = pd.read_sql_query("SELECT * FROM climbs WHERE is_first = 1", conn).set_index('flight_id')
    W = L[L.launch_type == 'winch'].copy()
    W = W.join(fc[['x0', 'y0', 'agl0', 'start_utc_s', 'duration', 'rate', 'agl1']], on='flight_id')

    rows = []
    for r in W.itertuples():
        has_release = pd.notna(r.release_x)
        ok = r.launch_quality == 'ok' and r.track_status in ('ok',)
        outcome = ('thermal_found' if r.connected == 1 else 'landed_back') if ok else 'unknown'
        rel_lat = rel_lon = ent_lat = ent_lon = None
        if has_release:
            la, lo = C.to_latlon(r.release_x, r.release_y)
            rel_lat, rel_lon = float(la), float(lo)
        bearing = dist = secs = ent_alt = None
        if ok and r.connected == 1 and pd.notna(r.x0):
            la, lo = C.to_latlon(r.x0, r.y0)
            ent_lat, ent_lon, ent_alt = float(la), float(lo), float(r.agl0)
            bearing = float(C.bearing_deg(r.x0 - r.release_x, r.y0 - r.release_y))
            dist = float(np.hypot(r.x0 - r.release_x, r.y0 - r.release_y))
            secs = int(round(r.time_to_first_climb_s))
        rows.append((outcome, rel_lat, rel_lon, None if not has_release else float(r.release_agl),
                     ent_lat, ent_lon, ent_alt, bearing, dist, secs,
                     None if pd.isna(r.dur_after_release_s) else int(r.dur_after_release_s),
                     None if pd.isna(r.max_agl) else float(r.max_agl), int(r.flight_id)))

    sets = ', '.join(f"{c} = ?" for c in COLS[:-2])
    others = L[L.launch_type != 'winch'].flight_id.astype(int).tolist()
    with conn:
        conn.execute(f"UPDATE flights SET {', '.join(f'{c} = NULL' for c in COLS[:-2])} "
                     f"WHERE start_methode IS NULL OR start_methode != 'lier'")
        conn.executemany(f"UPDATE flights SET {sets}, thermal_processed = 1, release_thermal_processed = 1 "
                         f"WHERE id = ?", rows)
        if others:
            conn.execute(f"UPDATE flights SET thermal_processed = 1, release_thermal_processed = 1 "
                         f"WHERE id IN ({','.join(map(str, others))})")

    # igc_data.first_thermal: fixes of the first real climb, up to 600 m AGL
    good = W[(W.launch_quality == 'ok') & (W.connected == 1) & W.start_utc_s.notna()]
    n_fix = 0
    with conn:
        conn.execute("UPDATE igc_data SET first_thermal = 0 WHERE first_thermal = 1")
        if len(good):
            ids = ','.join(str(int(i)) for i in good.flight_id)
            fx = pd.read_sql_query(f"SELECT id, flight_id, time FROM igc_data WHERE flight_id IN ({ids})", conn)
            fx = fx[fx.time.str.len() >= 8]
            fx['t'] = _seconds(fx.time)
            g = good.set_index('flight_id')
            t0 = fx.flight_id.map(g.start_utc_s).to_numpy()
            # time at which the climb passes 600 m AGL (linear), capped at the end of the climb
            t_cap = (REACHABLE_AGL - fx.flight_id.map(g.agl0)) / fx.flight_id.map(g.rate).clip(lower=0.1)
            t1 = t0 + np.minimum(fx.flight_id.map(g.duration).to_numpy(), t_cap.to_numpy())
            # logger time is HH:MM:SS; climb times may run past midnight in rare cases
            t = fx.t.to_numpy()
            t = np.where(t + 86400 <= t1, t + 86400, t)
            sel = fx.id[(t >= t0) & (t <= t1)].astype(int).tolist()
            n_fix = len(sel)
            for k in range(0, len(sel), 50000):
                chunk = sel[k:k + 50000]
                conn.execute(f"UPDATE igc_data SET first_thermal = 1 WHERE id IN ({','.join(map(str, chunk))})")
    if verbose:
        out = W.assign(o=[r[0] for r in rows]).o.value_counts().to_dict()
        print(f"  winch flights updated: {len(rows)} {out}")
        print(f"  non-winch flights cleared: {len(others)} (plus non-glider flights)")
        print(f"  igc_data.first_thermal = 1 for {n_fix} fixes of {len(good)} first climbs")
    return len(rows), n_fix


def restore(conn, verbose=True):
    if not _exists(conn, 'legacy_labels_backup'):
        print("No backup found; nothing to restore.")
        return
    sets = ', '.join(f"{c} = (SELECT b.{c} FROM legacy_labels_backup b WHERE b.id = flights.id)" for c in COLS)
    with conn:
        conn.execute(f"UPDATE flights SET {sets} WHERE id IN (SELECT id FROM legacy_labels_backup)")
        conn.execute("UPDATE igc_data SET first_thermal = 0 WHERE first_thermal = 1")
        conn.execute("UPDATE igc_data SET first_thermal = 1 WHERE id IN (SELECT id FROM legacy_first_thermal_backup)")
    if verbose:
        print("✓ old labels restored from legacy_labels_backup / legacy_first_thermal_backup")


def main():
    p = argparse.ArgumentParser(description='Write climb-detector results into the old label columns')
    p.add_argument('--db', '--db-path', dest='db', default=str(C.DB_PATH))
    p.add_argument('--restore', action='store_true', help='put the old labels back from the backup tables')
    args = p.parse_args()
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA busy_timeout = 20000")
    if args.restore:
        restore(conn)
    else:
        print("Syncing old label columns with the climb detector...")
        sync(conn)
        print("✓ done")
    return 0


if __name__ == '__main__':
    sys.exit(main())
