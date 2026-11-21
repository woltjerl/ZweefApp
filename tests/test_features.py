import numpy as np
import pandas as pd

from src.prediction.features import build_features, sun_elevation, sunrise_utc_hours, weather_frame


def test_sun_elevation_known_values():
    # De Bilt-ish, 21 June, solar noon ≈ 11:40 UTC: elevation ≈ 61°
    assert abs(sun_elevation(172, 11.67) - 61.2) < 1.0
    # 21 December noon: ≈ 14.4°
    assert abs(sun_elevation(355, 11.73) - 14.4) < 1.0
    # night
    assert sun_elevation(172, 0.0) < 0


def test_sunrise_times():
    assert abs(sunrise_utc_hours(172) - 3.3) < 0.2     # 05:18 local summer time
    assert abs(sunrise_utc_hours(355) - 7.8) < 0.2     # 08:47 local winter time


def _wx(hours=48, over=None):
    over = over or {}
    rows = []
    for h in range(hours):
        r = {'date': f"2025-06-{1 + h // 24:02d}", 'hour_utc': h % 24, 'blh': 1000.0, 'cape': 50.0,
             'shf': 100.0, 'sw': 600.0 if 6 <= h % 24 <= 18 else 0.0, 'direct': 400.0, 'cl_low': 10.0,
             'cl_mid': 0.0, 't2m': 20.0, 'td2m': 10.0, 'ws100': 20.0, 'wd100': 270.0, 'ws10': 10.0,
             'wd10': 260.0, 'precip': 0.0, 'soil': 0.3}
        r.update(over.get(h, {}))
        rows.append(r)
    return weather_frame(rows)


def test_features_interpolate_and_wind():
    wx = _wx()
    f = build_features(wx, [pd.Timestamp('2025-06-02 12:30')], runway='25')
    assert abs(f.spread[0] - 10) < 1e-6
    assert abs(f.cloudbase[0] - 1250) < 1e-6
    assert abs(f.w100_spd[0] - 20) < 0.01
    # runway 25 (249.6°) with wind from 270°: mostly headwind
    assert abs(f.headwind_rwy[0] - 20 * np.cos(np.radians(270 - 249.6))) < 0.1


def test_rain_history():
    wx = _wx(over={2: {'precip': 3.0}})
    f = build_features(wx, [pd.Timestamp('2025-06-02 12:00')])
    assert f.days_since_rain[0] == 1
    assert f.rain_24h[0] == 0
    f2 = build_features(wx, [pd.Timestamp('2025-06-01 12:00')])
    assert f2.rain_24h[0] == 3.0
