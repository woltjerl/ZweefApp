#!/usr/bin/env python3
"""
Reach model (plan section 12): which thermal sources can be searched from the
winch release with enough height left to fly the circuit.

Usage:
    python -m src.prediction.reach      # validate against historical first climbs
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import config as C  # noqa: E402


def height_loss(ax, ay, bx, by, wx, wy, V, E):
    """
    Height lost gliding from A to B through moving air (wind vector wx, wy in m/s),
    at glide speed V (m/s) and effective glide ratio E. np.inf where unreachable.
    """
    dx, dy = np.asarray(bx, float) - ax, np.asarray(by, float) - ay
    d = np.hypot(dx, dy)
    with np.errstate(invalid='ignore', divide='ignore'):
        ux, uy = np.where(d > 0, dx / d, 0.0), np.where(d > 0, dy / d, 0.0)
        wt = wx * ux + wy * uy
        wc2 = np.maximum(wx * wx + wy * wy - wt * wt, 0.0)
        under = V * V - wc2
        vg = np.sqrt(np.maximum(under, 0.0)) + wt
        loss = np.where(d > 0, d / vg * V / E, 0.0)
    return np.where((under <= 0) | (vg <= 2.0), np.inf, loss)


def reach(release_xy, h_rel, wind_xy, glider='single', circuit_xy=None, sources_xy=None,
          min_work=None, circuit_entry=None, glide=None, lean=True):
    """
    For each thermal source on the ground, where to look for it (it leans downwind)
    and whether it can be searched from the release with height to spare.

    lean=False: the points are already where the thermal is found at search height
    (maps built without drift correction), so they are not shifted downwind.

    Returns dict with arrays: look_x, look_y, h_look, h_return, in_reach.
    """
    min_work = C.MIN_WORK_AGL if min_work is None else min_work
    circuit_entry = C.CIRCUIT_ENTRY_AGL if circuit_entry is None else circuit_entry
    E = (glide or C.GLIDE)[glider]
    V = C.V_GLIDE_MS[glider]
    rx, ry = release_xy
    wx, wy = wind_xy
    sx, sy = np.asarray(sources_xy, float).T
    cpts = np.atleast_2d(np.asarray(circuit_xy if circuit_xy is not None else (0.0, 0.0), float))

    h = np.full(len(sx), float(h_rel))
    for _ in range(2 if lean else 1):
        hp = np.maximum(h, 0.0) if lean else np.zeros_like(h)
        px, py = sx + wx * hp / C.W_AIR_TYP, sy + wy * hp / C.W_AIR_TYP
        h = h_rel - height_loss(rx, ry, px, py, wx, wy, V, E)
    ret = np.min([height_loss(px, py, cx, cy, wx, wy, V, E) for cx, cy in cpts], axis=0)
    h_ret = h - ret - C.SEARCH_TURN_LOSS_M
    ok = (h >= min_work) & (h_ret >= circuit_entry)
    return {'look_x': px, 'look_y': py, 'h_look': h, 'h_return': h_ret, 'in_reach': ok}


def circuit_entry_points(conn, k=8) -> dict:
    """
    Per runway: typical places where non-connected launches pass circuit-entry height
    (k-means centres of those positions). Pilots join the circuit at different points,
    so the return check uses the cheapest of these.
    """
    from sklearn.cluster import KMeans
    q = f"""
        SELECT l.runway, s.flight_id, s.t_utc_s, s.x, s.y, s.agl
        FROM search_tracks s JOIN launches l ON l.flight_id = s.flight_id
        WHERE l.launch_type='winch' AND l.launch_quality='ok' AND l.connected=0
          AND l.runway IS NOT NULL
        ORDER BY s.flight_id, s.t_utc_s"""
    s = pd.read_sql_query(q, conn)
    below = s[s.agl < C.CIRCUIT_ENTRY_AGL].groupby('flight_id').first()
    out = {}
    for rw, g in list(below.groupby('runway')) + [('all', below)]:
        xy = g[['x', 'y']].to_numpy()
        n = min(k, len(xy))
        km = KMeans(n_clusters=n, n_init=5, random_state=0).fit(xy)
        out[rw] = km.cluster_centers_.round(1).tolist()
    return out


def validate(conn, verbose=True):
    """
    Coverage: share of historical first climbs whose position lies inside the reach
    computed for their own flight (wind, release height, glider class).
    Also measures the effective glide ratio pilots achieve from release to first climb.
    """
    from src.prediction.dataset import attach_wind, winch_launches
    L = winch_launches(conn, with_features=False)
    L = attach_wind(L, conn)
    fc = pd.read_sql_query("SELECT flight_id, x0, y0, agl0 FROM climbs WHERE is_first=1", conn).set_index('flight_id')
    W = L[L.connected == 1].join(fc, on='flight_id')
    W = W.dropna(subset=['x0'])
    ce = circuit_entry_points(conn)

    cover, h_model = [], []
    for r in W.itertuples():
        cxy = ce.get(r.runway, ce['all'])
        # the climb position itself is where the glider was: evaluate height there directly
        E, V = C.GLIDE[r.glider_class], C.V_GLIDE_MS[r.glider_class]
        h = r.release_agl - height_loss(r.release_x, r.release_y, r.x0, r.y0, r.wx, r.wy, V, E)
        ret = min(height_loss(r.x0, r.y0, cx, cy, r.wx, r.wy, V, E) for cx, cy in cxy)
        h_ret = h - ret - C.SEARCH_TURN_LOSS_M
        cover.append(bool(h >= C.MIN_WORK_AGL and h_ret >= C.CIRCUIT_ENTRY_AGL))
        h_model.append(float(h))
    W = W.assign(covered=cover, h_model=h_model)
    dist = np.hypot(W.x0 - W.release_x, W.y0 - W.release_y)
    lost = W.release_agl - W.agl0
    direct = (dist > 300) & (lost > 5)
    e_eff = (dist[direct] / lost[direct])
    stats = {
        'n_first_climbs': int(len(W)),
        'coverage': float(np.mean(cover)),
        'agl0_p5': float(W.agl0.quantile(0.05)),
        'agl0_p10': float(W.agl0.quantile(0.10)),
        'effective_glide_median': float(e_eff.median()),
        'effective_glide_p75': float(e_eff.quantile(0.75)),
        'circuit_entry_points': ce,
    }
    if verbose:
        print(f"First climbs: {stats['n_first_climbs']}; inside own reach: {stats['coverage']:.1%}")
        print(f"Start height of first climbs: p5 {stats['agl0_p5']:.0f} m, p10 {stats['agl0_p10']:.0f} m AGL")
        print(f"Effective glide release→first climb (direct legs): median {stats['effective_glide_median']:.1f}, "
              f"p75 {stats['effective_glide_p75']:.1f}")
        for k, v in ce.items():
            print(f"  circuit entry points {k}: " + ' '.join(f"({x:+.0f},{y:+.0f})" for x, y in v))
    return stats, W


def main():
    p = argparse.ArgumentParser(description='Validate the reach model')
    p.add_argument('--db', default=str(C.DB_PATH))
    args = p.parse_args()
    from src.prediction.dataset import connect
    validate(connect(args.db))
    return 0


if __name__ == '__main__':
    sys.exit(main())
