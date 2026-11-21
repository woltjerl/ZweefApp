"""
Thermal briefing map: one clear page per day / time window / runway that answers
"where do I find the first thermal after release?".

On the map
- reach: where you can still search and get back to a circuit-entry point;
- heat: where first thermals were found from this runway in similar wind (other days);
- A / B / C: the three strongest first-thermal spots, with distance, direction, how many of the
  similar launches found their first climb there, typical height and time after release;
- airspace floors that matter near the field (TMA1 1500 ft to the west, TMA4 3500 ft overhead);
- optional layers: first thermals of the best pilots (aces) and your own first thermals.

The spots describe history ("this is where it worked most often"); the evaluation did not show
that steering to one spot raises the connect chance, so the page says so.
"""

import math
import re
from html import escape

import numpy as np
import pandas as pd

from src import config as C
from src.prediction import layer_c as LC
from src.prediction.first_thermal_map import SECTORS, WINDOWS_LOCAL

SPOT_RADIUS_M = 350.0       # first climbs counted for a spot
SPOT_SEPARATION_M = 700.0   # minimum distance between A, B and C
HEAT_SIGMA_M = 120.0        # light smoothing: keeps separate thermal sources apart on the 150 m grid
# heat classes (share of the strongest cell) → colour, opacity; one hue, light → dark
HEAT_CLASSES = [(0.80, '#b82b13', 0.82), (0.60, '#de5a2f', 0.74), (0.40, '#f28a55', 0.66),
                (0.20, '#f9b88a', 0.58), (0.07, '#fde0c5', 0.52)]
COL = {'spot': '#d03b3b', 'ace': '#eda100', 'me': '#2a78d6', 'reach': '#0b0b0b', 'air': '#c0392b'}


def _latlon(x, y):
    lat, lon = C.to_latlon(x, y)
    return float(lat), float(lon)


def _first_climbs(conn):
    return pd.read_sql_query("""
        SELECT c.flight_id, c.xc0 AS x, c.yc0 AS y, c.agl0, c.s_since_release, c.rate, l.pilot_id, l.datum
        FROM climbs c JOIN launches l ON l.flight_id = c.flight_id
        WHERE c.is_first = 1 AND l.launch_type = 'winch' AND l.launch_quality = 'ok' AND l.connected = 1""", conn)


def _pilot_ids(conn, name):
    if not name:
        return set()
    rows = conn.execute("SELECT DISTINCT gezagvoerder_id FROM flights WHERE gezagvoerder_naam LIKE ?",
                        (f"%{name}%",)).fetchall()
    return {r[0] for r in rows if r[0] is not None}


def _airspace_near(release_xy, radius_m=9000, max_floor_m=1300):
    """Always-active controlled/prohibited airspace near the release with a low floor."""
    from src.airspace import openair as OA
    from src.airspace import schedule
    path = C.AIRSPACE_FILE
    if not path.exists():
        return []
    out = []
    rx, ry = release_xy
    for a in OA.load(path):
        if a.cls.upper() not in ('A', 'B', 'C', 'D', 'P') or a.lower.amsl() > max_floor_m:
            continue
        if schedule.is_intermittent(a.name) or schedule.time_rule_for(a.name, a.lower.text):
            continue
        d = a.distance_to_edge(np.array([rx]), np.array([ry]))[0]
        inside = bool(a.contains(np.array([rx]), np.array([ry]))[0])
        if inside or d <= radius_m:
            out.append((a, inside, float(d)))
    return out


def _limit_text(lim):
    """'1500FT MSL' → '1500 ft (457 m)'; 'FL055' stays; SFC → 'surface'."""
    t = lim.text.upper().replace(' ', '')
    m = lim.amsl()
    if lim.kind == 'sfc':
        return 'surface'
    if t.startswith('FL'):
        return f"{lim.text} (~{m:.0f} m)"
    num = re.match(r'(\d+)', t)
    return f"{num.group(1)} ft ({m:.0f} m)" if num else lim.text


def _floor_text(a):
    """Floor of an airspace; for low areas you could climb over (ceiling below ~5000 ft) the ceiling too."""
    if a.upper.amsl() < 1500:
        return f"{_limit_text(a.lower)} to {_limit_text(a.upper)}"
    return f"from {_limit_text(a.lower)}"


def _short(name):
    return re.sub(r'\s*<[^>]*>', '', name).replace(' SPL', '').strip()


def _nearest_edge_point(a, px, py):
    x0, y0 = a.x, a.y
    x1, y1 = np.roll(a.x, -1), np.roll(a.y, -1)
    dx, dy = x1 - x0, y1 - y0
    t = np.clip(((px - x0) * dx + (py - y0) * dy) / np.maximum(dx * dx + dy * dy, 1e-9), 0, 1)
    qx, qy = x0 + t * dx, y0 + t * dy
    k = int(np.argmin(np.hypot(qx - px, qy - py)))
    return float(qx[k]), float(qy[k])


def build_briefing(conn, fc, me=None, exclude_day=True, me_ids=None):
    """
    Extra content for the briefing map on top of a forecast dict from first_thermal_map.build_forecast.

    me: your name (or part of it) in the logbook, looked up in the flights table; me_ids: your
    pilot ids directly (the hosted site's history pack has no names).
    """
    from src.prediction import layer_b as B
    P, _ = B.load()
    g = fc['grid']
    T = P.T
    day = fc['date']
    sim = (T.runway.to_numpy() == fc['runway']) & (np.asarray(C.angdiff(T.w100_dir, fc['weather']['w100_dir'])) <= 45)
    if exclude_day:
        sim &= T.datum.to_numpy() != day
    sim_ids = set(T.flight_id[sim])
    n_sim = int(sim.sum())
    n_conn = int(T.connected.to_numpy()[sim].sum())

    fcl = _first_climbs(conn)
    fcl = fcl[fcl.flight_id.isin(sim_ids)].copy()

    # heat: smoothed density of first climbs in similar conditions, limited to the reach
    idx = g.index(fcl.x.to_numpy(), fcl.y.to_numpy())
    counts = np.bincount(idx[idx >= 0], minlength=g.N).astype(float)
    heat = g.smooth(counts, HEAT_SIGMA_M)
    heat_r = np.where(fc['in_reach'], heat, 0.0)
    from scipy.ndimage import uniform_filter
    near_n = np.rint(uniform_filter(counts.reshape(g.n, g.n), size=3, mode='constant') * 9).ravel()

    # A / B / C: strongest local maxima, at least SPOT_SEPARATION_M apart
    spots = []
    h = heat_r.copy()
    rx, ry = fc['release_xy']
    labels = iter('ABC')
    while len(spots) < 3:
        k = int(np.argmax(h))
        if h[k] <= 0:
            break
        sx, sy = g.xy[k]
        near = fcl[np.hypot(fcl.x - sx, fcl.y - sy) <= SPOT_RADIUS_M]
        # centre the spot on the climbs themselves; keep spots apart after re-centring
        if len(near):
            sx, sy = float(near.x.median()), float(near.y.median())
        if len(near) == 0 or any(math.hypot(sx - o['x'], sy - o['y']) < SPOT_SEPARATION_M for o in spots):
            h[np.hypot(g.xy[:, 0] - g.xy[k, 0], g.xy[:, 1] - g.xy[k, 1]) < g.cell * 1.5] = 0
            continue
        near = fcl[np.hypot(fcl.x - sx, fcl.y - sy) <= SPOT_RADIUS_M]
        label = next(labels)
        dist = math.hypot(sx - rx, sy - ry)
        brg = float(C.bearing_deg(sx - rx, sy - ry))
        spots.append({'label': label, 'x': sx, 'y': sy, 'n': int(len(near)),
                      'share': len(near) / max(n_conn, 1), 'dist_m': dist, 'bearing': brg,
                      'agl0': float(near.agl0.median()), 't_s': float(near.s_since_release.median()),
                      'rate': float(near.rate.median()),
                      'h_arrive': float(fc['h_look'][k]) if np.isfinite(fc['h_look'][k]) else np.nan})
        h[np.hypot(g.xy[:, 0] - sx, g.xy[:, 1] - sy) < SPOT_SEPARATION_M] = 0

    # aces and me
    try:
        sk = LC.pilot_skill(LC.load_launches(conn))
        ace_ids, _ = LC.aces(sk)
    except Exception:  # noqa: BLE001
        ace_ids = set()
    aces = fcl[fcl.pilot_id.isin(ace_ids)]
    my_ids = set(me_ids) if me_ids else _pilot_ids(conn, me)
    mine = fcl[fcl.pilot_id.isin(my_ids)]
    my_launches = T[sim & T.pilot_id.isin(my_ids).to_numpy()] if my_ids else T.iloc[0:0]
    ace_launches = T[sim & T.pilot_id.isin(ace_ids).to_numpy()]

    return {'heat': heat_r, 'near_n': near_n, 'spots': spots, 'n_sim': n_sim, 'n_conn': n_conn, 'climbs': fcl,
            'aces': aces, 'mine': mine, 'me': me or bool(my_ids),
            'my_rate': (float(my_launches.connected.mean()), len(my_launches)) if len(my_launches) else None,
            'ace_rate': (float(ace_launches.connected.mean()), len(ace_launches)) if len(ace_launches) else None,
            'airspace': _airspace_near(fc['release_xy'])}


def render_briefing(fc, br, path):
    import folium
    from src.visualization.basemap import make_map

    g = fc['grid']
    rx, ry = fc['release_xy']
    m = make_map(list(_latlon(rx, ry)), 14, default='grijs', control_scale=True, zoom_snap=0.25, zoom_delta=0.5)

    # reach outline
    from src.prediction.first_thermal_map import _reach_outline
    reach_fg = folium.FeatureGroup(name='Reach (search and still make the circuit)')
    for seg in _reach_outline(g, fc['in_reach']):
        folium.PolyLine(seg, color=COL['reach'], weight=2, opacity=0.7, dash_array='2,6').add_to(reach_fg)
    reach_fg.add_to(m)

    # airspace floors
    air_fg = folium.FeatureGroup(name='Airspace floors near the field')
    for a, inside, d in br['airspace']:
        lat, lon = C.to_latlon(a.x, a.y)
        name = re.sub(r'\s*<[^>]*>', '', a.name)
        floor_ft = a.lower.text
        floor_m = a.lower.amsl()
        folium.Polygon(list(zip(lat, lon)), color=COL['air'], weight=2.5 if not inside else 1.5, fill=False,
                       dash_array='8,6', opacity=0.85,
                       tooltip=f"{escape(name)} (class {a.cls}) — floor {escape(floor_ft)}"
                               f"{'' if floor_m < 0 else f' = {floor_m:.0f} m'}").add_to(air_fg)
        if not inside:
            ex, ey = _nearest_edge_point(a, rx, ry)
            dx, dy = ex - rx, ey - ry
            dd = max(math.hypot(dx, dy), 1.0)
            ex, ey = ex + 250 * dx / dd, ey + 250 * dy / dd       # just outside, away from the release
            folium.Marker(_latlon(ex, ey), z_index_offset=1000, icon=folium.DivIcon(icon_size=(0, 0),
                html=f"<div style='font:600 11.5px system-ui;color:#fff;background:{COL['air']};padding:2px 7px;"
                     f"border-radius:4px;white-space:nowrap;display:inline-block;width:max-content;transform:translate(-50%,-50%);"
                     f"box-shadow:0 1px 3px rgba(0,0,0,.35)'>{escape(_short(a.name))} · {escape(_floor_text(a))}</div>")
                          ).add_to(air_fg)
    air_fg.add_to(m)

    # where first thermals were found: 150 m cells in five classes (share of the strongest cell)
    heat = br['heat']
    if heat.max() > 0:
        half = g.cell / 2
        feats = []
        for c in np.where(heat >= HEAT_CLASSES[-1][0] * heat.max())[0]:
            share = heat[c] / heat.max()
            col, op = next((cl, o) for t, cl, o in HEAT_CLASSES if share >= t)
            x, y = g.xy[c]
            ring = [_latlon(x - half, y - half), _latlon(x + half, y - half), _latlon(x + half, y + half),
                    _latlon(x - half, y + half), _latlon(x - half, y - half)]
            n = int(br['near_n'][c])
            feats.append({'type': 'Feature', 'properties': {
                'c': col, 'o': op,
                't': f"{n} first thermal{'s' if n != 1 else ''} within ~200 m · {100 * share:.0f}% of the hottest cell"},
                'geometry': {'type': 'Polygon', 'coordinates': [[[lo, la] for la, lo in ring]]}})
        folium.GeoJson({'type': 'FeatureCollection', 'features': feats},
                       name='Where first thermals were found (similar conditions)',
                       style_function=lambda f: {'fillColor': f['properties']['c'], 'fillOpacity': f['properties']['o'],
                                                 'stroke': False},
                       highlight_function=lambda f: {'stroke': True, 'color': '#0b0b0b', 'weight': 1.5},
                       tooltip=folium.GeoJsonTooltip(fields=['t'], labels=False)).add_to(m)

    # aces and me (switchable)
    if len(br['aces']):
        fg = folium.FeatureGroup(name=f"First thermals of the best pilots ({len(br['aces'])})", show=True)
        for r in br['aces'].itertuples():
            folium.RegularPolygonMarker(_latlon(r.x, r.y), number_of_sides=5, radius=6, rotation=-18,
                                        color='#7a5200', weight=1, fill=True, fill_color=COL['ace'], fill_opacity=0.95,
                                        tooltip=f"Ace, {r.datum}: climb at {r.agl0:.0f} m, {r.s_since_release:.0f} s "
                                                f"after release, {r.rate:.1f} m/s").add_to(fg)
        fg.add_to(m)
    if br['me'] and len(br['mine']):
        fg = folium.FeatureGroup(name=f"Your first thermals ({len(br['mine'])})", show=False)
        for r in br['mine'].itertuples():
            folium.CircleMarker(_latlon(r.x, r.y), radius=6, color='#fff', weight=2, fill=True, fill_color=COL['me'],
                                fill_opacity=1, tooltip=f"You, {r.datum}: climb at {r.agl0:.0f} m, "
                                                        f"{r.s_since_release:.0f} s after release").add_to(fg)
        fg.add_to(m)

    # A / B / C
    sp_fg = folium.FeatureGroup(name='Top first-thermal spots A / B / C')
    for s in br['spots']:
        ll = _latlon(s['x'], s['y'])
        folium.Circle(ll, radius=SPOT_RADIUS_M, color=COL['spot'], weight=2, fill=True, fill_opacity=0.06).add_to(sp_fg)
        folium.Marker(ll, icon=folium.DivIcon(icon_size=(0, 0),
            html=f"<div style='width:28px;height:28px;border-radius:50%;background:{COL['spot']};color:#fff;"
                 f"font:700 15px system-ui;display:flex;align-items:center;justify-content:center;"
                 f"border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.4);transform:translate(-50%,-50%)'>"
                 f"{s['label']}</div>"),
            tooltip=f"{s['label']}: {s['n']} first climbs here ({100 * s['share']:.0f}% of connections) · "
                    f"{s['dist_m'] / 1000:.1f} km {C.cardinal(s['bearing'])} of release · typically at "
                    f"{s['agl0']:.0f} m, {s['t_s']:.0f} s after release").add_to(sp_fg)
    sp_fg.add_to(m)

    # runway, release, circuit entry, wind
    from runway_constants import RUNWAY_LINES
    rd = RUNWAY_LINES[fc['runway']]
    folium.PolyLine([rd['start'], rd['end']], color='#0b0b0b', weight=5, opacity=0.9, tooltip=rd['name']).add_to(m)
    folium.Marker(_latlon(rx, ry), icon=folium.DivIcon(icon_size=(0, 0),
        html="<div style='font:700 11px system-ui;color:#fff;background:#0b0b0b;padding:2px 6px;border-radius:4px;"
             "white-space:nowrap;display:inline-block;width:max-content;transform:translate(-50%,-140%)'>release</div>")).add_to(m)
    folium.CircleMarker(_latlon(rx, ry), radius=5, color='#0b0b0b', fill=True, fill_opacity=1).add_to(m)

    # fit to reach + spots
    xs = np.r_[g.xy[fc['in_reach'], 0], [s['x'] for s in br['spots']], rx]
    ys = np.r_[g.xy[fc['in_reach'], 1], [s['y'] for s in br['spots']], ry]
    (la0, lo0), (la1, lo1) = _latlon(xs.min(), ys.min()), _latlon(xs.max(), ys.max())
    # one fit, not animated (an animated fit would swallow a second one): beside the card on wide
    # screens, above the bottom sheet on phones
    m.get_root().html.add_child(folium.Element(
        f"<script>document.addEventListener('DOMContentLoaded', function () {{"
        f" var c = document.getElementById('tb-card'), phone = window.innerWidth <= 700;"
        f" {m.get_name()}.fitBounds([[{la0}, {lo0}], [{la1}, {lo1}]], phone"
        f" ? {{paddingTopLeft: [10, 50], paddingBottomRight: [10, (c ? c.offsetHeight : 0) + 10], animate: false}}"
        f" : {{paddingTopLeft: [400, 20], paddingBottomRight: [20, 20], animate: false}}); }});</script>"))

    m.get_root().html.add_child(folium.Element(_card(fc, br)))
    folium.LayerControl(collapsed=True, position='topright').add_to(m)
    m.save(str(path))
    return path


def _card(fc, br):
    w = fc['weather']
    hist = fc['pA_history']
    hours = fc['pA_hours']
    pct = [round(100 * p) for _, p in hours]
    best_set = {t for (t, _), q in zip(hours, pct) if q >= max(pct) - 1} if hours else set()
    if not hours:
        best_line = ''
    elif len(best_set) == len(hours):
        best_line = 'Every hour about the same'
    else:
        best_line = 'Best: <b>' + ', '.join(t for t, _ in hours if t in best_set) + '</b>'
    wdir = w['w100_dir']
    wind_from = SECTORS[int(((wdir + 22.5) % 360) // 45)]
    day = pd.Timestamp(fc['date'])
    lo_h, hi_h = WINDOWS_LOCAL[fc['window']]
    bars = ''.join(
        f"<div style='display:flex;flex-direction:column;align-items:center;gap:2px;flex:1'>"
        f"<div style='height:44px;display:flex;align-items:flex-end'><div style='width:18px;height:{min(44, max(2, 88 * p)):.0f}px;"
        f"background:{'#0ca30c' if t in best_set and len(best_set) < len(hours) else '#2a78d6'};border-radius:3px 3px 0 0'></div></div>"
        f"<div style='font-size:10.5px;color:#52514e'>{t}</div><div style='font-size:11px;font-weight:600'>{100 * p:.0f}%</div></div>"
        for t, p in hours)
    rows = []
    for s in br['spots']:
        arrive = '' if not np.isfinite(s['h_arrive']) else f" · you arrive at ~{s['h_arrive']:.0f} m"
        rows.append(
            f"<tr><td style='vertical-align:top;padding:3px 8px 3px 0'><span style='display:inline-flex;width:22px;"
            f"height:22px;border-radius:50%;background:#d03b3b;color:#fff;font-weight:700;align-items:center;"
            f"justify-content:center'>{s['label']}</span></td><td style='padding:3px 0'>"
            f"<b>{s['dist_m'] / 1000:.1f} km {C.cardinal(s['bearing'])}</b> of release · {s['n']} first climbs "
            f"({100 * s['share']:.0f}%)<br><span style='color:#52514e'>typically at {s['agl0']:.0f} m, "
            f"{s['t_s'] / 60:.1f} min after release, {s['rate']:.1f} m/s{arrive}</span></td></tr>")
    spots_html = (f"<table style='margin-top:4px;font-size:12.5px'>{''.join(rows)}</table>" if rows else
                  "<div style='margin-top:4px;color:#52514e'>Too few first thermals from this runway in this wind "
                  "to mark spots.</div>")
    air = []
    for a, inside, d in br['airspace']:
        name = re.sub(r'\s*<[^>]*>', '', a.name)
        fm = a.lower.amsl()
        if inside:
            air.append(f"<b>{escape(_short(a.name))}</b> overhead {escape(_floor_text(a))}")
        elif d <= 6000:
            where = C.cardinal(C.bearing_deg(*(np.subtract(_nearest_edge_point(a, *fc['release_xy']), fc['release_xy']))))
            kind = {'P': ' (prohibited)', 'R': ' (restricted)'}.get((a.cls or '').upper(), '')
            air.append(f"<b>{escape(_short(a.name))}</b>{kind} {d / 1000:.1f} km {where}, {escape(_floor_text(a))}")
    mine = ''
    if br['me'] and br['my_rate']:
        r, n = br['my_rate']
        ar = br['ace_rate']
        mine = (f"<div style='margin-top:8px;color:#52514e'>In these conditions you connected on <b>{100 * r:.0f}%</b> of "
                f"{n} launches" + (f"; the best pilots on {100 * ar[0]:.0f}% of {ar[1]}" if ar else '') + ".</div>")
    ramp = ''.join(f"<span style='width:11px;height:10px;background:{cl};opacity:{o + .1:.2f}'></span>"
                   for _, cl, o in reversed(HEAT_CLASSES))
    you = (" · <span style='color:#2a78d6'>●</span> you (layer menu)" if br['me'] and len(br['mine']) else '')
    return f"""
<style>
.leaflet-top.leaflet-left{{top:auto;bottom:34px;left:auto;right:10px}}
.leaflet-bottom{{bottom:var(--sheet,0px)}}
.tb-card{{position:fixed;top:10px;left:10px;z-index:1000;width:360px;max-height:calc(100% - 20px);overflow:auto;
  box-sizing:border-box;background:rgba(255,255,255,.97);border-radius:10px;box-shadow:0 2px 12px rgba(0,0,0,.25);
  font:13px/1.45 system-ui,-apple-system,'Segoe UI',sans-serif;color:#0b0b0b;padding:14px 16px}}
.tb-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}}
.tb-toggle{{border:1px solid #d6d5ce;background:#fff;border-radius:6px;font:600 12px system-ui;padding:3px 9px;
  cursor:pointer;color:#52514e;flex:none}}
.tb-mini{{display:none;color:#52514e;font-weight:400}}
.tb-card.tb-min .tb-body{{display:none}}
.tb-card.tb-min .tb-mini{{display:inline}}
.tb-sec{{margin-top:10px;padding-top:8px;border-top:1px solid #e1e0d9}}
@media (max-width:700px){{
  .tb-card{{top:auto;bottom:0;left:0;right:0;width:auto;max-height:50%;border-radius:12px 12px 0 0;padding:10px 14px 12px}}
  .leaflet-top.leaflet-left{{display:none}}
}}
</style>
<div class="tb-card" id="tb-card">
  <div class="tb-head"><div style="font-size:16px;font-weight:700">Thermal briefing
    <span class="tb-mini">· {100 * fc['pA']:.0f}% · rwy {fc['runway']} · {lo_h}–{hi_h} h</span></div>
    <button class="tb-toggle" id="tb-toggle" type="button">Hide</button></div>
  <div class="tb-body">
  <div style="color:#52514e">{day:%a %d %b %Y} · {lo_h}–{hi_h} h · runway {fc['runway']}<br>release ≈ {fc['release_agl']:.0f} m above the field</div>
  <div style="display:flex;align-items:baseline;gap:10px;margin:10px 0 2px">
    <span style="font-size:34px;font-weight:700;color:{'#0ca30c' if fc['pA'] > hist['club_rate'] else '#d03b3b'}">{100 * fc['pA']:.0f}%</span>
    <span>chance to connect<br><span style="color:#52514e">club average {100 * hist['club_rate']:.0f}%</span></span></div>
  <div style="display:flex;gap:4px;margin:6px 0 2px">{bars}</div>
  <div style="color:#52514e;font-size:12px">{best_line}</div>
  <div class="tb-sec">
    <span style="display:inline-block;transform:rotate({(wdir + 180) % 360:.0f}deg);font-size:16px;line-height:1;color:#2a78d6">&#8593;</span>
    Wind <b>{wind_from} {w['w100_spd']:.0f} km/h</b> (100 m) · cloud base ≈ <b>{w['cloudbase']:.0f} m</b> ·
    thermals to ≈ {w['blh']:.0f} m · low cloud {w['cl_low']:.0f}%</div>
  <div class="tb-sec">
    <b>Where the first thermal was found</b><br>
    <span style="color:#52514e;font-size:12px">runway {fc['runway']}, wind {wind_from} ±45°: {br['n_sim']} launches, {br['n_conn']} connected</span>
    {spots_html}</div>
  {('<div class="tb-sec"><b style="color:#c0392b">Watch the airspace</b><br>' + '<br>'.join(air) + '</div>') if air else ''}
  {mine}
  <div class="tb-sec" style="font-size:11.5px;color:#52514e">
    <span style="display:inline-flex;vertical-align:-1px">{ramp}</span> first thermals found: few → most ·
    <span style="color:#eda100">★</span> best pilots{you} ·
    dotted line = reach with circuit height left · red dashed = airspace.<br>
    Spots are history: where it worked most often in this wind, not a guarantee.</div>
  </div>
</div>
<script>
(function () {{
  var c = document.getElementById('tb-card'), b = document.getElementById('tb-toggle');
  function sheet() {{
    document.documentElement.style.setProperty('--sheet', window.innerWidth <= 700 ? c.offsetHeight + 'px' : '0px');
  }}
  b.addEventListener('click', function () {{
    c.classList.toggle('tb-min');
    b.textContent = c.classList.contains('tb-min') ? 'Show' : 'Hide';
    sheet();
  }});
  window.addEventListener('resize', sheet);
  window.addEventListener('load', sheet);
}})();
</script>"""
