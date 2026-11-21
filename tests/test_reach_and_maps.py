import numpy as np
import pandas as pd

from src import config as C
from src.prediction import evaluation as E
from src.prediction import layer_b as B
from src.prediction import routes as RT
from src.prediction.reach import height_loss, reach


# ── reach (plan 12) ──

def test_height_loss_still_air():
    loss = height_loss(0, 0, 1000, 0, 0, 0, V=25, E=25)
    assert abs(loss - 40) < 1e-6


def test_headwind_costs_more_than_tailwind():
    up = height_loss(0, 0, 0, 1000, 0, -5, 25, 25)     # air moves south, flying north = headwind
    down = height_loss(0, 0, 0, -1000, 0, -5, 25, 25)
    assert up > 40 > down


def test_wind_stronger_than_glider_is_unreachable():
    assert np.isinf(height_loss(0, 0, 0, 1000, 0, -30, 25, 25))


def _ring(r, n=36):
    a = np.linspace(0, 2 * np.pi, n, endpoint=False)
    return np.column_stack([r * np.sin(a), r * np.cos(a)])


def test_no_wind_reach_is_symmetric():
    src = _ring(1500)
    res = reach((0, 0), 300, (0, 0), 'single', [(0, 0)], src, min_work=150, circuit_entry=100)
    assert np.allclose(res['h_look'], res['h_look'][0])
    assert res['in_reach'].all() or not res['in_reach'].any()


def test_thermals_lean_downwind():
    src = np.array([[0, 2500], [0, -2500]])           # north (upwind) and south (downwind) sources
    res = reach((0, 0), 300, (0, -6), 'single', [(0, 0)], src, min_work=150, circuit_entry=50)
    # wind blows to the south: both thermals are found south of their source
    assert res['look_y'][0] < 2500 and res['look_y'][1] < -2500
    # the upwind source's thermal leans towards the release, the downwind one away from it
    assert abs(res['look_y'][0]) < abs(res['look_y'][1])


def test_beyond_glide_never_in_reach():
    far = np.array([[0, 300 * 30 + 500]])
    res = reach((0, 0), 300, (0, 0), 'single', [(0, 0)], far, min_work=0, circuit_entry=0)
    assert not res['in_reach'][0]


# ── layer B maths (plan 13) ──

def _prepared(n_launch=200, seed=0, hot_cell=None):
    rng = np.random.default_rng(seed)
    g = B.Grid(cell_m=500, radius=1500)            # 6×6 grid
    S = np.zeros((n_launch, g.N), bool)
    H = np.zeros((n_launch, g.N), bool)
    pA = np.full(n_launch, 0.2)
    for i in range(n_launch):
        cells = rng.choice(g.N, 5, replace=False)
        S[i, cells] = True
        p = 0.04 * np.ones(5)
        if hot_cell is not None:
            p[cells == hot_cell] = 0.3
        hit = rng.random(5) < p
        if hit.any():
            H[i, cells[np.argmax(hit)]] = True
    T = pd.DataFrame({'datum': [f'2025-06-{1 + i % 20:02d}' for i in range(n_launch)], 'pA': pA,
                      'connected': H.any(1).astype(int), 'w100_dir': 270.0, 'w100_spd': 15.0,
                      'hour_utc': 12.0, 'season': 'summer', 'runway': '25'})
    return B.Prepared(g, T, S, S, H, S, {}, np.zeros(g.N), pd.DataFrame(), {}), g


def test_expected_equals_observed_in_total():
    P, g = _prepared()
    base = B.base_map(P, np.ones(len(P.T), bool), B.default_params(smooth_sigma_m=0, lambda_aerotow=0))
    assert abs(base['E'].sum() - base['O'].sum()) < 1e-6


def test_hot_cell_gets_highest_lift():
    P, g = _prepared(n_launch=3000, hot_cell=14)
    base = B.base_map(P, np.ones(len(P.T), bool), B.default_params(smooth_sigma_m=0, lambda_aerotow=0, k_cell=2))
    assert int(np.argmax(base['lift'])) == 14
    assert base['lo'][14] > 1


def test_shrinkage_pulls_thin_cells_to_prior():
    P, g = _prepared(n_launch=40)
    base = B.base_map(P, np.ones(len(P.T), bool), B.default_params(smooth_sigma_m=0, lambda_aerotow=0, k_cell=50))
    assert np.all(np.abs(base['lift'] - 1) < 0.5)


# ── statistics (plan 15) ──

def test_mh_rr_known_value():
    # one stratum: exposed 20/100, unexposed 10/100 → RR 2
    y = np.r_[np.ones(20), np.zeros(80), np.ones(10), np.zeros(90)]
    x = np.r_[np.ones(100), np.zeros(100)]
    rr, se = E.mh_rr(y, x, np.zeros(200))
    assert abs(rr - 2.0) < 1e-9
    # Greenland–Robins SE for a single stratum equals the usual log-RR SE
    assert abs(se - np.sqrt(1 / 20 - 1 / 100 + 1 / 10 - 1 / 100)) < 1e-9


def test_mh_rr_removes_confounding_by_stratum():
    # good day: everybody 40%, mostly exposed; bad day: everybody 5%, mostly unexposed
    y = np.r_[np.r_[np.ones(36), np.zeros(54)], np.r_[np.ones(4), np.zeros(6)],
              np.r_[np.ones(1), np.zeros(9)], np.r_[np.ones(4), np.zeros(86)]]
    x = np.r_[np.ones(90), np.zeros(10), np.ones(10), np.zeros(90)]
    s = np.r_[np.zeros(100), np.ones(100)]
    crude = y[x == 1].mean() / y[x == 0].mean()
    rr, _ = E.mh_rr(y, x, s)
    assert crude > 3
    assert abs(rr - 1.0) < 0.15


def test_permutation_null_is_not_significant():
    rng = np.random.default_rng(1)
    y = rng.random(2000) < 0.15
    x = rng.random(2000) < 0.3
    s = rng.integers(0, 50, 2000)
    assert E.permutation_p(y, x, s, n_perm=300) > 0.05


def test_capture_and_concordance_helpers():
    res = pd.DataFrame({'cap_m': [0.01, 0.08, 0.5, np.nan], 'conc_m': [0.1, 0.3, 0.5, np.nan]})
    assert abs(E.capture(res, 'm', 0.1) - 2 / 3) < 1e-9
    assert abs(E.concordance(res, 'm') - 0.3) < 1e-9


# ── routes (plan 14.4) ──

def test_routes_respect_height_budget():
    g = B.Grid(cell_m=200, radius=3000)
    p = np.zeros(g.N)
    hot = g.index(np.array([800.0]), np.array([800.0]))[0]
    p[hot] = 0.3
    look = g.xy.copy()
    rc = reach((0, 0), 300, (0, 0), 'single', [(0, 0)], g.xy)
    lines = RT.recommend(g, p, p * 0 + 0.5, p * 0 + 2, 0.1, (0, 0), 300, (0, 0), 'single', [(0, 0)],
                         look, rc['in_reach'] & g.disc)
    assert lines, "expected at least one line"
    assert lines[0]['p'] >= 0.3 - 1e-9
    for ln in lines:
        assert ln['end_height'] >= C.MIN_WORK_AGL
