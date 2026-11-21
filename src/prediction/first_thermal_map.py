"""
First-thermal forecast page (plan section 16).

build_forecast() runs the pipeline for one date / time window / runway:
forecast weather → features → layer A (chance this hour) → reach → layer B (per-cell
lift for today's conditions) → direction history → ace routes → recommended lines
(only when the evaluation accepted them) → render_map() writes a folium page →
log_forecast() appends the forecast to data/forecast_log.jsonl for the prospective check.
"""

import json
import math
import sys
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from src import config as C
from src.prediction import layer_a as A
from src.prediction import layer_b as B
from src.prediction import layer_c as LC
from src.prediction import routes as RT
from src.prediction.features import build_features, weather_frame
from src.prediction.reach import reach

sys.path.insert(0, str(C.ROOT / 'src' / 'visualization'))
from basemap import make_map  # noqa: E402  (src/visualization is on sys.path)

WINDOWS_LOCAL = {'morning': (9, 12), 'midday': (12, 15), 'afternoon': (15, 18)}
SECTORS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']


def runway_for_wind(wind_from_deg: float) -> str:
    """Runway in use for a surface wind direction (take-off into wind; ranges from observed winch headings)."""
    w = wind_from_deg % 360
    if 20 <= w < 95:
        return '07'
    if 95 <= w < 155:
        return '12'
    if 155 <= w < 215:
        return '18'
    if 215 <= w < 275:
        return '25'
    if 275 <= w < 335:
        return '30'
    return '36'


def window_utc(day: date, window: str):
    lo, hi = WINDOWS_LOCAL[window]
    def to_utc(h):
        t = datetime(day.year, day.month, day.day, int(h), int(round((h % 1) * 60)), tzinfo=C.TZ_LOCAL)
        return t.astimezone(timezone.utc).replace(tzinfo=None)
    return to_utc(lo), to_utc((lo + hi) / 2), to_utc(hi)


def _wilson(k, n, z=1.96):
    if n == 0:
        return (np.nan, np.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def _historical_bin(conn, pA):
    L = pd.read_sql_query("""SELECT pA, connected FROM launches WHERE launch_type='winch'
                             AND launch_quality='ok' AND pA IS NOT NULL""", conn)
    L['bin'] = pd.qcut(L.pA.rank(method='first'), 10, labels=False)
    edges = L.groupby('bin').pA.max().to_numpy()
    b = int(np.searchsorted(edges, pA, side='left'))
    b = min(b, 9)
    g = L[L.bin == b]
    k, n = int(g.connected.sum()), len(g)
    return {'bin': b, 'observed': k / n if n else np.nan, 'ci': _wilson(k, n), 'n': n,
            'club_rate': float(L.connected.mean())}


def _load_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def build_forecast(conn, day: date, window: str = 'midday', runway: str = None, glider: str = 'single',
                   dual: int = 0, weather_rows=None, exclude_day: bool = False):
    """Run the whole prediction for one date/window. Returns a dict used by render_map/log_forecast."""
    from src.weather.open_meteo_forecast import fetch_convection_forecast

    rows = weather_rows if weather_rows is not None else fetch_convection_forecast(past_days=3, forecast_days=8)
    wx = weather_frame(rows)
    t_lo, t_mid, t_hi = window_utc(day, window)
    if t_mid < wx.index.min() or t_mid > wx.index.max():
        raise ValueError(f"No forecast available for {day} {window} (forecast covers "
                         f"{wx.index.min():%Y-%m-%d} to {wx.index.max():%Y-%m-%d})")

    # surface wind at mid-window → runway
    near = wx.iloc[np.argmin(np.abs((wx.index - t_mid).total_seconds()))]
    wind10 = (float(near.wd10), float(near.ws10))
    runway = runway or runway_for_wind(wind10[0])

    hours = pd.date_range(t_lo, t_hi, freq='1h')
    feats = build_features(wx, list(hours) + [t_mid], runway)
    f_mid = feats.iloc[-1]

    # layer A
    bundle_a = A.load()
    two_seat = int(glider == 'two_seat')
    pA_hours = A.predict(bundle_a, feats.iloc[:-1], two_seat=two_seat, dual=dual, runway=runway)
    pA = float(A.predict(bundle_a, feats.iloc[[-1]], two_seat=two_seat, dual=dual, runway=runway)[0])
    hist = _historical_bin(conn, pA)
    release_agl = float(bundle_a['release_agl_by_runway'].get(runway, bundle_a['release_agl_all']))

    # wind in the layer below the climbs (for drift and reach)
    w10 = C.wind_vector(near.wd10, near.ws10)
    w100 = C.wind_vector(near.wd100, near.ws100)
    wind = (0.5 * (w10[0] + w100[0]), 0.5 * (w10[1] + w100[1]))

    # layer B
    P, params = B.load()
    g = P.grid
    # exclude_day: leave the day itself out of the history (for examples on past days)
    train = (P.T.datum.to_numpy() != str(day)) if exclude_day else np.ones(len(P.T), bool)
    target = {'dir': float(f_mid.w100_dir), 'spd': float(f_mid.w100_spd),
              'hour': t_mid.hour + t_mid.minute / 60, 'season': C.season_of(day.month)}
    fitted = B.fit(P, train, target, params)
    p_cell = B.probability(fitted, pA)
    exp_hits = B.expected_hits_map(P, train, runway, target['dir'], fitted['lift_today'], params['smooth_sigma_m'])
    similar = B.similar_mask(P.T, train, runway, target['dir'])
    searched_similar = similar.astype(float) @ P.S
    hits_similar = similar.astype(float) @ P.H

    # reach from today's release point
    from runway_constants import RUNWAY_LINES  # noqa: E402  (src/visualization on sys.path)
    rel_lat, rel_lon = RUNWAY_LINES[runway]['end']
    rx, ry = C.to_xy(rel_lat, rel_lon)
    release_xy = (float(rx), float(ry))
    circuit = P.circuit.get(runway, P.circuit['all'])
    use_drift = bool(P.params.get('use_drift', True))
    rc = reach(release_xy, release_agl, wind, glider, circuit, g.xy, lean=use_drift)
    in_reach = rc['in_reach'] & g.disc

    # direction history (tuned tau)
    tuned = _load_json(C.MODEL_DIR / 'tuned_params.json') or {}
    rule = tuned.get('direction') or {'tau': 30, 'method': 'oe'}
    tau = int(rule['tau'])
    oe, n_dir = B.direction_oe(P.T, train, runway, target['dir'], tau)
    best_sector = int(np.argmax(oe))

    # coverage verdict
    sec = B.sector_of(P.T.w100_dir)
    m = (P.T.runway.to_numpy() == runway) & (sec == B.sector_of(target['dir']))
    n_cov, k_cov = int(m.sum()), int(P.T.connected.to_numpy()[m].sum())
    verdict = 'no advice' if n_cov < 50 else ('thin' if n_cov < 150 or k_cov < 15 else 'reliable')

    # acceptance and evidence
    acc = _load_json(C.MODEL_DIR / 'acceptance.json') or {'decision': 'degraded'}
    report = _load_json(C.OUTPUT_DIR / 'prediction_power.json')

    # recommended lines only when accepted and data is not too thin
    lines = []
    if acc.get('decision') == 'full' and verdict != 'no advice':
        lines = RT.recommend(g, p_cell, fitted['lo'], fitted['hi'], pA * fitted['q'], release_xy, release_agl,
                             wind, glider, circuit, np.column_stack([rc['look_x'], rc['look_y']]), in_reach,
                             match_radius=P.params.get('match_radius'), use_drift=use_drift)
        for ln in lines:
            ln['n_launches'] = int((similar[:, None] & P.S[:, ln['cells']]).any(axis=1).sum())

    # ace routes for this runway
    ace = None
    try:
        lc = LC.load()
        cen = lc['route_centres'].get(runway)
        if cen is not None:
            mem = lc['route_members'][runway]
            pref = lc['ace_route'].get(runway)
            ace = []
            for k, vec in enumerate(cen):
                xs, ys = LC.unrotate(vec, runway, release_xy)
                mk = mem[mem.cluster == k]
                na = mk[~mk.ace]
                ace.append({'x': xs, 'y': ys, 'ace_share': float(mk.ace.mean()) if len(mk) else 0.0,
                            'n': int(len(mk)), 'preferred': k == pref,
                            'oe_non_aces': float(na.connected.sum() / na.pA.sum()) if len(na) and na.pA.sum() else np.nan})
            ace_summary = lc['summary']
        else:
            ace_summary = lc['summary']
    except FileNotFoundError:
        ace_summary = None

    return {
        'date': str(day), 'window': window, 'window_utc': [t_lo.hour + t_lo.minute / 60, t_hi.hour + t_hi.minute / 60],
        'runway': runway, 'glider': glider, 'dual': dual, 'made': datetime.now().isoformat(timespec='seconds'),
        'weather': {'w100_dir': float(f_mid.w100_dir), 'w100_spd': float(f_mid.w100_spd), 'wind10': wind10,
                    'blh': float(f_mid.blh), 'cloudbase': float(f_mid.cloudbase), 'cl_low': float(f_mid.cl_low),
                    'sw': float(f_mid.sw_now), 'cape': float(f_mid.cape)},
        'pA': pA, 'pA_hours': [(h.tz_localize('UTC').tz_convert(C.TZ_LOCAL).strftime('%H:%M'), float(p))
                               for h, p in zip(hours, pA_hours)],
        'pA_history': hist, 'release_xy': release_xy, 'release_agl': release_agl, 'wind_xy': wind,
        'grid': g, 'in_reach': in_reach, 'look_xy': np.column_stack([rc['look_x'], rc['look_y']]),
        'h_look': rc['h_look'], 'fitted': fitted, 'p_cell': p_cell, 'expected_hits': exp_hits,
        'searched_similar': searched_similar, 'hits_similar': hits_similar,
        'circuit': circuit, 'direction': {'tau': tau, 'oe': oe, 'n': n_dir, 'best': best_sector},
        'coverage': {'launches': n_cov, 'connects': k_cov, 'verdict': verdict},
        'acceptance': acc, 'report': report, 'lines': lines, 'ace_routes': ace, 'ace_summary': ace_summary,
        'n_similar': fitted.get('n_similar', {}),
    }


# ═══════════════════════════ rendering ═══════════════════════════

def _lift_color(v):
    """Diverging colour centred on 1.0 (blue below, red above)."""
    t = float(np.clip(np.log(max(v, 1e-3)) / np.log(2.0), -1, 1))
    if t >= 0:
        r, g, b = 255, int(245 - 180 * t), int(235 - 200 * t)
    else:
        r, g, b = int(245 + 200 * t), int(245 + 90 * t), 255
    return f'#{max(0, r):02x}{max(0, g):02x}{max(0, b):02x}'


def _latlon(x, y):
    lat, lon = C.to_latlon(x, y)
    return float(lat), float(lon)


def _reach_outline(grid, mask):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    Z = mask.reshape(grid.n, grid.n).astype(float).T     # rows = y, cols = x
    fig = plt.figure()
    cs = plt.contour(grid.c, grid.c, Z, levels=[0.5])
    segs = cs.allsegs[0]
    plt.close(fig)
    return [[_latlon(x, y) for x, y in seg] for seg in segs if len(seg) > 2]


def render_map(fc: dict, output_file: str):
    import folium
    from folium.plugins import HeatMap
    from runway_constants import RUNWAY_LINES

    g = fc['grid']
    fit = fc['fitted']
    full = fc['acceptance'].get('decision') == 'full'
    m = make_map(location=[C.LAT0, C.LON0], zoom_start=14, control_scale=True)

    # reach outline
    reach_fg = folium.FeatureGroup(name='Reach (searchable with circuit height left)')
    for seg in _reach_outline(g, fc['in_reach']):
        folium.PolyLine(seg, color='#333', weight=2, dash_array='6,6', opacity=0.8).add_to(reach_fg)
    reach_fg.add_to(m)

    # per-cell rate map (ground sources)
    half = g.cell / 2
    rate_fg = folium.FeatureGroup(name='Connect rate when searched here (lift)')
    for c in np.where(fc['in_reach'])[0]:
        x, y = g.xy[c]
        lift = float(fit['lift_today'][c])
        ev = float(fit['Es'][c])
        col = _lift_color(lift)
        folium.Rectangle(
            bounds=[_latlon(x - half, y - half), _latlon(x + half, y + half)],
            color=col, weight=0.3, fill=True, fill_color=col, fill_opacity=0.15 + 0.5 * min(1.0, ev / 5.0),
            popup=folium.Popup(
                f"<b>Cell</b><br>lift today: <b>{lift:.2f}</b> "
                f"(base {fit['lift'][c]:.2f}, 90% {fit['lo'][c]:.2f}–{fit['hi'][c]:.2f})<br>"
                f"first climbs from here: {fit['O'][c]:.0f} (smoothed {fit['Os'][c]:.1f})<br>"
                f"expected on equally good days: {fit['Es'][c]:.1f}<br>"
                f"launches that searched here in similar conditions: {fc['searched_similar'][c]:.0f} "
                f"(climbs {fc['hits_similar'][c]:.0f})<br>"
                f"chance per search today: {100 * fc['p_cell'][c]:.1f}%<br>"
                f"expected height when you get there: {fc['h_look'][c]:.0f} m", max_width=320)
        ).add_to(rate_fg)
    rate_fg.add_to(m)

    # where first thermals are most likely found today (expected hits), at search height
    eh = fc['expected_hits'] * fc['in_reach']
    if eh.max() > 0:
        pts = [[*_latlon(*fc['look_xy'][c]), float(eh[c] / eh.max())] for c in np.where(eh > 0.05 * eh.max())[0]]
        hm = folium.FeatureGroup(name='Where first thermals are usually found (this runway & wind)', show=not full)
        HeatMap(pts, radius=22, blur=18, min_opacity=0.25, max_zoom=16).add_to(hm)
        hm.add_to(m)

    # look-here points and lines (accepted model only)
    if full:
        look = folium.FeatureGroup(name='Look here at search height (top sources)')
        top = np.argsort(-np.where(fc['in_reach'], fc['p_cell'], -1))[:8]
        for k, c in enumerate(top):
            folium.CircleMarker(_latlon(*fc['look_xy'][c]), radius=6 + 30 * fc['p_cell'][c], color='#b23b2e',
                                fill=True, fill_opacity=0.6,
                                tooltip=f"#{k + 1}: {100 * fc['p_cell'][c]:.0f}% per search").add_to(look)
        look.add_to(m)
        lines_fg = folium.FeatureGroup(name='Recommended search lines')
        colors = ['#b23b2e', '#d9822b', '#7a5195']
        for k, ln in enumerate(fc['lines']):
            pts = [_latlon(x, y) for x, y in ln['waypoints']]
            folium.PolyLine(pts, color=colors[k % 3], weight=5 - k, opacity=0.9,
                            tooltip=f"Line {k + 1}: {100 * ln['p']:.0f}% ({100 * ln['p_lo']:.0f}–{100 * ln['p_hi']:.0f}%), "
                                    f"{ln['n_launches']} launches searched it in similar conditions").add_to(lines_fg)
            folium.Marker(pts[-1], icon=folium.DivIcon(html=f"<div style='font:bold 13px sans-serif;color:{colors[k % 3]}'>{k + 1}</div>")).add_to(lines_fg)
        lines_fg.add_to(m)

    # ace routes
    if fc['ace_routes']:
        ace_fg = folium.FeatureGroup(name="Typical first 30 s after release (aces' preferred route dashed red)", show=False)
        for r in fc['ace_routes']:
            pts = [_latlon(x, y) for x, y in zip(r['x'], r['y'])]
            folium.PolyLine(pts, color='#b23b2e' if r['preferred'] else '#555', weight=3,
                            dash_array='8,6' if r['preferred'] else None, opacity=0.8,
                            tooltip=f"{r['n']} launches, {100 * r['ace_share']:.0f}% by aces; non-aces on this route "
                                    f"connected {r['oe_non_aces']:.2f}× expected").add_to(ace_fg)
        ace_fg.add_to(m)

    # runways, release, circuit entry, wind
    rw_fg = folium.FeatureGroup(name='Runways and circuit entry')
    for rid, rd in RUNWAY_LINES.items():
        act = rid == fc['runway']
        folium.PolyLine([rd['start'], rd['end']], color=rd['color'], weight=4 if act else 2,
                        opacity=1 if act else 0.4, dash_array=None if act else '5,5', tooltip=rd['name']).add_to(rw_fg)
    folium.Marker(_latlon(*fc['release_xy']), tooltip=f"Typical release ({fc['release_agl']:.0f} m)",
                  icon=folium.Icon(color='green', icon='arrow-up', prefix='fa')).add_to(rw_fg)
    for x, y in fc['circuit']:
        folium.CircleMarker(_latlon(x, y), radius=3, color='#333', fill=True,
                            tooltip=f'Circuit entry point ({C.CIRCUIT_ENTRY_AGL:.0f} m)').add_to(rw_fg)
    rw_fg.add_to(m)
    wdir = fc['weather']['w100_dir']
    folium.Marker([C.LAT0 + 0.006, C.LON0 - 0.02], icon=folium.DivIcon(
        html=f"<div style='font-size:30px;color:#1f6f8b;transform:rotate({wdir}deg);text-shadow:0 0 3px white'>⬇</div>"),
        tooltip=f"Wind at 100 m: from {wdir:.0f}° at {fc['weather']['w100_spd']:.0f} km/h").add_to(m)

    m.get_root().html.add_child(folium.Element(_panel(fc, full)))
    folium.LayerControl(collapsed=True).add_to(m)
    m.save(output_file)
    return output_file


def _panel(fc, full):
    w, h, cov = fc['weather'], fc['pA_history'], fc['coverage']
    rep = fc['report'] or {}
    d = fc['direction']
    best = d['best']
    dir_rows = ''.join(
        f"<tr{' style=\"font-weight:600\"' if (k == best and full) else ''}><td>{SECTORS[k]}</td>"
        f"<td style='text-align:right'>{d['n'][k]}</td><td style='text-align:right'>{d['oe'][k]:.2f}</td></tr>"
        for k in [k for k in np.argsort(-d['oe']) if d['n'][k] >= 10][:4])
    hours = ' · '.join(f"{t} {100 * p:.0f}%" for t, p in fc['pA_hours'])
    ev = ''
    if rep:
        cc = rep.get('concordance', {}).get('hazard', {})
        dr = rep.get('direction', {})
        ev = (f"Map: thermal found in the best third of searched cells {100 * cc.get('best_third', np.nan):.0f}% "
              f"(chance 33%). Heading the best direction: RR {dr.get('rr', np.nan):.2f} "
              f"({dr.get('ci_day_bootstrap', [np.nan, np.nan])[0]:.2f}–{dr.get('ci_day_bootstrap', [np.nan, np.nan])[1]:.2f}). ")
    status = ('Model accepted by the evaluation: ranked spots and lines shown.' if full else
              'Evaluation did not accept ranked advice (plan 4.6): the map shows history and rates, not a recommendation.')
    if cov['verdict'] == 'no advice':
        status += ' Too few launches for this runway and wind: no spot-level advice.'
    lines_html = ''
    if fc['lines']:
        lines_html = '<br><b>Search lines</b><br>' + '<br>'.join(
            f"{k + 1}. {100 * ln['p']:.0f}% ({100 * ln['p_lo']:.0f}–{100 * ln['p_hi']:.0f}%), "
            f"{ln['n_launches']} launches" for k, ln in enumerate(fc['lines']))
    ace = fc['ace_summary']
    ace_html = ''
    if ace:
        pp = ace.get('pooled_place_test', {})
        ace_html = (f"<br><span style='color:#555'>Aces connect {ace['oe_aces']:.1f}× expected, others "
                    f"{ace['oe_others']:.1f}×. Non-aces on the aces' route: RR {pp.get('rr', np.nan):.2f} "
                    f"({pp.get('lo', np.nan):.2f}–{pp.get('hi', np.nan):.2f}).</span>")
    return f"""
    <div style="position:fixed;top:10px;left:56px;z-index:1000;background:rgba(255,255,255,.96);
      padding:12px 14px;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,.2);
      font:13px/1.45 -apple-system,Segoe UI,sans-serif;max-width:340px;max-height:92vh;overflow:auto;color:#1d2327">
      <b style="font-size:15px">First thermal after winch — EHHV</b><br>
      <span style="color:#555">{fc['date']} · {fc['window']} ({WINDOWS_LOCAL[fc['window']][0]}–{WINDOWS_LOCAL[fc['window']][1]} h local) ·
      runway {fc['runway']} · {'two-seater' if fc['glider'] == 'two_seat' else 'single-seater'}</span><br><br>
      <b>Chance to connect after release</b><br>
      <span style="font-size:26px;color:{'#2e7d4f' if fc['pA'] > h['club_rate'] else '#b23b2e'}">{100 * fc['pA']:.0f}%</span>
      <span style="color:#555">club average {100 * h['club_rate']:.0f}%</span><br>
      <span style="color:#555">Past launches with a similar forecast connected {100 * h['observed']:.0f}%
      (95% {100 * h['ci'][0]:.0f}–{100 * h['ci'][1]:.0f}%, n={h['n']}).</span><br>
      <span style="color:#555">By hour (local): {hours}</span><br><br>
      <b>Conditions</b><br>
      Wind 100 m: {w['w100_dir']:.0f}° {w['w100_spd']:.0f} km/h · 10 m: {w['wind10'][0]:.0f}° {w['wind10'][1]:.0f} km/h<br>
      Boundary layer {w['blh']:.0f} m · cloud base ≈ {w['cloudbase']:.0f} m · low cloud {w['cl_low']:.0f}% ·
      sun {w['sw']:.0f} W/m²<br>Release ≈ {fc['release_agl']:.0f} m AGL (runway median)<br><br>
      <b>Direction history</b> <span style="color:#555">(heading {d['tau']} s after release; same runway, wind ±45°; directions with ≥10 launches{'' if full else '; history only — not validated as advice'})</span>
      <table style="width:100%;font-size:12px"><tr><th style="text-align:left">dir</th><th style="text-align:right">launches</th>
      <th style="text-align:right">obs/exp</th></tr>{dir_rows}</table>
      {lines_html}<br>
      <b>Status</b><br><span style="color:#555">{status} Data for this runway & wind: {cov['launches']} launches,
      {cov['connects']} connected ({cov['verdict']}). {ev}</span>{ace_html}<br>
      <a href="prediction_power_report.html" target="_blank">Prediction-power report</a>
    </div>"""


# ═══════════════════════════ log ═══════════════════════════

def log_forecast(fc: dict, path=None):
    path = path or C.FORECAST_LOG
    top = np.argsort(-np.where(fc['in_reach'], fc['p_cell'], -1))[:10]
    entry = {
        'made': fc['made'], 'date': fc['date'], 'window': fc['window'], 'window_utc': fc['window_utc'],
        'runway': fc['runway'], 'glider': fc['glider'], 'pA': fc['pA'],
        'best_sector': fc['direction']['best'], 'tau': fc['direction']['tau'],
        'top_cells': [[int(c), float(fc['p_cell'][c])] for c in top],
        'lines': [{'p': ln['p'], 'waypoints_latlon': [_latlon(x, y) for x, y in ln['waypoints']]} for ln in fc['lines']],
        'decision': fc['acceptance'].get('decision'), 'detector_version': C.config_hash(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as f:
        f.write(json.dumps(entry) + '\n')
