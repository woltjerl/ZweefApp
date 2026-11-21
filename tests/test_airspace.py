import numpy as np
import pytest

from src import config as C
from src.airspace import openair as OA
from src.airspace.violations import Settings, check_track, select_airspaces

# A 10 × 10 km box centred on the field (lat/lon from local metres), class A from 1500 ft MSL,
# a circular prohibited area, a gliding sector inside the box, and a NOTAM-activated area.
def _ll(x, y):
    lat, lon = C.to_latlon(x, y)
    d = lambda v: f"{int(abs(v)):02d}:{int(abs(v) * 60 % 60):02d}:{abs(v) * 3600 % 60:05.2f}"  # noqa: E731
    return f"{d(float(lat))} N {d(float(lon))} E"


OPENAIR = f"""
* test file
AC A
AN TMA TEST
AL 1500FT MSL
AH FL095
DP {_ll(-5000, -5000)}
DP {_ll(-5000, 5000)}
DP {_ll(5000, 5000)}
DP {_ll(5000, -5000)}
*
AC P
AN EHP TEST <H24>
AL SFC
AH 2000FT MSL
V X={_ll(20000, 0)}
DC 1
*
AC GSEC
AN G/A TEST
AL 1500FT MSL
AH 2500FT MSL
DP {_ll(-1000, -1000)}
DP {_ll(-1000, 1000)}
DP {_ll(1000, 1000)}
DP {_ll(1000, -1000)}
*
AC R
AN EHR TEST <notam>
AL SFC
AH FL060
DP {_ll(-30000, -1000)}
DP {_ll(-30000, 1000)}
DP {_ll(-28000, 1000)}
DP {_ll(-28000, -1000)}
*
AC R
AN EHR ARC
AL SFC
AH 1000FT MSL
V X={_ll(0, -20000)}
V D=+
DB {_ll(0, -19000)} , {_ll(0, -21000)}
DP {_ll(0, -20000)}
"""


@pytest.fixture(scope='module')
def airspaces():
    return OA.parse_openair(OPENAIR)


def test_parse_counts_and_limits(airspaces):
    names = [a.name for a in airspaces]
    assert names == ['TMA TEST', 'EHP TEST <H24>', 'G/A TEST', 'EHR TEST <notam>', 'EHR ARC']
    tma = airspaces[0]
    assert abs(tma.lower.amsl() - 1500 * 0.3048) < 1e-6
    assert abs(tma.upper.amsl() - 9500 * 0.3048) < 1e-6
    # flight level moves with QNH: 1023 hPa → ~81 m higher
    assert abs(tma.upper.amsl(1023.25) - tma.upper.amsl() - 83) < 1
    assert airspaces[1].lower.amsl() == -np.inf


def test_limits_parsing_variants():
    assert OA.parse_limit('GND').kind == 'sfc'
    assert OA.parse_limit('UNL').amsl() == np.inf
    assert abs(OA.parse_limit('1000FT AGL').amsl(ground_m=10) - (304.8 + 10)) < 1e-6
    assert abs(OA.parse_limit('500m MSL').amsl() - 500) < 1e-6
    assert abs(OA.parse_limit('FL065').amsl() - 6500 * 0.3048) < 1e-6


def test_circle_radius_and_containment(airspaces):
    p = airspaces[1]
    r = np.hypot(p.x - 20000, p.y)
    assert abs(r.mean() - 1852) < 25
    assert p.contains(np.array([20000.0, 20000.0]), np.array([0.0, 2500.0])).tolist() == [True, False]


def test_arc_direction(airspaces):
    arc = airspaces[4]
    # clockwise from north point to south point of a 1 km circle passes through the east
    assert arc.x.max() > 900 and arc.x.min() > -50


def test_notam_flag(airspaces):
    assert airspaces[3].notam_activated
    assert not airspaces[1].notam_activated
    assert not OA.Airspace('Q', 'PJE <hilversum>', OA.Limit('sfc'), OA.Limit('unl')).notam_activated


def _track(points, dt=1.0):
    """points: list of (seconds, x, y, alt) segments → arrays at 1 s steps (linear)."""
    t, x, y, a = [], [], [], []
    for (t0, x0, y0, a0), (t1, x1, y1, a1) in zip(points[:-1], points[1:]):
        n = int((t1 - t0) / dt)
        for k in range(n):
            f = k / n
            t.append(t0 + k * dt)
            x.append(x0 + f * (x1 - x0))
            y.append(y0 + f * (y1 - y0))
            a.append(a0 + f * (a1 - a0))
    return np.array(t), np.array(x), np.array(y), np.array(a)


def _check(airspaces, track, **kw):
    s = Settings(**kw)
    checked, sectors = select_airspaces(airspaces, s)
    return check_track(*track, checked, sectors, s)


def test_duration_threshold(airspaces):
    # 3 km east of centre (outside the gliding sector), 600 m AMSL (> 457 m floor + 50 m) for 40 s
    tr = _track([(0, 3000, 0, 300), (10, 3000, 0, 600), (50, 3000, 0, 600), (60, 3000, 0, 300), (61, 3000, 0, 300)])
    ev = _check(airspaces, tr)
    assert len(ev) == 1 and ev[0]['airspace'] == 'TMA TEST'
    assert 38 <= ev[0]['duration_s'] <= 50
    assert _check(airspaces, tr, min_duration_s=60) == []


def test_vertical_margin(airspaces):
    # 490 m: 33 m above the 457 m floor → inside with margin 0, not with margin 50
    tr = _track([(0, 3000, 0, 490), (120, 3000, 0, 490), (121, 3000, 0, 490)])
    assert len(_check(airspaces, tr, vertical_margin_m=0)) == 1
    assert _check(airspaces, tr, vertical_margin_m=50) == []


def test_horizontal_margin(airspaces):
    # 4.95 km east: 50 m inside the box edge
    tr = _track([(0, 4950, 0, 700), (120, 4950, 0, 700), (121, 4950, 0, 700)])
    assert len(_check(airspaces, tr)) == 1
    assert _check(airspaces, tr, horizontal_margin_m=100) == []


def test_logging_gap_splits_event(airspaces):
    t1, x1, y1, a1 = _track([(0, 3000, 0, 700), (40, 3000, 0, 700), (41, 3000, 0, 700)])
    t2, x2, y2, a2 = _track([(141, 3000, 0, 700), (181, 3000, 0, 700), (182, 3000, 0, 700)])
    tr = (np.r_[t1, t2], np.r_[x1, x2], np.r_[y1, y2], np.r_[a1, a2])
    assert len(_check(airspaces, tr, max_gap_s=30)) == 2
    assert len(_check(airspaces, tr, max_gap_s=200)) == 1


def test_gliding_sector_is_exempt(airspaces):
    tr = _track([(0, 0, 0, 600), (120, 0, 0, 600), (121, 0, 0, 600)])
    ev = _check(airspaces, tr)
    assert len(ev) == 1 and ev[0]['exempt_by'] == 'G/A TEST'
    ev2 = _check(airspaces, tr, gliding_sector_exemption=False)
    assert len(ev2) == 1 and ev2[0]['exempt_by'] is None


def test_prohibited_area_from_surface_and_notam_areas(airspaces):
    tr = _track([(0, 20000, 0, 200), (60, 20000, 0, 200), (61, 20000, 0, 200)])
    ev = _check(airspaces, tr)
    assert [e['airspace'] for e in ev] == ['EHP TEST <H24>']
    tr2 = _track([(0, -29000, 0, 300), (60, -29000, 0, 300), (61, -29000, 0, 300)])
    assert _check(airspaces, tr2) == []
    assert len(_check(airspaces, tr2, include_notam_areas=True)) == 1


def test_above_ceiling_is_not_inside(airspaces):
    tr = _track([(0, 20000, 0, 700), (60, 20000, 0, 700), (61, 20000, 0, 700)])   # above 2000 ft P area
    assert _check(airspaces, tr) == []


def test_only_inside_radius(airspaces):
    # the prohibited area is 20 km east: counted with a 50 km radius, not with 10 km
    tr = _track([(0, 20000, 0, 200), (60, 20000, 0, 200), (61, 20000, 0, 200)])
    assert len(_check(airspaces, tr, radius_m=50000)) == 1
    assert _check(airspaces, tr, radius_m=10000) == []
    assert len(_check(airspaces, tr, radius_m=None)) == 1


def test_easter_and_dutch_holidays():
    from datetime import date
    from src.airspace import schedule as S
    assert S.easter(2025) == date(2025, 4, 20) and S.easter(2026) == date(2026, 4, 5)
    h = S.nl_public_holidays(2025)
    assert date(2025, 4, 21) in h and date(2025, 5, 29) in h and date(2025, 6, 9) in h   # Easter Mon, Ascension, Whit Mon
    assert date(2025, 4, 26) in h          # King's Day moved: 27 April 2025 is a Sunday
    assert S.is_off_day('2025-06-14')      # Saturday
    assert not S.is_off_day('2025-06-11')  # Wednesday


def test_part_time_category():
    from src.airspace import schedule as S
    assert S.category('EHTRA 80', '2025-06-11', None) == 'unverified'                # only when booked
    assert S.category('EHR 2 <notam>', '2025-06-11', None) == 'unverified'
    assert S.category('EHTRA 80', '2025-06-11', None, count_intermittent=True) == 'violation'
    assert S.category('TMA B MIL <sup 3/2022>', '2025-06-14', None) == 'possible'   # Saturday
    assert S.category('TMA B MIL <sup 3/2022>', '2025-06-11', None) == 'violation'  # Wednesday
    assert S.category('CTR EHDL', '2025-06-14', None) == 'possible'
    assert S.category('TMA1 SPL', '2025-06-14', None) == 'violation'                # always active
    assert S.category('TMA1 SPL', '2025-06-14', 'G/A X') == 'exempt'
    assert S.category('TMA B MIL', '2025-06-14', None, count_off_days=True) == 'violation'


def test_circle_can_be_moved(airspaces):
    # prohibited area 20 km east: outside a 10 km circle round the field, inside one centred 15 km east
    tr = _track([(0, 20000, 0, 200), (60, 20000, 0, 200), (61, 20000, 0, 200)])
    assert _check(airspaces, tr, radius_m=10000) == []
    assert len(_check(airspaces, tr, radius_m=10000, centre_x=15000)) == 1


def test_best_circle_moves_towards_the_flying():
    from src.airspace.violations import best_circle
    rng = np.random.default_rng(0)
    tracks = []
    for k in range(40):            # flights going 20-60 km east, none west
        n = 200
        x = np.linspace(0, rng.uniform(20000, 60000), n)
        y = rng.normal(0, 3000, n)
        tracks.append(({}, np.arange(n, dtype=float), x, y, np.full(n, 800.0)))
    cx, cy, cov, cov_ehhv = best_circle(tracks, 30000, step_m=2000, keep_field_m=5000)
    assert cx > 15000 and abs(cy) < 5000            # shifted east
    assert np.hypot(cx, cy) <= 25000 + 1e-6         # EHHV stays at least 5 km inside
    assert cov > cov_ehhv


def test_time_dependent_classes():
    from src.airspace import schedule as S
    # Lelystad TMA 4: class D only 18-22 local, every day
    desc, fn = S.time_rule_for('TMA4 LLS <sup 3/2022>', '2500FT MSL')
    assert 'Lelystad' in desc
    assert fn(np.array([14.0, 18.5, 21.9, 22.0]), False).tolist() == [False, True, True, False]
    assert fn(np.array([14.0]), True).tolist() == [False]          # same on weekends
    # Nieuw Milligen TMA E upper band: class D all day on weekdays, only 18-22 on weekends
    _, fn2 = S.time_rule_for('TMA E MIL <sup 3/2022>', 'FL065')
    assert fn2(np.array([14.0]), False).tolist() == [True]
    assert fn2(np.array([14.0, 19.0]), True).tolist() == [False, True]
    _, fn3 = S.time_rule_for('TMA E MIL <sup 3/2022>', 'FL055')
    assert fn3(np.array([14.0]), False).tolist() == [False]
    assert S.time_rule_for('TMA1 SPL', '1500FT MSL') is None
    assert S.utc_offset_hours('2025-07-01') == 2 and S.utc_offset_hours('2025-12-01') == 1


def test_time_rule_applied_in_check():
    from src.airspace.violations import Settings, check_track, select_airspaces
    text = f"""
AC D
AN TMA4 LLS <sup 3/2022>
AL 2500FT MSL
AH FL065
DP {_ll(-5000, -5000)}
DP {_ll(-5000, 5000)}
DP {_ll(5000, 5000)}
DP {_ll(5000, -5000)}
"""
    asp = OA.parse_openair(text)
    s = Settings()
    checked, sectors = select_airspaces(asp, s)
    def track(start_utc_h):
        t0 = start_utc_h * 3600
        return _track([(t0, 0, 0, 1000), (t0 + 120, 0, 0, 1000), (t0 + 121, 0, 0, 1000)])
    # 13:00 UTC in summer = 15:00 local → class E → nothing; 17:00 UTC = 19:00 local → class D → violation
    assert check_track(*track(13), checked, sectors, s, local_offset_h=2) == []
    ev = check_track(*track(17), checked, sectors, s, local_offset_h=2)
    assert len(ev) == 1 and 'Lelystad' in ev[0]['time_rule']
    s2 = Settings(apply_time_rules=False)
    c2, sec2 = select_airspaces(asp, s2)
    assert len(check_track(*track(13), c2, sec2, s2, local_offset_h=2)) == 1


def test_restricted_without_h24_and_deelen_hours():
    from src.airspace import schedule as S
    assert S.category('EHR 9', '2025-06-11', None, cls='R') == 'unverified'          # NOTAM-activated range
    assert S.category('EHP 25 <H24>', '2025-06-14', None, cls='P') == 'violation'    # prohibited, H24
    assert S.category('EHR 3 <h24>', '2025-06-14', None, cls='R') == 'violation'
    assert S.category('CTR EHDL', '2025-06-11', None, cls='D', local_hour=10) == 'possible'    # clearance on request
    assert S.category('CTR EHDL', '2025-06-11', None, cls='D', local_hour=10, clearance_pattern='') == 'violation'
    assert S.category('CTR EHDL', '2025-06-11', None, cls='D', local_hour=17, clearance_pattern='') == 'possible'
    assert S.category('CTR EHDL', '2025-06-14', None, cls='D', local_hour=10) == 'possible'    # Saturday
    assert S.category('TMA1 SPL', '2025-06-14', None, cls='A', local_hour=10) == 'violation'   # class A, always
