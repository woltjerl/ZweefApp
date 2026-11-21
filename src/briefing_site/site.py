"""
Static briefing website for one day.

    <out>/index.html                     front page for the day (runway and time-window switcher)
    <out>/latest.json                    what the front page shows (date, odds, page names)
    <out>/<date>/index.html              the same front page, kept per day
    <out>/<date>/<window>_rwy<rw>.html   one briefing map per time window and runway
    <out>/<date>/forecast.html           thermal forecast for the whole day (day_forecast)
    <out>/<date>/briefing.json

The runway buttons are ordered by how likely each runway is in the forecast wind
(runway_odds); the most likely one opens first. Pages are plain HTML, so the folder can be
served from any static host (cloud/README: a public Cloud Storage bucket).
"""

import json
import time
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path

import numpy as np
import pandas as pd

from src import config as C
from src.briefing_site.day_forecast import (DEFAULT_CAL, day_table, render_day_forecast, summary,
                                            thermal_calibration)
from src.briefing_site.pack import read_meta
from src.briefing_site.runway_odds import RUNWAYS, runway_history, runway_odds
from src.prediction.briefing_map import build_briefing, render_briefing
from src.prediction.features import weather_frame
from src.prediction.first_thermal_map import WINDOWS_LOCAL, build_forecast, window_utc

WINDOWS = ('morning', 'midday', 'afternoon')


def _history(conn):
    try:
        return pd.read_sql_query("SELECT * FROM runway_history", conn)
    except Exception:  # noqa: BLE001  (full database: no precomputed table)
        return runway_history(conn)


def _calibration(conn, meta):
    if meta.get('thermal_cal'):
        return meta['thermal_cal']
    try:
        return thermal_calibration(conn)
    except Exception:  # noqa: BLE001  (no weather_conv in this database)
        return dict(DEFAULT_CAL)


def _wind_at(wx, t):
    near = wx.iloc[int(np.argmin(np.abs((wx.index - t).total_seconds())))]
    return float(near.wd100), float(near.ws100)


def build_site(conn, day: date, out_dir, weather_rows=None, me_ids=None, exclude_day=False,
               windows=WINDOWS, runways=RUNWAYS, log=print) -> dict:
    """Write all pages for `day` into out_dir; returns the day's index data."""
    from src.weather.open_meteo_forecast import fetch_convection_forecast

    t0 = time.time()
    out = Path(out_dir)
    day_dir = out / str(day)
    day_dir.mkdir(parents=True, exist_ok=True)
    rows = weather_rows if weather_rows is not None else fetch_convection_forecast(past_days=3, forecast_days=3)
    wx = weather_frame(rows)
    hist = _history(conn)
    meta = read_meta(conn)
    if me_ids is None:
        me_ids = meta.get('me_ids') or []

    info = {'date': str(day), 'made_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'windows': {}}

    # thermal forecast for the whole day
    from src.prediction import layer_a as A
    bundle_a = A.load()
    cal = _calibration(conn, meta)
    df = day_table(wx, day, hist, cal, bundle_a)
    summ = summary(df)
    club = conn.execute("SELECT AVG(connected) FROM launches WHERE launch_type = 'winch' "
                        "AND launch_quality = 'ok' AND pA IS NOT NULL").fetchone()[0]
    render_day_forecast(day, df, summ, day_dir / 'forecast.html', release_agl=float(bundle_a['release_agl_all']),
                        club=club, cal=cal)
    info['day'] = {'page': 'forecast.html', 'rating': summ['rating'], 'level': summ['level'], 'text': summ['text']}
    log(f"  {day}: {summ['rating']} — {summ['text']}")
    for win in windows:
        lo, hi = WINDOWS_LOCAL[win]
        wd, ws = _wind_at(wx, window_utc(day, win)[1])
        odds = [(rw, p) for rw, p in runway_odds(hist, wd, ws, exclude_day=day if exclude_day else None)
                if rw in runways]
        pages, chance = {}, {}
        for rw, _ in odds:
            fc = build_forecast(conn, day, win, rw, weather_rows=rows, exclude_day=exclude_day)
            br = build_briefing(conn, fc, me_ids=me_ids, exclude_day=exclude_day)
            name = f"{win}_rwy{rw}.html"
            render_briefing(fc, br, day_dir / name)
            pages[rw] = name
            chance[rw] = round(fc['pA'], 3)
        info['windows'][win] = {'label': f"{lo}–{hi}", 'wind100': [round(wd), round(ws)],
                                'odds': [[rw, round(p, 3)] for rw, p in odds], 'pages': pages, 'chance': chance}
        log(f"  {day} {win}: runways {', '.join(f'{rw} {100 * p:.0f}%' for rw, p in odds[:3])} … "
            f"({time.time() - t0:.0f} s)")

    (day_dir / 'briefing.json').write_text(json.dumps(info, indent=1))
    (day_dir / 'index.html').write_text(front_page(info, base=''))
    (out / 'index.html').write_text(front_page(info, base=f"{day}/"))
    (out / 'latest.json').write_text(json.dumps({**info, 'base': f"{day}/"}, indent=1))
    return info


def front_page(info: dict, base: str) -> str:
    """Front page: header with time-window and runway buttons, the briefing map below."""
    data = json.dumps(info, ensure_ascii=True).replace('</', '<\\/')
    d = date.fromisoformat(info['date'])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EHHV thermal briefing {escape(info['date'])}</title>
<style>
:root {{ --ink:#0b0b0b; --ink2:#52514e; --line:#e1e0d9; --bg:#fbfaf7; --acc:#0b0b0b; --warn:#ec835a; }}
* {{ box-sizing:border-box }}
html, body {{ margin:0; height:100%; background:var(--bg); color:var(--ink);
  font:14px/1.4 system-ui,-apple-system,'Segoe UI',sans-serif }}
body {{ display:flex; flex-direction:column; height:100dvh }}
header {{ display:flex; flex-wrap:wrap; align-items:center; gap:8px 18px; padding:8px 14px;
  border-bottom:1px solid var(--line); background:#fff }}
.title b {{ font-size:15px }} .title span {{ color:var(--ink2) }}
.group {{ display:flex; align-items:center; gap:6px; min-width:0; max-width:100% }}
.group > label {{ color:var(--ink2); font-size:12px; white-space:nowrap }}
.chips {{ display:flex; gap:6px; min-width:0; overflow-x:auto; scrollbar-width:none; padding:2px 0 }}
.chips::-webkit-scrollbar {{ display:none }}
button.chip {{ flex:none; border:1px solid #d6d5ce; background:#fff; color:var(--ink); border-radius:999px;
  padding:5px 11px; font:600 13px system-ui,-apple-system,sans-serif; cursor:pointer; white-space:nowrap }}
button.chip small {{ font-weight:400; color:var(--ink2); margin-left:4px }}
button.chip.on {{ background:var(--acc); border-color:var(--acc); color:#fff }}
button.chip.on small {{ color:#d6d5ce }}
button.chip .tag {{ font-size:10.5px; font-weight:600; color:#0a7d0a; margin-left:5px }}
button.chip.on .tag {{ color:#9be09b }}
.dayline {{ padding:6px 14px; background:#fff; border-bottom:1px solid var(--line); font-size:13px }}
.dayline b {{ margin-right:4px }}
.dayline a {{ color:#2a78d6; font-weight:600; text-decoration:none; white-space:nowrap }}
.lvl {{ display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; vertical-align:0 }}
.note {{ display:none; padding:6px 14px; background:#fff4ec; border-bottom:1px solid #f3c7ad; font-size:13px }}
.note.show {{ display:block }}
.meta {{ color:var(--ink2); font-size:12px; margin-left:auto }}
iframe {{ flex:1; width:100%; border:0; display:block; background:#eee }}
@media (max-width:700px) {{
  header {{ padding:6px 10px; gap:6px 12px }}
  .meta {{ display:none }}
  .group.rw {{ width:100% }}
}}
</style></head>
<body>
<header>
  <div class="title"><b>EHHV thermal briefing</b> <span>{d:%a %d %b %Y}</span></div>
  <div class="group"><label>Time</label><div class="chips" id="win"></div></div>
  <div class="group rw"><label>Runway<br>(likely)</label><div class="chips" id="rw"></div></div>
  <div class="meta" id="meta"></div>
</header>
<div class="dayline" id="dayline"></div>
<div class="note" id="note"></div>
<iframe id="map" title="Thermal briefing map"></iframe>
<script>
const D = {data};
const BASE = {json.dumps(base)};
const WIN_ORDER = ['morning', 'midday', 'afternoon'].filter(w => D.windows[w]);
const fmt = (o) => new Intl.DateTimeFormat('en-CA', Object.assign({{timeZone: 'Europe/Amsterdam'}}, o)).format(new Date());
const today = fmt({{year: 'numeric', month: '2-digit', day: '2-digit'}});
const hour = Number(fmt({{hour: '2-digit', hourCycle: 'h23'}}));

let state = {{v: 'map', w: null, r: null}};     // v: map | day; r = null: most likely runway for the window
const h = new URLSearchParams(location.hash.slice(1));
if (h.get('v') === 'day' && D.day) state.v = 'day';
if (D.windows[h.get('w')]) state.w = h.get('w');
if (h.get('r')) state.r = h.get('r');
if (!state.w) {{
  let w = 'midday';
  if (D.date === today) w = hour < 12 ? 'morning' : hour < 15 ? 'midday' : 'afternoon';
  state.w = D.windows[w] ? w : WIN_ORDER[0];
}}

function chip(label, small, on, tag, onclick) {{
  const b = document.createElement('button');
  b.className = 'chip' + (on ? ' on' : '');
  b.type = 'button';
  b.innerHTML = label + (small ? '<small>' + small + '</small>' : '') + (tag ? '<span class="tag">' + tag + '</span>' : '');
  b.onclick = onclick;
  return b;
}}

const LVL = ['#d03b3b', '#d03b3b', '#c98a00', '#0ca30c', '#0ca30c'];
function showDay(on) {{ state.v = on ? 'day' : 'map'; render(); }}

function render() {{
  const W = D.windows[state.w];
  const rw = (state.r && W.pages[state.r]) ? state.r : W.odds[0][0];
  const day = state.v === 'day';
  const win = document.getElementById('win'); win.innerHTML = '';
  if (D.day) win.appendChild(chip('Day forecast', '', day, '', () => showDay(true)));
  WIN_ORDER.forEach(w => win.appendChild(chip(D.windows[w].label + ' h', '', !day && w === state.w, '',
    () => {{ state.w = w; showDay(false); }})));
  const rws = document.getElementById('rw'); rws.innerHTML = '';
  W.odds.forEach(([r, p], i) => rws.appendChild(chip(r, Math.round(100 * p) + '%', r === rw,
    i === 0 ? 'most likely' : '', () => {{ state.r = r; showDay(false); }})));
  document.querySelector('.group.rw').style.display = day ? 'none' : '';
  const src = BASE + (day ? D.day.page : W.pages[rw]);
  const f = document.getElementById('map');
  if (f.getAttribute('src') !== src) f.setAttribute('src', src);
  history.replaceState(null, '', '#' + (day ? 'v=day&' : '') + 'w=' + state.w + (state.r ? '&r=' + state.r : ''));
  const dl = document.getElementById('dayline');
  if (D.day) {{
    dl.innerHTML = '<span class="lvl" style="background:' + LVL[D.day.level] + '"></span><b></b><span></span> ';
    dl.querySelector('b').textContent = D.day.rating;
    dl.querySelector('span:not(.lvl)').textContent = '· ' + D.day.text + ' ';
    const a = document.createElement('a'); a.href = '#';
    a.textContent = day ? '‹ back to the map' : 'Day forecast ›';
    a.onclick = (e) => {{ e.preventDefault(); showDay(!day); }};
    dl.appendChild(a);
  }} else dl.style.display = 'none';
  document.getElementById('meta').textContent = 'wind 100 m ' + W.wind100[0] + '° ' + W.wind100[1] +
    ' km/h · made ' + new Date(D.made_utc).toLocaleString('en-GB', {{timeZone: 'Europe/Amsterdam',
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}});
}}

const note = document.getElementById('note');
if (D.date < today) {{
  note.textContent = 'This briefing is for ' + D.date + ' — today\\'s has not been made yet.';
  note.classList.add('show');
}}
render();
</script>
</body></html>
"""
