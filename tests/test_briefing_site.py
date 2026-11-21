"""Briefing website: runway odds, day forecast, history pack, front page."""

import json
import sqlite3
from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.briefing_site import day_forecast as DF
from src.briefing_site.env import use_pack
from src.briefing_site.runway_odds import RUNWAYS, runway_odds
from src.briefing_site.site import front_page


def _hist():
    rows = []
    for d in range(60):
        west = d % 2 == 0
        for k in range(5):
            rows.append({'datum': f"2025-06-{1 + d % 30:02d}" if d < 30 else f"2025-07-{1 + d % 30:02d}",
                         'runway': '25' if west else '07', 'hour_utc': 10 + k,
                         'wd100': 255.0 if west else 75.0, 'ws100': 20.0})
    H = pd.DataFrame(rows)
    H['w'] = 1.0 / H.groupby('datum').datum.transform('size')
    return H


def test_runway_odds_follow_the_wind():
    H = _hist()
    odds = runway_odds(H, 250, 18)
    assert odds[0][0] == '25' and odds[0][1] > 0.9
    assert runway_odds(H, 80, 18)[0][0] == '07'
    assert abs(sum(p for _, p in odds) - 1) < 1e-9
    assert {r for r, _ in odds} == set(RUNWAYS)                 # every runway listed (switchable)


def test_runway_odds_calm_wind_is_uncertain():
    H = _hist()
    calm = dict(runway_odds(H, 250, 1))
    strong = dict(runway_odds(H, 250, 20))
    assert calm['25'] < strong['25']


def test_runway_odds_exclude_day_and_empty_history():
    H = _hist()
    day = H.datum.iloc[0]
    assert all(H[H.datum != day].datum != day)
    assert runway_odds(H, 250, 20, exclude_day=day)[0][0] == '25'
    empty = runway_odds(H.iloc[0:0], 250, 20)
    assert empty[0] == ('25', 1.0)                               # wind-sector rule as fallback


def test_wstar_known_value():
    # 194 W/m² upward, 26 °C, boundary layer 1590 m → about 2.0 m/s
    assert DF.wstar(-194.4, 26.2, 1590) == pytest.approx(2.03, abs=0.03)
    assert DF.wstar(50.0, 10, 800) == 0.0                         # downward flux: no thermals


def _day(climbs, tops, cu=False, rain=0.0):
    n = len(climbs)
    return pd.DataFrame({'hour': [f"{8 + i:02d}:00" for i in range(n)], 'climb': climbs, 'top': tops,
                         'usable': [c > 0 for c in climbs], 'cu': [cu] * n, 'base': [1400.0] * n,
                         'rain': [rain] * n})


def test_summary_ratings():
    assert DF.summary(_day([0, 0, 0], [0, 0, 0]))['rating'] == 'No thermals'
    s = DF.summary(_day([0, 1.2, 1.3, 1.1, 0], [0, 1100, 1250, 1200, 0], cu=True))
    assert s['rating'] == 'Good soaring day' and s['window'] == '09:00–12:00'
    assert s['cu_base'] == 1400 and s['best_hour'] == '10:00'
    assert DF.summary(_day([0.85, 0.95], [700, 900]))['rating'] == 'Fair soaring day'
    assert DF.summary(_day([1.5, 1.6], [1600, 1700]))['level'] == 4


def test_front_page_embeds_data_safely():
    info = {'date': '2026-07-28', 'made_utc': '2026-07-27T22:05:00+00:00',
            'day': {'page': 'forecast.html', 'rating': 'Good', 'level': 3, 'text': 'x</script><b>'},
            'windows': {'midday': {'label': '12–15', 'wind100': [250, 18], 'odds': [['25', 0.7], ['07', 0.3]],
                                   'pages': {'25': 'midday_rwy25.html', '07': 'midday_rwy07.html'},
                                   'chance': {'25': 0.3, '07': 0.2}}}}
    html = front_page(info, base='2026-07-28/')
    assert '</script><b>' not in html                            # no way out of the script block
    start = html.index('const D = ') + len('const D = ')
    data = json.loads(html[start:html.index(';\n', start)].replace('<\\/', '</'))
    assert data['windows']['midday']['odds'][0][0] == '25'
    assert 'const BASE = "2026-07-28/"' in html


def test_use_pack_must_come_first():
    import src.config  # noqa: F401  (already imported by the modules above)
    with pytest.raises(RuntimeError):
        use_pack('/tmp/nowhere')


def test_pack_export_has_no_names(tmp_path):
    from src.briefing_site.pack import export_pack, read_meta
    db = tmp_path / 'full.db'
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE flights (gezagvoerder_id INTEGER, gezagvoerder_naam TEXT)")
    con.executemany("INSERT INTO flights VALUES (?, ?)", [(7, 'Jan Jansen'), (9, 'Piet Pieters')])
    pd.DataFrame({'flight_id': [1, 2], 'datum': ['2025-06-01', '2025-06-02'], 'launch_type': ['winch'] * 2,
                  'runway': ['25', '07'], 'pilot_id': [7, 9], 'release_utc_s': [36000.0, 39600.0],
                  'connected': [1, 0], 'launch_quality': ['ok'] * 2, 'pA': [0.3, 0.1]}).to_sql('launches', con, index=False)
    pd.DataFrame({'flight_id': [1, 2], 'is_first': [1, 0], 'rate': [1.0, 0.5], 'agl1': [800, 600],
                  'start_utc_s': [36100.0, 39700.0]}).to_sql('climbs', con, index=False)
    pd.DataFrame({'date': ['2025-06-01', '2025-06-02'], 'hour_utc': [10, 11], 'wd100': [250.0, 80.0],
                  'ws100': [20.0, 15.0], 'blh': [1200.0, 900.0], 'shf': [-150.0, -100.0],
                  't2m': [22.0, 20.0]}).to_sql('weather_conv', con, index=False)
    con.commit()
    con.close()
    models = tmp_path / 'models'
    models.mkdir()
    for f in ('layer_a.pkl', 'layer_b.pkl', 'layer_c.pkl', 'tuned_params.json', 'acceptance.json'):
        (models / f).write_text('{}')
    air = tmp_path / 'air.txt'
    air.write_text('* empty\n')

    meta = export_pack(tmp_path / 'pack', db_path=db, me='Jansen', model_dir=models, airspace=air)
    assert meta['me_ids'] == [7]
    p = sqlite3.connect(tmp_path / 'pack' / 'history.db')
    tables = {r[0] for r in p.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {'launches', 'climbs', 'runway_history', 'meta'}
    assert p.execute("SELECT COUNT(*) FROM climbs").fetchone()[0] == 1          # first climbs only
    raw = (tmp_path / 'pack' / 'history.db').read_bytes()
    assert b'Jansen' not in raw and b'Pieters' not in raw
    assert read_meta(p)['thermal_cal']['n'] > 0 or read_meta(p)['thermal_cal'] == DF.DEFAULT_CAL
    with pytest.raises(ValueError):
        export_pack(tmp_path / 'pack2', db_path=db, me='Nobody', model_dir=models, airspace=air)


def test_day_table_from_forecast_rows(monkeypatch):
    """Hour rows 08–19 local with W*, climb and runway from a synthetic forecast."""
    import src.prediction.layer_a as A
    from src.prediction.features import weather_frame
    rows = []
    for h in range(0, 24):
        sun = max(0.0, np.sin((h - 4) / 16 * np.pi))
        rows.append({'date': '2025-06-15', 'hour_utc': h, 'blh': 200 + 1400 * sun, 'cape': 100, 'shf': -220 * sun,
                     'sw': 800 * sun, 'direct': 600 * sun, 'cl_low': 10, 'cl_mid': 0, 't2m': 15 + 8 * sun,
                     'td2m': 10, 'ws100': 18, 'wd100': 250, 'ws10': 10, 'wd10': 240, 'precip': 0, 'soil': 0.3})
    monkeypatch.setattr(A, 'predict', lambda bundle, feats, **k: np.full(len(feats), 0.25))
    df = DF.day_table(weather_frame(rows), date(2025, 6, 15), _hist(), bundle_a={'release_agl_all': 300})
    assert list(df.hour) == [f"{h:02d}:00" for h in range(8, 20)]
    noon = df[df.hour == '14:00'].iloc[0]
    assert noon.usable and noon.climb > 0.9 and noon.runway == '25'
    assert noon.top <= DF.DEFAULT_CAL['top_ratio'] * noon.blh + 1
    assert (df.chance == 0.25).all()
