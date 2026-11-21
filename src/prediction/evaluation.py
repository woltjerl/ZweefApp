"""
Evaluation core (plan section 15): leave-one-day-out maps, capture curves,
concordance within visited cells, direction tests (Mantel–Haenszel risk ratio),
bootstrap over days, permutation tests within strata, and power simulation.

Why three "where" tests:
- capture (plan 15.3.1): share of first climbs inside the map's top-k% of reachable area.
  Mostly measures where pilots fly; reported against the traffic/density baselines.
- concordance within visited cells: among the cells a pilot actually searched, was the
  thermal found in the cell the map ranked highest? Conditioning on the path removes
  traffic and the "connected flights stop searching" effect. Null = 0.5.
- direction test (replaces plan 15.3.2): did launches whose initial direction (bearing at
  tau seconds, before almost anyone connects) was the map's best direction connect more,
  on the same day and hour? Launches that connected before tau+10 s are excluded so the
  exposure cannot depend on the outcome.
"""

import numpy as np
import pandas as pd

from src import config as C
from src.prediction import layer_b as B

BASELINES = ('traffic', 'climbs_all', 'climbs_wind', 'climbs_runway')
CONC_MAPS = ('hazard', 'expected_hits', 'climbs_wind', 'climbs_runway', 'traffic')
DIR_METHODS = ('oe', 'hazard')


def _pct_rank(score, rng):
    """Percentile rank (0 = best) of each element; random tie-break."""
    s = np.nan_to_num(np.asarray(score, float), nan=-np.inf) + rng.random(len(score)) * 1e-9
    order = np.argsort(-s)
    pct = np.empty(len(s))
    pct[order] = (np.arange(len(s)) + 0.5) / len(s)
    return pct


def _rank_of(scores, k, rng):
    """Rank (0 = best) of element k among finite scores; NaN scores rank last."""
    s = np.where(np.isfinite(scores), scores, -np.inf) + rng.random(len(scores)) * 1e-9
    return int((s > s[k]).sum())


def lodo(P: B.Prepared, params: dict, days=None, baselines=True, seed=0, taus=B.DIRECTION_TAUS,
         pool_days=None):
    """
    Leave-one-day-out evaluation. One row per launch on the evaluated days.

    Columns:
      cap_<map>      percentile of the first-climb source cell among in-reach cells (NaN: no hit)
      conc_<map>     percentile of the hit cell among the cells this launch searched
      dir<tau>_<m>   rank (0 = best of 8) of the launch's initial direction under method m
                     (NaN when no direction or the launch connected before tau + 10 s)

    pool_days: if given, models are trained only on these days (minus the held-out day),
               so a final test set can be kept out of tuning entirely.
    """
    rng = np.random.default_rng(seed)
    T = P.T
    g = P.grid
    sig = params['smooth_sigma_m']
    day = T.datum.to_numpy()
    days = np.unique(day) if days is None else np.asarray(days)
    low = P.climbs_low
    rows = []
    in_pool = np.ones(len(T), bool) if pool_days is None else np.isin(day, list(pool_days))
    outside = set() if pool_days is None else set(P.A_day) - set(pool_days)
    low_pool = low if pool_days is None or not len(low) else low[low.datum.isin(list(pool_days))]

    for d in days:
        idx = np.where(day == d)[0]
        if len(idx) == 0:
            continue
        train = (day != d) & in_pool
        base = B.base_map(P, train, params, exclude_day=outside | {d})
        lift_cache, bl_cache = {}, {}
        lt = low_pool[(low_pool.datum != d) & (low_pool.cell >= 0)] if len(low_pool) else low_pool
        traffic_all = B.traffic_map(P, train, sig)
        climbs_all = g.smooth(np.bincount(lt.cell, minlength=g.N).astype(float), sig) if len(lt) else None

        for i in idx:
            r = T.iloc[i]
            tgt = B.target_of(r)
            key = (round(tgt['dir'] / 10), round(tgt['spd'] / 10), int(tgt['hour'] // 3), tgt['season'])
            if key not in lift_cache:
                lift_cache[key] = B.conditioned_lift(P, train, base, tgt, params)[0]
            lift = lift_cache[key]
            bkey = (r.runway, round(tgt['dir'] / 10))
            if bkey not in bl_cache:
                ent = {'expected_hits': B.expected_hits_map(P, train, r.runway, tgt['dir'], 1.0, sig)}
                if baselines and climbs_all is not None:
                    sw = lt[np.asarray(C.angdiff(lt.wd100.fillna(-999), tgt['dir'])) <= 45]
                    ent['climbs_wind'] = g.smooth(np.bincount(sw.cell, minlength=g.N).astype(float), sig)
                    sr = lt[(lt.launch_type == 'winch') & (lt.is_first == 1) & (lt.runway == r.runway)]
                    ent['climbs_runway'] = g.smooth(np.bincount(sr.cell, minlength=g.N).astype(float), sig)
                bl_cache[bkey] = ent
            ent = bl_cache[bkey]
            maps = {'hazard': lift, 'expected_hits': ent['expected_hits'] * lift}
            if baselines and climbs_all is not None:
                maps.update({'traffic': traffic_all, 'climbs_all': climbs_all,
                             'climbs_wind': ent['climbs_wind'], 'climbs_runway': ent['climbs_runway']})

            out = {'i': i, 'flight_id': r.flight_id, 'datum': d, 'connected': int(r.connected), 'pA': r.pA,
                   'hour_utc': r.hour_utc, 'runway': r.runway, 'pilot_id': r.pilot_id}
            reach_cells = np.where(P.R[i])[0]
            hit = np.where(P.H[i])[0]
            visited = np.where(P.S[i])[0]
            out['n_reach'] = len(reach_cells)

            # capture
            if len(reach_cells) and len(hit):
                pos = {c: k for k, c in enumerate(reach_cells)}
                for name, mp in maps.items():
                    if hit[0] in pos:
                        out[f'cap_{name}'] = _pct_rank(mp[reach_cells], rng)[pos[hit[0]]]
                    else:
                        out[f'cap_{name}'] = 1.0
            # concordance within visited cells
            if len(hit) and len(visited) >= 3:
                k = int(np.where(visited == hit[0])[0][0])
                for name, mp in maps.items():
                    out[f'conc_{name}'] = _pct_rank(mp[visited], rng)[k]
                out['n_visited'] = len(visited)
            # initial direction
            if len(reach_cells) and isinstance(r.runway, str):
                hz = B.direction_scores(P, (r.release_x, r.release_y), reach_cells, lift, 'mean')
                for tau in taus:
                    b = r[f'b{tau}']
                    early = r.connected == 1 and (r.ttc if np.isfinite(r.ttc) else 1e9) <= tau + 10
                    if not np.isfinite(b) or early:
                        continue
                    s = int(B.sector_of(b))
                    oe, _ = B.direction_oe(T, train, r.runway, tgt['dir'], tau)
                    out[f'dir{tau}_oe'] = _rank_of(oe, s, rng)
                    out[f'dir{tau}_hazard'] = _rank_of(hz, s, rng)
            rows.append(out)
    return pd.DataFrame(rows)


# ═══════════════════════════ statistics ═══════════════════════════

def capture(res: pd.DataFrame, name: str, k: float) -> float:
    col = f'cap_{name}'
    v = res[col].dropna() if col in res else pd.Series(dtype=float)
    return float((v < k).mean()) if len(v) else np.nan


def concordance(res: pd.DataFrame, name: str) -> float:
    col = f'conc_{name}'
    v = res[col].dropna() if col in res else pd.Series(dtype=float)
    return float(v.mean()) if len(v) else np.nan


def strata_of(res: pd.DataFrame):
    return res.datum.astype(str) + '_' + (res.hour_utc // 2).astype(int).astype(str)


def mh_rr(y, x, strata):
    """
    Mantel–Haenszel risk ratio of outcome y (0/1) for exposure x (0/1) across strata,
    with Greenland–Robins variance of log RR. Returns (rr, se_log).
    """
    st = StratTable(strata)
    return st.rr(y, x)


def _mh_from_counts(a, c, n1, n0, N):
    ok = (n1 > 0) & (n0 > 0)
    a, c, n1, n0, N = a[ok], c[ok], n1[ok], n0[ok], N[ok]
    R = np.sum(a * n0 / N)
    S = np.sum(c * n1 / N)
    if R <= 0 or S <= 0:
        return np.nan, np.nan
    var = np.sum((n1 * n0 * (a + c) - a * c * N) / N ** 2) / (R * S)
    return R / S, float(np.sqrt(max(var, 0)))


class StratTable:
    """Fast repeated MH computations for permutation / simulation."""

    def __init__(self, strata):
        codes, _ = pd.factorize(pd.Series(np.asarray(strata)))
        self.order = np.argsort(codes, kind='stable')
        self.codes = codes[self.order]
        self.starts = np.r_[0, np.flatnonzero(np.diff(self.codes)) + 1]
        self.N = np.diff(np.r_[self.starts, len(self.codes)]).astype(float)

    def rr(self, y, x):
        y, x = np.asarray(y, float)[self.order], np.asarray(x, float)[self.order]
        a = np.add.reduceat(y * x, self.starts)
        n1 = np.add.reduceat(x, self.starts)
        cy = np.add.reduceat(y, self.starts)
        return _mh_from_counts(a, cy - a, n1, self.N - n1, self.N)

    def permute_within(self, x, rng):
        x = np.asarray(x)[self.order]
        keys = rng.random(len(x))
        perm = np.lexsort((keys, self.codes))
        out = np.empty_like(x)
        out[self.order] = x[perm]
        return out


def permutation_p(y, x, strata, n_perm=5000, seed=0):
    """One-sided p-value for RR > 1, permuting exposure within strata."""
    rng = np.random.default_rng(seed)
    st = StratTable(strata)
    obs, _ = st.rr(y, x)
    if not np.isfinite(obs):
        return np.nan
    ge = 0
    for _ in range(n_perm):
        r, _ = st.rr(y, st.permute_within(x, rng))
        if np.isfinite(r) and r >= obs:
            ge += 1
    return (ge + 1) / (n_perm + 1)


def concordance_p(pcts, n_perm=None):
    """One-sided p-value that mean percentile < 0.5 (null: uniform). Normal approximation."""
    v = np.asarray(pcts, float)
    v = v[np.isfinite(v)]
    if len(v) < 5:
        return np.nan
    from scipy.stats import norm
    z = (v.mean() - 0.5) / (np.sqrt(1 / 12) / np.sqrt(len(v)))
    return float(norm.cdf(z))


def day_weights(n_days, n_boot=2000, seed=0):
    """Bootstrap multiplicities of days (n_boot × n_days)."""
    rng = np.random.default_rng(seed)
    return rng.multinomial(n_days, np.full(n_days, 1.0 / n_days), size=n_boot).astype(float)


def boot_ratio(num_by_day, den_by_day, W):
    """Bootstrap distribution of Σnum/Σden with day weights W."""
    den = W @ np.asarray(den_by_day, float)
    with np.errstate(invalid='ignore', divide='ignore'):
        return (W @ np.asarray(num_by_day, float)) / den


def ci(dist):
    d = np.asarray(dist, float)
    d = d[np.isfinite(d)]
    return (float(np.percentile(d, 2.5)), float(np.percentile(d, 97.5))) if len(d) else (np.nan, np.nan)


def mh_by_day(res: pd.DataFrame, x, days_index):
    """Per-day MH numerator/denominator sums (for fast day bootstrap of the MH RR)."""
    D = res.assign(x=np.asarray(x, int), s=strata_of(res))
    g = D.groupby(['datum', 's'])
    a = g.apply(lambda t: int(((t.x == 1) & (t.connected == 1)).sum()), include_groups=False)
    c = g.apply(lambda t: int(((t.x == 0) & (t.connected == 1)).sum()), include_groups=False)
    n1 = g.x.sum()
    N = g.size()
    n0 = N - n1
    ok = (n1 > 0) & (n0 > 0)
    Rn = (a * n0 / N)[ok].groupby(level=0).sum()
    Sn = (c * n1 / N)[ok].groupby(level=0).sum()
    return Rn.reindex(days_index, fill_value=0).to_numpy(), Sn.reindex(days_index, fill_value=0).to_numpy()


def bootstrap_days(res: pd.DataFrame, stat_fn, n_boot=2000, seed=0):
    """Percentile 95% interval of stat_fn(resampled frame), resampling days with replacement."""
    rng = np.random.default_rng(seed)
    days = res.datum.unique()
    groups = {d: g for d, g in res.groupby('datum')}
    vals = []
    for _ in range(n_boot):
        pick = rng.choice(days, len(days), replace=True)
        parts = []
        for k, d in enumerate(pick):
            gd = groups[d]
            parts.append(gd.assign(datum=f"{d}#{k}"))
        v = stat_fn(pd.concat(parts, ignore_index=True))
        if np.isfinite(v):
            vals.append(v)
    if not vals:
        return (np.nan, np.nan)
    return (float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5)))


def minimum_detectable_rr(pA, x, strata, rrs=(1.1, 1.2, 1.3, 1.4, 1.5, 1.75, 2.0, 2.5, 3.0),
                          n_sim=400, seed=0):
    """
    Smallest true risk ratio detected with ≥80% power (one-sided α=0.025 Wald test on the
    MH log RR) given this exposure pattern and the day-quality probabilities pA.
    """
    rng = np.random.default_rng(seed)
    st = StratTable(strata)
    pA = np.asarray(pA, float)
    x = np.asarray(x, float)
    power = {}
    for r in rrs:
        scale = 1.0 / (1 + x.mean() * (r - 1))     # keep the overall rate
        p = np.clip(pA * scale * np.where(x == 1, r, 1.0), 0, 0.99)
        hits = 0
        for _ in range(n_sim):
            y = rng.random(len(p)) < p
            rr, se = st.rr(y, x)
            if np.isfinite(rr) and se > 0 and np.log(rr) / se > 1.96:
                hits += 1
        power[r] = hits / n_sim
    mde = next((r for r in rrs if power[r] >= 0.8), None)
    return mde, power


def direction_test(res: pd.DataFrame, tau: int, method: str, top: int = 0):
    """MH RR for 'initial direction ranked ≤ top' vs the rest, plus permutation p."""
    col = f'dir{tau}_{method}'
    D = res[res[col].notna()] if col in res else res.iloc[0:0]
    if len(D) == 0:
        return {}
    x = (D[col] <= top).astype(int).to_numpy()
    st = strata_of(D)
    rr, se = mh_rr(D.connected.to_numpy(), x, st)
    return {'tau': tau, 'method': method, 'top': top + 1, 'n': int(len(D)), 'exposed': int(x.sum()),
            'rr': rr, 'lo': float(np.exp(np.log(rr) - 1.96 * se)) if np.isfinite(rr) else np.nan,
            'hi': float(np.exp(np.log(rr) + 1.96 * se)) if np.isfinite(rr) else np.nan, 'se_log': se}
