"""
Airspace violation detection for logged flights.

A violation event is a continuous stretch of the flight inside an airspace that is
checked (by class), lasting at least `min_duration_s`, where every fix is
  - horizontally inside the airspace by at least `horizontal_margin_m`, and
  - vertically above (lower limit + `vertical_margin_m`) and below
    (upper limit − `vertical_margin_m`).
The margins give the pilot the benefit of the doubt for GPS/altitude error.
Only fixes within the circle (`radius_m` around `centre_x/centre_y`, metres from the EHHV
field centre) count.

Altitudes in the logs are GPS altitudes above mean sea level (OGN/FLARM). Limits in
feet MSL are compared directly; flight levels are converted with the day's QNH;
AGL limits use `ground_elevation_m`.

Gliding sectors (class GSEC) are holes in controlled airspace when active: a fix
inside one (within its limits) is reported as "exempt" instead of a violation,
unless the exemption is switched off.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

from src.airspace import schedule
from src.airspace.openair import STD_HPA, Airspace

CONTROLLED = {'A', 'B', 'C', 'D', 'E'}


@dataclass
class Settings:
    min_duration_s: float = 30.0
    vertical_margin_m: float = 50.0
    horizontal_margin_m: float = 0.0
    classes: tuple = ('A', 'B', 'C', 'D', 'P', 'R')
    include_notam_areas: bool = False
    gliding_sector_exemption: bool = True
    max_gap_s: float = 30.0          # a logging gap longer than this splits an event
    ground_elevation_m: float = 0.0  # for AGL limits (the Netherlands: ~0 m)
    exclude_names: Optional[str] = None   # regex of airspace names to ignore
    apply_time_rules: bool = True         # published time-dependent classes (schedule.TIME_RULES)
    radius_m: Optional[float] = 50000.0   # only count fixes within this circle (None = no limit)
    centre_x: float = 0.0                 # circle centre, metres east of the EHHV field centre
    centre_y: float = 0.0                 # circle centre, metres north of the EHHV field centre

    def hash(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, default=str).encode()).hexdigest()[:10]


def select_airspaces(airspaces: List[Airspace], s: Settings):
    """(checked airspaces, gliding sectors) for these settings."""
    classes = {c.upper() for c in s.classes}
    excl = re.compile(s.exclude_names, re.IGNORECASE) if s.exclude_names else None
    checked = [a for a in airspaces
               if a.cls.upper() in classes
               and (s.include_notam_areas or not a.notam_activated)
               and not (excl and excl.search(a.name))]
    sectors = [a for a in airspaces if a.cls.upper() == 'GSEC'] if s.gliding_sector_exemption else []
    for a in checked:
        a.time_rule = schedule.time_rule_for(a.name, a.lower.text) if s.apply_time_rules else None
    return checked, sectors


def _runs(mask, t, max_gap):
    """(start, end) index pairs of True runs, split where consecutive fixes are > max_gap apart."""
    idx = np.flatnonzero(mask)
    if len(idx) == 0:
        return []
    out = []
    start = prev = idx[0]
    for i in idx[1:]:
        if i != prev + 1 or t[i] - t[prev] > max_gap:
            out.append((start, prev))
            start = i
        prev = i
    out.append((start, prev))
    return out


def check_track(t, x, y, alt, airspaces: List[Airspace], sectors: List[Airspace], s: Settings,
                qnh_hpa: float = STD_HPA, local_offset_h: float = 0.0, off_day: bool = False):
    """
    Violation events for one airborne track.

    Args:
        t (s), x, y (m, local), alt (m AMSL): fix arrays in time order
        airspaces: checked airspaces; sectors: gliding sectors (exemptions)
    Returns list of event dicts (key 'exempt_by' is set for events inside a gliding sector).
    """
    t, x, y, alt = (np.asarray(v, float) for v in (t, x, y, alt))
    if len(t) == 0:
        return []
    region = (np.hypot(x - s.centre_x, y - s.centre_y) <= s.radius_m) if s.radius_m else np.ones(len(t), bool)
    if not region.any():
        return []
    bx0, by0, bx1, by1 = x[region].min(), y[region].min(), x[region].max(), y[region].max()
    events = []

    # gliding-sector mask per sector (computed once)
    sector_masks = []
    for g in sectors:
        gx0, gy0, gx1, gy1 = g.bbox
        if gx1 < bx0 or gx0 > bx1 or gy1 < by0 or gy0 > by1:
            continue
        lo, hi = g.lower.amsl(qnh_hpa, s.ground_elevation_m), g.upper.amsl(qnh_hpa, s.ground_elevation_m)
        m = g.contains(x, y) & (alt >= lo) & (alt <= hi)
        if m.any():
            sector_masks.append((g.name, m))

    for a in airspaces:
        ax0, ay0, ax1, ay1 = a.bbox
        if ax1 < bx0 or ax0 > bx1 or ay1 < by0 or ay0 > by1:
            continue
        lower = a.lower.amsl(qnh_hpa, s.ground_elevation_m)
        upper = a.upper.amsl(qnh_hpa, s.ground_elevation_m)
        vert = (alt >= lower + s.vertical_margin_m) & (alt <= upper - s.vertical_margin_m) & region
        rule = getattr(a, 'time_rule', None)
        if rule is not None:
            hour_local = (t / 3600.0 + local_offset_h) % 24
            vert &= rule[1](hour_local, off_day)
        if not vert.any():
            continue
        cand = np.flatnonzero(vert)
        inside = np.zeros(len(t), bool)
        inside[cand] = a.contains(x[cand], y[cand])
        if not inside.any():
            continue
        depth = np.zeros(len(t))
        ii = np.flatnonzero(inside)
        depth[ii] = a.distance_to_edge(x[ii], y[ii])
        if s.horizontal_margin_m > 0:
            inside &= depth >= s.horizontal_margin_m
        viol = inside & vert

        exempt_name = np.full(len(t), None, dtype=object)
        if a.cls.upper() in CONTROLLED:
            for name, m in sector_masks:
                exempt_name[m & viol & (exempt_name == None)] = name  # noqa: E711

        for kind_mask, exempt in ((viol & (exempt_name == None), False), (viol & (exempt_name != None), True)):  # noqa: E711
            for i0, i1 in _runs(kind_mask, t, s.max_gap_s):
                dur = t[i1] - t[i0]
                if dur < s.min_duration_s:
                    continue
                seg = slice(i0, i1 + 1)
                above = alt[seg] - lower if np.isfinite(lower) else np.full(i1 - i0 + 1, np.nan)
                below_top = upper - alt[seg] if np.isfinite(upper) else np.full(i1 - i0 + 1, np.nan)
                events.append({
                    'airspace': a.name, 'class': a.cls, 'lower_text': a.lower.text, 'upper_text': a.upper.text,
                    'lower_m': float(lower), 'upper_m': float(upper), 'notam_area': bool(a.notam_activated),
                    'start_s': float(t[i0]), 'end_s': float(t[i1]), 'duration_s': float(dur),
                    'n_fixes': int(i1 - i0 + 1), 'max_alt_m': float(alt[seg].max()),
                    'max_above_lower_m': float(np.nanmax(above)) if np.isfinite(lower) else None,
                    'min_below_upper_m': float(np.nanmin(below_top)) if np.isfinite(upper) else None,
                    'max_inside_m': float(depth[seg].max()),
                    'entry_x': float(x[i0]), 'entry_y': float(y[i0]), 'exit_x': float(x[i1]), 'exit_y': float(y[i1]),
                    'qnh_hpa': float(qnh_hpa),
                    'exempt_by': (exempt_name[i0] if exempt else None),
                    'time_rule': rule[0] if rule is not None else None,
                    '_i0': int(i0), '_i1': int(i1),
                })
    events.sort(key=lambda e: e['start_s'])
    return events


def best_circle(tracks, radius_m, step_m=2000.0, keep_field_m=5000.0):
    """
    Centre of the circle of this radius that covers the most flying time, with EHHV at least
    `keep_field_m` inside it. Returns (cx, cy, coverage, coverage_if_centred_on_EHHV).
    """
    xs, ys, ws = [], [], []
    for _, t, x, y, _ in tracks:
        if len(t) < 2:
            continue
        dt = np.diff(t, append=t[-1])
        dt = np.clip(dt, 0, 30)
        xs.append(x)
        ys.append(y)
        ws.append(dt)
    x, y, w = np.concatenate(xs), np.concatenate(ys), np.concatenate(ws)
    # bin flying time on a 1 km grid
    ix, iy = np.round(x / 1000).astype(int), np.round(y / 1000).astype(int)
    cells = pd.DataFrame({'ix': ix, 'iy': iy, 'w': w}).groupby(['ix', 'iy']).w.sum().reset_index()
    cx_, cy_, cw = cells.ix.to_numpy() * 1000.0, cells.iy.to_numpy() * 1000.0, cells.w.to_numpy()
    total = cw.sum()
    lim = radius_m - keep_field_m
    grid = np.arange(-lim, lim + step_m, step_m)
    best = (0.0, 0.0, -1.0)
    for gx in grid:
        for gy in grid:
            if np.hypot(gx, gy) > lim:
                continue
            cov = cw[np.hypot(cx_ - gx, cy_ - gy) <= radius_m].sum() / total
            if cov > best[2]:
                best = (gx, gy, cov)
    ehhv = cw[np.hypot(cx_, cy_) <= radius_m].sum() / total
    return best[0], best[1], best[2], ehhv


def circle_coverage(tracks, cx, cy, radius_m):
    """Share of flying time inside a given circle, and inside the same-size circle around EHHV."""
    inside = ehhv = total = 0.0
    for _, t, x, y, _ in tracks:
        if len(t) < 2:
            continue
        w = np.clip(np.diff(t, append=t[-1]), 0, 30)
        total += w.sum()
        inside += w[np.hypot(x - cx, y - cy) <= radius_m].sum()
        ehhv += w[np.hypot(x, y) <= radius_m].sum()
    return (inside / total, ehhv / total) if total else (np.nan, np.nan)
