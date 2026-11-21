"""
Which runway will the club use? Odds from history: past winch launches weighted by how
similar the 100 m wind at launch time was to the forecast wind.

Weight of a past launch = direction kernel × speed kernel × 1 / (launches that day), so every
day counts once. The direction kernel is von Mises-like and gets sharper as the wind gets
stronger (in light wind the runway choice depends less on direction). Leave-one-day-out on
657 day-windows (2024-2026): most likely runway right 69%, right runway in the top two 91%;
the fixed wind-sector rule (first_thermal_map.runway_for_wind) is right 64%.
"""

import numpy as np
import pandas as pd

from src import config as C
from src.prediction.first_thermal_map import runway_for_wind

RUNWAYS = ('07', '12', '18', '25', '30', '36')
KAPPA = 16.0            # direction kernel sharpness at ≥ 10 km/h
KAPPA_FLOOR = 0.2       # direction still counts a little in calm wind
SPEED_SIGMA = 4.0       # km/h
PRIOR = 1e-3            # small share for every runway (no runway is ever impossible)


def runway_history(conn) -> pd.DataFrame:
    """Past winch launches with runway and the 100 m wind at release hour (from weather_conv)."""
    L = pd.read_sql_query("""
        SELECT l.datum, l.runway, CAST(l.release_utc_s / 3600 AS INTEGER) AS hour_utc, w.wd100, w.ws100
        FROM launches l JOIN weather_conv w ON w.date = l.datum
             AND w.hour_utc = CAST(l.release_utc_s / 3600 AS INTEGER)
        WHERE l.launch_type = 'winch' AND l.runway IS NOT NULL AND l.release_utc_s IS NOT NULL
          AND w.wd100 IS NOT NULL AND w.ws100 IS NOT NULL""", conn)
    L['w'] = 1.0 / L.groupby('datum').datum.transform('size')
    return L


def runway_odds(hist: pd.DataFrame, wd100: float, ws100: float, exclude_day=None):
    """[(runway, probability), ...] most likely first."""
    H = hist if exclude_day is None else hist[hist.datum != str(exclude_day)]
    if len(H) == 0:
        rw = runway_for_wind(wd100)
        return [(rw, 1.0)] + [(r, 0.0) for r in RUNWAYS if r != rw]
    dd = np.radians(np.asarray(C.angdiff(H.wd100.to_numpy(float), wd100), float))
    kappa = KAPPA * min(1.0, ws100 / 10.0) + KAPPA_FLOOR
    k = (np.exp(kappa * (np.cos(dd) - 1))
         * np.exp(-((H.ws100.to_numpy(float) - ws100) ** 2) / (2 * SPEED_SIGMA ** 2))
         * H.w.to_numpy(float))
    s = pd.Series(k).groupby(H.runway.to_numpy()).sum().reindex(list(RUNWAYS), fill_value=0.0)
    s = s / s.sum() if s.sum() > 0 else s
    s = (s + PRIOR) / (s + PRIOR).sum()
    return sorted(((r, float(p)) for r, p in s.items()), key=lambda t: -t[1])


def evaluate(hist: pd.DataFrame, min_launches=3) -> dict:
    """Leave-one-day-out check per day and time window (morning/midday/afternoon)."""
    from src.airspace.schedule import utc_offset_hours
    H = hist.copy()
    H['loc_h'] = H.hour_utc + H.datum.map(utc_offset_hours)
    H['win'] = pd.cut(H.loc_h, [0, 12, 15, 24], labels=['morning', 'midday', 'afternoon'], right=False)

    def circmean(d):
        r = np.radians(d)
        return float(np.degrees(np.arctan2(np.sin(r).mean(), np.cos(r).mean())) % 360)

    cases = H.groupby(['datum', 'win'], observed=True).agg(
        rw=('runway', lambda s: s.mode().iloc[0]), n=('runway', 'size'),
        wd=('wd100', circmean), ws=('ws100', 'mean')).reset_index()
    cases = cases[cases.n >= min_launches]
    top1 = top2 = rule = 0
    for r in cases.itertuples():
        order = [rw for rw, _ in runway_odds(hist, r.wd, r.ws, exclude_day=r.datum)]
        top1 += order[0] == r.rw
        top2 += r.rw in order[:2]
        rule += runway_for_wind(r.wd) == r.rw
    n = max(len(cases), 1)
    return {'cases': len(cases), 'top1': top1 / n, 'top2': top2 / n, 'rule_top1': rule / n}
