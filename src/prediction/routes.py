"""
Recommended search lines (plan section 14.4).

A line starts at the winch release and visits up to MAX_LEGS look-points (where a
thermal from a ground source is expected at search height, i.e. shifted downwind).
Every leg respects the height budget: the glider must stay above MIN_WORK_AGL and
always be able to reach a circuit-entry point at CIRCUIT_ENTRY_AGL.

P(line) = 1 − Π (1 − p_c) over the ground-source cells the line tests, where p_c is
layer B's chance that searching cell c today ends in a climb.
"""

import numpy as np

from src import config as C
from src.prediction.reach import height_loss

MAX_LEGS = 4
MIN_LEG_M = 300.0
BEAM = 20
SAMPLE_M = 100.0


def _tested_cells(grid, pts_xy, agl, wind, match_radius, use_drift=True):
    """Cells tested by flying through pts at heights agl (shifted to ground sources if use_drift)."""
    k = 1.0 if use_drift else 0.0
    sx = pts_xy[:, 0] - k * wind[0] * agl / C.W_AIR_TYP
    sy = pts_xy[:, 1] - k * wind[1] * agl / C.W_AIR_TYP
    return set(np.where(grid.within(sx, sy, match_radius))[0])


def _leg(a_xy, b_xy, h0, wind, V, E):
    d = float(np.hypot(b_xy[0] - a_xy[0], b_xy[1] - a_xy[1]))
    n = max(2, int(d / SAMPLE_M) + 1)
    t = np.linspace(0, 1, n)
    pts = np.column_stack([a_xy[0] + t * (b_xy[0] - a_xy[0]), a_xy[1] + t * (b_xy[1] - a_xy[1])])
    loss = height_loss(a_xy[0], a_xy[1], pts[:, 0], pts[:, 1], wind[0], wind[1], V, E)
    return pts, h0 - loss


def line_probability(cells, p_cell):
    if not cells:
        return 0.0
    v = np.clip(p_cell[list(cells)], 0, 0.999)
    return float(1 - np.prod(1 - v))


def recommend(grid, p_cell, lift_lo, lift_hi, scale, release_xy, h_rel, wind, glider, circuit_pts,
              look_xy, in_reach, match_radius=None, n_lines=3, use_drift=True):
    """
    Beam search for the best search lines.

    Args:
        grid: layer B Grid; p_cell: today's chance per source cell
        lift_lo, lift_hi, scale: per-cell lift interval and scale (pA × q) for line intervals
        look_xy: (cells, 2) where each source is expected at search height; in_reach: bool per cell

    Returns list of dicts: waypoints (list of xy), p, p_lo, p_hi, cells, end_height.
    """
    match_radius = match_radius or C.MATCH_RADIUS_M
    E, V = C.GLIDE[glider], C.V_GLIDE_MS[glider]
    cpts = np.atleast_2d(np.asarray(circuit_pts, float))
    cand = np.where(in_reach)[0]
    if len(cand) == 0:
        return []
    cand = cand[np.argsort(-p_cell[cand])][:60]

    def can_return(xy, h):
        ret = min(height_loss(xy[0], xy[1], cx, cy, wind[0], wind[1], V, E) for cx, cy in cpts)
        return h - ret - C.SEARCH_TURN_LOSS_M >= C.CIRCUIT_ENTRY_AGL

    beam = [{'wp': [tuple(release_xy)], 'h': float(h_rel), 'cells': set(), 'p': 0.0}]
    finished = []
    for _ in range(MAX_LEGS):
        nxt = []
        for st in beam:
            last = st['wp'][-1]
            for c in cand:
                tgt = tuple(look_xy[c])
                if np.hypot(tgt[0] - last[0], tgt[1] - last[1]) < MIN_LEG_M:
                    continue
                if any(np.hypot(tgt[0] - w[0], tgt[1] - w[1]) < MIN_LEG_M for w in st['wp'][:-1]):
                    continue
                pts, h = _leg(last, tgt, st['h'], wind, V, E)
                if not np.all(np.isfinite(h)) or h[-1] < C.MIN_WORK_AGL or not can_return(tgt, h[-1]):
                    continue
                cells = st['cells'] | _tested_cells(grid, pts, np.maximum(h, 0), wind, match_radius, use_drift)
                nxt.append({'wp': st['wp'] + [tgt], 'h': float(h[-1]), 'cells': cells,
                            'p': line_probability(cells, p_cell)})
        if not nxt:
            break
        nxt.sort(key=lambda s: -s['p'])
        # keep diversity: drop near-duplicate states
        kept = []
        for s in nxt:
            if all(np.hypot(s['wp'][-1][0] - k['wp'][-1][0], s['wp'][-1][1] - k['wp'][-1][1]) > 150
                   or len(s['wp']) != len(k['wp']) for k in kept):
                kept.append(s)
            if len(kept) >= BEAM:
                break
        beam = kept
        finished.extend(beam)

    finished.sort(key=lambda s: -s['p'])
    lines = []
    for s in finished:
        path = _resample(s['wp'])
        if all(np.mean(np.hypot(*(path - _resample(l['waypoints'])).T)) > 300 for l in lines):
            cells = list(s['cells'])
            lo = 1 - np.prod(1 - np.clip(scale * lift_lo[cells], 0, 0.999))
            hi = 1 - np.prod(1 - np.clip(scale * lift_hi[cells], 0, 0.999))
            lines.append({'waypoints': s['wp'], 'p': s['p'], 'p_lo': float(lo), 'p_hi': float(hi),
                          'cells': cells, 'end_height': s['h']})
        if len(lines) >= n_lines:
            break
    return lines


def _resample(wp, n=20):
    wp = np.asarray(wp, float)
    seg = np.hypot(*np.diff(wp, axis=0).T)
    s = np.concatenate([[0], np.cumsum(seg)])
    if s[-1] == 0:
        return np.repeat(wp[:1], n, axis=0)
    q = np.linspace(0, s[-1], n)
    return np.column_stack([np.interp(q, s, wp[:, 0]), np.interp(q, s, wp[:, 1])])
