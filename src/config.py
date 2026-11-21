"""
Shared configuration for the first-thermal pipeline.

All tunable constants live here so the evaluation script can vary them.
See docs/FIRST_THERMAL_PLAN.md, section 8.
"""

import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

# ── Paths ──
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / 'data' / 'flights.db'
MODEL_DIR = ROOT / 'data' / 'models'
OUTPUT_DIR = ROOT / 'output'
FORECAST_LOG = ROOT / 'data' / 'forecast_log.jsonl'

# ── Site ──
LAT0 = 52.193650          # field centre (median of ground-level GPS points)
LON0 = 5.145217
SITE_LAT = 52.19          # point used for weather requests
SITE_LON = 5.15
TZ_LOCAL = ZoneInfo('Europe/Amsterdam')
M_PER_DEG_LAT = 111000.0
M_PER_DEG_LON = 111000.0 * math.cos(math.radians(LAT0))

RUNWAYS = ['07', '12', '18', '25', '30', '36']
RUNWAY_HEADING = {'07': 64.2, '12': 93.7, '18': 182.8, '25': 249.6, '30': 273.2, '36': 357.9}

# ── Detection (Phase 1) ──
DRAWDOWN_M = 25.0           # a climb ends when the glider is this far below its top
MIN_GAIN_M = 60.0           # minimum height gain for a climb
MIN_DUR_S = 60.0            # minimum duration of a climb
MIN_TURN_DEG = 540.0        # minimum accumulated turning (1.5 circles)
GAP_S = 30.0                # logging gap that breaks a segment
MIN_STEP_M = 3.0            # ignore heading changes over steps shorter than this
TAKEOFF_SPEED_MS = 11.0     # ~40 km/h ground speed = airborne
LANDED_SPEED_MS = 7.0       # ~25 km/h
LANDED_HOLD_S = 30.0        # slow and level this long = landed
TRACK_MATCH_TOL_S = 1200.0  # take-off in track must be within 20 min of logbook start
WINCH_MIN_RATE = 5.0        # m/s, winch launch climb rate
WINCH_REL_MIN_AGL = 120.0
WINCH_REL_MAX_AGL = 700.0
TUG_REGS = ('PH-GOZ', 'PH GOZ')
TOW_SEP_H_M = 150.0
TOW_SEP_V_M = 50.0
TOW_SEP_HOLD_S = 20.0
TOW_GUARD_S = 120.0         # climbs of unpaired aerotow / self-launch count only after this
TOW_VZ_MIN = 0.7
TOW_VZ_STD_MAX = 0.8
TOW_TURN_MAX = 8.0          # deg/s while on tow
RELEASE_TURN = 12.0         # deg/s sustained = release turn / circling
CIRCLE_WINDOW_S = 25.0      # averaging window for the circle centre

# ── Search and reach (Phase 3) ──
CIRCUIT_ENTRY_AGL = 180.0  # safety floor: height at the circuit-entry point (user default)
MIN_WORK_AGL = 180.0       # data: 5th percentile start height of real first climbs (181 m)
SEARCH_END_AGL = 150.0
SEARCH_MAX_S = 600.0
SEARCH_THIN_S = 2.0
DECISION_WINDOW_S = 60.0    # "where did the pilot head in the first minute"
GLIDE = {'two_seat': 25.0, 'single': 30.0}       # effective (derated) glide ratio
V_GLIDE_MS = {'two_seat': 25.0, 'single': 26.0}
CIRCLING_SINK_MS = 0.9
W_AIR_TYP = 2.0             # typical updraft used to tilt thermals for the map
SEARCH_TURN_LOSS_M = 0.0   # plan default 30 m; 0 needed for ≥95% coverage of real first climbs
MAX_SOURCE_SHIFT_M = 1500.0

# ── Source grid (Layer B) ──
CELL_M = 200.0
MATCH_RADIUS_M = 200.0
REACH_RADIUS_M = 3000.0
SMOOTH_SIGMA_M = 200.0
K_CELL = 5.0
M_COND = 20.0
LAMBDA_AEROTOW = 0.3
USE_DRIFT = True
COND_DIMS = ('wind_sector', 'wind_speed', 'time_band', 'season')

# ── Layer C ──
ACE_MIN_LAUNCHES = 30
ACE_TOP_FRACTION = 0.2
SKILL_K = 3.0

# Parameters that define a detector run; a change triggers a rebuild.
DETECTOR_KEYS = [
    'DRAWDOWN_M', 'MIN_GAIN_M', 'MIN_DUR_S', 'MIN_TURN_DEG', 'GAP_S', 'MIN_STEP_M',
    'TAKEOFF_SPEED_MS', 'LANDED_SPEED_MS', 'LANDED_HOLD_S', 'TRACK_MATCH_TOL_S',
    'WINCH_MIN_RATE', 'WINCH_REL_MIN_AGL', 'WINCH_REL_MAX_AGL', 'TOW_SEP_H_M', 'TOW_SEP_V_M',
    'TOW_SEP_HOLD_S', 'TOW_GUARD_S', 'TOW_VZ_MIN', 'TOW_VZ_STD_MAX', 'TOW_TURN_MAX',
    'RELEASE_TURN', 'CIRCLE_WINDOW_S', 'SEARCH_END_AGL', 'SEARCH_MAX_S', 'SEARCH_THIN_S',
    'CIRCLING_SINK_MS', 'MAX_SOURCE_SHIFT_M',
]
DETECTOR_CODE_VERSION = '2'   # 2: gap / track-end rule from the label audit


def config_hash(keys=None) -> str:
    """Short hash of the named constants (default: detector constants)."""
    g = globals()
    keys = keys or DETECTOR_KEYS
    payload = {k: g[k] for k in keys}
    payload['_code'] = DETECTOR_CODE_VERSION
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


# ── Geometry helpers ──

def to_xy(lat, lon):
    """Lat/lon → metres east/north of the field centre."""
    return (np.asarray(lon) - LON0) * M_PER_DEG_LON, (np.asarray(lat) - LAT0) * M_PER_DEG_LAT


def to_latlon(x, y):
    """Metres east/north of the field centre → lat/lon."""
    return LAT0 + np.asarray(y) / M_PER_DEG_LAT, LON0 + np.asarray(x) / M_PER_DEG_LON


def wind_vector(dir_deg, spd_kmh):
    """Meteorological wind (from-direction, km/h) → vector the air moves to (m/s)."""
    r = np.radians(np.asarray(dir_deg, dtype=float))
    s = np.asarray(spd_kmh, dtype=float) / 3.6
    return -np.sin(r) * s, -np.cos(r) * s


def angdiff(a, b):
    """Absolute angular difference in degrees (0..180)."""
    d = np.abs(np.asarray(a, dtype=float) - np.asarray(b, dtype=float)) % 360
    return np.minimum(d, 360 - d)


def bearing_deg(dx, dy):
    return (np.degrees(np.arctan2(dx, dy)) + 360) % 360


def cardinal(bearing):
    dirs = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
            'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
    return dirs[int(round(float(bearing) / 22.5)) % 16]


# ── Time helpers ──

def local_to_utc_seconds(datum: str, hhmm: str):
    """Logbook local time (e.g. '14:05') on a date → seconds since UTC midnight of that date."""
    if not datum or not hhmm or not isinstance(hhmm, str) or ':' not in hhmm:
        return None
    try:
        d = date.fromisoformat(datum[:10])
        h, m = hhmm.split(':')[:2]
        local = datetime(d.year, d.month, d.day, int(h), int(m), tzinfo=TZ_LOCAL)
        utc = local.astimezone(timezone.utc)
        midnight = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        return (utc - midnight).total_seconds()
    except (ValueError, TypeError):
        return None


def utc_timestamp(datum: str, seconds_utc: float) -> datetime:
    d = date.fromisoformat(datum[:10])
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(seconds=float(seconds_utc))


# ── Glider classes ──

_TWO_SEAT = re.compile(r'DG-?1000|DUO ?DISCUS|AS-?K ?-?21|AS-?K ?-?13|^K ?-?13|JANUS|ARCUS|'
                       r'DG-?50[05]|T ?-?21|PUCHACZ|BOCIAN|TWIN', re.IGNORECASE)


def glider_class(type_name) -> str:
    """'two_seat' or 'single' from the logbook glider type."""
    if isinstance(type_name, str) and _TWO_SEAT.search(type_name.strip()):
        return 'two_seat'
    return 'single'


def season_of(month: int) -> str:
    if month in (3, 4, 5):
        return 'spring'
    if month in (6, 7, 8):
        return 'summer'
    if month in (9, 10, 11):
        return 'autumn'
    return 'winter'
