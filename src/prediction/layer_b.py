#!/usr/bin/env python3
"""
Layer B — where? Conversion rate per thermal ground source (plan section 13).

For every winch launch we know which ground-source cells its search tested
(search-track fixes shifted upwind to their source) and in which cell its first
climb originated (the hit). A cell's lift = observed hits / expected hits, where
"expected" uses the day's quality from layer A (pA). Lift > 1: searching here
converted more often than searching elsewhere on equally good days.

The maps are conditioned on wind direction, wind speed, time of day and season,
with shrinkage so thin data falls back to the cell's overall value, and an optional
prior from all other low climbs (aerotow, later climbs).

Usage:
    python -m src.prediction.layer_b          # build and save the prediction artefact
"""

import argparse
import pickle
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.ndimage import gaussian_filter
from scipy.stats import gamma

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import config as C  # noqa: E402
from src.prediction.dataset import attach_wind, connect, winch_launches  # noqa: E402
from src.prediction.reach import circuit_entry_points, reach  # noqa: E402

MODEL_PATH = C.MODEL_DIR / 'layer_b.pkl'
DIRECTION_TAUS = (20, 30, 45)   # seconds after release at which the initial direction is read


# ═══════════════════════════ grid ═══════════════════════════

class Grid:
    def __init__(self, cell_m=None, radius=None):
        self.cell = float(cell_m or C.CELL_M)
        self.radius = float(radius or C.REACH_RADIUS_M)
        self.n = int(np.ceil(2 * self.radius / self.cell))
        self.c = (np.arange(self.n) + 0.5) * self.cell - self.radius
        XX, YY = np.meshgrid(self.c, self.c, indexing='ij')     # [ix, iy]
        self.xy = np.column_stack([XX.ravel(), YY.ravel()])
        self.N = len(self.xy)
        self.disc = np.hypot(self.xy[:, 0], self.xy[:, 1]) <= self.radius

    def index(self, x, y):
        ix = np.floor((np.asarray(x) + self.radius) / self.cell).astype(int)
        iy = np.floor((np.asarray(y) + self.radius) / self.cell).astype(int)
        ok = (ix >= 0) & (ix < self.n) & (iy >= 0) & (iy < self.n)
        return np.where(ok, ix * self.n + iy, -1)

    def nearest(self, x, y):
        return self.index(x, y)

    def smooth(self, v, sigma_m):
        if not sigma_m:
            return np.asarray(v, float)
        g = np.asarray(v, float).reshape(self.n, self.n)
        return gaussian_filter(g, sigma_m / self.cell, mode='constant').ravel()

    def within(self, px, py, r):
        """Boolean (cells,) of cells whose centre is within r of any point."""
        out = np.zeros(self.N, bool)
        if len(px) == 0:
            return out
        k = int(np.ceil(r / self.cell)) + 1
        ix = np.floor((np.asarray(px) + self.radius) / self.cell).astype(int)
        iy = np.floor((np.asarray(py) + self.radius) / self.cell).astype(int)
        for dx in range(-k, k + 1):
            for dy in range(-k, k + 1):
                jx, jy = ix + dx, iy + dy
                ok = (jx >= 0) & (jx < self.n) & (jy >= 0) & (jy < self.n)
                if not ok.any():
                    continue
                cx, cy = self.c[jx[ok]], self.c[jy[ok]]
                close = np.hypot(cx - np.asarray(px)[ok], cy - np.asarray(py)[ok]) <= r
                out[(jx[ok] * self.n + jy[ok])[close]] = True
        return out


# ═══════════════════════════ prepared data ═══════════════════════════

@dataclass
class Prepared:
    grid: Grid
    T: pd.DataFrame                 # winch launches with pA, wind, conditions
    S: np.ndarray                   # (launch, cell) searched during the whole search
    S60: np.ndarray                 # (launch, cell) searched in the decision window
    H: np.ndarray                   # (launch, cell) first-climb hit
    R: np.ndarray                   # (launch, cell) in reach for that launch
    A_day: dict                     # day -> (cell,) counts of other low climbs (prior)
    A_all: np.ndarray
    climbs_low: pd.DataFrame        # all low climbs (for baselines)
    circuit: dict
    params: dict = field(default_factory=dict)


def _sources(climbs: pd.DataFrame, use_drift: bool):
    if use_drift:
        return climbs.source_x.to_numpy(), climbs.source_y.to_numpy()
    return climbs.xc0.to_numpy(), climbs.yc0.to_numpy()


def prepare(conn, cell_m=None, match_radius=None, use_drift=None, verbose=True) -> Prepared:
    cell_m = cell_m or C.CELL_M
    match_radius = match_radius or C.MATCH_RADIUS_M
    use_drift = C.USE_DRIFT if use_drift is None else use_drift
    grid = Grid(cell_m)

    T = winch_launches(conn)
    pa = pd.read_sql_query("SELECT flight_id, pA FROM launches", conn).set_index('flight_id').pA
    T['pA'] = pa.reindex(T.flight_id).to_numpy()
    if T.pA.isna().any():
        raise RuntimeError("launches.pA missing: run `python -m src.prediction.layer_a` first")
    T = attach_wind(T, conn)
    T['season'] = [C.season_of(m) for m in T.month]
    T = T.reset_index(drop=True)

    cl = pd.read_sql_query("""SELECT c.*, l.launch_type, l.datum FROM climbs c
                              JOIN launches l ON l.flight_id = c.flight_id""", conn)
    first = cl[(cl.is_first == 1)].set_index('flight_id')
    sx, sy = _sources(first, use_drift)
    first = first.assign(sx=sx, sy=sy)

    st = pd.read_sql_query("SELECT * FROM search_tracks ORDER BY flight_id, t_utc_s", conn)
    st_groups = {fid: g for fid, g in st.groupby('flight_id', sort=False)}
    circuit = circuit_entry_points(conn)

    # initial direction: bearing from release to the position at tau seconds after release
    ttc = pd.read_sql_query("SELECT flight_id, time_to_first_climb_s FROM launches", conn) \
        .set_index('flight_id').time_to_first_climb_s
    T['ttc'] = T.flight_id.map(ttc)
    st2 = st.merge(T[['flight_id', 'release_utc_s', 'release_x', 'release_y']], on='flight_id')
    st2['dt'] = st2.t_utc_s - st2.release_utc_s
    for tau in DIRECTION_TAUS:
        pos = st2[st2.dt <= tau].groupby('flight_id').last()
        px, py = T.flight_id.map(pos.x), T.flight_id.map(pos.y)
        far = np.hypot(px - T.release_x, py - T.release_y) > 100
        T[f'b{tau}'] = np.where(far, C.bearing_deg(px - T.release_x, py - T.release_y), np.nan)

    n = len(T)
    S = np.zeros((n, grid.N), bool)
    S60 = np.zeros((n, grid.N), bool)
    H = np.zeros((n, grid.N), bool)
    R = np.zeros((n, grid.N), bool)
    for i, r in enumerate(T.itertuples()):
        g = st_groups.get(r.flight_id)
        if g is not None:
            g = g[g.agl >= C.MIN_WORK_AGL]
            px, py = g.x.to_numpy(), g.y.to_numpy()
            if use_drift:
                px = px - r.wx * g.agl.to_numpy() / C.W_AIR_TYP
                py = py - r.wy * g.agl.to_numpy() / C.W_AIR_TYP
            S[i] = grid.within(px, py, match_radius)
            early = (g.t_utc_s.to_numpy() - r.release_utc_s) <= C.DECISION_WINDOW_S
            S60[i] = grid.within(px[early], py[early], match_radius)
        if r.connected and r.flight_id in first.index:
            f = first.loc[r.flight_id]
            c = grid.nearest(f.sx, f.sy)
            if c >= 0:
                H[i, c] = True
                S[i, c] = True
                if f.s_since_release <= C.DECISION_WINDOW_S:
                    S60[i, c] = True
        res = reach((r.release_x, r.release_y), r.release_agl, (r.wx, r.wy), r.glider_class,
                    circuit.get(r.runway, circuit['all']), grid.xy, lean=use_drift)
        R[i] = res['in_reach'] & grid.disc

    # prior evidence: every other low climb (aerotow, self-launch, later winch climbs)
    other = cl[~((cl.launch_type == 'winch') & (cl.is_first == 1)) & (cl.agl0 < 600)]
    ox, oy = _sources(other, use_drift)
    oc = grid.index(ox, oy)
    other = other.assign(cell=oc)
    A_day = {}
    for d, g in other[other.cell >= 0].groupby('datum'):
        A_day[d] = np.bincount(g.cell, minlength=grid.N).astype(float)
    A_all = np.sum(list(A_day.values()), axis=0) if A_day else np.zeros(grid.N)

    low = cl[cl.agl0 < 500].copy()
    lx, ly = _sources(low, use_drift)
    low['cell'] = grid.index(lx, ly)
    low = low.merge(T[['flight_id', 'runway']], on='flight_id', how='left')
    wd = pd.read_sql_query("SELECT date, hour_utc, wd100 FROM weather_conv", conn).set_index(['date', 'hour_utc'])
    hh = np.clip(np.round(low.start_utc_s / 3600).astype(int), 0, 23)
    low['wd100'] = wd.wd100.reindex(list(zip(low.datum, hh))).to_numpy()

    if verbose:
        print(f"Prepared {n} launches × {grid.N} cells ({grid.cell:.0f} m), match {match_radius:.0f} m, "
              f"drift {'on' if use_drift else 'off'}; hits {int(H.sum())}, "
              f"mean cells searched {S.sum(1).mean():.1f}, in reach {R.sum(1).mean():.0f}")
    return Prepared(grid, T, S, S60, H, R, A_day, A_all, low, circuit,
                    {'cell_m': cell_m, 'match_radius': match_radius, 'use_drift': use_drift})


# ═══════════════════════════ conditions ═══════════════════════════

def target_of(row) -> dict:
    """Conditions of a launch (or a forecast row with the same feature names)."""
    month = int(row['month']) if 'month' in row else int(pd.Timestamp(row['ts']).month)
    return {'dir': float(row['w100_dir']), 'spd': float(row['w100_spd']),
            'hour': float(row['hour_utc']), 'season': C.season_of(month)}


def _band(v, edges):
    return int(np.searchsorted(edges, v, side='right'))


def cond_masks(T: pd.DataFrame, target: dict, dims=None) -> dict:
    dims = C.COND_DIMS if dims is None else dims
    m = {}
    if 'wind_sector' in dims:
        m['wind_sector'] = np.asarray(C.angdiff(T.w100_dir, target['dir'])) <= 45
    if 'wind_speed' in dims:
        e = [10, 20]
        m['wind_speed'] = np.array([_band(v, e) for v in T.w100_spd]) == _band(target['spd'], e)
    if 'time_band' in dims:
        e = [11, 14]
        m['time_band'] = np.array([_band(v, e) for v in T.hour_utc]) == _band(target['hour'], e)
    if 'season' in dims:
        m['season'] = (T.season == target['season']).to_numpy()
    return m


# ═══════════════════════════ fitting ═══════════════════════════

def default_params(**over):
    p = {'smooth_sigma_m': C.SMOOTH_SIGMA_M, 'k_cell': C.K_CELL, 'm_cond': C.M_COND,
         'lambda_aerotow': C.LAMBDA_AEROTOW, 'dims': tuple(C.COND_DIMS)}
    p.update(over)
    return p


def base_map(P: Prepared, train: np.ndarray, params: dict, exclude_day=None):
    """
    Unconditioned lift per cell from the training launches.
    exclude_day: a day, or a collection of days, whose other low climbs are left out of the prior.
    """
    g = P.grid
    pa = P.T.pA.to_numpy()
    w = train.astype(float)
    O = w @ P.H
    Pexp = (w * pa) @ P.S
    q = O.sum() / max(Pexp.sum(), 1e-9)
    E = q * Pexp
    Os, Es = g.smooth(O, params['smooth_sigma_m']), g.smooth(E, params['smooth_sigma_m'])
    if exclude_day is None:
        A = P.A_all
    else:
        excl = [exclude_day] if isinstance(exclude_day, str) else list(exclude_day)
        A = P.A_all - sum((P.A_day.get(d, 0) for d in excl), np.zeros(g.N))
    As = g.smooth(A, max(params['smooth_sigma_m'], g.cell))
    mean_a = As[g.disc].mean() if As[g.disc].sum() > 0 else 1.0
    prior = np.clip(1 + params['lambda_aerotow'] * (As / max(mean_a, 1e-9) - 1), 0.5, 2.0)
    K = params['k_cell']
    lift = (Os + K * prior) / (Es + K)
    return {'O': O, 'E': E, 'Os': Os, 'Es': Es, 'q': q, 'prior': prior, 'lift': lift,
            'lo': gamma.ppf(0.05, Os + K * prior, scale=1 / (Es + K)),
            'hi': gamma.ppf(0.95, Os + K * prior, scale=1 / (Es + K))}


def conditioned_lift(P: Prepared, train: np.ndarray, base: dict, target: dict, params: dict):
    """Lift for today's conditions: base lift adjusted per condition dimension."""
    g = P.grid
    pa = P.T.pA.to_numpy()
    lift_c = base['lift']
    out = lift_c.copy()
    n_similar = {}
    for dim, mask in cond_masks(P.T, target, params['dims']).items():
        w = (train & mask).astype(float)
        O = w @ P.H
        Pexp = (w * pa) @ P.S
        if O.sum() < 5:
            continue
        qk = O.sum() / max(Pexp.sum(), 1e-9)
        Os = g.smooth(O, params['smooth_sigma_m'])
        Es = g.smooth(qk * Pexp, params['smooth_sigma_m'])
        M = params['m_cond']
        lift_k = (Os + M * lift_c) / (Es + M)
        out = out * (lift_k / lift_c)
        n_similar[dim] = int(w.sum())
    return np.clip(out, 0.2, 5.0), n_similar


def fit(P: Prepared, train: np.ndarray, target: dict, params: dict, exclude_day=None):
    base = base_map(P, train, params, exclude_day)
    lift, n_similar = conditioned_lift(P, train, base, target, params)
    return {**base, 'lift_today': lift, 'n_similar': n_similar}


def probability(fitted: dict, pA_today: float):
    """Chance that searching a cell today ends in a climb from it."""
    return np.minimum(0.9, pA_today * fitted['q'] * fitted['lift_today'])


# ═══════════════════════════ derived maps ═══════════════════════════

def similar_mask(T, train, runway, wdir):
    m = train & (T.runway.to_numpy() == runway)
    return m & (np.asarray(C.angdiff(T.w100_dir, wdir)) <= 45)


def traffic_map(P: Prepared, mask, sigma_m):
    """Where pilots search (launch visits per cell) among the masked launches."""
    return P.grid.smooth(mask.astype(float) @ P.S, sigma_m)


def expected_hits_map(P: Prepared, train, runway, wdir, lift, sigma_m):
    """
    Where first thermals are most likely to be found today if pilots search as they
    usually do from this runway in this wind: search traffic × per-visit lift.
    """
    m = similar_mask(P.T, train, runway, wdir)
    if m.sum() < 30:
        m = train & (P.T.runway.to_numpy() == runway)
    return traffic_map(P, m, sigma_m) * lift


def sector_of(bearing):
    return (((np.asarray(bearing, float) + 22.5) // 45) % 8).astype(int)


def direction_scores(P: Prepared, release_xy, reach_cells, cell_score, how='mean'):
    """Score per 8 compass sectors (from the release) from a per-cell map over reachable cells."""
    xy = P.grid.xy[reach_cells]
    sec = sector_of(C.bearing_deg(xy[:, 0] - release_xy[0], xy[:, 1] - release_xy[1]))
    out = np.full(8, np.nan)
    for s in range(8):
        v = cell_score[reach_cells][sec == s]
        if len(v):
            out[s] = v.mean() if how == 'mean' else v.sum()
    return out


def direction_oe(T, train, runway, wdir, tau, k=5.0, exclude_early=True):
    """
    Empirical value of each initial direction: observed / expected connections of launches
    (same runway, wind ±45°) that headed that way at tau seconds after release.
    Returns (oe[8], n[8]).
    """
    b = T[f'b{tau}'].to_numpy()
    m = similar_mask(T, train, runway, wdir) & np.isfinite(b)
    if exclude_early:
        early = (T.connected.to_numpy() == 1) & (T.ttc.fillna(1e9).to_numpy() <= tau + 10)
        m &= ~early
    sec = sector_of(b[m])
    y = T.connected.to_numpy()[m]
    pa = T.pA.to_numpy()[m]
    O = np.bincount(sec, weights=y, minlength=8)
    Ex = np.bincount(sec, weights=pa, minlength=8)
    q = O.sum() / max(Ex.sum(), 1e-9)
    return (O + k) / (q * Ex + k), np.bincount(sec, minlength=8)


# ═══════════════════════════ persistence ═══════════════════════════

def save(P: Prepared, params: dict, path=MODEL_PATH):
    C.MODEL_DIR.mkdir(parents=True, exist_ok=True)
    keep_cols = ['flight_id', 'datum', 'runway', 'pA', 'connected', 'w100_dir', 'w100_spd', 'hour_utc',
                 'season', 'month', 'pilot_id', 'glider_class', 'release_x', 'release_y', 'release_agl',
                 'wx', 'wy', 'release_utc_s', 'ttc'] + [f'b{t}' for t in DIRECTION_TAUS]
    data = {'cell_m': P.grid.cell, 'radius': P.grid.radius, 'T': P.T[keep_cols],
            'S': np.packbits(P.S, axis=1), 'S60': np.packbits(P.S60, axis=1),
            'H': np.packbits(P.H, axis=1), 'A_day': P.A_day, 'A_all': P.A_all,
            'circuit': P.circuit, 'prep_params': P.params, 'params': params, 'N': P.grid.N,
            'built': pd.Timestamp.now().isoformat()}
    with open(path, 'wb') as f:
        pickle.dump(data, f)


def load(path=MODEL_PATH):
    with open(path, 'rb') as f:
        d = pickle.load(f)
    grid = Grid(d['cell_m'], d['radius'])
    unpack = lambda a: np.unpackbits(a, axis=1, count=d['N']).astype(bool)  # noqa: E731
    P = Prepared(grid, d['T'], unpack(d['S']), unpack(d['S60']), unpack(d['H']),
                 np.zeros((0, grid.N), bool), d['A_day'], d['A_all'], pd.DataFrame(), d['circuit'],
                 d['prep_params'])
    return P, d['params']


def main():
    p = argparse.ArgumentParser(description='Build the layer B artefact')
    p.add_argument('--db', default=str(C.DB_PATH))
    args = p.parse_args()
    conn = connect(args.db)
    tuned = C.MODEL_DIR / 'tuned_params.json'
    params, prep = default_params(), {}
    if tuned.exists():
        import json
        t = json.loads(tuned.read_text())
        params = default_params(**t.get('fit', {}))
        prep = t.get('prepare', {})
        print(f"Using tuned parameters from {tuned}")
    P = prepare(conn, **prep)
    save(P, params)
    print(f"✓ saved {MODEL_PATH}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
