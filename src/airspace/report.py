"""
Visualisation of airspace violations: overview map, ceiling map, charts, statistics
(per month, weekday, launch method, airspace), per-flight pages (track + barogram
with the airspace floor), the overview report (no names) and a separate details
report with every event and the pilots involved.

`region` everywhere is (centre_x, centre_y, radius) in metres from the EHHV field
centre; radius None means no limit.
"""

import base64
import io
import re
import time
from html import escape

import numpy as np
import pandas as pd

from src import config as C
from src.airspace import schedule

# ── palette (dataviz reference palette: status colours for event states, categorical for airspace) ──
STATUS = {'violation': '#d03b3b', 'possible': '#ec835a', 'unverified': '#fab219', 'exempt': '#0ca30c'}
STATUS_LABEL = {'violation': 'Violation',
                'possible': 'Possible (clearance on request, or military airspace on a day off)',
                'unverified': 'Unverified (activated by NOTAM: check that day)',
                'exempt': 'Inside a gliding sector (not counted)'}
CLASS_GROUP_COLOR = {'A': '#2a78d6', 'B': '#6250d6', 'C': '#6250d6', 'D': '#6250d6', 'E': '#6250d6',
                     'P': '#eb6834', 'R': '#eb6834', 'GSEC': '#1baf7a'}
INK, INK2, MUTED, GRID, AXIS, SURFACE = '#0b0b0b', '#52514e', '#898781', '#e1e0d9', '#c3c2b7', '#fcfcfb'
BLUE_RAMP = ['#0d366b', '#104281', '#184f95', '#1c5cab', '#256abf', '#2a78d6', '#3987e5', '#5598e7',
             '#6da7ec', '#86b6ef', '#9ec5f4', '#b7d3f6', '#cde2fb']
LANDMARKS = {'EHHV': (52.1917, 5.1469), 'Schiphol': (52.3086, 4.7639), 'Utrecht': (52.0907, 5.1214),
             'Amersfoort': (52.1561, 5.3878), 'Lelystad': (52.4603, 5.5272), 'Almere': (52.3508, 5.2647),
             'Deelen': (52.0606, 5.8731), 'Apeldoorn': (52.2112, 5.9699), 'Ede': (52.0402, 5.6649)}
WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def _plt():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.family': 'sans-serif', 'font.size': 9, 'axes.edgecolor': AXIS,
                         'axes.labelcolor': INK2, 'xtick.color': MUTED, 'ytick.color': MUTED,
                         'axes.spines.top': False, 'axes.spines.right': False, 'figure.facecolor': SURFACE,
                         'axes.facecolor': SURFACE, 'savefig.facecolor': SURFACE})
    return plt


def _img(fig, alt):
    plt = _plt()
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=130, bbox_inches='tight')
    plt.close(fig)
    return (f'<img alt="{escape(alt)}" src="data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}" '
            f'style="max-width:100%;height:auto">')


def short(name):
    """Airspace name without tags like <sup 3/2022>."""
    return re.sub(r'\s*<[^>]*>', '', str(name)).strip()


def _latlon(x, y):
    lat, lon = C.to_latlon(x, y)
    return float(lat), float(lon)


def in_region(a, region):
    """Does an airspace's bounding box touch the circle?"""
    cx, cy, r = region
    if r is None:
        return True
    x0, y0, x1, y1 = a.bbox
    px, py = min(max(cx, x0), x1), min(max(cy, y0), y1)
    return np.hypot(px - cx, py - cy) <= r


def region_text(region):
    cx, cy, r = region
    if r is None:
        return 'everywhere'
    lat, lon = _latlon(cx, cy)
    where = 'around EHHV' if np.hypot(cx, cy) < 500 else (
        f"centred {np.hypot(cx, cy) / 1000:.0f} km {C.cardinal(C.bearing_deg(cx, cy))} of EHHV ({lat:.3f} N, {lon:.3f} E)")
    return f"{r / 1000:.0f} km circle {where}"


# ═══════════════════════════ ceiling map ═══════════════════════════

FLOOR_BINS_FT = [0, 1000, 1500, 2000, 2500, 3000, 3500, 4500, 5500, 6500]


def ceiling_grid(airspaces, region, step_m=400):
    """Lowest floor (ft MSL, standard pressure for FL) of always-active checked airspace per grid cell."""
    cx, cy, r = region
    r = r or 60000
    xs = np.arange(cx - r, cx + r + step_m, step_m)
    ys = np.arange(cy - r, cy + r + step_m, step_m)
    X, Y = np.meshgrid(xs, ys)
    floor = np.full(X.shape, np.inf)
    px, py = X.ravel(), Y.ravel()
    for a in airspaces:
        if not in_region(a, (cx, cy, r)):
            continue
        f_ft = max(a.lower.amsl(), 0.0) / 0.3048       # surface-based airspace: floor 0 ft
        inside = a.contains(px, py).reshape(X.shape)
        floor = np.where(inside, np.minimum(floor, f_ft), floor)
    floor[np.hypot(X - cx, Y - cy) > r] = np.nan
    return xs, ys, floor


def fig_ceiling(always, dashed, events, region):
    plt = _plt()
    import matplotlib.patheffects as pe
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.patches import Circle
    cx, cy, r = region
    r = r or 60000
    xs, ys, floor = ceiling_grid(always, (cx, cy, r))
    bins = FLOOR_BINS_FT + [99999]
    cmap = ListedColormap(BLUE_RAMP[:len(bins) - 1])
    cmap.set_bad(SURFACE)
    norm = BoundaryNorm(bins, cmap.N)
    shown = np.where(np.isinf(floor), np.nan, floor)
    fig, ax = plt.subplots(figsize=(7.6, 7.2))
    mesh = ax.pcolormesh(xs / 1000, ys / 1000, np.ma.masked_invalid(shown), cmap=cmap, norm=norm, shading='nearest')
    ax.contourf(xs / 1000, ys / 1000, np.isinf(floor).astype(float), levels=[0.5, 1.5], colors=['#f0efec'])
    clip = Circle((cx / 1000, cy / 1000), r / 1000, transform=ax.transData)
    for a in dashed:
        if in_region(a, (cx, cy, r)):
            ln, = ax.plot(np.r_[a.x, a.x[0]] / 1000, np.r_[a.y, a.y[0]] / 1000, color=INK2, lw=0.8, ls=(0, (4, 3)))
            ln.set_clip_path(clip)
    th = np.linspace(0, 2 * np.pi, 200)
    ax.plot(cx / 1000 + r / 1000 * np.sin(th), cy / 1000 + r / 1000 * np.cos(th), color=AXIS, lw=1)
    if len(events):
        for cat in ('unverified', 'possible', 'violation'):
            e = events[events.category == cat]
            if len(e):
                ax.scatter(e.entry_x / 1000, e.entry_y / 1000, s=16, color=STATUS[cat], edgecolor=SURFACE,
                           linewidth=0.8, zorder=5, label=f"{STATUS_LABEL[cat]} ({len(e)})")
    halo = [pe.withStroke(linewidth=2.5, foreground=SURFACE)]
    for name, (la, lo) in LANDMARKS.items():
        x, y = C.to_xy(la, lo)
        if np.hypot(x - cx, y - cy) <= r:
            ax.plot(x / 1000, y / 1000, marker='^' if name == 'EHHV' else 'o', color=INK,
                    ms=7 if name == 'EHHV' else 3, markeredgecolor=SURFACE, zorder=6)
            ax.annotate(name, (x / 1000, y / 1000), xytext=(5, 4), textcoords='offset points', fontsize=8.5,
                        color=INK, fontweight='bold' if name == 'EHHV' else 'normal', path_effects=halo, zorder=7)
    ax.set_xlim((cx - 1.04 * r) / 1000, (cx + 1.04 * r) / 1000)
    ax.set_ylim((cy - 1.04 * r) / 1000, (cy + 1.04 * r) / 1000)
    cb = fig.colorbar(mesh, ax=ax, shrink=0.75, pad=0.02, ticks=FLOOR_BINS_FT)
    cb.set_label('lowest floor of always-active airspace (ft MSL)')
    cb.ax.set_yticklabels(['SFC'] + [f"{b:,}" for b in FLOOR_BINS_FT[1:]])
    ax.set_xlabel('km east of EHHV')
    ax.set_ylabel('km north of EHHV')
    ax.set_aspect('equal')
    ax.set_title('How high you can go: floor of always-active controlled, prohibited and restricted airspace\n'
                 'light = none below FL095 · dashed = only active part of the time', fontsize=9.5, color=INK)
    if len(events):
        ax.legend(loc='lower left', fontsize=7.2, frameon=True, facecolor=SURFACE, edgecolor=GRID)
    return fig


# ═══════════════════════════ charts ═══════════════════════════

def _bars(ax, labels, values, color='#2a78d6'):
    ax.bar(range(len(values)), values, color=color, width=0.7)
    ax.set_xticks(range(len(values)))
    ax.set_xticklabels(labels)
    ax.yaxis.grid(True, color=GRID, lw=0.6)
    ax.set_axisbelow(True)


def fig_monthly(ok):
    plt = _plt()
    t = ok.groupby('month').agg(flights=('flight_id', 'size'), viol=('violated', 'sum'))
    t = t[t.flights >= 20]
    fig, ax = plt.subplots(figsize=(9, 3.0))
    _bars(ax, [m[2:].replace('-', '/') for m in t.index], 100 * t.viol / t.flights)
    ax.tick_params(axis='x', rotation=90, labelsize=7.5)
    ax.set_ylabel('% of flights with a violation')
    ax.set_title('Share of flights with a violation per month (months with ≥ 20 flights)', fontsize=9.5,
                 color=INK, loc='left')
    return fig


def fig_weekday(ok):
    plt = _plt()
    t = ok.groupby('weekday').agg(flights=('flight_id', 'size'), viol=('violated', 'sum')).reindex(range(7), fill_value=0)
    share = np.where(t.flights > 0, 100 * t.viol / t.flights.replace(0, np.nan), 0)
    fig, ax = plt.subplots(figsize=(6.2, 2.9))
    _bars(ax, WEEKDAYS, share)
    for k, (v, n) in enumerate(zip(t.viol, t.flights)):
        ax.text(k, share[k], f"{int(v)}/{int(n)}", ha='center', va='bottom', fontsize=7.5, color=INK2)
    ax.set_ylabel('% of flights with a violation')
    ax.set_title('Share of flights with a violation per weekday (label: violating / flown)', fontsize=9.5,
                 color=INK, loc='left')
    return fig


def fig_by_airspace(events, top=12):
    plt = _plt()
    t = events.assign(airspace=events.airspace.map(short)).groupby(['airspace', 'category']).size().unstack(fill_value=0)
    cats = [c for c in schedule.CATEGORIES if c in t and t[c].sum() > 0]
    t = t.assign(total=t[cats].sum(axis=1)).sort_values('total').tail(top)
    fig, ax = plt.subplots(figsize=(7.5, 0.32 * len(t) + 1.0))
    left = np.zeros(len(t))
    for c in cats:
        ax.barh(range(len(t)), t[c], left=left, color=STATUS[c], edgecolor=SURFACE, linewidth=1.5, height=0.7,
                label=STATUS_LABEL[c])
        left += t[c].to_numpy()
    for k, v in enumerate(t.total):
        ax.text(v, k, f" {v}", va='center', fontsize=8, color=INK2)
    ax.set_yticks(range(len(t)))
    ax.set_yticklabels(t.index, fontsize=8)
    ax.set_xlabel('events')
    ax.xaxis.grid(True, color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    ax.legend(fontsize=7.2, loc='lower right', frameon=False)
    return fig


def fig_severity(events):
    plt = _plt()
    e = events[events.max_above_lower_m.notna() & events.category.isin(['violation', 'possible', 'unverified'])]
    fig, ax = plt.subplots(figsize=(6.5, 4.0))
    for c in ('unverified', 'possible', 'violation'):
        d = e[e.category == c]
        if len(d):
            ax.scatter(d.duration_s / 60, d.max_above_lower_m, s=14, color=STATUS[c], edgecolor=SURFACE,
                       linewidth=0.6, alpha=0.85, label=f"{STATUS_LABEL[c]} ({len(d)})")
    ax.set_xscale('log')
    ax.set_xlabel('time inside (minutes, log scale)')
    ax.set_ylabel('highest point above the airspace floor (m)')
    ax.grid(True, color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    ax.legend(fontsize=7.2, frameon=False, loc='upper left')
    ax.set_title('Severity: brief nudges (bottom left) vs. serious busts (top right)', fontsize=9.5, color=INK,
                 loc='left')
    return fig


def fig_barogram(t_local, alt, events):
    plt = _plt()
    import matplotlib.dates as mdates
    import matplotlib.patheffects as pe
    fig, ax = plt.subplots(figsize=(9, 2.8))
    ax.plot(t_local, alt, color='#2a78d6', lw=1.2)
    for e in events.itertuples():
        t0 = pd.Timestamp(e.start_ts)
        ax.axvspan(t0, t0 + pd.Timedelta(seconds=e.duration_s), color=STATUS[e.category], alpha=0.18, lw=0)
    halo = [pe.withStroke(linewidth=2.5, foreground=SURFACE)]
    if len(events):
        for k, (name, g) in enumerate(events.groupby('airspace', sort=False)):
            lower = float(g.lower_m.iloc[0])
            if not np.isfinite(lower) or lower < -1e6:
                continue
            t0 = pd.Timestamp(g.start_ts.min()) - pd.Timedelta(minutes=4)
            t1 = max(pd.Timestamp(a) + pd.Timedelta(seconds=d) for a, d in zip(g.start_ts, g.duration_s))
            t1 += pd.Timedelta(minutes=4)
            ax.hlines(lower, t0, t1, color=STATUS[g.category.iloc[0]], lw=1.6, ls=(0, (4, 2)))
            ax.annotate(f"{short(name)} floor {g.lower_text.iloc[0]}", (t1, lower), xytext=(4, -3 + 9 * (k % 2)),
                        textcoords='offset points', fontsize=7.5, color=INK2, path_effects=halo)
    ax.set_ylabel('m AMSL (GPS)')
    ax.yaxis.grid(True, color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax.set_title('Barogram (local time) · shaded = time inside the airspace · dashed = its floor',
                 fontsize=9, color=INK, loc='left')
    return fig


# ═══════════════════════════ maps (same base map as the thermal finder) ═══════════════════════════

def _base_map(zoom=10, location=None, scale=True):
    from src.visualization.basemap import make_map
    return make_map(location or [C.LAT0, C.LON0], zoom, control_scale=scale)


def _airspace_layer(folium, airspaces, region, part_time_pattern, intermittent_pattern, checked_names):
    fg = folium.FeatureGroup(name='Airspace within the circle')
    for a in airspaces:
        if (a.name not in checked_names and a.cls != 'GSEC') or not in_region(a, region):
            continue
        lat, lon = C.to_latlon(a.x, a.y)
        state = ('only active when booked (NOTAM)' if schedule.is_intermittent(a.name, intermittent_pattern) else
                 'military: weekdays' if schedule.is_part_time(a.name, part_time_pattern) else '')
        folium.Polygon(list(zip(lat, lon)), color=CLASS_GROUP_COLOR.get(a.cls, MUTED), weight=1.4, fill=True,
                       fill_opacity=0.04, dash_array='6,5' if state else None,
                       tooltip=(f"<b>{escape(short(a.name))}</b> · class {a.cls}<br>{escape(a.lower.text)} – "
                                f"{escape(a.upper.text)}" + (f"<br>{state}" if state else ''))).add_to(fg)
    return fg


def _legend_html():
    rows = ''.join(f"<div><span style='display:inline-block;width:18px;height:4px;background:{STATUS[c]};"
                   f"vertical-align:middle;margin-right:6px'></span>{STATUS_LABEL[c]}</div>" for c in schedule.CATEGORIES)
    line = ("<div><span style='display:inline-block;width:18px;border-top:2px {s} {c};vertical-align:middle;"
            "margin-right:6px'></span>{t}</div>")
    rows += "<div style='margin-top:4px'></div>"
    rows += line.format(s='solid', c='#2a78d6', t='Class A') + line.format(s='solid', c='#6250d6', t='Class B/C/D')
    rows += line.format(s='solid', c='#eb6834', t='Prohibited / restricted')
    rows += line.format(s='dashed', c='#555', t='Only active part of the time')
    return (f"<div style='position:fixed;bottom:24px;left:12px;z-index:1000;background:rgba(255,255,255,.95);"
            f"padding:8px 10px;border-radius:6px;font:12px/1.5 system-ui,sans-serif;color:#0b0b0b;"
            f"box-shadow:0 1px 6px rgba(0,0,0,.2)'>{rows}</div>")


def overview_map(airspaces, events, region, part_time_pattern, intermittent_pattern, checked_names, path, title=None):
    import folium
    from folium.plugins import HeatMap
    cx, cy, r = region
    m = _base_map(10, list(_latlon(cx, cy)))
    _airspace_layer(folium, airspaces, region, part_time_pattern, intermittent_pattern, checked_names).add_to(m)
    if r:
        folium.Circle(list(_latlon(cx, cy)), radius=r, color=INK2, weight=1.5, fill=False, dash_array='3,6',
                      tooltip=region_text(region)).add_to(m)
    folium.Marker([C.LAT0, C.LON0], tooltip='EHHV', icon=folium.Icon(color='green', icon='plane', prefix='fa')).add_to(m)
    for cat in schedule.CATEGORIES:
        e = events[events.category == cat] if len(events) else events
        fg = folium.FeatureGroup(name=f"{STATUS_LABEL[cat]} ({len(e)})", show=cat in ('violation', 'possible'))
        for rw in e.itertuples():
            pts = rw.path if isinstance(rw.path, list) and len(rw.path) >= 2 else [[rw.entry_lat, rw.entry_lon]] * 2
            popup = (f"<b>{escape(short(rw.airspace))}</b> ({rw.cls}) · {STATUS_LABEL[cat]}<br>{rw.datum} "
                     f"{rw.start_local} local · {escape(str(rw.registration))} · {escape(str(rw.launch_method))}<br>"
                     f"{rw.duration_s:.0f} s inside, max {rw.max_alt_m:.0f} m AMSL"
                     + (f", {rw.max_above_lower_m:.0f} m above the floor ({escape(rw.lower_text)})"
                        if pd.notna(rw.max_above_lower_m) else '')
                     + f"<br><a href='airspace_maps/flight_{rw.flight_id}.html' target='_blank'>"
                       f"flight {rw.flight_id}: track & barogram</a>")
            folium.PolyLine(pts, color=STATUS[cat], weight=4, opacity=0.85,
                            popup=folium.Popup(popup, max_width=320)).add_to(fg)
            folium.CircleMarker(pts[0], radius=3, color=STATUS[cat], fill=True, fill_opacity=1).add_to(fg)
        fg.add_to(m)
    v = events[events.category == 'violation'] if len(events) else events
    heat = [p for rw in v.itertuples() if isinstance(rw.path, list) for p in rw.path]
    if heat:
        hm = folium.FeatureGroup(name='Density of violation track (heat)', show=False)
        HeatMap(heat, radius=12, blur=15, min_opacity=0.3).add_to(hm)
        hm.add_to(m)
    m.get_root().html.add_child(folium.Element(_legend_html()))
    if title:
        m.get_root().html.add_child(folium.Element(
            f"<div style='position:fixed;top:12px;left:56px;z-index:1000;background:rgba(255,255,255,.95);"
            f"padding:6px 12px;border-radius:6px;font:600 15px system-ui,sans-serif;color:#0b0b0b;"
            f"box-shadow:0 1px 6px rgba(0,0,0,.2)'>{escape(title)}</div>"))
    folium.LayerControl(collapsed=False).add_to(m)
    m.save(str(path))


def flight_page(conn, flight_id, airspaces, events, region, part_time_pattern, intermittent_pattern, checked_names,
                path):
    import folium
    from src.thermal_detection.climb_detector import parse_times
    g = pd.read_sql_query(f"SELECT time, latitude, longitude, altitude FROM igc_data WHERE flight_id={int(flight_id)} "
                          f"ORDER BY sequence", conn)
    info = conn.execute("SELECT datum, registratie, type, start_methode, start_tijd, landings_tijd FROM flights "
                        "WHERE id=?", (int(flight_id),)).fetchone()
    m = _base_map(11)
    _airspace_layer(folium, airspaces, region, part_time_pattern, intermittent_pattern, checked_names).add_to(m)
    cx, cy, r = region
    if r:
        folium.Circle(list(_latlon(cx, cy)), radius=r, color=INK2, weight=1, fill=False, dash_array='3,6').add_to(m)
    ev = events[events.flight_id == flight_id] if len(events) else events
    if len(g) and info:
        datum = info[0][:10]
        folium.PolyLine(list(zip(g.latitude, g.longitude)), color='#2a78d6', weight=2, opacity=0.8,
                        tooltip=f'flight {flight_id}').add_to(m)
        for rw in ev.itertuples():
            if isinstance(rw.path, list) and len(rw.path) >= 2:
                folium.PolyLine(rw.path, color=STATUS[rw.category], weight=6, opacity=0.9,
                                tooltip=f"{short(rw.airspace)}: {rw.duration_s:.0f} s from {rw.start_local} "
                                        f"({STATUS_LABEL[rw.category]})").add_to(m)
        t = parse_times(g.time)
        t = t + np.concatenate([[0], np.cumsum(np.where(np.diff(t) < -43200, 86400, 0))])
        ts = pd.to_datetime(datum) + pd.to_timedelta(t, unit='s')
        t_local = ts.tz_localize('UTC').tz_convert(C.TZ_LOCAL).tz_localize(None)
        evp = ev.copy()
        if len(evp):
            evp['start_ts'] = [(pd.Timestamp(datum) + pd.Timedelta(seconds=s)).tz_localize('UTC')
                               .tz_convert(C.TZ_LOCAL).tz_localize(None) for s in evp.start_s]
        img = _img(fig_barogram(t_local, g.altitude.to_numpy(float), evp), 'barogram')
        lat_all, lon_all = g.latitude.to_numpy(), g.longitude.to_numpy()
        m.fit_bounds([[float(lat_all.min()), float(lon_all.min())], [float(lat_all.max()), float(lon_all.max())]],
                     padding_top_left=(20, 20), padding_bottom_right=(20, 380))
        rows = ''.join(f"<tr><td><span style='color:{STATUS[rw.category]}'>■</span> {escape(short(rw.airspace))}</td>"
                       f"<td>{escape(STATUS_LABEL[rw.category].split(' (')[0])}</td><td>{rw.start_local}</td>"
                       f"<td>{rw.duration_s:.0f} s</td><td>{rw.max_alt_m:.0f} m</td>"
                       f"<td>{'' if pd.isna(rw.max_above_lower_m) else f'{rw.max_above_lower_m:.0f} m'}</td></tr>"
                       for rw in ev.itertuples())
        panel = (f"<div style='position:fixed;bottom:10px;left:10px;right:10px;z-index:1000;background:rgba(255,255,255,.97);"
                 f"padding:8px 12px;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,.2);font:12px/1.45 system-ui,sans-serif;"
                 f"color:#0b0b0b;max-height:44vh;overflow:auto'><b>Flight {flight_id}</b> · {datum} · "
                 f"{escape(str(info[1]))} ({escape(str(info[2]))}) · {escape(str(info[3]))} · {info[4]}–{info[5]} local"
                 f"<table style='font-size:12px;margin:4px 0'><tr><th align=left>airspace</th><th align=left>status</th>"
                 f"<th>entered (local)</th><th>inside</th><th>max alt</th><th>above floor</th></tr>{rows}</table>{img}</div>")
        m.get_root().html.add_child(folium.Element(panel))
    folium.LayerControl(collapsed=True).add_to(m)
    path.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(path))
    return path


# ═══════════════════════════ statistics & report ═══════════════════════════

def _table(df, fmt=None, raw=()):
    fmt = fmt or {}
    head = ''.join(f"<th>{escape(str(c)).replace('_', ' ')}</th>" for c in df.columns)
    rows = ''
    for _, r in df.iterrows():
        cells = ''
        for c in df.columns:
            val = r[c]
            if c in fmt and pd.notna(val):
                val = fmt[c](val)
            elif isinstance(val, float):
                val = '' if pd.isna(val) else f"{val:.0f}"
            cells += f"<td>{val if c in raw else escape(str(val))}</td>"
        rows += f"<tr>{cells}</tr>"
    return f"<div class='scroll'><table><tr>{head}</tr>{rows}</table></div>"


def flights_frame(checks, events):
    ok = checks[checks.status.isin(['ok', 'starts_airborne'])].copy()
    ok['year'] = ok.datum.str[:4]
    ok['month'] = ok.datum.str[:7]
    ok['weekday'] = pd.to_datetime(ok.datum).dt.dayofweek
    v = events[events.category == 'violation'] if len(events) else events
    ok['violated'] = ok.flight_id.isin(set(v.flight_id))
    return ok, v


def _pilot_name(s):
    s = s.fillna('').astype(str).str.strip()
    return s.where(~s.isin(['', '-', '--', 'None', 'nan', '?']), '(unknown)')


def pilot_summary(ok, v, n=None):
    """Pilots in command with at least one violation: flights, share, events, time inside, worst case."""
    p = ok.assign(pilot=_pilot_name(ok.pilot)).groupby('pilot').agg(
        flights=('flight_id', 'size'), with_violation=('violated', 'sum'))
    if len(v):
        ev = v.assign(pilot=_pilot_name(v.pilot)).groupby('pilot').agg(
            events=('flight_id', 'size'), minutes_inside=('duration_s', lambda d: d.sum() / 60),
            max_above_floor_m=('max_above_lower_m', 'max'), first=('datum', 'min'), last=('datum', 'max'))
        p = p.join(ev)
    p = p[p.with_violation > 0].copy()
    p['share'] = p.with_violation / p.flights
    p['_unknown'] = p.index == '(unknown)'
    p = p.sort_values(['_unknown', 'with_violation', 'share'], ascending=[True, False, False]).drop(columns='_unknown')
    if n:
        p = p.head(n)
    p = p.reset_index()
    cols = ['pilot', 'flights', 'with_violation', 'share', 'events', 'minutes_inside', 'max_above_floor_m', 'first', 'last']
    return p[cols] if len(p) else p


top_pilots = pilot_summary


def weekday_table(ok, events):
    t = ok.groupby('weekday').agg(flights=('flight_id', 'size'), with_violation=('violated', 'sum')) \
        .reindex(range(7), fill_value=0)
    t['share'] = np.where(t.flights > 0, t.with_violation / t.flights.replace(0, np.nan), 0)
    if len(events):
        wd = pd.to_datetime(events.datum).dt.dayofweek
        ec = events.assign(weekday=wd).groupby(['weekday', 'category']).size().unstack(fill_value=0)
        for c in ('violation', 'possible', 'unverified'):
            t[f'{c}_events'] = ec[c].reindex(range(7), fill_value=0) if c in ec else 0
    t.index = WEEKDAYS
    return t.rename_axis('weekday').reset_index()


def write_report(checks, events, settings, version, airspaces, checked, part_time_pattern, intermittent_pattern,
                 region, coverage, path, n_pages=0, title=None, map_href='airspace_violations_map.html'):
    ok, v = flights_frame(checks, events)
    n_ok, n_v = len(ok), int(ok.violated.sum())
    count = lambda c: int(events.category.eq(c).sum()) if len(events) else 0  # noqa: E731
    pct = lambda x: f"{100 * x:.1f}%"  # noqa: E731

    def rate(by):
        t = ok.groupby(by).agg(flights=('flight_id', 'size'), with_violation=('violated', 'sum'))
        t['share'] = t.with_violation / t.flights
        return t.reset_index()

    always = [a for a in checked if not schedule.is_part_time(a.name, part_time_pattern)
              and not schedule.is_intermittent(a.name, intermittent_pattern)]
    dashed = [a for a in airspaces if schedule.is_part_time(a.name, part_time_pattern)
              or schedule.is_intermittent(a.name, intermittent_pattern)]
    imgs = {'ceiling': _img(fig_ceiling(always, dashed, events, region), 'ceiling map'),
            'monthly': _img(fig_monthly(ok), 'monthly share'), 'weekday': _img(fig_weekday(ok), 'weekday share')}
    if len(events):
        imgs['airspace'] = _img(fig_by_airspace(events), 'events per airspace')
        imgs['severity'] = _img(fig_severity(events), 'severity')

    if len(v):
        by_as = v.assign(airspace=v.airspace.map(short)).rename(columns={'cls': 'class'}).groupby(
            ['airspace', 'class', 'lower_text', 'upper_text']).agg(
            events=('flight_id', 'size'), flights=('flight_id', 'nunique'),
            median_inside_s=('duration_s', 'median'), max_inside_s=('duration_s', 'max'),
            median_above_floor_m=('max_above_lower_m', 'median'),
            max_above_floor_m=('max_above_lower_m', 'max')).reset_index().sort_values('events', ascending=False)
        worst = v.sort_values(['max_above_lower_m', 'duration_s'], ascending=False).head(25).copy()
        worst['airspace'] = worst.airspace.map(short)
        worst['page'] = [f"<a href='airspace_maps/flight_{f}.html'>track</a>" for f in worst.flight_id]
        worst = worst[['datum', 'start_local', 'registration', 'launch_method', 'airspace', 'duration_s',
                       'max_alt_m', 'max_above_lower_m', 'page']]
    else:
        by_as = worst = pd.DataFrame()
    wdt = weekday_table(ok, events)
    st = settings.__dict__
    status_counts = checks.status.value_counts().to_dict()
    cov_auto, cov_ehhv = coverage
    css = """
    :root{--bg:#f9f9f7;--card:#fcfcfb;--fg:#0b0b0b;--muted:#52514e;--line:#e1e0d9;--crit:#d03b3b}
    @media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#0d0d0d;--card:#1a1a19;--fg:#f0efec;--muted:#c3c2b7;--line:#2c2c2a}}
    :root[data-theme="dark"]{--bg:#0d0d0d;--card:#1a1a19;--fg:#f0efec;--muted:#c3c2b7;--line:#2c2c2a}
    body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0}
    main{max-width:1040px;margin:0 auto;padding:24px 16px 64px} h1{font-size:25px;margin:0 0 4px}
    h2{font-size:18px;margin:34px 0 10px} .muted{color:var(--muted)}
    .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:18px 0}
    .tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
    .tile b{display:block;font-size:26px;font-weight:600} .tile span{color:var(--muted);font-size:13px}
    .card{background:#fcfcfb;border:1px solid var(--line);border-radius:10px;padding:10px;margin:10px 0}
    table{border-collapse:collapse;width:100%;font-size:13.5px} th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line)}
    th{font-weight:600} .scroll{overflow-x:auto} a{color:#2a78d6}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;align-items:start}
    .btn{display:inline-block;padding:8px 14px;border-radius:8px;background:#2a78d6;color:#fff;text-decoration:none;font-weight:600}"""
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title or 'Airspace Violations')}</title>
<style>{css}</style></head><body><main>
<h1>{escape(title or 'Airspace violations — EHHV flights')}</h1>
<p class="muted">Measured within a {escape(region_text(region))} · it covers {pct(cov_auto)} of all flying time
(a circle of the same size around EHHV would cover {pct(cov_ehhv)}) · generated {time.strftime('%Y-%m-%d %H:%M')} ·
airspace: {escape(version.get('header') or version['file'])} (unofficial — check the AIP)</p>
<div class="tiles">
<div class="tile"><b>{n_ok:,}</b><span>flights checked</span></div>
<div class="tile"><b style="color:var(--crit)">{n_v} · {pct(n_v / n_ok) if n_ok else '–'}</b><span>flights with a violation</span></div>
<div class="tile"><b>{len(v)}</b><span>violation events</span></div>
<div class="tile"><b>{count('unverified')}</b><span>unverified: area only active when booked (check NOTAM)</span></div>
<div class="tile"><b>{count('possible')}</b><span>possible: Deelen (clearance on request) or military airspace on a day off</span></div>
</div>
<p><a class="btn" href="{escape(map_href)}">Open the interactive map</a></p>

<h2>Where — and why there</h2>
<div class="card">{imgs['ceiling']}</div>
{('<div class="card">' + imgs['airspace'] + '</div>') if 'airspace' in imgs else ''}
<h3>Violations per airspace</h3>
{_table(by_as) if len(by_as) else '<p>No violations.</p>'}

<h2>When</h2>
<div class="grid"><div class="card">{imgs['weekday']}</div>
<div>{_table(wdt, {'share': pct})}</div></div>
<div class="card">{imgs['monthly']}</div>
<div class="grid"><div><h3>By year</h3>{_table(rate('year'), {'share': pct})}</div>
<div><h3>By launch method</h3>{_table(rate('launch_method'), {'share': pct})}</div></div>

<h2>How serious</h2>
{('<div class="card">' + imgs['severity'] + '</div>') if 'severity' in imgs else ''}
<h3>Largest violations (by height above the floor)</h3>
{_table(worst, {'duration_s': lambda x: f'{x:.0f}', 'max_alt_m': lambda x: f'{x:.0f}', 'max_above_lower_m': lambda x: f'{x:.0f}'}, raw=('page',)) if len(worst) else ''}
<p class="muted">Every flight with an event has a page with its track and barogram ({n_pages} pages).</p>

<h2>Rules used</h2>
<table>
<tr><td>Area measured</td><td>{escape(region_text(region))}; fixes outside it are ignored</td></tr>
<tr><td>Minimum time continuously inside</td><td>{st['min_duration_s']:.0f} s</td></tr>
<tr><td>Vertical margin (above the floor, below the ceiling)</td><td>{st['vertical_margin_m']:.0f} m ({st['vertical_margin_m'] / 0.3048:.0f} ft)</td></tr>
<tr><td>Horizontal margin inside the boundary</td><td>{st['horizontal_margin_m']:.0f} m</td></tr>
<tr><td>Airspace classes checked</td><td>{', '.join(st['classes'])}{' (incl. NOTAM-only areas)' if st['include_notam_areas'] else ' (areas tagged NOTAM-only left out)'}</td></tr>
<tr><td>Only active when booked → "unverified"</td><td><code>{escape(intermittent_pattern or '–')}</code>, and restricted areas (class R) without an H24 tag (activation by NOTAM, AIP ENR 5.1)</td></tr>
<tr><td>Military, weekdays only → "possible" on weekends / holidays</td><td><code>{escape(part_time_pattern or '–')}</code>; also outside known opening hours: {'; '.join(escape(h[3]) for h in schedule.MIL_CTR_HOURS)}</td></tr>
<tr><td>Time-dependent classification (AIP SUP 03/2022, Regeling luchtverkeersdienstverlening): only counted while class D</td><td>{'<br>'.join(escape(r[2]) for r in schedule.TIME_RULES) if st.get('apply_time_rules', True) else 'not applied'}</td></tr>
<tr><td>Crossing clearance on request → always "possible"</td><td><code>{escape(schedule.CLEARANCE_DEFAULT)}</code> (CTR Deelen: clearance from Dutch Mil)</td></tr>
<tr><td>Gliding sectors as exemption</td><td>{'yes' if st['gliding_sector_exemption'] else 'no'}</td></tr>
<tr><td>Altitude</td><td>GPS altitude AMSL from the logs (OGN/FLARM); flight levels converted with that day's QNH</td></tr>
<tr><td>Flights not checked</td><td>{escape(', '.join(f'{k}: {n}' for k, n in status_counts.items() if k not in ('ok', 'starts_airborne')))}</td></tr>
</table>

<h2>Caveats</h2><ul>
<li>One airspace file (current version) is used for all years; airspace changed over time.</li>
<li>Activation is not in the file: TRA/TSA and NOTAM areas are "unverified" — compare with the NOTAMs / airspace use plan of that day.</li>
<li>Tracks come from OGN/FLARM receivers; coverage gaps can hide or shorten events.</li>
<li>GPS altitude vs. barometric limits differ by tens of metres — that is what the vertical margin is for.</li>
</ul></main></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html)


# ═══════════════════════════ details report (with names) ═══════════════════════════

def _reason(r):
    if r.category == 'exempt':
        return f"gliding sector {short(r.exempt_by)}"
    if r.category == 'unverified':
        return 'activated by NOTAM — check that day'
    if r.category == 'possible':
        if schedule.clearance_possible(r.airspace):
            return 'crossing clearance can be requested from Dutch Mil — may have had clearance'
        return 'weekend / public holiday' if r.off_day else 'outside the opening hours of the military aerodrome'
    return short(r.time_rule).split(':')[0] + ' (class D at that time)' if isinstance(r.time_rule, str) else ''


def write_details(checks, events, settings, region, path, anonymised=False, event_pages=(), event_dir=None,
                  title=None):
    """Every event with pilot, aircraft, time, airspace and severity; filterable and sortable."""
    ok, v = flights_frame(checks, events)
    pct = lambda x: f"{100 * x:.1f}%"  # noqa: E731
    pilots = pilot_summary(ok, v)
    n_cat = events.category.value_counts().to_dict() if len(events) else {}
    rows = []
    if len(events):
        ev = events.sort_values(['datum', 'start_local'], ascending=[False, False])
        for r in ev.itertuples():
            mmss = f"{int(r.duration_s // 60)}:{int(r.duration_s % 60):02d}"
            above = '' if pd.isna(r.max_above_lower_m) else f"{r.max_above_lower_m:.0f}"
            wd = WEEKDAYS[pd.Timestamp(r.datum).dayofweek]
            cells = [
                (f"<span class='badge' style='background:{STATUS[r.category]}'>{escape(r.category)}</span>", r.category),
                (escape(r.datum), r.datum), (wd, pd.Timestamp(r.datum).dayofweek), (escape(r.start_local), r.start_local),
                (escape(str(_pilot_name(pd.Series([r.pilot])).iloc[0])), None),
                (escape('' if pd.isna(r.second_pilot) or str(r.second_pilot).strip() in ('', '-') else str(r.second_pilot)), None),
                (escape(str(r.registration)), None), (escape(str(r.glider_type or '')), None),
                (escape(str(r.launch_method)), None), (escape(short(r.airspace)), None), (escape(r.cls), None),
                (escape(f"{r.lower_text} – {r.upper_text}"), r.lower_m if np.isfinite(r.lower_m) else -1),
                (mmss, r.duration_s), (f"{r.max_alt_m:.0f}", r.max_alt_m),
                (above, r.max_above_lower_m if pd.notna(r.max_above_lower_m) else -1),
                (escape(_reason(r)), None),
                (("<b>track</b>" if r.event_id in event_pages else "<span class='muted'>flight</span>"), None),
                (f"<a href='airspace_maps/flight_{r.flight_id}.html' target='_blank'>{r.flight_id}</a>", r.flight_id),
            ]
            tds = ''.join(f"<td{'' if sv is None else f' data-v={chr(34)}{escape(str(sv))}{chr(34)}'}>{c}</td>"
                          for c, sv in cells)
            href = (f"{event_dir or EVENT_DIR}/event_{r.event_id}.html" if r.event_id in event_pages
                    else f"airspace_maps/flight_{r.flight_id}.html")
            rows.append(f"<tr data-cat='{r.category}' data-href='{href}' "
                        f"data-title='{escape(short(r.airspace))} · {escape(r.datum)} {escape(r.start_local)} · "
                        f"{escape(str(r.registration))}' class='{'has-track' if r.event_id in event_pages else ''}'>"
                        f"{tds}</tr>")
    heads = ['status', 'date', 'day', 'local time', 'pilot in command', 'second pilot', 'registration', 'glider',
             'launch', 'airspace', 'class', 'floor – ceiling', 'time inside', 'max alt (m)', 'above floor (m)',
             'why this status', 'opens', 'flight']
    thead = ''.join(f"<th data-i='{k}'>{h}</th>" for k, h in enumerate(heads))
    boxes = ''.join(
        f"<label><input type='checkbox' class='cat' value='{c}' {'checked' if c == 'violation' else ''}> "
        f"<span class='badge' style='background:{STATUS[c]}'>{c}</span> {n_cat.get(c, 0)}</label>"
        for c in schedule.CATEGORIES)
    st = settings.__dict__
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title or 'Airspace Violations — Details')}</title>
<style>
:root{{--bg:#f9f9f7;--card:#fcfcfb;--fg:#0b0b0b;--muted:#52514e;--line:#e1e0d9}}
@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{--bg:#0d0d0d;--card:#1a1a19;--fg:#f0efec;--muted:#c3c2b7;--line:#2c2c2a}}}}
:root[data-theme="dark"]{{--bg:#0d0d0d;--card:#1a1a19;--fg:#f0efec;--muted:#c3c2b7;--line:#2c2c2a}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0}}
main{{max-width:1400px;margin:0 auto;padding:22px 16px 60px}} h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:18px;margin:28px 0 8px}}
.muted{{color:var(--muted)}} .warn{{border:1px solid var(--line);border-radius:8px;padding:8px 12px;background:var(--card)}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{text-align:left;padding:4px 7px;border-bottom:1px solid var(--line);white-space:nowrap}}
th{{font-weight:600;position:sticky;top:0;background:var(--bg);cursor:pointer;user-select:none}} th:hover{{text-decoration:underline}}
.scroll{{overflow-x:auto}} a{{color:#2a78d6}} .badge{{color:#fff;border-radius:4px;padding:1px 6px;font-size:12px}}
.filters{{display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin:10px 0}}
#t tbody tr{{cursor:pointer}} #t tbody tr:hover td{{background:rgba(42,120,214,.08)}} #t tbody tr.has-track td:first-child{{box-shadow:inset 3px 0 0 #2a78d6}}
#ov{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:50}} #ov.open{{display:block}}
#ovbox{{position:absolute;inset:3vh 2vw;background:var(--card);border-radius:10px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 8px 30px rgba(0,0,0,.35)}}
#ovbar{{display:flex;gap:14px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--line)}} #ovbar b{{flex:1}}
#ovbar button{{border:1px solid var(--line);background:var(--bg);color:var(--fg);border-radius:6px;padding:4px 10px;cursor:pointer}}
#ovframe{{flex:1;border:0;width:100%}} #q{{padding:6px 10px;border:1px solid var(--line);border-radius:6px;min-width:260px;background:var(--card);color:var(--fg)}}
</style></head><body><main>
<h1>{escape(title or 'Airspace violations — details')}</h1>
<p class="muted">Generated {time.strftime('%Y-%m-%d %H:%M')} · measured within a {escape(region_text(region))} ·
≥{st['min_duration_s']:.0f} s inside, ≥{st['vertical_margin_m']:.0f} m above the floor · see the overview report for the rules.</p>
<p class="warn">{'Pilot names are anonymised.' if anonymised else '<b>Contains pilot names</b> — for internal use (e.g. the instructors / safety officer). Run with <code>--anonymise</code> before sharing.'}</p>

<h2>Pilots with violations</h2>
<p class="muted">Pilot in command. Share = flights with a violation ÷ flights checked for that pilot. Only "violation" events count.</p>
{_table(pilots, {'share': pct, 'minutes_inside': lambda x: f'{x:.1f}', 'max_above_floor_m': lambda x: f'{x:.0f}'}) if len(pilots) else '<p>No violations.</p>'}

<h2>All events</h2>
<div class="filters">{boxes}
<input id="q" type="search" placeholder="Search pilot, registration, airspace, date…">
<span id="count" class="muted"></span></div>
<div class="scroll"><table id="t"><thead><tr>{thead}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p class="muted">Click an event to see its track. Rows marked with a blue edge (<b>track</b>) open the detailed track of
that event (map zoomed on the violation, altitude vs. floor, distance inside the boundary, every fix); the others open the
whole flight. Click a column header to sort.</p>
</main>
<div id="ov"><div id="ovbox"><div id="ovbar"><b id="ovtitle"></b><a id="ovnew" href="#" target="_blank">open in new tab</a>
<button id="ovclose" type="button">close (Esc)</button></div><iframe id="ovframe" title="track"></iframe></div></div>
<script>
const rows=[...document.querySelectorAll('#t tbody tr')], q=document.getElementById('q'), cnt=document.getElementById('count');
function apply(){{
  const cats=new Set([...document.querySelectorAll('.cat:checked')].map(c=>c.value));
  const s=q.value.trim().toLowerCase(); let n=0;
  rows.forEach(r=>{{const show=cats.has(r.dataset.cat)&&(!s||r.textContent.toLowerCase().includes(s));
    r.style.display=show?'':'none'; if(show)n++;}});
  cnt.textContent=n+' events shown';
}}
document.querySelectorAll('.cat').forEach(c=>c.addEventListener('change',apply)); q.addEventListener('input',apply);
const ov=document.getElementById('ov'), fr=document.getElementById('ovframe');
function openTrack(r){{fr.src=r.dataset.href; document.getElementById('ovtitle').textContent=r.dataset.title;
  document.getElementById('ovnew').href=r.dataset.href; ov.classList.add('open');}}
function closeTrack(){{ov.classList.remove('open'); fr.src='about:blank';}}
rows.forEach(r=>r.addEventListener('click',e=>{{if(e.target.closest('a'))return; openTrack(r);}}));
document.getElementById('ovclose').addEventListener('click',closeTrack);
ov.addEventListener('click',e=>{{if(e.target===ov)closeTrack();}});
document.addEventListener('keydown',e=>{{if(e.key==='Escape')closeTrack();}});
let dir={{}};
document.querySelectorAll('#t th').forEach(th=>th.addEventListener('click',()=>{{
  const i=+th.dataset.i; dir[i]=-(dir[i]||-1); const tb=document.querySelector('#t tbody');
  const val=r=>{{const c=r.children[i]; const v=c.dataset.v; return v!==undefined&&v!==''&&!isNaN(+v)?+v:(v??c.textContent).toLowerCase();}};
  rows.sort((a,b)=>{{const x=val(a),y=val(b); return (x>y?1:x<y?-1:0)*dir[i];}}).forEach(r=>tb.appendChild(r));
}}));
apply();
</script></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html)


# ═══════════════════════════ event pages (detailed track of one event) ═══════════════════════════

EVENT_DIR = 'airspace_events'
EVENT_WINDOW_S = 600          # track shown before and after the event
TABLE_MARGIN_S = 30           # fixes listed before and after the event


def event_id(flight_id, start_utc):
    return f"{int(flight_id)}_{str(start_utc).replace(':', '')}"


def find_airspace(airspaces, name, lower_text):
    for a in airspaces:
        if a.name == name and a.lower.text == lower_text:
            return a
    return next((a for a in airspaces if a.name == name), None)


def fig_event_profile(tl, alt, floor, ceiling, margin, in_ev, t0, t1, category, name):
    plt = _plt()
    import matplotlib.dates as mdates
    fig, ax = plt.subplots(figsize=(8.8, 2.8))
    ax.axvspan(t0, t1, color=STATUS[category], alpha=0.14, lw=0)
    lo, hi = np.nanmin(alt), np.nanmax(alt)
    if np.isfinite(floor):
        ax.axhspan(floor, floor + margin, color=STATUS[category], alpha=0.10, lw=0)
        ax.axhline(floor, color=INK2, lw=1.4, ls=(0, (4, 2)))
        ax.annotate(f"{name} floor", (tl[0], floor), xytext=(2, 3), textcoords='offset points', fontsize=7.5, color=INK2)
        ax.annotate(f"+{margin:.0f} m margin", (tl[0], floor + margin), xytext=(2, 2), textcoords='offset points',
                    fontsize=7, color=MUTED)
        lo, hi = min(lo, floor), max(hi, floor + margin)
    if np.isfinite(ceiling) and ceiling < hi + 400:
        ax.axhline(ceiling, color=INK2, lw=1.2, ls=(0, (1, 2)))
        ax.annotate(f"{name} ceiling", (tl[0], ceiling), xytext=(2, -10), textcoords='offset points', fontsize=7.5,
                    color=INK2)
        hi = max(hi, ceiling)
    ax.plot(tl, alt, color='#2a78d6', lw=1.3)
    a_ev = np.where(in_ev, alt, np.nan)
    ax.plot(tl, a_ev, color=STATUS[category], lw=2.4)
    pad = max(30, 0.08 * (hi - lo))
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_ylabel('m AMSL (GPS)')
    ax.yaxis.grid(True, color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax.set_title('Altitude (local time) · shaded = inside the airspace · dashed = floor, band = vertical margin',
                 fontsize=9, color=INK, loc='left')
    return fig


def fig_event_depth(tl, signed, hmargin, in_ev, t0, t1, category):
    plt = _plt()
    import matplotlib.dates as mdates
    fig, ax = plt.subplots(figsize=(8.8, 2.2))
    ax.axvspan(t0, t1, color=STATUS[category], alpha=0.14, lw=0)
    ax.axhline(0, color=INK2, lw=1.2)
    ax.annotate('boundary', (tl[0], 0), xytext=(2, 3), textcoords='offset points', fontsize=7.5, color=INK2)
    if hmargin > 0:
        ax.axhline(hmargin, color=MUTED, lw=1, ls=(0, (4, 2)))
    ax.fill_between(tl, 0, np.maximum(signed, 0), color=STATUS[category], alpha=0.25, lw=0)
    ax.plot(tl, signed / 1, color='#2a78d6', lw=1.3)
    ax.set_ylabel('m inside (+) / outside (−)')
    ax.yaxis.grid(True, color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax.set_title('Horizontal: distance inside the airspace boundary', fontsize=9, color=INK, loc='left')
    return fig


def event_page(conn, ev, airspaces, region, part_time_pattern, intermittent_pattern, checked_names, settings, path):
    """Detailed track of one event: zoomed map, altitude and boundary profiles, facts, fix table."""
    import folium
    from src.thermal_detection.climb_detector import parse_times
    a = find_airspace(airspaces, ev.airspace, ev.lower_text)
    g = pd.read_sql_query("SELECT time, latitude, longitude, altitude FROM igc_data WHERE flight_id=? ORDER BY sequence",
                          conn, params=(int(ev.flight_id),))
    g = g.dropna(subset=['time', 'latitude', 'longitude', 'altitude'])
    g = g[g.time.str.len() >= 8]
    if g.empty or a is None:
        return None
    t = parse_times(g.time)
    t = t + np.concatenate([[0], np.cumsum(np.where(np.diff(t) < -43200, 86400, 0))])
    keep = np.concatenate([[True], np.diff(t) > 0])
    g, t = g[keep], t[keep]
    t0, t1 = float(ev.start_s), float(ev.start_s + ev.duration_s)
    win = (t >= t0 - EVENT_WINDOW_S) & (t <= t1 + EVENT_WINDOW_S)
    g, t = g[win], t[win]
    lat, lon, alt = g.latitude.to_numpy(), g.longitude.to_numpy(), g.altitude.to_numpy(float)
    x, y = C.to_xy(lat, lon)
    inside = a.contains(x, y)
    signed = np.where(inside, 1, -1) * a.distance_to_edge(x, y)
    in_ev = (t >= t0) & (t <= t1)
    floor, ceiling = float(ev.lower_m), float(ev.upper_m)
    floor = floor if np.isfinite(floor) and floor > -1e6 else np.nan
    ceiling = ceiling if np.isfinite(ceiling) and ceiling < 1e6 else np.nan
    datum = str(ev.datum)[:10]
    base = pd.Timestamp(datum)
    to_local = lambda s: (base + pd.to_timedelta(s, unit='s')).tz_localize('UTC').tz_convert(C.TZ_LOCAL).tz_localize(None)  # noqa: E731
    tl = to_local(t)
    tl0, tl1 = to_local(t0), to_local(t1)

    m = _base_map(13, scale=False)
    _airspace_layer(folium, airspaces, region, part_time_pattern, intermittent_pattern, checked_names).add_to(m)
    alat, alon = C.to_latlon(a.x, a.y)
    folium.Polygon(list(zip(alat, alon)), color=STATUS[ev.category], weight=3, fill=True, fill_opacity=0.12,
                   tooltip=f"<b>{escape(short(a.name))}</b> · class {a.cls} · {escape(a.lower.text)} – {escape(a.upper.text)}"
                   ).add_to(m)
    folium.PolyLine(list(zip(lat, lon)), color='#2a78d6', weight=2.5, opacity=0.8,
                    tooltip=f'track ±{EVENT_WINDOW_S // 60} min').add_to(m)
    ev_idx = np.flatnonzero(in_ev)
    if len(ev_idx):
        folium.PolyLine(list(zip(lat[ev_idx], lon[ev_idx])), color=STATUS[ev.category], weight=6, opacity=0.95).add_to(m)
        last_t = -1e9
        for i in ev_idx:
            if t[i] - last_t < 10:
                continue
            last_t = t[i]
            above = '' if np.isnan(floor) else f", {alt[i] - floor:+.0f} m vs floor"
            folium.CircleMarker([lat[i], lon[i]], radius=3, color=STATUS[ev.category], fill=True, fill_opacity=1,
                                tooltip=f"{tl[i]:%H:%M:%S} · {alt[i]:.0f} m AMSL{above} · {signed[i]:.0f} m inside"
                                ).add_to(m)
        for i, label in ((ev_idx[0], 'IN'), (ev_idx[-1], 'OUT')):
            folium.Marker([lat[i], lon[i]], icon=folium.DivIcon(
                html=f"<div style='font:bold 11px system-ui;color:#fff;background:{STATUS[ev.category]};padding:1px 4px;"
                     f"border-radius:3px;white-space:nowrap;transform:translate(6px,-8px)'>{label} {tl[i]:%H:%M:%S}</div>")
                          ).add_to(m)
    m.fit_bounds([[float(lat.min()), float(lon.min())], [float(lat.max()), float(lon.max())]],
                 padding_top_left=(20, 20), padding_bottom_right=(20, 430))

    prof = _img(fig_event_profile(tl, alt, floor, ceiling, settings.vertical_margin_m, in_ev, tl0, tl1, ev.category,
                                  short(a.name)), 'altitude profile')
    depth = _img(fig_event_depth(tl, signed, settings.horizontal_margin_m, in_ev, tl0, tl1, ev.category), 'boundary')
    sel = (t >= t0 - TABLE_MARGIN_S) & (t <= t1 + TABLE_MARGIN_S)
    fix_rows = ''.join(
        f"<tr{' style=\"font-weight:600\"' if in_ev[i] else ''}><td>{tl[i]:%H:%M:%S}</td><td>{lat[i]:.5f}</td>"
        f"<td>{lon[i]:.5f}</td><td>{alt[i]:.0f}</td><td>{'' if np.isnan(floor) else f'{alt[i] - floor:+.0f}'}</td>"
        f"<td>{signed[i]:+.0f}</td><td>{'inside' if in_ev[i] else ''}</td></tr>" for i in np.flatnonzero(sel))
    pilot = _pilot_name(pd.Series([ev.pilot])).iloc[0]
    second = '' if pd.isna(ev.second_pilot) or str(ev.second_pilot).strip() in ('', '-') else str(ev.second_pilot)
    above = '–' if pd.isna(ev.max_above_lower_m) else f"{ev.max_above_lower_m:.0f} m"
    facts = [('Status', f"<span style='color:#fff;background:{STATUS[ev.category]};border-radius:4px;padding:1px 6px'>"
                        f"{escape(ev.category)}</span> {escape(_reason(ev))}"),
             ('Date', f"{datum} ({WEEKDAYS[base.dayofweek]})"),
             ('In → out (local)', f"{tl0:%H:%M:%S} → {tl1:%H:%M:%S} ({int(ev.duration_s // 60)}:{int(ev.duration_s % 60):02d} inside)"),
             ('Pilot in command', escape(pilot)), ('Second pilot', escape(second) or '–'),
             ('Aircraft', f"{escape(str(ev.registration))} · {escape(str(ev.glider_type or ''))} · {escape(str(ev.launch_method))}"),
             ('Airspace', f"{escape(short(a.name))} · class {a.cls} · {escape(a.lower.text)} – {escape(a.upper.text)}"),
             ('Deepest', f"max {ev.max_alt_m:.0f} m AMSL · {above} above the floor · {ev.max_inside_m:.0f} m inside the boundary"),
             ('QNH used', f"{ev.qnh_hpa:.1f} hPa"),
             ('Whole flight', f"<a href='../airspace_maps/flight_{int(ev.flight_id)}.html' target='_blank'>track & barogram</a>")]
    facts_html = ''.join(f"<tr><td style='color:#52514e;padding-right:12px'>{k}</td><td>{v}</td></tr>" for k, v in facts)
    panel = (f"<div style='position:fixed;bottom:10px;left:10px;right:10px;z-index:1000;background:rgba(255,255,255,.97);"
             f"padding:10px 14px;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,.2);font:12.5px/1.45 system-ui,sans-serif;"
             f"color:#0b0b0b;max-height:50vh;overflow:auto'>"
             f"<b style='font-size:14px'>{escape(short(a.name))} — flight {int(ev.flight_id)}</b>"
             f"<div style='display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start'>"
             f"<table style='font-size:12.5px;align-self:flex-start;max-width:520px'>{facts_html}</table>"
             f"<div style='flex:1;min-width:420px'>{prof}{depth}</div></div>"
             f"<details><summary style='cursor:pointer;margin:6px 0'>Fixes ({TABLE_MARGIN_S} s before → {TABLE_MARGIN_S} s after; bold = inside)</summary>"
             f"<table style='font-size:12px;border-collapse:collapse'><tr><th align=left>local time</th><th>lat</th><th>lon</th>"
             f"<th>alt m AMSL</th><th>vs floor m</th><th>inside boundary m</th><th></th></tr>{fix_rows}</table></details></div>")
    m.get_root().html.add_child(folium.Element(panel))
    folium.LayerControl(collapsed=True).add_to(m)
    path.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(path))
    return path
