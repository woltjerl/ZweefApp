import sqlite3

import pandas as pd

from src.thermal_detection import sync_legacy_columns as S


def _db(tmp_path):
    conn = sqlite3.connect(tmp_path / 't.db')
    conn.executescript("""
    CREATE TABLE flights (id INTEGER PRIMARY KEY, start_methode TEXT, flight_outcome TEXT,
      release_lat REAL, release_lon REAL, release_alt REAL, thermal_entry_lat REAL, thermal_entry_lon REAL,
      thermal_entry_alt REAL, release_to_thermal_bearing REAL, release_to_thermal_distance_m REAL,
      release_to_thermal_seconds INTEGER, flight_duration_after_release INTEGER, max_altitude REAL,
      thermal_processed INTEGER, release_thermal_processed INTEGER);
    CREATE TABLE igc_data (id INTEGER PRIMARY KEY, flight_id INTEGER, time TEXT, first_thermal INTEGER);
    """)
    # 1: winch, connected; 2: winch, landed back but old label said thermal; 3: self-launch with old label
    conn.executemany("INSERT INTO flights (id, start_methode, flight_outcome) VALUES (?, ?, ?)",
                     [(1, 'lier', 'landed_back'), (2, 'lier', 'thermal_found'), (3, 'zelf', 'thermal_found')])
    rows = [(k + 1, 1, f"12:{k // 60:02d}:{k % 60:02d}", 1 if k < 5 else 0) for k in range(600)]
    rows += [(1000 + k, 2, f"13:00:{k:02d}", 1) for k in range(10)]
    conn.executemany("INSERT INTO igc_data VALUES (?, ?, ?, ?)", rows)
    pd.DataFrame([
        {'flight_id': 1, 'launch_type': 'winch', 'launch_quality': 'ok', 'track_status': 'ok', 'connected': 1,
         'release_x': 0.0, 'release_y': 0.0, 'release_agl': 300.0, 'time_to_first_climb_s': 60.0,
         'dur_after_release_s': 1800.0, 'max_agl': 900.0},
        {'flight_id': 2, 'launch_type': 'winch', 'launch_quality': 'ok', 'track_status': 'ok', 'connected': 0,
         'release_x': 10.0, 'release_y': 10.0, 'release_agl': 280.0, 'time_to_first_climb_s': None,
         'dur_after_release_s': 240.0, 'max_agl': 280.0},
        {'flight_id': 3, 'launch_type': 'self', 'launch_quality': 'ok', 'track_status': 'ok', 'connected': 0,
         'release_x': None, 'release_y': None, 'release_agl': None, 'time_to_first_climb_s': None,
         'dur_after_release_s': None, 'max_agl': None},
    ]).to_sql('launches', conn, index=False)
    # first climb of flight 1 starts 12:01:00 (43260 s), 250 m AGL, 1 m/s for 300 s → reaches 600 m at +350 s
    pd.DataFrame([{'flight_id': 1, 'is_first': 1, 'x0': 500.0, 'y0': 0.0, 'agl0': 250.0, 'agl1': 550.0,
                   'start_utc_s': 43260.0, 'duration': 300.0, 'rate': 1.0}]).to_sql('climbs', conn, index=False)
    return conn


def test_sync_overwrites_and_restore_brings_back(tmp_path):
    conn = _db(tmp_path)
    S.sync(conn, verbose=False)
    f = pd.read_sql_query("SELECT * FROM flights ORDER BY id", conn).set_index('id')
    assert f.loc[1, 'flight_outcome'] == 'thermal_found'
    assert f.loc[2, 'flight_outcome'] == 'landed_back'
    assert pd.isna(f.loc[3, 'flight_outcome'])                 # winch logic does not apply to self-launch
    assert abs(f.loc[1, 'release_to_thermal_distance_m'] - 500) < 1e-6
    assert abs(f.loc[1, 'release_to_thermal_bearing'] - 90) < 1e-6
    assert f.loc[1, 'thermal_entry_alt'] == 250.0
    ft = pd.read_sql_query("SELECT id, flight_id, time FROM igc_data WHERE first_thermal = 1", conn)
    assert set(ft.flight_id) == {1}
    assert ft.time.min() == '12:01:00' and ft.time.max() == '12:06:00'   # whole 300 s climb (< 600 m)

    S.restore(conn, verbose=False)
    f = pd.read_sql_query("SELECT * FROM flights ORDER BY id", conn).set_index('id')
    assert list(f.flight_outcome) == ['landed_back', 'thermal_found', 'thermal_found']
    assert conn.execute("SELECT COUNT(*) FROM igc_data WHERE first_thermal = 1").fetchone()[0] == 15


def test_backup_is_taken_once(tmp_path):
    conn = _db(tmp_path)
    S.sync(conn, verbose=False)
    S.sync(conn, verbose=False)        # second run must not overwrite the backup with new labels
    b = pd.read_sql_query("SELECT id, flight_outcome FROM legacy_labels_backup ORDER BY id", conn)
    assert list(b.flight_outcome) == ['landed_back', 'thermal_found', 'thermal_found']
