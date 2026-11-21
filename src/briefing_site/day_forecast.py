"""
Thermal forecast for the whole day at EHHV, hour by hour (08–19 local), for the briefing site.

From the Open-Meteo forecast (same variables as the first-thermal model):
- thermal strength W* (convective velocity) from the upward surface heat flux and the
  boundary-layer height: W* = (g / T · H / (ρ c_p) · z_i)^(1/3);
- typical climb at Hilversum = a + b · W*, fitted on the climbs the detector found in past
  flights (thermal_calibration; 11,025 climbs 2024–2026: 0.39 + 0.43 · W* m/s);
- climbs usually end at top_ratio × boundary-layer height (median 0.76), capped by cloud base;
- cloud base from the temperature–dew point spread (125 m per °C); cumulus when the
  boundary layer reaches the cloud base;
- chance to connect after a winch launch (layer A) for the most likely runway that hour.
"""

from datetime import datetime, timezone
from html import escape

import numpy as np
import pandas as pd

from src import config as C
from src.briefing_site.runway_odds import runway_odds

HOURS_LOCAL = range(8, 20)
DEFAULT_CAL = {'a': 0.39, 'b': 0.43, 'top_ratio': 0.76, 'n': 11025}
USABLE_WSTAR = 1.0          # m/s
USABLE_BLH = 500.0          # m
RAIN_MM = 0.2               # mm/h
SPREAD_M_PER_C = 125.0
SECTORS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
INK, INK2, MUTED, GRID = '#0b0b0b', '#52514e', '#8a8983', '#ecebe6'
BLUE, BLUE_LIGHT, OFF = '#2a78d6', '#d8e7f8', '#c9c8c1'


def wstar(shf, t2m, blh):
    """Convective velocity scale (m/s). shf: Open-Meteo sensible heat flux (negative = upward)."""
    H = np.clip(-np.asarray(shf, float), 0, None)
    T = np.asarray(t2m, float) + 273.15
    zi = np.clip(np.asarray(blh, float), 0, None)
    return np.cbrt(9.81 / T * H / (1.2 * 1005.0) * zi)


def thermal_calibration(conn) -> dict:
    """Fit typical climb rate on W* and the climb top / boundary-layer ratio from past climbs."""
    d = pd.read_sql_query("""
        SELECT c.rate, c.agl1, w.blh, w.shf, w.t2m
        FROM climbs c JOIN launches l ON l.flight_id = c.flight_id
        JOIN weather_conv w ON w.date = l.datum AND w.hour_utc = CAST(c.start_utc_s / 3600 AS INTEGER)
        WHERE c.rate IS NOT NULL""", conn).dropna()
    if len(d) < 500:
        return dict(DEFAULT_CAL)
    ws = wstar(d.shf, d.t2m, d.blh)
    b, a = np.polyfit(ws, d.rate.to_numpy(float), 1)
    ratio = float(np.median(d.agl1 / d.blh.clip(lower=100)))
    return {'a': round(float(a), 3), 'b': round(float(b), 3), 'top_ratio': round(ratio, 3), 'n': int(len(d))}


def _sector(deg):
    return SECTORS[int(((deg + 22.5) % 360) // 45)]


def day_table(wx: pd.DataFrame, day, hist, cal=None, bundle_a=None) -> pd.DataFrame:
    """One row per local hour 08–19 with the thermal forecast."""
    from src.prediction import layer_a as A
    from src.prediction.features import build_features
    cal = cal or DEFAULT_CAL
    rows = []
    for h in HOURS_LOCAL:
        t = datetime(day.year, day.month, day.day, h, tzinfo=C.TZ_LOCAL).astimezone(timezone.utc).replace(tzinfo=None)
        i = int(np.argmin(np.abs((wx.index - pd.Timestamp(t)).total_seconds())))
        r = wx.iloc[i]
        ws = float(wstar(r.shf, r.t2m, r.blh))
        blh = float(r.blh)
        base = max(0.0, float(r.t2m - r.td2m) * SPREAD_M_PER_C)
        cu = blh >= 0.9 * base and base > 0
        rain = float(r.precip or 0)
        usable = ws >= USABLE_WSTAR and blh >= USABLE_BLH and rain < RAIN_MM
        climb = max(0.0, cal['a'] + cal['b'] * ws) if usable else 0.0
        top = min(cal['top_ratio'] * blh, base if cu else np.inf) if usable else 0.0
        odds = runway_odds(hist, float(r.wd100), float(r.ws100))
        rows.append({'hour': f"{h:02d}:00", 't_utc': t, 'wstar': ws, 'climb': climb, 'top': top, 'blh': blh,
                     'base': base, 'cu': bool(cu), 'cl_low': float(r.cl_low), 'cl_mid': float(r.cl_mid),
                     'rain': rain, 'sw': float(r.sw), 'wd10': float(r.wd10), 'ws10': float(r.ws10),
                     'wd100': float(r.wd100), 'ws100': float(r.ws100), 'runway': odds[0][0],
                     'runway_p': odds[0][1], 'usable': bool(usable)})
    df = pd.DataFrame(rows)
    bundle_a = bundle_a or A.load()
    df['chance'] = np.nan
    for rw, g in df.groupby('runway'):
        feats = build_features(wx, list(g.t_utc), rw)
        df.loc[g.index, 'chance'] = A.predict(bundle_a, feats, two_seat=0, dual=0, runway=rw)
    return df


def summary(df: pd.DataFrame) -> dict:
    u = df[df.usable]
    if u.empty:
        rating, text = 'No thermals', 'No usable thermals expected'
        return {'rating': rating, 'level': 0, 'text': text, 'window': None, 'best_hour': None,
                'climb': 0.0, 'top': 0.0, 'cu_base': None, 'rain_hours': list(df.hour[df.rain >= RAIN_MM])}
    best = u.loc[u.climb.idxmax()]
    climb, top = float(u.climb.max()), float(u.top.max())
    if climb >= 1.4 and top >= 1500:
        rating, level = 'Very good soaring day', 4
    elif climb >= 1.1 and top >= 1100:
        rating, level = 'Good soaring day', 3
    elif climb >= 0.9 and top >= 800:
        rating, level = 'Fair soaring day', 2
    else:
        rating, level = 'Weak thermals', 1
    cu = u[u.cu]
    cu_base = float(cu.base.median()) if len(cu) else None
    win = f"{u.hour.iloc[0]}–{int(u.hour.iloc[-1][:2]) + 1:02d}:00"
    text = (f"Thermals {win} · climbs ~{climb:.1f} m/s, ending ~{top:.0f} m · "
            + (f"cumulus base ~{cu_base:.0f} m" if cu_base else "blue thermals (no cumulus)"))
    return {'rating': rating, 'level': level, 'text': text, 'window': win, 'best_hour': best.hour,
            'climb': climb, 'top': top, 'cu_base': cu_base, 'rain_hours': list(df.hour[df.rain >= RAIN_MM])}


# ───────────────────────────── page ─────────────────────────────

def _chart(df, kind, release_agl=300.0, club=None, W=760, H=190):
    ml, mr, mt, mb = 46, 12, 12, 26
    n = len(df)
    bw = (W - ml - mr) / n
    xs = [ml + bw * (i + 0.5) for i in range(n)]
    if kind == 'height':
        vmax = max(1000.0, float(np.nanmax(np.r_[df.blh, df.base[df.cu & df.usable]])))
        step = 500
    elif kind == 'climb':
        vmax, step = max(2.0, float(df.climb.max()) + 0.2), 0.5
    else:
        vmax, step = max(0.4, float(df.chance.max()) + 0.05), 0.1
    vmax = np.ceil(vmax / step) * step

    def y(v):
        return mt + (H - mt - mb) * (1 - v / vmax)

    parts = [f"<svg viewBox='0 0 {W} {H}' width='100%' role='img' style='display:block;max-width:{W}px'>"]
    k = 0
    while k * step <= vmax + 1e-9:
        v = k * step
        lab = f"{v:.0f}" if kind == 'height' else (f"{v:.1f}" if kind == 'climb' else f"{100 * v:.0f}%")
        parts.append(f"<line x1='{ml}' x2='{W - mr}' y1='{y(v):.1f}' y2='{y(v):.1f}' stroke='{GRID}'/>"
                     f"<text x='{ml - 6}' y='{y(v) + 4:.1f}' text-anchor='end' font-size='11' fill='{MUTED}'>{lab}</text>")
        k += 1
    for i, hr in enumerate(df.hour):
        if i % 2 == 0:
            parts.append(f"<text x='{xs[i]:.1f}' y='{H - 8}' text-anchor='middle' font-size='11' fill='{MUTED}'>{hr[:2]}</text>")
    if kind == 'height':
        pts = ' '.join(f"{x:.1f},{y(v):.1f}" for x, v in zip(xs, df.blh))
        parts.append(f"<polygon points='{xs[0]:.1f},{y(0):.1f} {pts} {xs[-1]:.1f},{y(0):.1f}' fill='{BLUE_LIGHT}'/>")
        parts.append(f"<polyline points='{pts}' fill='none' stroke='{BLUE}' stroke-width='1.5' stroke-opacity='.5'/>")
        tp = [(x, v) for x, v, u in zip(xs, df.top, df.usable) if u]
        if tp:
            parts.append("<polyline points='" + ' '.join(f"{x:.1f},{y(v):.1f}" for x, v in tp)
                         + f"' fill='none' stroke='{BLUE}' stroke-width='2.5' stroke-linejoin='round'/>")
            for x, v in tp:
                parts.append(f"<circle cx='{x:.1f}' cy='{y(v):.1f}' r='3.5' fill='{BLUE}' stroke='#fff' stroke-width='1.5'/>")
        for x, b, c, u in zip(xs, df.base, df.cu, df.usable):
            if c and u and b <= vmax:                     # cumulus from thermals, not low stratus
                parts.append(f"<line x1='{x - bw * 0.38:.1f}' x2='{x + bw * 0.38:.1f}' y1='{y(b):.1f}' y2='{y(b):.1f}' "
                             f"stroke='{INK2}' stroke-width='2' stroke-dasharray='4 3'/>")
        parts.append(f"<line x1='{ml}' x2='{W - mr}' y1='{y(release_agl):.1f}' y2='{y(release_agl):.1f}' "
                     f"stroke='{INK}' stroke-width='1' stroke-dasharray='1 3'/>"
                     f"<text x='{W - mr - 2}' y='{y(release_agl) - 5:.1f}' text-anchor='end' font-size='11' "
                     f"fill='{INK2}'>winch release ~{release_agl:.0f} m</text>")
    else:
        col = df.climb if kind == 'climb' else df.chance
        best = int(np.nanargmax(col.to_numpy())) if np.nanmax(col.to_numpy()) > 0 else -1
        for i, (x, v) in enumerate(zip(xs, col)):
            if not np.isfinite(v) or v <= 0:
                continue
            on = df.usable.iloc[i] if kind == 'climb' else True
            w = bw * 0.56
            hgt = y(0) - y(v)
            r = min(4.0, hgt / 2)
            parts.append(f"<path d='M{x - w / 2:.1f},{y(0):.1f} V{y(v) + r:.1f} Q{x - w / 2:.1f},{y(v):.1f} "
                         f"{x - w / 2 + r:.1f},{y(v):.1f} H{x + w / 2 - r:.1f} Q{x + w / 2:.1f},{y(v):.1f} "
                         f"{x + w / 2:.1f},{y(v) + r:.1f} V{y(0):.1f} Z' fill='{BLUE if on else OFF}'/>")
            if i == best:
                lab = f"{v:.1f} m/s" if kind == 'climb' else f"{100 * v:.0f}%"
                parts.append(f"<text x='{x:.1f}' y='{y(v) - 5:.1f}' text-anchor='middle' font-size='11.5' "
                             f"font-weight='600' fill='{INK}'>{lab}</text>")
        if kind == 'chance' and club:
            parts.append(f"<line x1='{ml}' x2='{W - mr}' y1='{y(club):.1f}' y2='{y(club):.1f}' stroke='{INK}' "
                         f"stroke-width='1' stroke-dasharray='4 3'/><text x='{ml + 4}' y='{y(club) - 5:.1f}' "
                         f"font-size='11' fill='{INK2}'>club average {100 * club:.0f}%</text>")
    # hover: one hit area per hour with the values
    for i, r in df.iterrows():
        tip = (f"{r.hour}: " + (f"climbs ~{r.climb:.1f} m/s to ~{r.top:.0f} m" if r.usable else "no usable thermals")
               + f" · thermal top {r.blh:.0f} m · cloud base {r.base:.0f} m{' (Cu)' if r.cu else ''}"
               + f" · chance after winch {100 * r.chance:.0f}% (rwy {r.runway})")
        parts.append(f"<rect x='{xs[i] - bw / 2:.1f}' y='{mt}' width='{bw:.1f}' height='{H - mt - mb}' fill='transparent'>"
                     f"<title>{escape(tip)}</title></rect>")
    parts.append(f"<line x1='{ml}' x2='{W - mr}' y1='{y(0):.1f}' y2='{y(0):.1f}' stroke='{MUTED}'/></svg>")
    return ''.join(parts)


def render_day_forecast(day, df, summ, path, release_agl=300.0, club=None, cal=None):
    cal = cal or DEFAULT_CAL
    level_col = {4: '#0ca30c', 3: '#0ca30c', 2: '#c98a00', 1: '#d03b3b', 0: '#d03b3b'}[summ['level']]
    best = df.loc[df.chance.idxmax()] if df.chance.notna().any() else None
    mid = df.iloc[len(df) // 2]
    tiles = [
        ('Thermals', summ['window'] or '—', 'climb ≥ ~0.8 m/s, no rain'),
        ('Typical climb', f"{summ['climb']:.1f} m/s" if summ['climb'] else '—',
         f"best around {summ['best_hour']}" if summ['best_hour'] else ''),
        ('Climbs end', f"~{summ['top']:.0f} m" if summ['top'] else '—', 'above the field'),
        ('Cumulus', f"base ~{summ['cu_base']:.0f} m" if summ['cu_base'] else ('none (blue)' if summ['window'] else '—'), ''),
        ('Wind 100 m', f"{_sector(mid.wd100)} {mid.ws100:.0f} km/h", f"at {mid.hour}"),
        ('Chance after winch', f"{100 * best.chance:.0f}%" if best is not None else '—',
         f"at {best.hour}, rwy {best.runway}" if best is not None else ''),
    ]
    tiles_html = ''.join(f"<div class='tile'><div class='k'>{k}</div><div class='v'>{v}</div><div class='s'>{s}</div></div>"
                         for k, v, s in tiles)
    rain = (f"<p class='warn'>Rain or showers possible at {', '.join(summ['rain_hours'])}.</p>"
            if summ['rain_hours'] else '')

    def arrow(deg):
        return (f"<span style='display:inline-block;transform:rotate({(deg + 180) % 360:.0f}deg);color:{BLUE}'>&#8593;</span>")

    trs = ''.join(
        f"<tr class='{'' if r.usable else 'off'}'><td>{r.hour}</td><td>{100 * r.chance:.0f}%</td>"
        f"<td>{f'{r.climb:.1f}' if r.usable else '–'}</td><td>{f'{r.top:.0f}' if r.usable else '–'}</td>"
        f"<td>{r.blh:.0f}</td><td>{r.base:.0f}{' Cu' if r.cu else ''}</td><td>{r.cl_low:.0f}/{r.cl_mid:.0f}</td>"
        f"<td>{r.rain:.1f}</td><td>{arrow(r.wd10)} {_sector(r.wd10)} {r.ws10:.0f}</td>"
        f"<td>{arrow(r.wd100)} {_sector(r.wd100)} {r.ws100:.0f}</td><td>{r.runway} <span class='m'>{100 * r.runway_p:.0f}%</span></td></tr>"
        for r in df.itertuples())
    d = pd.Timestamp(day)
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>EHHV day forecast {day}</title>
<style>
* {{ box-sizing:border-box }}
body {{ margin:0; background:#fbfaf7; color:{INK}; font:14px/1.45 system-ui,-apple-system,'Segoe UI',sans-serif }}
main {{ max-width:1000px; margin:0 auto; padding:16px 16px 40px }}
h1 {{ font-size:20px; margin:0 0 2px }} h2 {{ font-size:15px; margin:22px 0 2px }}
.sub, .m {{ color:{INK2} }} .m {{ font-size:12px }}
.badge {{ display:inline-block; margin:8px 0 4px; padding:3px 10px; border-radius:999px; font-weight:700;
  color:#fff; background:{level_col} }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(160px,1fr)); gap:8px; margin-top:10px }}
.tile {{ background:#fff; border:1px solid #e1e0d9; border-radius:8px; padding:8px 10px }}
.tile .k {{ font-size:12px; color:{INK2} }} .tile .v {{ font-size:19px; font-weight:700 }} .tile .s {{ font-size:12px; color:{INK2} }}
.card {{ background:#fff; border:1px solid #e1e0d9; border-radius:8px; padding:10px 12px 6px; margin-top:6px }}
.legend {{ display:flex; flex-wrap:wrap; gap:4px 14px; font-size:12px; color:{INK2}; margin:2px 0 4px }}
.legend i {{ display:inline-block; width:14px; height:10px; margin-right:5px; vertical-align:-1px }}
.warn {{ background:#fff4ec; border:1px solid #f3c7ad; border-radius:8px; padding:8px 10px }}
.tbl {{ overflow-x:auto; margin-top:6px; background:#fff; border:1px solid #e1e0d9; border-radius:8px }}
table {{ border-collapse:collapse; width:100%; font-size:13px; white-space:nowrap }}
th, td {{ padding:5px 8px; text-align:right; border-bottom:1px solid #f0efe9 }}
th {{ font-weight:600; color:{INK2}; font-size:12px; background:#fbfaf7 }}
th:first-child, td:first-child {{ text-align:left }}
tr.off td {{ color:{MUTED} }}
.note {{ font-size:12px; color:{INK2}; margin-top:18px }}
</style></head><body><main>
<h1>Thermal forecast EHHV</h1>
<div class="sub">{d:%A %d %B %Y} · hours in Dutch time</div>
<div class="badge">{escape(summ['rating'])}</div>
<div>{escape(summ['text'])}</div>
{rain}
<div class="tiles">{tiles_html}</div>

<h2>How high</h2>
<div class="card">
<div class="legend"><span><i style="background:{BLUE_LIGHT}"></i>thermal top (boundary layer)</span>
<span><i style="background:{BLUE};height:3px;vertical-align:3px"></i>climbs usually end</span>
<span><i style="border-top:2px dashed {INK2};height:0;vertical-align:3px"></i>cumulus base</span>
<span><i style="border-top:1px dotted {INK};height:0;vertical-align:3px"></i>winch release</span></div>
{_chart(df, 'height', release_agl=release_agl)}
<div class="m">metres above the field</div></div>

<h2>How strong</h2>
<div class="card">{_chart(df, 'climb')}<div class="m">typical climb, m/s (grey: no usable thermals)</div></div>

<h2>Chance to find the first thermal after a winch launch</h2>
<div class="card">{_chart(df, 'chance', club=club)}<div class="m">for the most likely runway each hour</div></div>

<h2>Hour by hour</h2>
<div class="tbl"><table>
<tr><th>Hour</th><th>Chance</th><th>Climb m/s</th><th>Climbs end m</th><th>Thermal top m</th><th>Cloud base m</th>
<th>Low/mid cloud %</th><th>Rain mm</th><th>Wind 10 m km/h</th><th>Wind 100 m km/h</th><th>Runway</th></tr>
{trs}</table></div>

<p class="note">Forecast: Open-Meteo, made {datetime.now(C.TZ_LOCAL):%d %b %H:%M}. Thermal strength W* from the forecast
surface heat flux and boundary-layer height; typical climb = {cal['a']:.2f} + {cal['b']:.2f} × W*, fitted on {cal['n']:,} climbs
flown from Hilversum; climbs usually end at {100 * cal['top_ratio']:.0f}% of the boundary-layer height (capped at cloud base).
Usable: W* ≥ {USABLE_WSTAR:.1f} m/s, thermal top ≥ {USABLE_BLH:.0f} m and no rain. A model, not an observation — look outside.</p>
</main></body></html>"""
    path.write_text(html)
    return path
