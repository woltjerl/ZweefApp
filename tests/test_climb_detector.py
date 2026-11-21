import numpy as np
import pytest

from src import config as C
from src.thermal_detection.climb_detector import analyse_flight, prepare_tug, zigzag_segments
from tests.synthetic import Track, winch_launch


def run(track, launch_type='winch', tug=None, wind_at=None, logbook=True):
    t, x, y, z = track.arrays()
    start = t[60] if logbook else None
    return analyse_flight(t, x, y, z, launch_type, start, tug, wind_at)


def test_winch_then_straight_glide_has_no_climb():
    tr = winch_launch(Track())
    tr.straight(200, 25, -1.0).straight(95, 25, -1.0).land()
    out = run(tr)
    L = out['launch']
    assert L['track_status'] == 'ok'
    assert L['launch_quality'] == 'ok'
    assert abs(L['release_agl'] - 300) < 15
    assert out['climbs'] == []
    assert L['connected'] == 0
    assert out['search'] is not None and len(out['search']) > 10


def test_winch_then_circling_climb_is_detected():
    tr = winch_launch(Track())
    tr.straight(30, 25, -1.0)
    t_climb = tr.t[-1]
    tr.circle(120, vz=0.75).straight(300, 25, -1.0).straight(160, 25, -1.0).land()
    out = run(tr)
    assert len(out['climbs']) == 1
    c = out['climbs'][0]
    assert abs(c['start_utc_s'] - t_climb) <= 3
    assert 80 <= c['gain'] <= 95
    assert c['turn_deg'] >= 540
    L = out['launch']
    assert L['connected'] == 1
    assert 25 <= L['time_to_first_climb_s'] <= 35
    # search track ends where the climb starts
    assert out['search'][-1, 0] <= c['start_utc_s']


def test_pull_up_is_not_a_climb():
    tr = winch_launch(Track())
    tr.straight(20, 25, -1.0).straight(5, 25, 3.0).straight(250, 25, -1.0).land()
    assert run(tr)['climbs'] == []


def test_s_turns_are_not_a_climb():
    tr = winch_launch(Track())
    tr.s_turns(240).straight(270, 25, -1.0).land()
    assert run(tr)['climbs'] == []


def test_low_point_before_climb_is_climb_start():
    agl = np.array([300, 290, 280, 285, 280, 270, 300, 340, 380, 360, 330])
    t = np.arange(len(agl), dtype=float)
    segs = zigzag_segments(t, agl.astype(float))
    assert (5, 8) in segs


def test_aerotow_release_from_tug_pairing():
    def tow(tr):
        tr.ground(60).accelerate(10, 30.0)
        tr.straight(160, 32.0, 3.0, drift=False)   # tow to ~480 m
        return tr
    glider = tow(Track())
    t_release = glider.t[-1]
    glider.straight(20, 25, -0.5).circle(150, vz=1.5).straight(400, 25, -1.5).straight(200, 25, -1.5).land()
    tug = tow(Track(y=-50.0))
    tug.straight(60, 35, -5.0, heading=200).straight(40, 35, -4.0).land()

    tt, tx, ty, tz = tug.arrays()
    tug_track = prepare_tug(tt, tx, ty, tz, tt[60])
    assert tug_track is not None
    out = run(glider, 'aerotow', tug=tug_track)
    L = out['launch']
    assert L['release_method'] == 'tug_pair'
    assert abs(L['release_utc_s'] - t_release) <= 5
    # the climb right after release counts (no guard when paired), the tow does not
    assert len(out['climbs']) == 1
    assert out['climbs'][0]['agl0'] > 400


def test_aerotow_signature_fallback():
    glider = Track()
    glider.ground(60).accelerate(10, 30.0).straight(160, 32.0, 3.0, drift=False)
    t_release = glider.t[-1]
    glider.straight(30, 25, -1.0).straight(500, 25, -0.8).land()
    out = run(glider, 'aerotow')
    L = out['launch']
    assert L['release_method'] == 'signature'
    assert abs(L['release_utc_s'] - t_release) <= 20
    assert out['climbs'] == []


def test_drift_correction_moves_source_upwind():
    wind = (20 / 3.6, 0.0)  # air moves east at 20 km/h (wind from the west)
    tr = winch_launch(Track(wind=wind))
    tr.straight(20, 25, -1.0)
    tr.circle(150, vz=1.0).straight(500, 25, -1.0).land()
    out = run(tr)
    assert len(out['climbs']) == 1
    c = out['climbs'][0]
    assert c['wind_source'] == 'own_drift'
    expected = wind[0] * c['agl0'] / (c['rate'] + C.CIRCLING_SINK_MS)
    assert abs((c['xc0'] - c['source_x']) - expected) < 50
    assert abs(c['source_y'] - c['yc0']) < 50


def test_midnight_wrap_and_logging_gap():
    tr = winch_launch(Track(t0=86400 - 120.0))
    tr.straight(30, 25, -1.0).circle(120, vz=0.8)
    # 60 s logging gap during the glide
    tr.t.append(tr.t[-1] + 60)
    tr.x.append(tr.x[-1] + 1500)
    tr.y.append(tr.y[-1])
    tr.z.append(tr.z[-1] - 60)
    tr.straight(250, 25, -1.0).land()
    t, x, y, z = tr.arrays()
    t = t % 86400  # logger writes HH:MM:SS
    out = analyse_flight(t, x, y, z, 'winch', None, None, None)
    assert out['launch']['launch_quality'] == 'ok'
    assert len(out['climbs']) == 1


def test_logging_gap_in_search_marks_launch():
    tr = winch_launch(Track())
    tr.straight(20, 25, -1.0)
    tr.t.append(tr.t[-1] + 240)   # 4-minute gap, glider climbs meanwhile
    tr.x.append(tr.x[-1] + 500)
    tr.y.append(tr.y[-1])
    tr.z.append(tr.z[-1] + 250)
    tr.circle(120, vz=1.0).straight(700, 25, -1.0).land()
    out = run(tr)
    assert out['launch']['launch_quality'] == 'gap_in_search'


def test_track_ending_in_the_air_marks_launch():
    tr = winch_launch(Track())
    tr.straight(60, 25, -1.0)
    out = run(tr)
    assert out['launch']['launch_quality'] == 'track_ends_airborne'


def test_track_not_matching_logbook_is_rejected():
    tr = winch_launch(Track())
    tr.straight(300, 25, -1.0).land()
    t, x, y, z = tr.arrays()
    out = analyse_flight(t, x, y, z, 'winch', t[60] + 3 * 3600, None, None)
    assert out['launch']['track_status'] == 'no_takeoff_match'


def test_two_flights_in_one_file_selects_logbook_flight():
    tr = winch_launch(Track())
    tr.straight(300, 25, -1.0).land(120)
    second_start = tr.t[-1]
    winch_launch(tr, release_agl=350).straight(20, 25, -1).circle(120, vz=1).straight(400, 25, -1).land()
    t, x, y, z = tr.arrays()
    out = analyse_flight(t, x, y, z, 'winch', second_start + 60, None, None)
    assert out['launch']['track_status'] == 'ok'
    assert abs(out['launch']['release_agl'] - 350) < 20
    assert len(out['climbs']) == 1
