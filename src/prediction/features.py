"""
Weather/time features shared by training and prediction (plan section 10.3).

Input is an hourly UTC weather table with the columns of `weather_conv`
(see src/weather/open_meteo_forecast.CONVECTION_COLUMNS); the same function
is used for history (weather_conv) and for the live forecast.
"""

import math

import numpy as np
import pandas as pd

from src import config as C
from src.weather.open_meteo_forecast import CONVECTION_COLUMNS

FEATURES = ['blh', 'cape', 'shf', 'sw_now', 'sw_prev2h', 'direct_frac', 'cl_low', 'cl_mid', 'spread',
            'cloudbase', 'w100_spd', 'w100_sin', 'w100_cos', 'ws10', 'shear', 'headwind_rwy',
            'sun_elev', 'h_since_sunrise', 'rain_24h', 'days_since_rain', 'soil', 'doy_sin', 'doy_cos',
            'wstar_proxy']


def weather_frame(rows) -> pd.DataFrame:
    """Rows/DataFrame with date, hour_utc + convection columns → hourly frame indexed by UTC time."""
    df = pd.DataFrame(rows).copy()
    df['ts'] = pd.to_datetime(df['date']) + pd.to_timedelta(df['hour_utc'], unit='h')
    df = df.set_index('ts').sort_index()
    df = df[~df.index.duplicated(keep='last')]
    for c in CONVECTION_COLUMNS:
        df[c] = pd.to_numeric(df.get(c), errors='coerce')
    return df[CONVECTION_COLUMNS]


def load_weather(conn) -> pd.DataFrame:
    return weather_frame(pd.read_sql_query("SELECT * FROM weather_conv", conn))


# ── solar geometry (NOAA approximation) ──

def _solar(doy, hour_utc):
    g = 2 * np.pi / 365 * (doy - 1 + (hour_utc - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * np.cos(g) - 0.032077 * np.sin(g)
                       - 0.014615 * np.cos(2 * g) - 0.040849 * np.sin(2 * g))
    decl = (0.006918 - 0.399912 * np.cos(g) + 0.070257 * np.sin(g) - 0.006758 * np.cos(2 * g)
            + 0.000907 * np.sin(2 * g) - 0.002697 * np.cos(3 * g) + 0.00148 * np.sin(3 * g))
    return eqtime, decl


def sun_elevation(doy, hour_utc, lat=C.SITE_LAT, lon=C.SITE_LON):
    """Solar elevation in degrees."""
    doy = np.asarray(doy, float)
    hour_utc = np.asarray(hour_utc, float)
    eqtime, decl = _solar(doy, hour_utc)
    tst = hour_utc * 60 + eqtime + 4 * lon
    ha = np.radians(tst / 4 - 180)
    phi = math.radians(lat)
    cosz = np.sin(phi) * np.sin(decl) + np.cos(phi) * np.cos(decl) * np.cos(ha)
    return 90 - np.degrees(np.arccos(np.clip(cosz, -1, 1)))


def sunrise_utc_hours(doy, lat=C.SITE_LAT, lon=C.SITE_LON):
    doy = np.asarray(doy, float)
    eqtime, decl = _solar(doy, 12.0)
    phi = math.radians(lat)
    cos_ha = np.cos(np.radians(90.833)) / (np.cos(phi) * np.cos(decl)) - np.tan(phi) * np.tan(decl)
    ha = np.degrees(np.arccos(np.clip(cos_ha, -1, 1)))
    return (720 - 4 * (lon + ha) - eqtime) / 60


# ── feature builder ──

def _interp(wx: pd.DataFrame, col: str, ts: pd.DatetimeIndex) -> np.ndarray:
    s = wx[col].dropna()
    if s.empty:
        return np.full(len(ts), np.nan)
    xs = s.index.asi8 / 1e9
    q = ts.asi8 / 1e9
    v = np.interp(q, xs, s.to_numpy(float))
    # do not extrapolate further than 2 hours from data
    nearest = np.abs(xs[np.clip(np.searchsorted(xs, q), 0, len(xs) - 1)] - q)
    nearest = np.minimum(nearest, np.abs(xs[np.clip(np.searchsorted(xs, q) - 1, 0, len(xs) - 1)] - q))
    v[nearest > 7200] = np.nan
    return v


def build_features(wx: pd.DataFrame, times, runway=None) -> pd.DataFrame:
    """
    Feature table for the given UTC times.

    Args:
        wx: hourly weather frame (weather_frame / load_weather)
        times: iterable of UTC timestamps (naive = UTC)
        runway: None, a runway string, or an iterable of runway strings per time
    """
    ts = pd.DatetimeIndex(pd.to_datetime(list(times)))
    if ts.tz is not None:
        ts = ts.tz_convert('UTC').tz_localize(None)
    n = len(ts)
    f = pd.DataFrame(index=range(n))
    hours = 3600 * 1e9

    for col in ('blh', 'cape', 'shf', 'cl_low', 'cl_mid', 'soil', 'ws10'):
        f[col] = _interp(wx, col, ts)
    f['sw_now'] = _interp(wx, 'sw', ts)
    f['sw_prev2h'] = 0.5 * (_interp(wx, 'sw', ts - pd.Timedelta(hours=0.5))
                            + _interp(wx, 'sw', ts - pd.Timedelta(hours=1.5)))
    direct = _interp(wx, 'direct', ts)
    f['direct_frac'] = np.where(f.sw_now > 20, direct / np.maximum(f.sw_now, 1), 0.0)
    t2m, td2m = _interp(wx, 't2m', ts), _interp(wx, 'td2m', ts)
    f['spread'] = np.clip(t2m - td2m, 0, 30)
    f['cloudbase'] = 125 * f['spread']

    # wind at 100 m: interpolate as a vector
    u100, v100 = C.wind_vector(wx['wd100'], wx['ws100'])
    tmp = pd.DataFrame({'u': u100, 'v': v100}, index=wx.index)
    u, v = _interp(tmp, 'u', ts), _interp(tmp, 'v', ts)
    spd = np.hypot(u, v) * 3.6
    from_dir = (np.degrees(np.arctan2(-u, -v)) + 360) % 360
    f['w100_spd'] = spd
    f['w100_dir'] = from_dir
    f['w100_sin'] = np.sin(np.radians(from_dir))
    f['w100_cos'] = np.cos(np.radians(from_dir))
    f['shear'] = spd - f['ws10']
    if runway is None or isinstance(runway, str):
        rw = [runway] * n
    else:
        rw = list(runway)
    hdg = np.array([C.RUNWAY_HEADING.get(r, np.nan) if isinstance(r, str) else np.nan for r in rw])
    f['headwind_rwy'] = spd * np.cos(np.radians(from_dir - hdg))

    doy = ts.dayofyear.to_numpy()
    hour = (ts.hour + ts.minute / 60 + ts.second / 3600).to_numpy()
    f['sun_elev'] = sun_elevation(doy, hour)
    f['h_since_sunrise'] = hour - sunrise_utc_hours(doy)
    f['doy_sin'] = np.sin(2 * np.pi * doy / 365.25)
    f['doy_cos'] = np.cos(2 * np.pi * doy / 365.25)

    # rain history
    pr = wx['precip'].fillna(0)
    cum = pr.cumsum()
    cq = cum.index.asi8
    def cum_at(q):
        idx = np.searchsorted(cq, q.asi8, side='right') - 1
        out = np.where(idx >= 0, cum.to_numpy()[np.clip(idx, 0, None)], 0.0)
        return out
    f['rain_24h'] = cum_at(ts) - cum_at(ts - pd.Timedelta(hours=24))
    daily = pr.groupby(pr.index.normalize()).sum()
    wet_days = daily.index[daily > 1.0].asi8
    day0 = ts.normalize().asi8
    if len(wet_days):
        k = np.searchsorted(wet_days, day0, side='left') - 1      # last wet day strictly before today
        dsr = np.where(k >= 0, (day0 - wet_days[np.clip(k, 0, None)]) / (24 * hours), 14)
    else:
        dsr = np.full(n, 14.0)
    f['days_since_rain'] = np.minimum(dsr, 14)

    f['wstar_proxy'] = np.cbrt(f['blh'] * np.maximum(f['sw_prev2h'] - 100, 0))
    return f
