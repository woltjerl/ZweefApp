"""
Loading helpers for the first-thermal models: launches, climbs and search tracks,
all in local metres (x east, y north of the field centre).
"""

import sqlite3

import numpy as np
import pandas as pd

from src import config as C
from src.prediction.features import build_features, load_weather


def connect(db_path=None):
    return sqlite3.connect(str(db_path or C.DB_PATH))


def winch_launches(conn, with_features=True, wx=None) -> pd.DataFrame:
    """Clean winch launches (quality ok), optionally with weather features at release time."""
    L = pd.read_sql_query("""
        SELECT * FROM launches
        WHERE launch_type='winch' AND launch_quality='ok' AND track_status='ok'
          AND release_utc_s IS NOT NULL
        ORDER BY datum, release_utc_s""", conn)
    L['ts'] = pd.to_datetime(L.datum) + pd.to_timedelta(L.release_utc_s, unit='s')
    L['hour_utc'] = L.release_utc_s / 3600.0
    L['month'] = pd.to_datetime(L.datum).dt.month
    L['two_seat'] = (L.glider_class == 'two_seat').astype(int)
    if with_features:
        wx = load_weather(conn) if wx is None else wx
        F = build_features(wx, L.ts, L.runway)
        L = pd.concat([L.reset_index(drop=True), F.reset_index(drop=True)], axis=1)
    return L


def climbs(conn) -> pd.DataFrame:
    return pd.read_sql_query("""
        SELECT c.*, l.launch_type, l.datum, l.runway, l.pilot_id
        FROM climbs c JOIN launches l ON l.flight_id = c.flight_id""", conn)


def search_tracks(conn, flight_ids=None) -> pd.DataFrame:
    q = "SELECT * FROM search_tracks"
    if flight_ids is not None:
        q += f" WHERE flight_id IN ({','.join(str(int(i)) for i in flight_ids)})"
    return pd.read_sql_query(q + " ORDER BY flight_id, t_utc_s", conn)


def layer_wind(conn) -> pd.DataFrame:
    """Hourly layer-mean wind (0.5 × (10 m + 100 m)) as a vector in m/s, plus 100 m wind."""
    w = pd.read_sql_query("SELECT date, hour_utc, wd10, ws10, wd100, ws100 FROM weather_conv", conn)
    x10, y10 = C.wind_vector(w.wd10, w.ws10)
    x100, y100 = C.wind_vector(w.wd100, w.ws100)
    w['wx'] = 0.5 * (x10 + x100)
    w['wy'] = 0.5 * (y10 + y100)
    return w


def attach_wind(L: pd.DataFrame, conn) -> pd.DataFrame:
    """Add layer wind vector (wx, wy) at release hour to a launches frame."""
    w = layer_wind(conn).set_index(['date', 'hour_utc'])
    h = np.clip(np.round(L.release_utc_s / 3600.0).astype(int), 0, 23)
    keys = list(zip(L.datum, h))
    wx = w['wx'].reindex(keys).to_numpy()
    wy = w['wy'].reindex(keys).to_numpy()
    L = L.copy()
    L['wx'] = np.nan_to_num(wx)
    L['wy'] = np.nan_to_num(wy)
    return L
