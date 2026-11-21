"""
OpenAir airspace parser and geometry.

Supports the records used in Dutch/European OpenAir files: AC (class), AN (name),
AL/AH (limits), DP (polygon point), V X= (centre), V D= (arc direction), DB (arc
between two points), DA (arc by radius and angles), DC (circle, radius in NM).

Geometry is converted to local metres (x east, y north of the EHHV field centre,
see src/config.to_xy), which is accurate enough over the Netherlands for
containment tests.
"""

import math
import re
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from src import config as C

FT = 0.3048
NM = 1852.0
M_PER_HPA = 8.3          # height change of a pressure level per hPa near sea level
STD_HPA = 1013.25
ARC_STEP_DEG = 2.0


@dataclass
class Limit:
    """A vertical limit. kind: 'sfc', 'msl', 'agl', 'fl', 'unl'."""
    kind: str
    value_m: float = 0.0      # metres (msl/agl) or pressure altitude in metres (fl)
    text: str = ''

    def amsl(self, qnh_hpa: float = STD_HPA, ground_m: float = 0.0) -> float:
        """Altitude above mean sea level in metres for the given QNH and ground elevation."""
        if self.kind == 'sfc':
            return -math.inf
        if self.kind == 'unl':
            return math.inf
        if self.kind == 'msl':
            return self.value_m
        if self.kind == 'agl':
            return self.value_m + ground_m
        # flight level: pressure altitude relative to 1013.25 hPa
        return self.value_m + (qnh_hpa - STD_HPA) * M_PER_HPA


@dataclass
class Airspace:
    cls: str
    name: str
    lower: Limit
    upper: Limit
    x: np.ndarray = field(default_factory=lambda: np.zeros(0))   # polygon vertices (closed implicitly)
    y: np.ndarray = field(default_factory=lambda: np.zeros(0))
    comments: List[str] = field(default_factory=list)

    @property
    def bbox(self):
        return float(self.x.min()), float(self.y.min()), float(self.x.max()), float(self.y.max())

    @property
    def notam_activated(self) -> bool:
        """Only active by NOTAM or on specific dates (name tags like <notam>, <4 mei>, <15 aug>)."""
        for tag in re.findall(r'<([^>]*)>', self.name.lower()):
            if 'notam' in tag or re.search(r'\d+\s*(jan|feb|mrt|mar|apr|mei|may|jun|jul|aug|sep|okt|oct|nov|dec)', tag):
                return True
        return False

    def contains(self, px, py):
        """Boolean array: points strictly inside the polygon (horizontal test only)."""
        from matplotlib.path import Path
        path = Path(np.column_stack([self.x, self.y]))
        return path.contains_points(np.column_stack([px, py]))

    def distance_to_edge(self, px, py):
        """Distance (m) from each point to the nearest polygon edge."""
        px, py = np.asarray(px, float), np.asarray(py, float)
        x0, y0 = self.x, self.y
        x1, y1 = np.roll(self.x, -1), np.roll(self.y, -1)
        dx, dy = x1 - x0, y1 - y0
        L2 = np.maximum(dx * dx + dy * dy, 1e-9)
        out = np.full(len(px), np.inf)
        for k in range(0, len(px), 2000):
            qx, qy = px[k:k + 2000, None], py[k:k + 2000, None]
            t = np.clip(((qx - x0) * dx + (qy - y0) * dy) / L2, 0, 1)
            d = np.hypot(qx - (x0 + t * dx), qy - (y0 + t * dy))
            out[k:k + 2000] = d.min(axis=1)
        return out


# ═══════════════════════════ parsing ═══════════════════════════

_COORD = re.compile(r"(\d+):(\d+)(?::(\d+(?:\.\d+)?))?\s*([NS])\s*[, ]?\s*(\d+):(\d+)(?::(\d+(?:\.\d+)?))?\s*([EW])",
                    re.IGNORECASE)


def parse_coord(s: str):
    """'52:18:30 N 004:45:00 E' → (lat, lon)."""
    m = _COORD.search(s)
    if not m:
        raise ValueError(f"cannot parse coordinate: {s!r}")
    d1, m1, s1, ns, d2, m2, s2, ew = m.groups()
    lat = int(d1) + int(m1) / 60 + (float(s1) if s1 else 0) / 3600
    lon = int(d2) + int(m2) / 60 + (float(s2) if s2 else 0) / 3600
    if ns.upper() == 'S':
        lat = -lat
    if ew.upper() == 'W':
        lon = -lon
    return lat, lon


def parse_limit(s: str) -> Limit:
    t = s.strip().upper()
    if t in ('SFC', 'GND', '0', 'MSL', '0FT', '0 FT') or t.startswith('SFC') or t.startswith('GND'):
        return Limit('sfc', 0.0, s.strip())
    if t.startswith('UNL'):
        return Limit('unl', 0.0, s.strip())
    m = re.match(r'FL\s*(\d+)', t)
    if m:
        return Limit('fl', int(m.group(1)) * 100 * FT, s.strip())
    m = re.match(r'(\d+(?:\.\d+)?)\s*(FT|F|M)?\s*(AMSL|MSL|AGL|AGND|GND|SFC|ALT)?', t)
    if not m:
        raise ValueError(f"cannot parse limit: {s!r}")
    v = float(m.group(1))
    unit = m.group(2) or 'FT'
    ref = m.group(3) or 'MSL'
    metres = v if unit == 'M' else v * FT
    if ref in ('AGL', 'AGND', 'GND', 'SFC'):
        return Limit('agl', metres, s.strip())
    return Limit('msl', metres, s.strip())


def _arc_points(cx, cy, r, a0, a1, clockwise, full=False):
    """Points on a circle (centre cx, cy, radius r) from bearing a0 to a1 (degrees, 0 = north)."""
    if full:
        span = 360.0
    elif clockwise:
        span = (a1 - a0) % 360
    else:
        span = -((a0 - a1) % 360)
    n = max(2, int(abs(span) / ARC_STEP_DEG) + 1)
    ang = np.radians(a0 + np.linspace(0, span, n))
    return cx + r * np.sin(ang), cy + r * np.cos(ang)


def parse_openair(text: str) -> List[Airspace]:
    """Parse an OpenAir file into Airspace objects (local metres)."""
    out: List[Airspace] = []
    cur = None
    xs, ys = [], []
    centre = None
    clockwise = True
    comments = []

    def finish():
        nonlocal cur, xs, ys
        if cur is not None and len(xs) >= 3:
            cur.x, cur.y = np.array(xs), np.array(ys)
            out.append(cur)
        cur, xs, ys = None, [], []

    for raw in text.replace('\r', '').split('\n'):
        line = raw.strip()
        if not line:
            continue
        if line.startswith('*'):
            comments.append(line)
            continue
        key = line[:2].upper()
        rest = line[2:].strip()
        if key == 'AC':
            finish()
            cur = Airspace(rest.split()[0].upper() if rest else '?', '', Limit('sfc'), Limit('unl'), comments=[])
            centre, clockwise = None, True
        elif cur is None:
            continue
        elif key == 'AN':
            cur.name = rest
        elif key == 'AL':
            cur.lower = parse_limit(rest)
        elif key == 'AH':
            cur.upper = parse_limit(rest)
        elif key == 'DP':
            lat, lon = parse_coord(rest)
            x, y = C.to_xy(lat, lon)
            xs.append(float(x))
            ys.append(float(y))
        elif line.upper().startswith('V'):
            body = line[1:].strip()
            if body.upper().startswith('X'):
                lat, lon = parse_coord(body.split('=', 1)[1])
                x, y = C.to_xy(lat, lon)
                centre = (float(x), float(y))
            elif body.upper().startswith('D'):
                clockwise = body.split('=', 1)[1].strip() != '-'
        elif key == 'DC':
            r = float(rest.split()[0]) * NM
            ax, ay = _arc_points(centre[0], centre[1], r, 0, 360, True, full=True)
            xs.extend(ax[:-1].tolist())
            ys.extend(ay[:-1].tolist())
        elif key == 'DB':
            p1, p2 = rest.split(',', 1) if ',' in rest else (rest, rest)
            (la1, lo1), (la2, lo2) = parse_coord(p1), parse_coord(p2)
            x1, y1 = (float(v) for v in C.to_xy(la1, lo1))
            x2, y2 = (float(v) for v in C.to_xy(la2, lo2))
            cx, cy = centre
            r = math.hypot(x1 - cx, y1 - cy)
            a0 = math.degrees(math.atan2(x1 - cx, y1 - cy))
            a1 = math.degrees(math.atan2(x2 - cx, y2 - cy))
            ax, ay = _arc_points(cx, cy, r, a0, a1, clockwise)
            xs.extend(ax.tolist())
            ys.extend(ay.tolist())
        elif key == 'DA':
            r_nm, a0, a1 = (float(v) for v in rest.replace(',', ' ').split()[:3])
            ax, ay = _arc_points(centre[0], centre[1], r_nm * NM, a0, a1, clockwise)
            xs.extend(ax.tolist())
            ys.extend(ay.tolist())
    finish()
    return out


def load(path) -> List[Airspace]:
    with open(path, encoding='utf-8', errors='replace') as f:
        return parse_openair(f.read())


def at_point(airspaces, lat, lon) -> List[Airspace]:
    """Airspaces whose polygon contains the point (any height)."""
    x, y = C.to_xy(lat, lon)
    return [a for a in airspaces if a.contains(np.array([float(x)]), np.array([float(y)]))[0]]
