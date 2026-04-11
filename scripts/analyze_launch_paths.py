#!/usr/bin/env python3
"""Analyze actual launch paths from GPS data to calculate runway lines."""

import sqlite3
import math
import sys
from statistics import mean, median

def calculate_bearing(lat1, lon1, lat2, lon2):
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(lat2_rad)
    y = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def main():
    conn = sqlite3.connect('data/flights.db')
    cursor = conn.cursor()

    print("=" * 70)
    print("COMPREHENSIVE LAUNCH PATH ANALYSIS")
    print("=" * 70)
    sys.stdout.flush()

    # STEP 1: Find actual airfield location from ground-level GPS points
    print("\n--- STEP 1: Find actual airfield location ---\n")
    sys.stdout.flush()

    cursor.execute("""
        SELECT i.latitude, i.longitude
        FROM igc_data i
        JOIN flights f ON i.flight_id = f.id
        WHERE i.altitude <= 10
          AND i.sequence < 5
          AND f.start_methode = 'lier'
          AND i.latitude IS NOT NULL
        LIMIT 5000
    """)
    ground_points = cursor.fetchall()
    print(f"Ground-level points (alt <= 10m): {len(ground_points)}")

    if ground_points:
        all_lats = [p[0] for p in ground_points]
        all_lons = [p[1] for p in ground_points]
        center_lat = median(all_lats)
        center_lon = median(all_lons)
        print(f"Median ground position: ({center_lat:.6f}, {center_lon:.6f})")
        print(f"Defined AIRPORT_LAT/LON: (52.223611, 5.146944)")
        print(f"Difference: {haversine(52.223611, 5.146944, center_lat, center_lon):.0f}m")
    sys.stdout.flush()

    # STEP 2: Analyze each winch launch runway
    print("\n--- STEP 2: Winch launch paths per runway ---\n")
    sys.stdout.flush()

    runways = ['07', '12', '18', '25', '30', '36']
    results = {}

    for runway in runways:
        cursor.execute("""
            SELECT f.id FROM flights f
            WHERE f.runway = ? AND f.start_methode = 'lier'
        """, (runway,))
        flight_ids = [r[0] for r in cursor.fetchall()]

        if not flight_ids:
            print(f"Runway {runway}: No flights")
            continue

        print(f"Runway {runway}: analyzing {len(flight_ids)} flights...", end=" ")
        sys.stdout.flush()

        start_lats, start_lons = [], []
        release_lats, release_lons = [], []
        release_alts = []

        for flight_id in flight_ids:
            cursor.execute("""
                SELECT latitude, longitude, altitude, vertical_speed, sequence
                FROM igc_data
                WHERE flight_id = ?
                ORDER BY sequence
                LIMIT 200
            """, (flight_id,))
            points = cursor.fetchall()

            if len(points) < 10:
                continue

            # START: average of points at alt < 20m
            ground = [(lat, lon) for lat, lon, alt, vs, seq in points
                      if alt is not None and alt < 20 and lat and lon]
            if ground:
                start_lats.append(mean([p[0] for p in ground]))
                start_lons.append(mean([p[1] for p in ground]))

            # RELEASE: find peak altitude during initial climb
            max_alt = 0
            release_point = None
            for lat, lon, alt, vs, seq in points:
                if alt is not None and lat and lon:
                    if alt > max_alt:
                        max_alt = alt
                        release_point = (lat, lon, alt)
                    elif release_point and alt < max_alt - 20:
                        break

            if release_point and release_point[2] > 100:
                release_lats.append(release_point[0])
                release_lons.append(release_point[1])
                release_alts.append(release_point[2])

        if start_lats and release_lats:
            s_lat = median(start_lats)
            s_lon = median(start_lons)
            r_lat = median(release_lats)
            r_lon = median(release_lons)
            r_alt = median(release_alts)
            bearing = calculate_bearing(s_lat, s_lon, r_lat, r_lon)
            dist = haversine(s_lat, s_lon, r_lat, r_lon)

            results[runway] = {
                'start': (round(s_lat, 6), round(s_lon, 6)),
                'end': (round(r_lat, 6), round(r_lon, 6)),
                'bearing': round(bearing, 1),
                'distance_m': round(dist),
                'release_alt': round(r_alt),
            }

            print(f"done")
            print(f"  Start (ground):   ({s_lat:.6f}, {s_lon:.6f})")
            print(f"  Release (peak):   ({r_lat:.6f}, {r_lon:.6f})")
            print(f"  Release altitude: {r_alt:.0f}m (median)")
            print(f"  Bearing:          {bearing:.1f}°")
            print(f"  Distance:         {dist:.0f}m")
            print()
        sys.stdout.flush()

    # STEP 3: Aerotow (takeoff only, alt < 50m)
    print("\n--- STEP 3: Aerotow takeoff path (alt < 50m) ---\n")
    sys.stdout.flush()

    cursor.execute("""
        SELECT f.id FROM flights f
        WHERE f.start_methode = 'sleep'
          AND f.registratie NOT IN ('PH-GOZ', 'PH GOZ')
    """)
    aero_ids = [r[0] for r in cursor.fetchall()]
    print(f"Total aerotow flights: {len(aero_ids)}")

    aero_start_lats, aero_start_lons = [], []
    aero_end_lats, aero_end_lons = [], []

    for flight_id in aero_ids:
        cursor.execute("""
            SELECT latitude, longitude, altitude, sequence
            FROM igc_data
            WHERE flight_id = ?
              AND altitude <= 50
            ORDER BY sequence
            LIMIT 50
        """, (flight_id,))
        points = cursor.fetchall()

        if len(points) < 5:
            continue

        first = [(lat, lon) for lat, lon, alt, seq in points[:5] if lat and lon]
        last = [(lat, lon) for lat, lon, alt, seq in points[-5:] if lat and lon]

        if first and last:
            aero_start_lats.append(mean([p[0] for p in first]))
            aero_start_lons.append(mean([p[1] for p in first]))
            aero_end_lats.append(mean([p[0] for p in last]))
            aero_end_lons.append(mean([p[1] for p in last]))

    if aero_start_lats and aero_end_lats:
        as_lat = median(aero_start_lats)
        as_lon = median(aero_start_lons)
        ae_lat = median(aero_end_lats)
        ae_lon = median(aero_end_lons)
        abearing = calculate_bearing(as_lat, as_lon, ae_lat, ae_lon)
        adist = haversine(as_lat, as_lon, ae_lat, ae_lon)

        print(f"Aerotow ({len(aero_start_lats)} flights with data):")
        print(f"  Start (ground):   ({as_lat:.6f}, {as_lon:.6f})")
        print(f"  End (at 50m):     ({ae_lat:.6f}, {ae_lon:.6f})")
        print(f"  Bearing:          {abearing:.1f}°")
        print(f"  Distance:         {adist:.0f}m")
    sys.stdout.flush()

    # STEP 4: Output ready-to-use constants
    print("\n\n" + "=" * 70)
    print("READY-TO-USE RUNWAY_CONSTANTS.PY VALUES")
    print("=" * 70)

    colors = {
        '07': ('#FF6B6B', 'Red'),
        '12': ('#4ECDC4', 'Teal'),
        '18': ('#95E1D3', 'Light teal'),
        '25': ('#F38181', 'Pink'),
        '30': ('#AA96DA', 'Purple'),
        '36': ('#FCBAD3', 'Light pink'),
    }

    print("\nRUNWAY_LINES = {")
    for rw in runways:
        if rw in results:
            r = results[rw]
            c, cn = colors[rw]
            print(f"    '{rw}': {{")
            print(f"        'bearing': {r['bearing']},")
            print(f"        'start': {r['start']},")
            print(f"        'end': {r['end']},")
            print(f"        'color': '{c}',  # {cn}")
            print(f"        'name': 'Runway {rw}',")
            print(f"    }},")
    print("}")

    if aero_start_lats:
        print(f"\nAEROTOW_LINE = {{")
        print(f"    'bearing': {round(abearing, 1)},")
        print(f"    'start': ({round(as_lat, 6)}, {round(as_lon, 6)}),")
        print(f"    'end': ({round(ae_lat, 6)}, {round(ae_lon, 6)}),")
        print(f"    'color': '#FFD93D',  # Yellow")
        print(f"    'name': 'Aerotow',")
        print(f"}}")

    conn.close()

if __name__ == "__main__":
    main()
