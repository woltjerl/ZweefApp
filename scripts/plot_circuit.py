#!/usr/bin/env python3
"""
Plot glider circuit patterns for a given runway.

Draws actual GPS tracks of short flights (circuit flights) on a Folium map
to visualize the typical traffic pattern. Shows only the last 3 minutes
of each flight (approach + circuit), removes outlier flights, and overlays
the median circuit as the most representative pattern.

Usage:
    python scripts/plot_circuit.py                  # default: runway 25, 50 flights
    python scripts/plot_circuit.py --runway 07 --flights 80
"""

import sqlite3
import argparse
import sys
import os
from colorsys import hsv_to_rgb

import folium
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src', 'visualization'))
from runway_constants import RUNWAY_LINES, AIRPORT_LAT, AIRPORT_LON


LAST_SECONDS = 180          # Only plot the last 3 minutes of each flight
RESAMPLE_POINTS = 60        # Points per flight after time-based resampling
OUTLIER_MAD_K = 3.0         # Drop flights whose deviation > median + K * MAD


def time_to_seconds(t):
    """Convert 'HH:MM:SS' string into seconds since midnight."""
    if not t:
        return None
    parts = t.split(':')
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return None


def get_circuit_flights(db_path, runway, num_flights):
    """Select flights that landed on the given runway; use only their final minutes."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Over-select to absorb any post-filtering (short tracks, outliers)
    fetch_limit = num_flights * 2 + 20

    # Every flight ending on this runway contains an approach/circuit,
    # regardless of launch method or total duration. Exclude only the
    # tug plane registration.
    rows = conn.execute('''
        SELECT f.id, f.registratie, f.vluchtduur, f.datum,
               COUNT(i.id) as points
        FROM flights f
        JOIN igc_data i ON i.flight_id = f.id
        WHERE f.runway = ?
          AND f.registratie NOT IN ('PH-GOZ', 'PH GOZ')
        GROUP BY f.id
        HAVING points >= 30
        ORDER BY RANDOM()
        LIMIT ?
    ''', (runway, fetch_limit)).fetchall()

    flights = []
    for row in rows:
        track = conn.execute('''
            SELECT time, latitude, longitude, altitude
            FROM igc_data
            WHERE flight_id = ?
            ORDER BY sequence
        ''', (row['id'],)).fetchall()

        pts = [(time_to_seconds(t['time']), t['latitude'], t['longitude'], t['altitude'])
               for t in track
               if t['latitude'] is not None and t['longitude'] is not None
               and time_to_seconds(t['time']) is not None]

        if len(pts) < 10:
            continue

        # Keep only the last LAST_SECONDS of the flight
        t_end = pts[-1][0]
        pts = [p for p in pts if t_end - p[0] <= LAST_SECONDS]
        if len(pts) < 5:
            continue

        flights.append({
            'id': row['id'],
            'reg': row['registratie'],
            'duration': row['vluchtduur'],
            'date': row['datum'],
            'times': [p[0] for p in pts],
            'track': [(p[1], p[2]) for p in pts],
            'altitudes': [p[3] for p in pts if p[3] is not None],
        })

    conn.close()
    return flights


def resample_track(flight, n=RESAMPLE_POINTS):
    """Resample a flight track to n points evenly spaced in time."""
    times = np.array(flight['times'], dtype=float)
    lats = np.array([p[0] for p in flight['track']], dtype=float)
    lons = np.array([p[1] for p in flight['track']], dtype=float)

    t0, t1 = times[0], times[-1]
    if t1 <= t0:
        return None
    grid = np.linspace(t0, t1, n)
    return np.column_stack([np.interp(grid, times, lats),
                            np.interp(grid, times, lons)])


def remove_outliers(flights, k=OUTLIER_MAD_K):
    """Drop flights whose track is unusually far from the median.

    Uses a robust MAD-based threshold (median + k * MAD) rather than a fixed
    percentile, so tightly clustered sets lose almost nothing while truly
    aberrant tracks are still removed.
    """
    resampled = []
    keep = []
    for f in flights:
        r = resample_track(f)
        if r is not None:
            resampled.append(r)
            keep.append(f)

    if len(resampled) < 5:
        return keep, (np.median(np.stack(resampled), axis=0) if resampled else None)

    stack = np.stack(resampled)                     # (n_flights, n_points, 2)
    median = np.median(stack, axis=0)               # (n_points, 2)

    lat0 = np.deg2rad(median[:, 0].mean())
    dlat = (stack[:, :, 0] - median[:, 0]) * 111_000
    dlon = (stack[:, :, 1] - median[:, 1]) * 111_000 * np.cos(lat0)
    dist = np.sqrt(dlat**2 + dlon**2).mean(axis=1)  # meters, per flight

    med = np.median(dist)
    mad = np.median(np.abs(dist - med))
    cutoff = med + k * (mad if mad > 0 else med)

    kept = [f for f, d in zip(keep, dist) if d <= cutoff]
    removed = len(keep) - len(kept)
    if removed:
        print(f"  Removed {removed} outlier flight(s) (cutoff {cutoff:.0f} m)")

    kept_resampled = [resample_track(f) for f in kept]
    kept_resampled = [r for r in kept_resampled if r is not None]
    median_track = np.median(np.stack(kept_resampled), axis=0) if kept_resampled else median

    return kept, median_track


def generate_colors(n):
    """Generate n visually distinct colors."""
    colors = []
    for i in range(max(n, 1)):
        hue = i / max(n, 1)
        r, g, b = hsv_to_rgb(hue, 0.65, 0.85)
        colors.append(f'#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}')
    return colors


def draw_all_runways(m, active_runway):
    """Draw every runway; highlight the active one."""
    for rwy_name, rwy in RUNWAY_LINES.items():
        is_active = rwy_name == active_runway
        folium.PolyLine(
            locations=[rwy['start'], rwy['end']],
            color='#222222' if is_active else rwy['color'],
            weight=6 if is_active else 3,
            opacity=0.9 if is_active else 0.55,
            tooltip=f"Runway {rwy_name} ({rwy['bearing']:.0f}°)",
        ).add_to(m)

        folium.Marker(
            location=rwy['start'],
            icon=folium.DivIcon(html=(
                f'<div style="font-size:{"13" if is_active else "11"}px;'
                f'font-weight:{"bold" if is_active else "normal"};'
                f'color:{"#222" if is_active else "#555"};'
                f'text-shadow:1px 1px white,-1px -1px white,1px -1px white,-1px 1px white">'
                f'RWY {rwy_name}</div>'
            )),
        ).add_to(m)


def create_circuit_map(flights, median_track, runway, output_path):
    """Create a Folium map with circuit tracks and median overlay."""
    m = folium.Map(
        location=[AIRPORT_LAT, AIRPORT_LON],
        zoom_start=14,
        tiles='OpenStreetMap',
    )

    draw_all_runways(m, runway)

    folium.Marker(
        location=[AIRPORT_LAT, AIRPORT_LON],
        icon=folium.Icon(color='green', icon='plane', prefix='fa'),
        tooltip='EHHV - Hilversum Airport',
    ).add_to(m)

    colors = generate_colors(len(flights))
    for flight, color in zip(flights, colors):
        if len(flight['track']) < 2:
            continue
        max_alt = max(flight['altitudes']) if flight['altitudes'] else 0
        folium.PolyLine(
            locations=flight['track'],
            color=color,
            weight=2,
            opacity=0.55,
            tooltip=(f"{flight['reg']} | {flight['date']} | "
                     f"{flight['duration']}min | max {max_alt:.0f}m"),
        ).add_to(m)

    # Median / representative circuit
    if median_track is not None:
        folium.PolyLine(
            locations=[(lat, lon) for lat, lon in median_track],
            color='#000000',
            weight=5,
            opacity=0.95,
            tooltip='Median circuit (last 3 min)',
        ).add_to(m)
        folium.PolyLine(
            locations=[(lat, lon) for lat, lon in median_track],
            color='#FFEB3B',
            weight=3,
            opacity=0.95,
            tooltip='Median circuit (last 3 min)',
        ).add_to(m)

    title_html = f'''
    <div style="position:fixed;top:10px;left:60px;z-index:9999;
                background:white;padding:12px 20px;border-radius:8px;
                box-shadow:0 2px 8px rgba(0,0,0,0.3);font-family:sans-serif;">
        <b>Circuit Pattern - Runway {runway}</b><br>
        <span style="font-size:12px;color:#666;">
            {len(flights)} flights | last 3 min | outliers removed<br>
            Yellow/black line = median circuit | EHHV Hilversum
        </span>
    </div>
    '''
    m.get_root().html.add_child(folium.Element(title_html))

    m.save(output_path)
    return output_path


def plot_one_runway(runway, num_flights, db_path, output_path):
    """Generate the circuit map for a single runway. Returns output path or None."""
    print(f"\n=== Runway {runway} ===")
    flights = get_circuit_flights(db_path, runway, num_flights)
    print(f"Loaded {len(flights)} flights (last {LAST_SECONDS//60} min of each)")

    if not flights:
        print(f"No flights found for runway {runway}.")
        return None

    flights, median_track = remove_outliers(flights)
    flights = flights[:num_flights]
    print(f"Using {len(flights)} flights in plot")

    output = create_circuit_map(flights, median_track, runway, output_path)
    print(f"Map saved to: {output}")
    return output


def main():
    parser = argparse.ArgumentParser(description='Plot glider circuit patterns')
    parser.add_argument('--runway', default=None, choices=list(RUNWAY_LINES.keys()),
                        help='Runway to plot. If omitted, plots every runway.')
    parser.add_argument('--flights', type=int, default=50,
                        help='Target number of flights to draw (after outlier removal)')
    parser.add_argument('--db', default='data/flights.db')
    parser.add_argument('--output', default=None,
                        help='Output HTML file (only used with --runway)')
    args = parser.parse_args()

    runways = [args.runway] if args.runway else list(RUNWAY_LINES.keys())

    if args.output and len(runways) > 1:
        print("--output is ignored when plotting all runways; using default paths.")
        args.output = None

    produced = []
    for rwy in runways:
        out_path = args.output or f'output/circuit_rwy{rwy}.html'
        result = plot_one_runway(rwy, args.flights, args.db, out_path)
        if result:
            produced.append(result)

    if not produced:
        return 1

    print("\nDone. Open with:")
    for p in produced:
        print(f"  open {p}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
