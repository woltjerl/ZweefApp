#!/usr/bin/env python3
"""
Climb detector (Phase 1 of docs/FIRST_THERMAL_PLAN.md).

For every glider flight:
  1. cut the logger track to the logbook flight (take-off nearest the logbook start),
  2. compute height above the flight's own ground level,
  3. find the end of the launch: winch release, aerotow release (tug-track pairing,
     or a powered-climb signature as fallback) or self-launch engine stop,
  4. detect real circling climbs (gain, duration, accumulated turning),
  5. correct each climb for wind drift to estimate its ground source,
  6. for winch launches: store the search track from release to first climb.

Writes tables `launches`, `climbs`, `search_tracks` and `ft_meta`.

Usage:
    python -m src.thermal_detection.climb_detector            # incremental
    python -m src.thermal_detection.climb_detector --force    # rebuild everything
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import config as C  # noqa: E402

LAUNCH_TYPES = {'lier': 'winch', 'sleep': 'aerotow', 'zelf': 'self'}
TUG_METHODS = ('sep-a', 'tmg-a')


# ═══════════════════════════ track preparation ═══════════════════════════

def parse_times(time_str: pd.Series) -> np.ndarray:
    s = time_str.str
    return (s[0:2].astype(int) * 3600 + s[3:5].astype(int) * 60 + s[6:8].astype(int)).to_numpy(float)


def clean_track(t, x, y, alt):
    """Unwrap midnight, drop non-increasing times and impossible altitudes."""
    t = np.asarray(t, float).copy()
    d = np.diff(t)
    wraps = np.concatenate([[0], np.cumsum(np.where(d < -43200, 86400, 0))])
    t = t + wraps
    ok = np.isfinite(alt) & (alt > -100) & (alt < 6000) & np.isfinite(x) & np.isfinite(y)
    t, x, y, alt = t[ok], np.asarray(x)[ok], np.asarray(y)[ok], np.asarray(alt, float)[ok]
    if len(t) == 0:
        return t, x, y, alt
    keep = np.concatenate([[True], np.diff(t) > 0])
    # enforce strictly increasing (drop fixes that go back in time)
    tm = np.maximum.accumulate(t)
    keep &= t >= tm
    keep[1:] &= np.diff(tm) > 0
    return t[keep], x[keep], y[keep], alt[keep]


def ground_speed(t, x, y):
    if len(t) < 2:
        return np.zeros(len(t))
    v = np.hypot(np.diff(x), np.diff(y)) / np.maximum(np.diff(t), 0.5)
    v = np.concatenate([[v[0]], v])
    return pd.Series(v).rolling(5, center=True, min_periods=1).median().to_numpy()


def _runs(mask):
    """Start/end (inclusive) indices of True runs."""
    if len(mask) == 0:
        return []
    m = np.concatenate([[False], mask, [False]]).astype(int)
    d = np.diff(m)
    return list(zip(np.where(d == 1)[0], np.where(d == -1)[0] - 1))


def airborne_intervals(t, x, y, alt):
    """
    Split a track into flights.

    Returns list of dicts: {i_ground_before, i_takeoff, i_landing, i_ground_after,
    starts_airborne, ends_airborne}.
    """
    n = len(t)
    if n < 10:
        return []
    v = ground_speed(t, x, y)
    ground = []
    for a, b in _runs(v < C.LANDED_SPEED_MS):
        if t[b] - t[a] >= C.LANDED_HOLD_S and np.ptp(alt[a:b + 1]) <= 25:
            ground.append((a, b))
    fast = v > C.TAKEOFF_SPEED_MS

    # gaps between ground runs are candidate flights
    bounds = [(-1, -1)] + ground + [(n, n)]
    out = []
    for (ga, gb), (na, nb) in zip(bounds[:-1], bounds[1:]):
        lo, hi = gb + 1, na - 1
        if hi - lo < 10:
            continue
        seg_fast = fast[lo:hi + 1]
        runs = [(a, b) for a, b in _runs(seg_fast) if t[lo + b] - t[lo + a] >= 20]
        if not runs:
            continue
        out.append({
            'i_ground_before': gb if gb >= 0 else None,   # last fix of the ground run before
            'i_takeoff': lo + runs[0][0],
            'i_landing': hi,
            'i_ground_after': na if na < n else None,     # first fix of the ground run after
            'starts_airborne': gb < 0 and bool(fast[0]),
            'ends_airborne': na >= n,
        })
    return out


def select_interval(intervals, t, logbook_start_utc):
    """Pick the flight whose take-off matches the logbook start time."""
    if not intervals:
        return None, 'no_flight_in_track'
    if logbook_start_utc is None:
        return (intervals[0], 'ok') if len(intervals) == 1 else (None, 'ambiguous_no_logbook_time')

    def gap(iv):
        tt = t[iv['i_takeoff']]
        d = tt - logbook_start_utc
        # handle a track day that differs from the logbook day by exactly 24h (rare)
        return min(abs(d), abs(d - 86400), abs(d + 86400))

    best = min(intervals, key=gap)
    if gap(best) <= C.TRACK_MATCH_TOL_S:
        return best, 'ok'
    # logger switched on in flight: accept if logbook start lies inside the airborne part
    for iv in intervals:
        if iv['starts_airborne'] and t[iv['i_takeoff']] - C.TRACK_MATCH_TOL_S <= logbook_start_utc <= t[iv['i_landing']]:
            return iv, 'starts_airborne'
    return None, 'no_takeoff_match'


def ground_level(t, alt, iv):
    """Median altitude on the ground before take-off (or after landing)."""
    if iv['i_ground_before'] is not None:
        gb = iv['i_ground_before']
        sel = (t <= t[gb]) & (t >= t[gb] - 60)
        if sel.sum() >= 3:
            return float(np.median(alt[sel])), False
    if iv['i_ground_after'] is not None:
        ga = iv['i_ground_after']
        sel = (t >= t[ga]) & (t <= t[ga] + 60)
        if sel.sum() >= 3:
            return float(np.median(alt[sel])), False
    return float(np.min(alt[iv['i_takeoff']:iv['i_landing'] + 1])), True


# ═══════════════════════════ signal helpers ═══════════════════════════

def rate_over(t, v, window_s):
    """Forward difference of v over ~window_s seconds."""
    j = np.searchsorted(t, t + window_s)
    j = np.clip(j, 0, len(t) - 1)
    dt = t[j] - t
    with np.errstate(invalid='ignore', divide='ignore'):
        r = np.where(dt > 0, (v[j] - v) / dt, 0.0)
    return r


def window_mean(t, v, window_s):
    """Centered time-window mean of v."""
    cs = np.concatenate([[0.0], np.cumsum(v)])
    j0 = np.searchsorted(t, t - window_s / 2)
    j1 = np.searchsorted(t, t + window_s / 2, side='right')
    return (cs[j1] - cs[j0]) / np.maximum(j1 - j0, 1)


def cumulative_turn(x, y):
    """Cumulative signed heading change (deg) along the ground track."""
    n = len(x)
    if n < 3:
        return np.zeros(n)
    dx, dy = np.diff(x), np.diff(y)
    hdg = np.degrees(np.arctan2(dx, dy))
    dh = (np.diff(hdg) + 180) % 360 - 180
    step = np.hypot(dx, dy)
    dh[(step[1:] < C.MIN_STEP_M) | (step[:-1] < C.MIN_STEP_M)] = 0
    return np.concatenate([[0.0, 0.0], np.cumsum(dh)])


def zigzag_segments(t, agl, start=0):
    """
    Split into rising segments (low point → top). A segment closes when the glider
    drops DRAWDOWN_M below its top, or at a logging gap.
    """
    segs = []
    n = len(agl)
    if start >= n:
        return segs
    lo = hi = start
    for i in range(start + 1, n):
        if t[i] - t[i - 1] > C.GAP_S:
            segs.append((lo, hi))
            lo = hi = i
            continue
        a = agl[i]
        if a > agl[hi]:
            hi = i
        elif agl[hi] - a > C.DRAWDOWN_M:
            segs.append((lo, hi))
            lo = hi = i
        elif a < agl[lo]:
            lo = hi = i
    segs.append((lo, hi))
    return [(a, b) for a, b in segs if b > a]


# ═══════════════════════════ launch end detection ═══════════════════════════

def winch_release(t, agl, i_to):
    """Return (release_index, quality)."""
    vz5 = rate_over(t, agl, 5.0)
    win = np.where((t >= t[i_to] - 5) & (t <= t[i_to] + 60))[0]
    if len(win) == 0:
        return None, 'no_launch'
    fast = win[vz5[win] > C.WINCH_MIN_RATE]
    if len(fast) == 0:
        return None, 'no_launch'
    i_start = fast[0]
    after = np.where((t >= t[i_start]) & (t <= t[i_start] + 150))[0]
    i_peak_rate = after[np.argmax(vz5[after])]
    later = after[after >= i_peak_rate]
    slow = later[vz5[later] < 3.0]
    if len(slow) == 0:
        return None, 'no_release'
    k = slow[0]
    sel = np.where((t >= t[k]) & (t <= t[k] + 8))[0]
    rel = sel[np.argmax(agl[sel])]
    quality = 'ok'
    if not (C.WINCH_REL_MIN_AGL <= agl[rel] <= C.WINCH_REL_MAX_AGL):
        quality = 'suspect'
    if agl[i_start] > 80 or t[rel] - t[i_start] > 120:
        quality = 'suspect'
    return rel, quality


def _grid(t, i0, t_max_s):
    tg = np.arange(t[i0], min(t[-1], t[i0] + t_max_s), 1.0)
    return tg


def tow_release_signature(t, x, y, agl, i_to):
    """Powered-climb signature: release = first sustained descent or release turn."""
    tg = _grid(t, i_to, 2400)
    if len(tg) < 120:
        return None
    ag = np.interp(tg, t, agl)
    xg, yg = np.interp(tg, t, x), np.interp(tg, t, y)
    vz = np.zeros_like(ag)
    vz[10:-10] = (ag[20:] - ag[:-20]) / 20.0
    hd = np.degrees(np.arctan2(np.diff(xg), np.diff(yg)))
    dh = (np.diff(hd) + 180) % 360 - 180
    step = np.hypot(np.diff(xg), np.diff(yg))
    dh[(step[1:] < 3) | (step[:-1] < 3)] = 0
    turn = np.concatenate([[0, 0], dh])
    turn10 = np.abs(pd.Series(turn).rolling(10, center=True, min_periods=1).mean().to_numpy())

    start = 60
    desc = vz < 0
    turning = turn10 > C.RELEASE_TURN
    for i in range(start, len(tg) - 15):
        if ag[i] < 150:
            continue
        if desc[i:i + 15].all() or turning[i:i + 10].all():
            return float(tg[i])
    return None


def tow_release_paired(gt, gx, gy, gagl, i_to, tug):
    """Release from separation between glider and tug tracks. Returns (t_release, lag) or (None, reason)."""
    tt, tx, ty, tagl = tug['t'], tug['x'], tug['y'], tug['agl']
    t_to = gt[i_to]
    if tt[0] > t_to + 30 or tt[-1] < t_to + 180:
        return None, 'tug_track_does_not_cover_tow'
    win = (gt >= t_to + 20) & (gt <= t_to + 120)
    if win.sum() < 30:
        return None, 'too_few_fixes'
    # clock offset between the loggers: the lag that keeps the glider closest behind
    # the tug (rope length) while both climb out
    best_lag, best_err = 0.0, np.inf
    for lag in np.arange(-30, 30.5, 0.5):
        tq = gt[win] + lag
        ok = (tq >= tt[0]) & (tq <= tt[-1])
        if ok.sum() < 30:
            continue
        hd = np.hypot(gx[win][ok] - np.interp(tq[ok], tt, tx), gy[win][ok] - np.interp(tq[ok], tt, ty))
        err = np.median(hd)
        if err < best_err:
            best_lag, best_err = lag, err
    if best_err > 120:
        return None, 'tracks_do_not_match'
    tq = gt[win] + best_lag
    ok = (tq >= tt[0]) & (tq <= tt[-1])
    if np.median(np.abs(np.interp(tq[ok], tt, tagl) - gagl[win][ok])) > 40:
        return None, 'tracks_do_not_match'

    sel = np.where((gt >= t_to + 30) & (gt + best_lag <= tt[-1]))[0]
    if len(sel) < 10:
        return None, 'no_overlap'
    tq = gt[sel] + best_lag
    h = np.hypot(gx[sel] - np.interp(tq, tt, tx), gy[sel] - np.interp(tq, tt, ty))
    v = np.abs(gagl[sel] - np.interp(tq, tt, tagl))
    h = pd.Series(h).rolling(5, center=True, min_periods=1).median().to_numpy()   # GPS spikes
    v = pd.Series(v).rolling(5, center=True, min_periods=1).median().to_numpy()
    sep = (h > C.TOW_SEP_H_M) | (v > C.TOW_SEP_V_M)
    for a, b in _runs(sep):
        if gt[sel[b]] - gt[sel[a]] >= C.TOW_SEP_HOLD_S:
            # separation becomes visible a few seconds after the release: step back to
            # where it started to grow beyond this tow's normal rope distance (max 30 s)
            base_sel = (gt[sel] < gt[sel[a]] - 30)
            h0 = np.median(h[base_sel]) if base_sel.sum() >= 5 else 60.0
            v0 = np.median(v[base_sel]) if base_sel.sum() >= 5 else 10.0
            k = a
            while (k > 0 and gt[sel[a]] - gt[sel[k - 1]] <= 30
                   and (h[k - 1] > h0 + 25 or v[k - 1] > v0 + 15)):
                k -= 1
            return float(gt[sel[k]]), float(best_lag)
    return None, 'no_separation'


# ═══════════════════════════ flight analysis ═══════════════════════════

def analyse_flight(t, x, y, alt, launch_type, logbook_start_utc=None, tug=None, wind_at=None):
    """
    Analyse one logged flight.

    Args:
        t, x, y, alt: arrays (UTC seconds, metres east/north, metres altitude)
        launch_type: 'winch' | 'aerotow' | 'self'
        logbook_start_utc: logbook take-off time (UTC seconds) used to cut the track
        tug: prepared tug track dict (t, x, y, agl) for aerotow pairing, or None
        wind_at: callable(t_utc_s) -> (wx, wy) m/s, wind in the layer below the climbs

    Returns dict with keys 'launch', 'climbs' (list), 'search' (array of t, x, y, agl).
    """
    launch = {'launch_type': launch_type, 'track_status': 'ok', 'launch_quality': None,
              'release_method': None, 'agl_uncertain': 0, 'connected': 0, 'n_climbs': 0}
    res = {'launch': launch, 'climbs': [], 'search': None}

    t, x, y, alt = clean_track(t, x, y, alt)
    if len(t) < 30:
        launch['track_status'] = 'too_few_fixes'
        return res

    intervals = airborne_intervals(t, x, y, alt)
    iv, status = select_interval(intervals, t, logbook_start_utc)
    if iv is None:
        launch['track_status'] = status
        return res
    launch['track_status'] = status
    ground, uncertain = ground_level(t, alt, iv)
    launch['agl_uncertain'] = int(uncertain or iv['starts_airborne'])

    i0 = iv['i_ground_before'] if iv['i_ground_before'] is not None else iv['i_takeoff']
    i1 = iv['i_landing']
    t, x, y, alt = t[i0:i1 + 1], x[i0:i1 + 1], y[i0:i1 + 1], alt[i0:i1 + 1]
    agl = alt - ground
    i_to = iv['i_takeoff'] - i0
    launch['takeoff_utc_s'] = float(t[i_to])
    launch['max_agl'] = float(agl.max())

    if iv['starts_airborne']:
        launch['launch_quality'] = 'suspect'
        launch['track_status'] = 'starts_airborne'
        return res

    # ── launch end ──
    guard_t = None
    if launch_type == 'winch':
        rel, quality = winch_release(t, agl, i_to)
        launch['release_method'] = 'winch'
        launch['launch_quality'] = quality
        if rel is None:
            return res
        t_rel = t[rel]
    else:
        t_sig = tow_release_signature(t, x, y, agl, i_to)
        launch['release_sig_utc_s'] = t_sig
        t_rel = None
        if tug is not None:
            t_pair, info = tow_release_paired(t, x, y, agl, i_to, tug)
            if t_pair is not None:
                t_rel = t_pair
                launch['release_method'] = 'tug_pair'
                launch['tug_lag_s'] = info
            else:
                launch['tug_pair_failure'] = info
        if t_rel is None:
            if t_sig is None:
                launch['launch_quality'] = 'no_release'
                return res
            t_rel = t_sig
            launch['release_method'] = 'signature'
            guard_t = t_rel + C.TOW_GUARD_S
        launch['launch_quality'] = 'ok'
        rel = int(np.searchsorted(t, t_rel))
        rel = min(rel, len(t) - 1)

    launch['release_utc_s'] = float(t[rel])
    launch['release_x'] = float(x[rel])
    launch['release_y'] = float(y[rel])
    launch['release_agl'] = float(agl[rel])
    launch['dur_after_release_s'] = float(t[-1] - t[rel])

    # ── climbs ──
    cum = cumulative_turn(x, y)
    cx = window_mean(t, x, C.CIRCLE_WINDOW_S)
    cy = window_mean(t, y, C.CIRCLE_WINDOW_S)
    half = C.CIRCLE_WINDOW_S / 2
    climbs = []
    for a, b in zigzag_segments(t, agl, rel):
        if guard_t is not None and t[a] < guard_t:
            if t[b] <= guard_t:
                continue
            a = int(np.searchsorted(t, guard_t))
            a = a + int(np.argmin(agl[a:b + 1]))
            if a >= b:
                continue
        gain = agl[b] - agl[a]
        dur = t[b] - t[a]
        if gain < C.MIN_GAIN_M or dur < C.MIN_DUR_S:
            continue
        turn = abs(cum[b] - cum[a])
        if turn < C.MIN_TURN_DEG:
            continue
        ia = min(int(np.searchsorted(t, t[a] + half)), b)
        ib = max(int(np.searchsorted(t, t[b] - half)), ia)
        xc0, yc0 = float(cx[ia]), float(cy[ia])
        ddur = t[ib] - t[ia]
        drift_x, drift_y = float(cx[ib] - cx[ia]), float(cy[ib] - cy[ia])
        rate = gain / dur
        if ddur >= 120:
            wx, wy = drift_x / ddur, drift_y / ddur
            wind_src = 'own_drift'
        elif wind_at is not None:
            wx, wy = wind_at(t[a])
            wind_src = 'model'
        else:
            wx, wy, wind_src = 0.0, 0.0, 'none'
        w_air = rate + C.CIRCLING_SINK_MS
        sx, sy = -wx * agl[a] / w_air, -wy * agl[a] / w_air
        shift = float(np.hypot(sx, sy))
        if shift > C.MAX_SOURCE_SHIFT_M:
            sx, sy = sx * C.MAX_SOURCE_SHIFT_M / shift, sy * C.MAX_SOURCE_SHIFT_M / shift
            shift = C.MAX_SOURCE_SHIFT_M
        climbs.append({
            'start_utc_s': float(t[a]), 'x0': float(x[a]), 'y0': float(y[a]),
            'xc0': xc0, 'yc0': yc0, 'agl0': float(agl[a]), 'agl1': float(agl[b]),
            'gain': float(gain), 'duration': float(dur), 'rate': float(rate), 'turn_deg': float(turn),
            'drift_x': drift_x, 'drift_y': drift_y, 'wind_x': float(wx), 'wind_y': float(wy),
            'wind_source': wind_src, 'source_x': xc0 + sx, 'source_y': yc0 + sy, 'shift_m': shift,
            's_since_release': float(t[a] - t[rel]), '_a': a,
        })
    for k, c in enumerate(climbs):
        c['seq'] = k
        c['is_first'] = int(k == 0)
    launch['n_climbs'] = len(climbs)

    # ── search track (winch) ──
    if launch_type == 'winch':
        after = np.arange(rel, len(t))
        low = after[agl[after] < C.SEARCH_END_AGL]
        end_t = t[rel] + C.SEARCH_MAX_S
        if len(low):
            end_t = min(end_t, t[low[0]])
        end_t = min(end_t, t[-1])
        first = climbs[0] if climbs else None
        if first is not None and first['start_utc_s'] <= end_t:
            launch['connected'] = 1
            launch['time_to_first_climb_s'] = first['s_since_release']
            launch['height_lost_to_first_climb_m'] = float(agl[rel] - first['agl0'])
            launch['first_climb_dist_m'] = float(np.hypot(first['x0'] - x[rel], first['y0'] - y[rel]))
            end_t = first['start_utc_s']
        sel = np.where((t >= t[rel]) & (t <= end_t))[0]
        # the outcome cannot be judged when the logger has a gap during the search,
        # or stops while the glider is still searching in the air (label audit, plan 9.10)
        if len(sel) > 1 and np.diff(t[sel]).max() > C.GAP_S:
            launch['launch_quality'] = 'gap_in_search'
        elif not launch['connected'] and end_t >= t[-1] and agl[-1] > C.SEARCH_END_AGL:
            launch['launch_quality'] = 'track_ends_airborne'
        if len(sel):
            keep = [sel[0]]
            for i in sel[1:]:
                if t[i] - t[keep[-1]] >= C.SEARCH_THIN_S:
                    keep.append(i)
            keep = np.array(keep)
            res['search'] = np.column_stack([t[keep], x[keep], y[keep], agl[keep]])
        launch['search_end_utc_s'] = float(end_t)

    for c in climbs:
        c.pop('_a', None)
    res['climbs'] = climbs
    return res


def prepare_tug(t, x, y, alt, logbook_start_utc):
    """Cut a tug track to its flight and convert to AGL. Returns dict or None."""
    t, x, y, alt = clean_track(t, x, y, alt)
    if len(t) < 30:
        return None
    iv, status = select_interval(airborne_intervals(t, x, y, alt), t, logbook_start_utc)
    if iv is None or status != 'ok':
        return None
    ground, _ = ground_level(t, alt, iv)
    i0 = iv['i_ground_before'] if iv['i_ground_before'] is not None else iv['i_takeoff']
    i1 = iv['i_landing']
    return {'t': t[i0:i1 + 1], 'x': x[i0:i1 + 1], 'y': y[i0:i1 + 1], 'agl': alt[i0:i1 + 1] - ground}


# ═══════════════════════════ database ═══════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS launches (
  flight_id INTEGER PRIMARY KEY, datum TEXT, launch_type TEXT, runway TEXT,
  glider_type TEXT, glider_class TEXT, pilot_id INTEGER, dual INTEGER,
  track_status TEXT, launch_quality TEXT, release_method TEXT, agl_uncertain INTEGER,
  takeoff_utc_s REAL, release_utc_s REAL, release_sig_utc_s REAL, tug_lag_s REAL, tug_pair_failure TEXT,
  release_x REAL, release_y REAL, release_agl REAL, max_agl REAL, dur_after_release_s REAL,
  connected INTEGER, n_climbs INTEGER, time_to_first_climb_s REAL,
  height_lost_to_first_climb_m REAL, first_climb_dist_m REAL, search_end_utc_s REAL,
  pA REAL, detector_version TEXT);
CREATE TABLE IF NOT EXISTS climbs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, flight_id INTEGER, seq INTEGER, is_first INTEGER,
  start_utc_s REAL, x0 REAL, y0 REAL, xc0 REAL, yc0 REAL, agl0 REAL, agl1 REAL, gain REAL,
  duration REAL, rate REAL, turn_deg REAL, drift_x REAL, drift_y REAL, wind_x REAL, wind_y REAL,
  wind_source TEXT, source_x REAL, source_y REAL, shift_m REAL, s_since_release REAL);
CREATE TABLE IF NOT EXISTS search_tracks (flight_id INTEGER, t_utc_s REAL, x REAL, y REAL, agl REAL);
CREATE INDEX IF NOT EXISTS idx_climbs_flight ON climbs(flight_id);
CREATE INDEX IF NOT EXISTS idx_search_flight ON search_tracks(flight_id);
CREATE INDEX IF NOT EXISTS idx_launches_datum ON launches(datum);
CREATE TABLE IF NOT EXISTS ft_meta (key TEXT PRIMARY KEY, value TEXT);
"""

LAUNCH_COLS = ['flight_id', 'datum', 'launch_type', 'runway', 'glider_type', 'glider_class', 'pilot_id',
               'dual', 'track_status', 'launch_quality', 'release_method', 'agl_uncertain',
               'takeoff_utc_s', 'release_utc_s', 'release_sig_utc_s', 'tug_lag_s', 'tug_pair_failure',
               'release_x', 'release_y', 'release_agl', 'max_agl', 'dur_after_release_s', 'connected',
               'n_climbs', 'time_to_first_climb_s', 'height_lost_to_first_climb_m', 'first_climb_dist_m',
               'search_end_utc_s', 'detector_version']
CLIMB_COLS = ['flight_id', 'seq', 'is_first', 'start_utc_s', 'x0', 'y0', 'xc0', 'yc0', 'agl0', 'agl1',
              'gain', 'duration', 'rate', 'turn_deg', 'drift_x', 'drift_y', 'wind_x', 'wind_y',
              'wind_source', 'source_x', 'source_y', 'shift_m', 's_since_release']


def load_wind_table(conn):
    """(date, hour) -> layer-mean wind vector (m/s), from weather_conv (fallback: weather_data)."""
    try:
        w = pd.read_sql_query("SELECT date, hour_utc AS hour, wd10, ws10, wd100, ws100 FROM weather_conv", conn)
    except Exception:
        w = pd.DataFrame()
    table = {}
    if not w.empty:
        x10, y10 = C.wind_vector(w.wd10, w.ws10)
        x100, y100 = C.wind_vector(w.wd100, w.ws100)
        wx, wy = 0.5 * (x10 + x100), 0.5 * (y10 + y100)
        for d, h, a, b in zip(w.date, w.hour, wx, wy):
            if np.isfinite(a) and np.isfinite(b):
                table[(d, int(h))] = (float(a), float(b))
    return table


def make_wind_at(table, datum):
    def wind_at(t_utc_s):
        h = int(np.clip(round(t_utc_s / 3600.0), 0, 23))
        return table.get((datum, h), (0.0, 0.0))
    return wind_at


def run(db_path, force=False, flight_ids=None, verbose=True):
    t_start = time.time()
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    version = C.config_hash()
    meta = dict(conn.execute("SELECT key, value FROM ft_meta").fetchall())

    if force or meta.get('detector_version') != version:
        if verbose:
            reason = 'forced' if force else f"detector version {meta.get('detector_version')} → {version}"
            print(f"Full rebuild ({reason})")
        conn.executescript("DELETE FROM launches; DELETE FROM climbs; DELETE FROM search_tracks;")
        done = set()
    else:
        done = {r[0] for r in conn.execute("SELECT flight_id FROM launches")}

    flights = pd.read_sql_query("""
        SELECT id, datum, start_tijd, start_methode, registratie, type, uuid, sleep_uuid, runway,
               gezagvoerder_id, tweede_inzittende_id
        FROM flights""", conn)
    by_uuid = flights.set_index('uuid')
    gliders = flights[flights.start_methode.isin(LAUNCH_TYPES.keys())
                      & ~flights.registratie.isin(C.TUG_REGS)]
    todo = gliders[~gliders.id.isin(done)]
    if flight_ids is not None:
        todo = todo[todo.id.isin(flight_ids)]
    if todo.empty:
        if verbose:
            print("No new flights to process.")
        conn.close()
        return 0

    # tug flight ids needed for pairing
    tug_for = {}
    for fid, su in zip(todo.id, todo.sleep_uuid):
        if su and su in by_uuid.index:
            tug = by_uuid.loc[[su]].iloc[0]
            if tug.start_methode in TUG_METHODS or tug.registratie in C.TUG_REGS:
                tug_for[fid] = int(tug.id)
    need = set(todo.id) | set(tug_for.values())

    if verbose:
        print(f"Loading fixes for {len(need)} flights...", flush=True)
    ids = ','.join(str(int(i)) for i in need)
    fx = pd.read_sql_query(f"""SELECT flight_id, time, latitude, longitude, altitude FROM igc_data
                               WHERE flight_id IN ({ids}) ORDER BY flight_id, sequence""", conn)
    fx = fx.dropna(subset=['time', 'latitude', 'longitude', 'altitude'])
    fx = fx[fx.time.str.len() >= 8]
    fx['t'] = parse_times(fx.time)
    fx['x'], fx['y'] = C.to_xy(fx.latitude.to_numpy(), fx.longitude.to_numpy())
    groups = {fid: g for fid, g in fx.groupby('flight_id', sort=False)}
    del fx

    wind_table = load_wind_table(conn)
    finfo = flights.set_index('id')

    tugs = {}
    for tid in set(tug_for.values()):
        g = groups.get(tid)
        if g is None:
            continue
        r = finfo.loc[tid]
        tugs[tid] = prepare_tug(g.t.values, g.x.values, g.y.values, g.altitude.values,
                                C.local_to_utc_seconds(r.datum, r.start_tijd))

    launches, climbs, search = [], [], []
    stats = {}
    iterator = todo.itertuples(index=False)
    try:
        from tqdm import tqdm
        iterator = tqdm(iterator, total=len(todo), desc='Detecting climbs', disable=not verbose)
    except ImportError:
        pass
    for f in iterator:
        lt = LAUNCH_TYPES[f.start_methode]
        g = groups.get(f.id)
        row = {'flight_id': int(f.id), 'datum': f.datum[:10], 'launch_type': lt, 'runway': f.runway,
               'glider_type': f.type, 'glider_class': C.glider_class(f.type),
               'pilot_id': None if pd.isna(f.gezagvoerder_id) else int(f.gezagvoerder_id),
               'dual': int(not pd.isna(f.tweede_inzittende_id)), 'detector_version': version}
        if g is None:
            row['track_status'] = 'no_track'
            launches.append(row)
            stats['no_track'] = stats.get('no_track', 0) + 1
            continue
        tug = tugs.get(tug_for.get(f.id))
        out = analyse_flight(g.t.values, g.x.values, g.y.values, g.altitude.values.astype(float), lt,
                             C.local_to_utc_seconds(f.datum, f.start_tijd), tug,
                             make_wind_at(wind_table, f.datum[:10]))
        row.update(out['launch'])
        launches.append(row)
        key = row['track_status'] if row['track_status'] not in ('ok',) else f"{lt}:{row.get('launch_quality')}"
        stats[key] = stats.get(key, 0) + 1
        for c in out['climbs']:
            c['flight_id'] = int(f.id)
            climbs.append(c)
        if out['search'] is not None:
            s = out['search']
            search.append(pd.DataFrame({'flight_id': int(f.id), 't_utc_s': s[:, 0], 'x': s[:, 1],
                                        'y': s[:, 2], 'agl': s[:, 3]}))

    L = pd.DataFrame(launches).reindex(columns=LAUNCH_COLS)
    ids_sql = ','.join(str(i) for i in L.flight_id)
    conn.execute(f"DELETE FROM launches WHERE flight_id IN ({ids_sql})")
    conn.execute(f"DELETE FROM climbs WHERE flight_id IN ({ids_sql})")
    conn.execute(f"DELETE FROM search_tracks WHERE flight_id IN ({ids_sql})")
    L.to_sql('launches', conn, if_exists='append', index=False)
    if climbs:
        pd.DataFrame(climbs).reindex(columns=CLIMB_COLS).to_sql('climbs', conn, if_exists='append', index=False)
    if search:
        pd.concat(search).to_sql('search_tracks', conn, if_exists='append', index=False)
    conn.execute("INSERT OR REPLACE INTO ft_meta VALUES ('detector_version', ?)", (version,))
    conn.execute("INSERT OR REPLACE INTO ft_meta VALUES ('detector_last_run', ?)",
                 (time.strftime('%Y-%m-%d %H:%M:%S'),))
    conn.commit()
    conn.close()

    if verbose:
        print(f"\nProcessed {len(L)} flights in {time.time() - t_start:.0f}s; {len(climbs)} climbs")
        for k in sorted(stats):
            print(f"  {k:32s} {stats[k]}")
        w = L[(L.launch_type == 'winch') & (L.launch_quality == 'ok')]
        if len(w):
            print(f"  winch launches ok: {len(w)}, connected: {int(w.connected.sum())} "
                  f"({w.connected.mean():.1%}), median release AGL {w.release_agl.median():.0f} m")
    return len(L)


def main():
    p = argparse.ArgumentParser(description='Detect launches and circling climbs')
    p.add_argument('--db', '--db-path', dest='db', default=str(C.DB_PATH))
    p.add_argument('--force', '-f', action='store_true', help='rebuild all flights')
    args = p.parse_args()
    run(args.db, force=args.force)
    return 0


if __name__ == '__main__':
    sys.exit(main())
