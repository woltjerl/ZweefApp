#!/usr/bin/env python3
"""
Prediction-power evaluation (plan section 15).

Modes:
    python scripts/evaluate_prediction.py --tune         # tune on the first 70% of days only
    python scripts/evaluate_prediction.py                # full report (test set = last 30% of days)
    python scripts/evaluate_prediction.py --prospective  # score logged forecasts against new flights

Outputs:
    output/prediction_power_report.html, output/prediction_power.json,
    data/models/tuned_params.json (tune), data/models/acceptance.json (report)
"""

import argparse
import base64
import io
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config as C  # noqa: E402
from src.prediction import evaluation as E  # noqa: E402
from src.prediction import layer_a as A  # noqa: E402
from src.prediction import layer_b as B  # noqa: E402
from src.prediction import layer_c as LC  # noqa: E402
from src.prediction.dataset import connect  # noqa: E402
from src.prediction.reach import validate as validate_reach  # noqa: E402

TUNED = C.MODEL_DIR / 'tuned_params.json'
ACCEPT = C.MODEL_DIR / 'acceptance.json'
REPORT = C.OUTPUT_DIR / 'prediction_power_report.html'
REPORT_JSON = C.OUTPUT_DIR / 'prediction_power.json'
TUNE_FRACTION = 0.7
QUICK = False          # smoke-test mode: every 10th day only


def split_days(P):
    days = np.sort(P.T.datum.unique())
    n = int(len(days) * TUNE_FRACTION)
    tune_days, test_days = days[:n], days[n:]
    if QUICK:
        return tune_days[::10], test_days[::10]
    return tune_days, test_days


def direction_choice(res, taus=B.DIRECTION_TAUS, methods=E.DIR_METHODS):
    best = None
    rows = []
    for tau in taus:
        for m in methods:
            t = E.direction_test(res, tau, m)
            if not t:
                continue
            rows.append(t)
            if t['exposed'] >= 50 and np.isfinite(t['rr']) and (best is None or t['rr'] > best['rr']):
                best = t
    return best, rows


# ═══════════════════════════ tuning ═══════════════════════════

def tune(conn, log=print):
    t0 = time.time()
    prep = {'cell_m': C.CELL_M, 'match_radius': C.MATCH_RADIUS_M, 'use_drift': C.USE_DRIFT}
    fit = B.default_params()
    P = B.prepare(conn, **prep, verbose=False)
    tune_days, test_days = split_days(P)
    log(f"Tuning on {len(tune_days)} days ({tune_days[0]} → {tune_days[-1]}); "
        f"test days {test_days[0]} → {test_days[-1]} are not used.")
    history = []

    def score(P, fit):
        res = E.lodo(P, fit, days=tune_days, baselines=False, pool_days=tune_days)
        return {'conc_hazard': E.concordance(res, 'hazard'),
                'cap10_expected_hits': E.capture(res, 'expected_hits', 0.10),
                'n_conc': int(res.conc_hazard.notna().sum())}, res

    def better(a, b):
        if a['conc_hazard'] < b['conc_hazard'] - 0.003:
            return True
        return abs(a['conc_hazard'] - b['conc_hazard']) <= 0.003 and a['cap10_expected_hits'] > b['cap10_expected_hits']

    cur, res = score(P, fit)
    history.append({'change': 'defaults', **prep, **{k: v for k, v in fit.items() if k != 'dims'},
                    'dims': '+'.join(fit['dims']), **cur})
    log(f"  defaults: concordance {cur['conc_hazard']:.3f}, capture@10 {cur['cap10_expected_hits']:.3f}")

    fit_grid = [('smooth_sigma_m', [0.0, 200.0, 300.0]), ('k_cell', [2.0, 5.0, 10.0]),
                ('m_cond', [10.0, 20.0, 50.0]), ('lambda_aerotow', [0.0, 0.3, 0.6])]
    for name, values in fit_grid:
        for v in values:
            if v == fit[name]:
                continue
            trial = dict(fit, **{name: v})
            s, _ = score(P, trial)
            history.append({'change': f'{name}={v}', **prep, **{k: x for k, x in trial.items() if k != 'dims'},
                            'dims': '+'.join(trial['dims']), **s})
            log(f"  {name}={v}: concordance {s['conc_hazard']:.3f}, capture@10 {s['cap10_expected_hits']:.3f}")
            if better(s, cur):
                fit, cur = trial, s
    # conditioning dimensions: drop each in turn
    for dim in list(fit['dims']):
        trial = dict(fit, dims=tuple(x for x in fit['dims'] if x != dim))
        s, _ = score(P, trial)
        history.append({'change': f'without {dim}', **prep, **{k: x for k, x in trial.items() if k != 'dims'},
                        'dims': '+'.join(trial['dims']), **s})
        log(f"  without {dim}: concordance {s['conc_hazard']:.3f}, capture@10 {s['cap10_expected_hits']:.3f}")
        if better(s, cur):
            fit, cur = trial, s
    # data preparation: grid, matching radius, drift correction
    prep_grid = [('use_drift', [False]), ('cell_m', [150.0, 300.0]), ('match_radius', [150.0, 300.0])]
    for name, values in prep_grid:
        for v in values:
            trial = dict(prep, **{name: v})
            Pt = B.prepare(conn, **trial, verbose=False)
            s, _ = score(Pt, fit)
            history.append({'change': f'{name}={v}', **trial, **{k: x for k, x in fit.items() if k != 'dims'},
                            'dims': '+'.join(fit['dims']), **s})
            log(f"  {name}={v}: concordance {s['conc_hazard']:.3f}, capture@10 {s['cap10_expected_hits']:.3f}")
            if better(s, cur):
                prep, cur, P = trial, s, Pt

    # direction rule (tau, method) on tuning days with the final map settings
    _, res = score(P, fit)
    best_dir, dir_rows = direction_choice(res)
    log(f"  direction rule chosen on tuning days: {best_dir}")
    out = {'prepare': prep, 'fit': {k: (list(v) if isinstance(v, tuple) else v) for k, v in fit.items()},
           'direction': {'tau': best_dir['tau'], 'method': best_dir['method']} if best_dir else None,
           'tune_score': cur, 'tune_days': [str(tune_days[0]), str(tune_days[-1]), len(tune_days)],
           'test_days': [str(test_days[0]), str(test_days[-1]), len(test_days)],
           'history': history, 'direction_tuning': dir_rows, 'minutes': round((time.time() - t0) / 60, 1)}
    C.MODEL_DIR.mkdir(parents=True, exist_ok=True)
    TUNED.write_text(json.dumps(out, indent=2, default=float))
    log(f"✓ tuned parameters saved to {TUNED} ({out['minutes']} min)")
    return out


def load_tuned():
    if TUNED.exists():
        t = json.loads(TUNED.read_text())
        fit = B.default_params(**{k: (tuple(v) if k == 'dims' else v) for k, v in t['fit'].items()})
        return t, t['prepare'], fit
    return None, {}, B.default_params()


# ═══════════════════════════ figures ═══════════════════════════

def _fig_to_img(fig):
    import matplotlib.pyplot as plt
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=110, bbox_inches='tight')
    plt.close(fig)
    return f'<img src="data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}" style="max-width:100%">'


def fig_reliability(L):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    L = L.copy()
    L['bin'] = pd.qcut(L.pA.rank(method='first'), 10, labels=False)
    t = L.groupby('bin').agg(pred=('pA', 'mean'), obs=('connected', 'mean'), n=('connected', 'size'))
    fig, ax = plt.subplots(figsize=(4.2, 4))
    ax.plot([0, 0.45], [0, 0.45], color='0.7', lw=1)
    se = np.sqrt(t.obs * (1 - t.obs) / t.n)
    ax.errorbar(t.pred, t.obs, yerr=1.96 * se, fmt='o', color='#1f6f8b', ms=5)
    ax.set_xlabel('predicted chance of connecting (out-of-fold)')
    ax.set_ylabel('observed share connected')
    ax.set_title('Layer A reliability (10 equal-size bins)', fontsize=10)
    return _fig_to_img(fig)


def fig_capture(res, names):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    ks = np.linspace(0.01, 0.5, 50)
    fig, ax = plt.subplots(figsize=(5.5, 4))
    ax.plot(ks * 100, ks * 100, color='0.75', lw=1, ls='--', label='random')
    for n in names:
        if f'cap_{n}' in res:
            ax.plot(ks * 100, [E.capture(res, n, k) * 100 for k in ks], label=n.replace('_', ' '))
    ax.set_xlabel('top % of reachable area (ranked by map)')
    ax.set_ylabel('% of first climbs inside')
    ax.legend(fontsize=8)
    ax.set_title('Capture curve (held-out test days)', fontsize=10)
    return _fig_to_img(fig)


def fig_dose(res, col):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    D = res[res[col].notna()].copy()
    D['rk'] = np.minimum(D[col], 4).astype(int)
    t = D.groupby('rk').agg(O=('connected', 'sum'), Ex=('pA', 'sum'), n=('connected', 'size'))
    fig, ax = plt.subplots(figsize=(5, 3.4))
    labels = ['best', '2nd', '3rd', '4th', '5th–8th']
    ax.bar([labels[i] for i in t.index], t.O / t.Ex, color='#1f6f8b')
    ax.axhline(1, color='0.5', lw=1)
    for k, (o, n) in enumerate(zip(t.O, t.n)):
        ax.text(k, 0.03, f"n={n}", ha='center', fontsize=7, color='white')
    ax.set_ylabel('connections: observed / expected')
    ax.set_xlabel('rank of the direction the pilot chose (by the model)')
    return _fig_to_img(fig)


# ═══════════════════════════ report ═══════════════════════════

def coverage_table(P, fit):
    T = P.T
    sec = B.sector_of(T.w100_dir)
    names = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
    rows = []
    for rw in C.RUNWAYS:
        for s in range(8):
            m = ((T.runway == rw).to_numpy() & (sec == s))
            n, k = int(m.sum()), int(T.connected.to_numpy()[m].sum())
            if n == 0:
                continue
            cells = int(((m.astype(float) @ P.S) >= 20).sum())
            verdict = 'no advice' if n < 50 else ('thin' if n < 150 or k < 15 else 'reliable')
            rows.append({'runway': rw, 'wind_from': names[s], 'launches': n, 'connects': k,
                         'cells_with_evidence': cells, 'verdict': verdict})
    return pd.DataFrame(rows)


def day_stats(res, days_index, col, fn):
    D = res[res[col].notna()]
    g = D.groupby('datum')[col]
    num = g.apply(fn).reindex(days_index, fill_value=0).to_numpy()
    den = g.size().reindex(days_index, fill_value=0).to_numpy()
    return num, den


def report(conn, n_boot=2000, log=print):
    t0 = time.time()
    tuned, prep, fit = load_tuned()
    direction = (tuned or {}).get('direction') or {'tau': 30, 'method': 'oe'}
    log("Preparing data...")
    P = B.prepare(conn, **prep, verbose=False)
    tune_days, test_days = split_days(P)
    log(f"Leave-one-day-out over all {P.T.datum.nunique()} days (with baselines)...")
    res = E.lodo(P, fit, baselines=True,
                 days=np.concatenate([tune_days, test_days]) if QUICK else None)
    res['set'] = np.where(res.datum.isin(test_days), 'test', 'tune')
    R = res[res.set == 'test']
    days_idx = np.sort(R.datum.unique())
    W = E.day_weights(len(days_idx), n_boot)

    out = {'generated': pd.Timestamp.now().isoformat(), 'params': {'prepare': prep, 'fit': {
        k: (list(v) if isinstance(v, tuple) else v) for k, v in fit.items()}, 'direction': direction},
        'test_days': [str(test_days[0]), str(test_days[-1]), int(len(test_days))],
        'tune_days': [str(tune_days[0]), str(tune_days[-1]), int(len(tune_days))]}

    # ── data summary ──
    L_all = pd.read_sql_query("SELECT * FROM launches", conn)
    Wn = L_all[(L_all.launch_type == 'winch')]
    ok = Wn[(Wn.launch_quality == 'ok') & (Wn.track_status == 'ok')]
    Ar = L_all[L_all.launch_type == 'aerotow']
    paired = Ar[Ar.release_method == 'tug_pair']
    err = (paired.release_sig_utc_s - paired.release_utc_s).dropna()
    ncl = conn.execute("SELECT COUNT(*) FROM climbs").fetchone()[0]
    first_c = pd.read_sql_query("""SELECT c.agl0, c.s_since_release, l.release_agl, l.height_lost_to_first_climb_m,
        l.first_climb_dist_m FROM climbs c JOIN launches l ON l.flight_id=c.flight_id
        WHERE c.is_first=1 AND l.launch_type='winch' AND l.launch_quality='ok' AND l.connected=1""", conn)
    audit = C.ROOT / 'data' / 'label_audit.csv'
    aud = pd.read_csv(audit) if audit.exists() else None
    out['data'] = {
        'winch_launches_clean': int(len(ok)), 'winch_connected': int(ok.connected.sum()),
        'connect_rate': float(ok.connected.mean()), 'days': int(ok.datum.nunique()),
        'release_agl_median': float(ok.release_agl.median()),
        'height_lost_quantiles': first_c.height_lost_to_first_climb_m.quantile([.1, .5, .9]).round().tolist(),
        'dist_to_first_climb_quantiles': first_c.first_climb_dist_m.quantile([.5, .9]).round().tolist(),
        'share_within_3min': float((first_c.s_since_release <= 180).mean()),
        'climbs_total': int(ncl), 'aerotow_flights': int(len(Ar)),
        'aerotow_release_methods': Ar.release_method.value_counts(dropna=False).to_dict(),
        'tow_signature_error_s': {'n_pairs': int(len(err)), 'median_abs': float(err.abs().median()),
                                  'p90_abs': float(err.abs().quantile(.9)),
                                  'early_over_120s': int((err < -120).sum())},
        'excluded_winch': Wn.launch_quality.value_counts(dropna=False).to_dict(),
        'audit': None if aud is None else {'n': int(len(aud)), 'agreement': float(aud.correct.mean())},
    }

    # ── layer A ──
    log("Layer A metrics...")
    LA = A.load()
    from src.prediction.dataset import winch_launches
    L = winch_launches(conn)
    pa = pd.read_sql_query("SELECT flight_id, pA FROM launches", conn).set_index('flight_id').pA
    L['pA'] = pa.reindex(L.flight_id).to_numpy()
    _, base_cal = A.baseline_oof(L)
    mA = A.metrics(L.connected.to_numpy(), L.pA.to_numpy())
    mB = A.metrics(L.connected.to_numpy(), base_cal)
    L['terc'] = pd.qcut(L.pA.rank(method='first'), 3, labels=['low', 'middle', 'high'])
    terc = L.groupby('terc', observed=True).agg(pred=('pA', 'mean'), obs=('connected', 'mean'), n=('connected', 'size'))
    dA = L.groupby('datum')
    from sklearn.metrics import brier_score_loss
    days_all = np.sort(L.datum.unique())
    WA = E.day_weights(len(days_all), n_boot)
    b_model = dA.apply(lambda t: ((t.pA - t.connected) ** 2).sum(), include_groups=False).reindex(days_all).to_numpy()
    rate = L.connected.mean()
    b_const = dA.apply(lambda t: ((rate - t.connected) ** 2).sum(), include_groups=False).reindex(days_all).to_numpy()
    bss = 1 - (WA @ b_model) / (WA @ b_const)
    out['layer_a'] = {'model': mA, 'baseline_month_hour': mB, 'brier_skill_ci': E.ci(bss),
                      'terciles': terc.round(4).reset_index().to_dict('records'),
                      'max_calibration_gap': float(np.max(np.abs(
                          L.groupby(pd.qcut(L.pA.rank(method='first'), 10, labels=False)).apply(
                              lambda t: t.pA.mean() - t.connected.mean(), include_groups=False))))}

    # ── reach ──
    log("Reach validation...")
    rv, _ = validate_reach(conn, verbose=False)
    out['reach'] = {k: v for k, v in rv.items() if k != 'circuit_entry_points'}

    # ── capture ──
    log("Capture, concordance, direction tests...")
    names = ['expected_hits', 'hazard'] + list(E.BASELINES)
    cap = {}
    for n in names:
        col = f'cap_{n}'
        row = {}
        for k in (0.05, 0.10, 0.20):
            num, den = day_stats(R, days_idx, col, lambda v, k=k: (v < k).sum())
            row[f'top{int(k * 100)}'] = float(num.sum() / den.sum())
            row[f'top{int(k * 100)}_ci'] = E.ci(E.boot_ratio(num, den, W))
        cap[n] = row
    best_base = max(E.BASELINES, key=lambda b: cap[b]['top10'])
    nm, dm = day_stats(R, days_idx, 'cap_expected_hits', lambda v: (v < 0.1).sum())
    nb, db = day_stats(R, days_idx, f'cap_{best_base}', lambda v: (v < 0.1).sum())
    diff = E.boot_ratio(nm, dm, W) - E.boot_ratio(nb, db, W)
    out['capture'] = {'maps': cap, 'best_baseline': best_base, 'n_first_climbs': int(R.cap_hazard.notna().sum()),
                      'diff_expected_hits_vs_best_baseline_top10': float(nm.sum() / dm.sum() - nb.sum() / db.sum()),
                      'diff_ci': E.ci(diff)}

    # ── concordance ──
    conc = {}
    for n in names:
        col = f'conc_{n}'
        if col not in R:
            continue
        num, den = day_stats(R, days_idx, col, lambda v: v.sum())
        b3n, _ = day_stats(R, days_idx, col, lambda v: (v < 1 / 3).sum())
        conc[n] = {'mean_pct': float(num.sum() / den.sum()), 'ci': E.ci(E.boot_ratio(num, den, W)),
                   'best_third': float(b3n.sum() / den.sum()), 'n': int(den.sum()),
                   'p_value': E.concordance_p(R[col])}
    out['concordance'] = conc
    out['concordance_all_days'] = {n: E.concordance(res, n) for n in names if f'conc_{n}' in res}

    # ── direction tests ──
    tau, meth = direction['tau'], direction['method']
    col = f'dir{tau}_{meth}'
    D = R[R[col].notna()]
    x = (D[col] == 0).astype(int).to_numpy()
    st = E.strata_of(D)
    rr, se = E.mh_rr(D.connected.to_numpy(), x, st)
    Rn, Sn = E.mh_by_day(D, x, days_idx)
    p_perm = E.permutation_p(D.connected.to_numpy(), x, st, n_perm=5000)
    mde, power = E.minimum_detectable_rr(D.pA.to_numpy(), x, st)
    dose = D.assign(rk=np.minimum(D[col], 4).astype(int)).groupby('rk').agg(
        O=('connected', 'sum'), Ex=('pA', 'sum'), n=('connected', 'size'))
    dose['oe'] = dose.O / dose.Ex
    LCb = LC.load()
    ace_ids = set()
    if LCb is not None:
        sk = LC.pilot_skill(LC.load_launches(conn))
        ace_ids, _ = LC.aces(sk)
    non = D[~D.pilot_id.isin(ace_ids)]
    xn = (non[col] == 0).astype(int).to_numpy()
    rr_n, se_n = E.mh_rr(non.connected.to_numpy(), xn, E.strata_of(non))
    per_rw = {}
    for rw, g in D.groupby('runway'):
        xg = (g[col] == 0).astype(int).to_numpy()
        r_, s_ = E.mh_rr(g.connected.to_numpy(), xg, E.strata_of(g))
        per_rw[rw] = {'n': int(len(g)), 'exposed': int(xg.sum()), 'rr': r_,
                      'lo': float(np.exp(np.log(r_) - 1.96 * s_)) if np.isfinite(r_) and np.isfinite(s_) else np.nan,
                      'hi': float(np.exp(np.log(r_) + 1.96 * s_)) if np.isfinite(r_) and np.isfinite(s_) else np.nan}
    all_dir = [E.direction_test(R, t, m) for t in B.DIRECTION_TAUS for m in E.DIR_METHODS]
    out['direction'] = {
        'rule': direction, 'n': int(len(D)), 'exposed': int(x.sum()), 'rr': rr,
        'ci_wald': [float(np.exp(np.log(rr) - 1.96 * se)), float(np.exp(np.log(rr) + 1.96 * se))],
        'ci_day_bootstrap': E.ci(E.boot_ratio(Rn, Sn, W)), 'p_permutation': p_perm,
        'mde_rr_80pct_power': mde, 'power': power,
        'dose_response': dose.round(3).reset_index().to_dict('records'),
        'non_aces': {'n': int(len(non)), 'rr': rr_n,
                     'ci': [float(np.exp(np.log(rr_n) - 1.96 * se_n)), float(np.exp(np.log(rr_n) + 1.96 * se_n))]
                     if np.isfinite(rr_n) else [np.nan, np.nan]},
        'per_runway': per_rw, 'all_rules_test_days': [t for t in all_dir if t]}

    # ── layer C ──
    out['layer_c'] = None if LCb is None else {'summary': LCb['summary'],
                                               'route_table': LCb['route_table'].round(3).to_dict('records')}

    # ── coverage ──
    cov = coverage_table(P, fit)
    out['coverage'] = cov.to_dict('records')

    # ── acceptance (plan 4.6) ──
    crit_capture = out['capture']['diff_ci'][0] > 0
    crit_lift = out['direction']['ci_day_bootstrap'][0] > 1 and rr > 1
    crit_conc = conc['hazard']['ci'][1] < 0.5
    decision = 'full' if (crit_capture and crit_lift) else 'degraded'
    out['acceptance'] = {
        'capture_above_best_baseline': bool(crit_capture), 'conversion_lift_above_1': bool(crit_lift),
        'concordance_above_chance': bool(crit_conc), 'decision': decision,
        'meaning': ('All plan criteria met: show ranked spots and recommended direction.' if decision == 'full' else
                    'Plan criteria not both met: show layer A, the per-cell rate map with sample counts and '
                    'where first thermals were found; no ranked "fly here" recommendation.')}
    ACCEPT.write_text(json.dumps(out['acceptance'], indent=2))

    # ── tuning history ──
    out['tuning'] = tuned

    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(out, indent=2, default=lambda o: None if o is None else float(o)))
    html = render(out, L, R, cov, col)
    REPORT.write_text(html)
    log(f"✓ report: {REPORT}  ({(time.time() - t0) / 60:.1f} min)")
    return out


# ═══════════════════════════ HTML ═══════════════════════════

def _pct(v, d=1):
    return '–' if v is None or not np.isfinite(v) else f"{100 * v:.{d}f}%"


def _ci(c, pct=True, d=1):
    if c is None or any(v is None or not np.isfinite(v) for v in c):
        return ''
    return f" ({_pct(c[0], d)}–{_pct(c[1], d)})" if pct else f" ({c[0]:.2f}–{c[1]:.2f})"


def render(o, L, R, cov, dircol):
    a, cap, conc, dr, acc = o['layer_a'], o['capture'], o['concordance'], o['direction'], o['acceptance']
    d = o['data']
    css = """
    :root{--bg:#fbfbf8;--fg:#1d2327;--muted:#5d6b73;--line:#d9ddd8;--ok:#2e7d4f;--bad:#b23b2e;--accent:#1f6f8b}
    @media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#15191b;--fg:#e6e9ea;--muted:#9aa7ad;--line:#2e3639;--ok:#6cc08c;--bad:#e2786b;--accent:#6cb8d1}}
    body{background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0}
    main{max-width:980px;margin:0 auto;padding:24px 16px 64px}
    h1{font-size:26px;margin:0 0 4px} h2{font-size:19px;margin:36px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
    h3{font-size:15px;margin:20px 0 6px} .muted{color:var(--muted)} table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13.5px}
    th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top} th{font-weight:600}
    td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
    .verdict{border:1px solid var(--line);border-radius:8px;padding:12px 16px;margin:14px 0}
    .ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;align-items:start}
    .scroll{overflow-x:auto}
    """
    yes = lambda b: '<span class="ok">yes</span>' if b else '<span class="bad">no</span>'  # noqa: E731

    cap_rows = ''.join(
        f"<tr><td>{n.replace('_', ' ')}</td>" + ''.join(
            f"<td class='num'>{_pct(v[f'top{k}'])}<span class='muted'>{_ci(v[f'top{k}_ci'])}</span></td>"
            for k in (5, 10, 20)) + "</tr>" for n, v in cap['maps'].items())
    conc_rows = ''.join(
        f"<tr><td>{n.replace('_', ' ')}</td><td class='num'>{v['mean_pct']:.3f}<span class='muted'>"
        f"{_ci(v['ci'], pct=False)}</span></td><td class='num'>{_pct(v['best_third'])}</td>"
        f"<td class='num'>{v['p_value']:.2g}</td><td class='num'>{v['n']}</td></tr>" for n, v in conc.items())
    dose_rows = ''.join(
        f"<tr><td>{['best', '2nd', '3rd', '4th', '5th–8th'][int(r['rk'])]}</td><td class='num'>{int(r['n'])}</td>"
        f"<td class='num'>{int(r['O'])}</td><td class='num'>{r['Ex']:.1f}</td><td class='num'>{r['oe']:.2f}</td></tr>"
        for r in dr['dose_response'])
    rw_rows = ''.join(
        f"<tr><td>{rw}</td><td class='num'>{v['n']}</td><td class='num'>{v['exposed']}</td>"
        f"<td class='num'>{v['rr']:.2f}</td><td class='num muted'>{v['lo']:.2f}–{v['hi']:.2f}</td></tr>"
        if v['rr'] is not None and np.isfinite(v['rr']) else
        f"<tr><td>{rw}</td><td class='num'>{v['n']}</td><td class='num'>{v['exposed']}</td><td colspan=2>–</td></tr>"
        for rw, v in dr['per_runway'].items())
    rules_rows = ''.join(
        f"<tr><td>{t['tau']} s</td><td>{t['method']}</td><td class='num'>{t['n']}</td><td class='num'>{t['exposed']}</td>"
        f"<td class='num'>{t['rr']:.2f}</td><td class='num muted'>{t['lo']:.2f}–{t['hi']:.2f}</td></tr>"
        for t in dr['all_rules_test_days'] if t.get('rr') is not None and np.isfinite(t['rr']))
    power_txt = ', '.join(f"RR {k}: {v:.0%}" for k, v in dr['power'].items())
    lc = o['layer_c']
    lc_html = ''
    if lc:
        s = lc['summary']
        pm, pp = s['permutation'], s['pooled_place_test']
        lc_html = f"""
        <p>{s['n_pilots_eligible']} pilots with ≥{C.ACE_MIN_LAUNCHES} clean winch launches. Skill = connections ÷
        expected connections (from layer A: weather, time, glider class, dual), shrunk towards 1.</p>
        <table><tr><th>Skill quantile</th>{''.join(f"<th class='num'>p{int(float(k) * 100)}</th>" for k in s['skill_quantiles'])}</tr>
        <tr><td>observed ÷ expected</td>{''.join(f"<td class='num'>{v:.2f}</td>" for v in s['skill_quantiles'].values())}</tr></table>
        <p>Aces (top {int(C.ACE_TOP_FRACTION * 100)}%, {s['n_aces']} pilots) connect <b>{s['oe_aces']:.2f}×</b> expected,
        everyone else {s['oe_others']:.2f}×. Spread in skill vs chance (pilot labels shuffled within each day,
        2,000 times): SD of log skill {pm['observed_sd_log_skill']:.3f} vs {pm['null_mean']:.3f} expected,
        p = {pm['p_value']:.4f}. <b>{'Skill differences are real.' if pm['p_value'] < 0.05 else 'No evidence of real skill differences.'}</b></p>
        <p><b>Place or skill?</b> Non-aces who flew the aces' preferred first-{int(LC.ROUTE_S)}-second route vs non-aces on
        other routes, same day and hour: RR {pp['rr']:.2f} ({pp['lo']:.2f}–{pp['hi']:.2f}), permutation p = {pp['p_value']:.3f}
        (n {pp['n']}, {pp['exposed']} on the aces' route).
        <b>{'Following the aces route helps.' if pp['lo'] > 1 else 'Following the aces route does not measurably help non-aces: the aces advantage is skill (finding and centring), not the first direction they fly.'}</b></p>"""

    cov_rows = ''.join(
        f"<tr><td>{r['runway']}</td><td>{r['wind_from']}</td><td class='num'>{r['launches']}</td>"
        f"<td class='num'>{r['connects']}</td><td class='num'>{r['cells_with_evidence']}</td><td>{r['verdict']}</td></tr>"
        for r in o['coverage'])
    tun = o.get('tuning') or {}
    hist_rows = ''.join(
        f"<tr><td>{h['change']}</td><td class='num'>{h['conc_hazard']:.3f}</td>"
        f"<td class='num'>{_pct(h['cap10_expected_hits'])}</td></tr>" for h in tun.get('history', []))

    best_base = cap['best_baseline']
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>First Thermal Prediction Power</title>
<style>{css}</style></head><body><main>
<h1>First-thermal prediction: how much does it know?</h1>
<p class="muted">EHHV winch launches · generated {o['generated'][:16]} · test set = last 30% of days
({o['test_days'][0]} → {o['test_days'][1]}, {o['test_days'][2]} days), never used for tuning.
Every number on held-out days the model never saw (leave-one-day-out).</p>

<div class="verdict"><b>Verdict (plan 4.6)</b><br>
Capture above the best baseline: {yes(acc['capture_above_best_baseline'])} ·
Following the model's direction raises the connect rate: {yes(acc['conversion_lift_above_1'])} ·
Per-cell map knows more than chance about where thermals are: {yes(acc['concordance_above_chance'])}<br>
<b>Decision: {acc['decision']}.</b> {acc['meaning']}</div>

<h2>1. Data</h2>
<p>{d['winch_launches_clean']} clean winch launches on {d['days']} days; {d['winch_connected']} connected
({_pct(d['connect_rate'])}). Median release {d['release_agl_median']:.0f} m AGL. First climbs: height lost after release
p10/p50/p90 {' / '.join(f'{v:.0f}' for v in d['height_lost_quantiles'])} m, distance p50/p90
{' / '.join(f'{v:.0f}' for v in d['dist_to_first_climb_quantiles'])} m, {_pct(d['share_within_3min'])} within 3 minutes.
{d['climbs_total']} circling climbs in total (all launch types).
Label audit: {d['audit']['n'] if d['audit'] else '–'} flights, agreement {_pct(d['audit']['agreement']) if d['audit'] else '–'}.
Aerotow release: {', '.join(f"{v} {('no release found' if k in (None, 'None') else k.replace('_', ' '))}" for k, v in d['aerotow_release_methods'].items())}; fallback rule vs tug pairing (n={d['tow_signature_error_s']['n_pairs']}):
median error {d['tow_signature_error_s']['median_abs']:.0f} s, p90 {d['tow_signature_error_s']['p90_abs']:.0f} s,
{d['tow_signature_error_s']['early_over_120s']} early by more than the 120 s guard.</p>

<h2>2. When — layer A (chance of connecting this hour)</h2>
<div class="grid"><div>
<table><tr><th></th><th class="num">AUC</th><th class="num">Brier skill</th></tr>
<tr><td>Layer A (weather, time, glider)</td><td class="num">{a['model']['auc']:.3f}</td><td class="num">{a['model']['brier_skill']:.3f}
<span class="muted">{_ci(a['brier_skill_ci'], pct=False)}</span></td></tr>
<tr><td>Baseline: month + hour</td><td class="num">{a['baseline_month_hour']['auc']:.3f}</td><td class="num">{a['baseline_month_hour']['brier_skill']:.3f}</td></tr></table>
<table><tr><th>Predicted tercile</th><th class="num">predicted</th><th class="num">observed</th><th class="num">launches</th></tr>
{''.join(f"<tr><td>{r['terc']}</td><td class='num'>{_pct(r['pred'])}</td><td class='num'>{_pct(r['obs'])}</td><td class='num'>{r['n']}</td></tr>" for r in a['terciles'])}</table>
<p class="muted">Largest calibration gap over 10 bins: {100 * a['max_calibration_gap']:.1f} points. Out-of-fold by day (10 folds).</p>
</div><div>{fig_reliability(L)}</div></div>

<h2>3. Reach</h2>
<p>{_pct(o['reach']['coverage'])} of {o['reach']['n_first_climbs']} historical first climbs lie inside the reach computed for
their own flight (target ≥ 95%). Minimum workable height {C.MIN_WORK_AGL:.0f} m (data: 5th percentile of first-climb
start {o['reach']['agl0_p5']:.0f} m); circuit-entry floor {C.CIRCUIT_ENTRY_AGL:.0f} m. Pilots achieve an effective glide of
{o['reach']['effective_glide_median']:.1f} (median) from release to first climb — lower than the still-air glide used for
reach, so arrival heights on the map are optimistic.</p>

<h2>4. Where — three tests</h2>
<h3>4.1 Capture: share of first climbs inside the map's top-k% of reachable area</h3>
<div class="grid"><div class="scroll"><table><tr><th>Map</th><th class="num">top 5%</th><th class="num">top 10%</th><th class="num">top 20%</th></tr>{cap_rows}</table>
<p class="muted">n = {cap['n_first_climbs']} first climbs on test days; 95% intervals by day bootstrap. <i>expected hits</i> =
search traffic from the same runway in similar wind × per-cell lift; <i>hazard</i> = per-visit lift alone (not designed for
capture). Best baseline: {best_base}. Difference expected hits − {best_base} at top 10%:
{100 * cap['diff_expected_hits_vs_best_baseline_top10']:+.1f} points{_ci(cap['diff_ci'])}.</p></div>
<div>{fig_capture(R, ['expected_hits', 'hazard', 'traffic', 'climbs_wind', 'climbs_runway'])}</div></div>
<p>Capture mostly measures where pilots fly: first thermals are found where people search. It cannot tell a good place
from a busy one, so it is reported but not trusted on its own.</p>

<h3>4.2 Concordance: among the cells a pilot searched, was the thermal in the best-ranked one?</h3>
<table><tr><th>Map</th><th class="num">mean percentile (0.5 = chance)</th><th class="num">hit in best third (33% = chance)</th>
<th class="num">p</th><th class="num">n</th></tr>{conc_rows}</table>
<p>Conditioning on the actual path removes traffic and the fact that successful flights stop searching early.
Lower is better.</p>

<h3>4.3 Direction: does heading the model's way after release raise the connect rate?</h3>
<p>Rule (chosen on tuning days only): direction = bearing from release to the position {dr['rule']['tau']} s after release,
method <i>{dr['rule']['method']}</i>, 8 compass sectors. Launches that connected before {dr['rule']['tau'] + 10} s are left out so the
direction cannot depend on the outcome. Exposed = chose the model's best sector. Strata = day × 2-hour block
(weather cancels out).</p>
<table><tr><th></th><th class="num">value</th></tr>
<tr><td>Launches / chose best direction</td><td class="num">{dr['n']} / {dr['exposed']}</td></tr>
<tr><td>Mantel–Haenszel risk ratio</td><td class="num"><b>{dr['rr']:.2f}</b></td></tr>
<tr><td>95% interval (day bootstrap)</td><td class="num">{dr['ci_day_bootstrap'][0]:.2f}–{dr['ci_day_bootstrap'][1]:.2f}</td></tr>
<tr><td>95% interval (Greenland–Robins)</td><td class="num">{dr['ci_wald'][0]:.2f}–{dr['ci_wald'][1]:.2f}</td></tr>
<tr><td>Permutation p (one-sided, 5,000 within strata)</td><td class="num">{dr['p_permutation']:.3f}</td></tr>
<tr><td>Non-aces only</td><td class="num">{('RR %.2f (%.2f–%.2f)' % (dr['non_aces']['rr'], *dr['non_aces']['ci'])) if dr['non_aces']['rr'] is not None and np.isfinite(dr['non_aces']['rr']) else '–'}, n {dr['non_aces']['n']}</td></tr>
<tr><td>Smallest effect detectable with 80% power</td><td class="num">{('RR ' + str(dr['mde_rr_80pct_power'])) if dr['mde_rr_80pct_power'] else '> 3'}</td></tr></table>
<p class="muted">Power by true RR: {power_txt}.</p>
<div class="grid"><div><table><tr><th>Chosen direction</th><th class="num">launches</th><th class="num">connected</th>
<th class="num">expected</th><th class="num">obs/exp</th></tr>{dose_rows}</table></div><div>{fig_dose(R, dircol)}</div></div>
<h3>Per runway</h3><table><tr><th>Runway</th><th class="num">launches</th><th class="num">best dir</th><th class="num">RR</th><th class="num">95%</th></tr>{rw_rows}</table>
<h3>All direction rules on test days (for transparency; only the pre-chosen rule counts)</h3>
<table><tr><th>τ</th><th>method</th><th class="num">n</th><th class="num">exposed</th><th class="num">RR</th><th class="num">95%</th></tr>{rules_rows}</table>

<h2>5. Aces — layer C</h2>{lc_html}

<h2>6. Coverage per runway and wind</h2>
<p class="muted">thin: &lt;150 launches or &lt;15 connections · no advice: &lt;50 launches. The forecast page follows this verdict.</p>
<div class="scroll"><table><tr><th>Runway</th><th>Wind from</th><th class="num">launches</th><th class="num">connected</th>
<th class="num">cells searched by ≥20 launches</th><th>verdict</th></tr>{cov_rows}</table></div>

<h2>7. Tuning and ablations (tuning days only)</h2>
<p class="muted">Coordinate-wise search; objective = concordance of the per-cell map (lower is better), tie-break capture@10 of
expected hits. Each row changes one setting from the best so far.</p>
<table><tr><th>Change</th><th class="num">concordance</th><th class="num">capture@10</th></tr>{hist_rows}</table>

<h2>8. Caveats</h2>
<ul>
<li>Exploratory analyses while building the model looked at all days, including the test days; the test set is therefore
not fully pristine. The only untouched evidence will be the prospective log (forecasts written before the flights).</li>
<li>Layer A probabilities are out-of-fold over all days, so tuning-day probabilities were fitted with test-day data in the
training folds (small effect on the day-quality adjustment only).</li>
<li>Pilots who searched in high-ranked places differ from others in ways the strata may not capture (glider, experience);
the non-ace analysis is shown for that reason.</li>
<li>Reach uses a still-air glide; real pilots lose height faster while searching.</li>
</ul>
</main></body></html>"""


# ═══════════════════════════ prospective ═══════════════════════════

def prospective(conn, log=print):
    if not C.FORECAST_LOG.exists():
        log("No forecast log yet.")
        return
    logs = [json.loads(line) for line in C.FORECAST_LOG.read_text().splitlines() if line.strip()]
    from src.prediction.dataset import winch_launches
    L = winch_launches(conn, with_features=False)
    rows = []
    for f in logs:
        made = pd.Timestamp(f['made'])
        dd = L[(L.datum == f['date'])]
        if f.get('runway'):
            dd = dd[dd.runway == f['runway']]
        lo, hi = f['window_utc']
        dd = dd[(dd.hour_utc >= lo) & (dd.hour_utc < hi)]
        if made.date() > pd.Timestamp(f['date']).date() or dd.empty:
            continue
        best = f.get('best_sector')
        st = pd.read_sql_query(f"SELECT * FROM search_tracks WHERE flight_id IN "
                               f"({','.join(str(int(i)) for i in dd.flight_id)})", conn)
        st = st.merge(dd[['flight_id', 'release_utc_s', 'release_x', 'release_y']], on='flight_id')
        tau = f.get('tau', 30)
        pos = st[st.t_utc_s - st.release_utc_s <= tau].groupby('flight_id').last()
        dd = dd.join(pos[['x', 'y']], on='flight_id')
        sec = B.sector_of(C.bearing_deg(dd.x - dd.release_x, dd.y - dd.release_y))
        rows.append({'date': f['date'], 'window': f['window'], 'pA': f['pA'], 'launches': len(dd),
                     'connected': int(dd.connected.sum()),
                     'followed_best': int((sec == best).sum()) if best is not None else None,
                     'connected_when_followed': int(dd.connected[sec == best].sum()) if best is not None else None})
    if not rows:
        log("No logged forecasts with flights after them yet.")
        return
    df = pd.DataFrame(rows)
    log(df.to_string(index=False))
    exp = (df.pA * df.launches).sum()
    log(f"\nObserved {df.connected.sum()} connections vs {exp:.1f} expected from logged pA "
        f"({len(df)} forecast windows, {df.launches.sum()} launches)")


def main():
    p = argparse.ArgumentParser(description='Prediction-power evaluation')
    p.add_argument('--tune', action='store_true')
    p.add_argument('--prospective', action='store_true')
    p.add_argument('--boot', type=int, default=2000)
    p.add_argument('--quick', action='store_true', help='smoke test on every 10th day (results not meaningful)')
    p.add_argument('--db', default=str(C.DB_PATH))
    args = p.parse_args()
    global QUICK
    QUICK = args.quick
    conn = connect(args.db)
    if args.tune:
        tune(conn)
    elif args.prospective:
        prospective(conn)
    else:
        report(conn, n_boot=args.boot)
    return 0


if __name__ == '__main__':
    sys.exit(main())
