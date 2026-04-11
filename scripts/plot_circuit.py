#!/usr/bin/env python3
"""
Plot glider circuit patterns for a given runway.

Draws actual GPS tracks of short flights (circuit flights) on a Folium map
to visualize the typical traffic pattern.

Usage:
    python scripts/plot_circuit.py                  # default: runway 25, 30 flights
    python scripts/plot_circuit.py --runway 07 --flights 20
"""

import sqlite3
import argparse
import sys
import os
import random
from colorsys import hsv_to_rgb

import folium

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src', 'visualization'))
from runway_constants import RUNWAY_LINES, AIRPORT_LAT, AIRPORT_LON


def get_circuit_flights(db_path, runway, num_flights):
    """Select short winch-launched flights (circuits) for the given runway."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Short flights = circuit flights (no thermal soaring)
    # Duration 4-15 min, winch launch, not aerotow plane
    rows = conn.execute('''
        SELECT f.id, f.registratie, f.vluchtduur, f.datum,
               COUNT(i.id) as points
        FROM flights f
        JOIN igc_data i ON i.flight_id = f.id
        WHERE f.runway = ?
          AND f.start_methode = 'sleep'
          AND f.registratie NOT IN ('PH-GOZ', 'PH GOZ')
          AND f.vluchtduur BETWEEN 4 AND 15
        GROUP BY f.id
        HAVING points >= 30
        ORDER BY RANDOM()
        LIMIT ?
    ''', (runway, num_flights)).fetchall()

    flights = []
    for row in rows:
        track = conn.execute('''
            SELECT latitude, longitude, altitude
            FROM igc_data
            WHERE flight_id = ?
            ORDER BY sequence
        ''', (row['id'],)).fetchall()

        flights.append({
            'id': row['id'],
            'reg': row['registratie'],
            'duration': row['vluchtduur'],
            'date': row['datum'],
            'track': [(t['latitude'], t['longitude']) for t in track
                       if t['latitude'] and t['longitude']],
            'altitudes': [t['altitude'] for t in track if t['altitude'] is not None],
        })

    conn.close()
    return flights


def generate_colors(n):
    """Generate n visually distinct colors."""
    colors = []
    for i in range(n):
        hue = i / n
        r, g, b = hsv_to_rgb(hue, 0.65, 0.85)
        colors.append(f'#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}')
    return colors


def create_circuit_map(flights, runway, output_path):
    """Create a Folium map with circuit tracks."""
    rwy = RUNWAY_LINES[runway]

    m = folium.Map(
        location=[AIRPORT_LAT, AIRPORT_LON],
        zoom_start=14,
        tiles='OpenStreetMap',
    )

    # Draw runway line
    folium.PolyLine(
        locations=[rwy['start'], rwy['end']],
        color='#333333',
        weight=6,
        opacity=0.9,
        tooltip=f"Runway {runway} ({rwy['bearing']:.0f}°)",
    ).add_to(m)

    # Runway labels
    folium.Marker(
        location=rwy['start'],
        icon=folium.DivIcon(html=f'<div style="font-size:14px;font-weight:bold;color:#333;'
                                  f'text-shadow:1px 1px white,-1px -1px white,1px -1px white,-1px 1px white">'
                                  f'RWY {runway}</div>'),
    ).add_to(m)

    # Airport marker
    folium.Marker(
        location=[AIRPORT_LAT, AIRPORT_LON],
        icon=folium.Icon(color='green', icon='plane', prefix='fa'),
        tooltip='EHHV - Hilversum Airport',
    ).add_to(m)

    # Draw flight tracks
    colors = generate_colors(len(flights))

    for flight, color in zip(flights, colors):
        if len(flight['track']) < 2:
            continue

        max_alt = max(flight['altitudes']) if flight['altitudes'] else 0

        folium.PolyLine(
            locations=flight['track'],
            color=color,
            weight=2,
            opacity=0.7,
            tooltip=f"{flight['reg']} | {flight['date']} | {flight['duration']}min | max {max_alt:.0f}m",
        ).add_to(m)

    # Title
    title_html = f'''
    <div style="position:fixed;top:10px;left:60px;z-index:9999;
                background:white;padding:12px 20px;border-radius:8px;
                box-shadow:0 2px 8px rgba(0,0,0,0.3);font-family:sans-serif;">
        <b>Circuit Pattern - Runway {runway}</b><br>
        <span style="font-size:12px;color:#666;">{len(flights)} flights | Winch launch | EHHV Hilversum</span>
    </div>
    '''
    m.get_root().html.add_child(folium.Element(title_html))

    m.save(output_path)
    return output_path


def main():
    parser = argparse.ArgumentParser(description='Plot glider circuit patterns')
    parser.add_argument('--runway', default='25', choices=['07', '12', '18', '25', '30', '36'])
    parser.add_argument('--flights', type=int, default=30, help='Number of flights to draw')
    parser.add_argument('--db', default='data/flights.db')
    parser.add_argument('--output', default=None, help='Output HTML file')
    args = parser.parse_args()

    if args.output is None:
        args.output = f'output/circuit_rwy{args.runway}.html'

    print(f"Fetching {args.flights} circuit flights for runway {args.runway}...")
    flights = get_circuit_flights(args.db, args.runway, args.flights)
    print(f"Found {len(flights)} flights")

    if not flights:
        print("No circuit flights found for this runway.")
        return 1

    output = create_circuit_map(flights, args.runway, args.output)
    print(f"Map saved to: {output}")
    print(f"  open {output}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
