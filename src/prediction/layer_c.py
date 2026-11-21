#!/usr/bin/env python3
"""
Layer C — what do the aces do? (plan section 14)

1. Pilot skill adjusted for day quality and glider: skill = (connects + K) / (Σ pA + K),
   where pA (layer A, out-of-fold) already includes weather, time, glider class and dual.
2. Permutation test (pilot labels shuffled within each day) for whether the spread in
   skill is larger than chance.
3. Early routes: the first ROUTE_S seconds after release (decided before almost anyone
   has connected), rotated so the runway heading points up, clustered per runway.
4. Place or skill: do non-aces who fly the aces' preferred route connect more often
   than non-aces on other routes, on the same day and hour?

Pilot ids are used internally only; no names are stored or shown.

Usage:
    python -m src.prediction.layer_c
"""

import argparse
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import config as C  # noqa: E402
from src.prediction import evaluation as E  # noqa: E402
from src.prediction.dataset import connect, winch_launches  # noqa: E402

MODEL_PATH = C.MODEL_DIR / 'layer_c.pkl'
ROUTE_S = 30.0
ROUTE_STEP_S = 5.0


def load_launches(conn):
    L = winch_launches(conn, with_features=False)
    pa = pd.read_sql_query("SELECT flight_id, pA FROM launches", conn).set_index('flight_id').pA
    L['pA'] = pa.reindex(L.flight_id).to_numpy()
    return L[L.pA.notna()].reset_index(drop=True)


def pilot_skill(L, K=C.SKILL_K, min_launches=C.ACE_MIN_LAUNCHES):
    g = L[L.pilot_id.notna()].groupby('pilot_id').agg(n=('connected', 'size'), O=('connected', 'sum'),
                                                      E=('pA', 'sum'))
    g['skill'] = (g.O + K) / (g.E + K)
    g['eligible'] = g.n >= min_launches
    return g


def aces(skill: pd.DataFrame, top=C.ACE_TOP_FRACTION):
    el = skill[skill.eligible]
    thr = el.skill.quantile(1 - top)
    return set(el.index[el.skill >= thr]), float(thr)


def skill_permutation(L, n_perm=2000, seed=0, K=C.SKILL_K, min_launches=C.ACE_MIN_LAUNCHES):
    """
    Is the spread in adjusted skill larger than chance? Shuffles pilot ids among the
    launches of the same day (keeps each day's conditions), statistic = SD of log skill
    over pilots with ≥ min_launches launches.
    """
    rng = np.random.default_rng(seed)
    D = L[L.pilot_id.notna()].reset_index(drop=True)
    pid_codes, pids = pd.factorize(D.pilot_id)
    counts = np.bincount(pid_codes)
    elig = counts >= min_launches
    y, pa = D.connected.to_numpy(float), D.pA.to_numpy(float)
    day_codes = pd.factorize(D.datum)[0]

    def stat(codes):
        O = np.bincount(codes, weights=y, minlength=len(pids))
        Ex = np.bincount(codes, weights=pa, minlength=len(pids))
        s = np.log((O + K) / (Ex + K))
        return s[elig].std()

    obs = stat(pid_codes)
    order = np.argsort(day_codes, kind='stable')
    starts = np.r_[0, np.flatnonzero(np.diff(day_codes[order])) + 1]
    ends = np.r_[starts[1:], len(order)]
    null = []
    for _ in range(n_perm):
        perm = pid_codes.copy()
        for a, b in zip(starts, ends):
            ix = order[a:b]
            perm[ix] = pid_codes[ix][rng.permutation(b - a)]
        null.append(stat(perm))
    null = np.array(null)
    return {'observed_sd_log_skill': float(obs), 'null_mean': float(null.mean()),
            'null_p95': float(np.percentile(null, 95)), 'p_value': float((np.sum(null >= obs) + 1) / (n_perm + 1)),
            'n_pilots': int(elig.sum())}


def early_routes(conn, L):
    """Positions at 5, 10, … ROUTE_S s after release, relative to release, rotated runway-up."""
    st = pd.read_sql_query("SELECT * FROM search_tracks ORDER BY flight_id, t_utc_s", conn)
    st = st.merge(L[['flight_id', 'release_utc_s', 'release_x', 'release_y', 'runway']], on='flight_id')
    st['dt'] = st.t_utc_s - st.release_utc_s
    times = np.arange(ROUTE_STEP_S, ROUTE_S + 0.1, ROUTE_STEP_S)
    out = {}
    for fid, g in st.groupby('flight_id', sort=False):
        if g.dt.max() < 0.6 * ROUTE_S:
            continue          # climbed (or landed) very early: no route decision to describe
        x = np.interp(times, g.dt, g.x) - g.release_x.iloc[0]
        y = np.interp(times, g.dt, g.y) - g.release_y.iloc[0]
        h = np.radians(C.RUNWAY_HEADING.get(g.runway.iloc[0], 0.0))
        # rotate so the runway heading points up (+y)
        xr = x * np.cos(h) - y * np.sin(h)
        yr = x * np.sin(h) + y * np.cos(h)
        out[fid] = np.concatenate([xr, yr])
    return out, times


def cluster_routes(L, routes, ace_ids, kmin=3, kmax=5, seed=0):
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score
    result = {}
    L = L[L.flight_id.isin(routes.keys()) & L.runway.notna()].copy()
    for rw, g in L.groupby('runway'):
        if len(g) < 60:
            continue
        X = np.stack([routes[f] for f in g.flight_id])
        best = None
        for k in range(kmin, kmax + 1):
            km = KMeans(n_clusters=k, n_init=5, random_state=seed).fit(X)
            s = silhouette_score(X, km.labels_, sample_size=min(2000, len(X)), random_state=seed)
            if best is None or s > best[0]:
                best = (s, km)
        km = best[1]
        g = g.assign(cluster=km.labels_, ace=g.pilot_id.isin(ace_ids))
        result[rw] = {'centres': km.cluster_centers_, 'silhouette': float(best[0]),
                      'members': g[['flight_id', 'cluster', 'ace', 'connected', 'pA', 'datum', 'hour_utc']]}
    return result


def unrotate(route_vec, runway, release_xy):
    n = len(route_vec) // 2
    xr, yr = route_vec[:n], route_vec[n:]
    h = np.radians(C.RUNWAY_HEADING.get(runway, 0.0))
    x = xr * np.cos(h) + yr * np.sin(h)
    y = -xr * np.sin(h) + yr * np.cos(h)
    return np.concatenate([[release_xy[0]], x + release_xy[0]]), np.concatenate([[release_xy[1]], y + release_xy[1]])


def place_vs_skill(clusters):
    """Per runway: observed/expected for aces and non-aces per route, and the stratified test."""
    rows, tests = [], {}
    for rw, c in clusters.items():
        m = c['members']
        ace_pref = m[m.ace].cluster.value_counts(normalize=True)
        pref = int(ace_pref.index[0]) if len(ace_pref) else None
        for k, g in m.groupby('cluster'):
            a, na = g[g.ace], g[~g.ace]
            rows.append({'runway': rw, 'cluster': int(k), 'n': len(g), 'ace_share_of_route': float(g.ace.mean()),
                         'aces_on_route_share': float(ace_pref.get(k, 0.0)),
                         'oe_aces': float(a.connected.sum() / a.pA.sum()) if len(a) and a.pA.sum() > 0 else np.nan,
                         'oe_non_aces': float(na.connected.sum() / na.pA.sum()) if na.pA.sum() > 0 else np.nan,
                         'n_aces': len(a), 'n_non_aces': len(na), 'ace_preferred': k == pref})
        na = m[~m.ace]
        if pref is not None and na.cluster.nunique() > 1:
            x = (na.cluster == pref).astype(int)
            rr, se = E.mh_rr(na.connected, x, E.strata_of(na))
            tests[rw] = {'ace_route': pref, 'rr_non_aces': rr, 'se_log': se, 'n': len(na),
                         'exposed': int(x.sum())}
    return pd.DataFrame(rows), tests


def pooled_place_test(clusters):
    """All runways together: non-aces on the ace-preferred route vs on other routes."""
    parts = []
    for rw, c in clusters.items():
        m = c['members']
        pref = m[m.ace].cluster.value_counts().index
        if len(pref) == 0:
            continue
        na = m[~m.ace].assign(x=lambda t: (t.cluster == pref[0]).astype(int))
        parts.append(na)
    if not parts:
        return {}
    D = pd.concat(parts)
    rr, se = E.mh_rr(D.connected, D.x, E.strata_of(D))
    p = E.permutation_p(D.connected.to_numpy(), D.x.to_numpy(), E.strata_of(D), n_perm=2000)
    return {'rr': rr, 'lo': float(np.exp(np.log(rr) - 1.96 * se)), 'hi': float(np.exp(np.log(rr) + 1.96 * se)),
            'p_value': p, 'n': len(D), 'exposed': int(D.x.sum())}


def train(conn, verbose=True):
    L = load_launches(conn)
    sk = pilot_skill(L)
    ace_ids, thr = aces(sk)
    perm = skill_permutation(L)
    routes, times = early_routes(conn, L)
    clusters = cluster_routes(L, routes, ace_ids)
    table, tests = place_vs_skill(clusters)
    pooled = pooled_place_test(clusters)
    L2 = L.assign(ace=L.pilot_id.isin(ace_ids))
    el = sk[sk.eligible]
    summary = {
        'n_pilots_eligible': int(len(el)), 'n_aces': len(ace_ids), 'ace_threshold_skill': thr,
        'rate_aces': float(L2[L2.ace].connected.mean()), 'rate_others': float(L2[~L2.ace].connected.mean()),
        'oe_aces': float(L2[L2.ace].connected.sum() / L2[L2.ace].pA.sum()),
        'oe_others': float(L2[~L2.ace].connected.sum() / L2[~L2.ace].pA.sum()),
        'skill_quantiles': el.skill.quantile([0.1, 0.25, 0.5, 0.75, 0.9]).round(2).to_dict(),
        'permutation': perm, 'pooled_place_test': pooled,
    }
    bundle = {'summary': summary, 'route_table': table, 'route_tests': tests, 'times': times,
              'route_centres': {rw: c['centres'] for rw, c in clusters.items()},
              'route_members': {rw: c['members'] for rw, c in clusters.items()},
              'ace_route': {rw: t['ace_route'] for rw, t in tests.items()},
              'trained': pd.Timestamp.now().isoformat()}
    C.MODEL_DIR.mkdir(parents=True, exist_ok=True)
    with open(MODEL_PATH, 'wb') as f:
        pickle.dump(bundle, f)
    if verbose:
        print(f"Pilots with ≥{C.ACE_MIN_LAUNCHES} launches: {len(el)}; aces: {len(ace_ids)}")
        print(f"Adjusted skill quantiles: {summary['skill_quantiles']}")
        print(f"Observed/expected: aces {summary['oe_aces']:.2f}, others {summary['oe_others']:.2f}")
        print(f"Skill spread vs chance: SD(log skill) {perm['observed_sd_log_skill']:.3f} vs null "
              f"{perm['null_mean']:.3f} (p95 {perm['null_p95']:.3f}), p = {perm['p_value']:.4f}")
        if pooled:
            print(f"Non-aces on the aces' preferred route: RR {pooled['rr']:.2f} [{pooled['lo']:.2f}-{pooled['hi']:.2f}], "
                  f"permutation p = {pooled['p_value']:.3f} (n {pooled['n']}, on route {pooled['exposed']})")
    return bundle


def load():
    with open(MODEL_PATH, 'rb') as f:
        return pickle.load(f)


def main():
    p = argparse.ArgumentParser(description='Layer C: pilot skill and ace routes')
    p.add_argument('--db', default=str(C.DB_PATH))
    args = p.parse_args()
    train(connect(args.db))
    print(f"✓ saved {MODEL_PATH}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
