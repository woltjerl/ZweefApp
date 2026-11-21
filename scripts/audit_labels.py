#!/usr/bin/env python3
"""
Label audit for the climb detector (plan section 9.10).

Draws a random, stratified sample of winch launches (half connected, half not),
plots each one (altitude/time with release, search window and climbs; plus the
track) and records whether the label is correct.

Usage:
    python scripts/audit_labels.py                     # interactive: y/n per flight
    python scripts/audit_labels.py --sheets out/dir    # write contact sheets (8 flights each)
    python scripts/audit_labels.py --summary           # agreement from data/label_audit.csv
"""

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import config as C  # noqa: E402
from src.thermal_detection.climb_detector import (clean_track, parse_times)  # noqa: E402

AUDIT_CSV = C.ROOT / 'data' / 'label_audit.csv'


def sample(conn, n, seed):
    L = pd.read_sql_query("""SELECT * FROM launches WHERE launch_type='winch' AND launch_quality='ok'
                             AND track_status='ok'""", conn)
    rng = np.random.default_rng(seed)
    pos = L[L.connected == 1].sample(n // 2, random_state=rng.integers(1e9))
    neg = L[L.connected == 0].sample(n - n // 2, random_state=rng.integers(1e9))
    return pd.concat([pos, neg]).sample(frac=1, random_state=seed).reset_index(drop=True)


def plot_flight(ax_alt, ax_map, conn, launch):
    fid = int(launch.flight_id)
    g = pd.read_sql_query(f"SELECT time, latitude, longitude, altitude FROM igc_data "
                          f"WHERE flight_id={fid} ORDER BY sequence", conn)
    t = parse_times(g.time)
    x, y = C.to_xy(g.latitude.to_numpy(), g.longitude.to_numpy())
    t, x, y, alt = clean_track(t, x, y, g.altitude.to_numpy(float))
    cl = pd.read_sql_query(f"SELECT * FROM climbs WHERE flight_id={fid} ORDER BY seq", conn)
    t_rel = launch.release_utc_s
    sel = (t >= launch.takeoff_utc_s - 30) & (t <= t_rel + 900)
    ground = alt[sel].min()
    tm = (t - t_rel) / 60
    ax_alt.plot(tm[sel], alt[sel] - ground, color='0.3', lw=0.8)
    ax_alt.axvline(0, color='tab:blue', lw=0.8)
    ax_alt.axvspan(0, (launch.search_end_utc_s - t_rel) / 60, color='tab:blue', alpha=0.08)
    ax_map.plot(x[sel], y[sel], color='0.6', lw=0.5)
    ax_map.plot(launch.release_x, launch.release_y, 'b^', ms=4)
    for _, c in cl.iterrows():
        cs = (t >= c.start_utc_s) & (t <= c.start_utc_s + c.duration) & sel
        ax_alt.plot(tm[cs], alt[cs] - ground, color='tab:red', lw=1.4)
        ax_map.plot(x[cs], y[cs], color='tab:red', lw=0.8)
    ax_alt.axhline(C.SEARCH_END_AGL, color='0.7', lw=0.5, ls=':')
    lab = 'CONNECTED' if launch.connected else 'not connected'
    ax_alt.set_title(f"#{fid}  {lab}  rel {launch.release_agl:.0f} m  climbs {len(cl)}", fontsize=7)
    ax_alt.tick_params(labelsize=6)
    ax_alt.set_xlim(-1.5, 15)
    ax_map.set_aspect('equal')
    ax_map.set_xticks([])
    ax_map.set_yticks([])


def sheets(conn, df, out_dir, per_sheet=8):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for s in range(0, len(df), per_sheet):
        chunk = df.iloc[s:s + per_sheet]
        fig, axes = plt.subplots(len(chunk) // 2 + len(chunk) % 2, 4, figsize=(14, 2.4 * ((len(chunk) + 1) // 2)),
                                 gridspec_kw={'width_ratios': [3, 1, 3, 1]}, squeeze=False)
        for k, (_, row) in enumerate(chunk.iterrows()):
            r, cpos = divmod(k, 2)
            plot_flight(axes[r][cpos * 2], axes[r][cpos * 2 + 1], conn, row)
        fig.tight_layout()
        p = out_dir / f'audit_sheet_{s // per_sheet + 1:02d}.png'
        fig.savefig(p, dpi=90)
        plt.close(fig)
        paths.append(p)
    df[['flight_id', 'connected', 'n_climbs']].to_csv(out_dir / 'audit_sample.csv', index=False)
    return paths


def interactive(conn, df):
    import matplotlib.pyplot as plt
    rows = []
    for _, row in df.iterrows():
        fig, (a, m) = plt.subplots(1, 2, figsize=(11, 4), gridspec_kw={'width_ratios': [3, 1]})
        plot_flight(a, m, conn, row)
        plt.show(block=False)
        ans = input(f"Flight {row.flight_id}: label correct? [y/n/q] ").strip().lower()
        plt.close(fig)
        if ans == 'q':
            break
        note = input("  note (optional): ").strip() if ans == 'n' else ''
        rows.append({'flight_id': row.flight_id, 'label_connected': row.connected,
                     'correct': int(ans == 'y'), 'note': note})
    pd.DataFrame(rows).to_csv(AUDIT_CSV, index=False)
    print(f"Saved {len(rows)} judgements to {AUDIT_CSV}")


def summary():
    a = pd.read_csv(AUDIT_CSV)
    print(f"Audited flights: {len(a)}  agreement: {a.correct.mean():.1%}")
    print(a.groupby('label_connected').correct.agg(['mean', 'size']))
    for _, r in a[a.correct == 0].iterrows():
        print(f"  #{r.flight_id}: {r.note}")


def main():
    p = argparse.ArgumentParser(description='Audit climb-detector labels')
    p.add_argument('--n', type=int, default=100)
    p.add_argument('--seed', type=int, default=1)
    p.add_argument('--sheets', help='write contact sheets to this directory instead of asking')
    p.add_argument('--summary', action='store_true')
    p.add_argument('--db', default=str(C.DB_PATH))
    args = p.parse_args()
    if args.summary:
        summary()
        return 0
    conn = sqlite3.connect(args.db)
    df = sample(conn, args.n, args.seed)
    if args.sheets:
        for pth in sheets(conn, df, args.sheets):
            print(pth)
    else:
        interactive(conn, df)
    return 0


if __name__ == '__main__':
    sys.exit(main())
