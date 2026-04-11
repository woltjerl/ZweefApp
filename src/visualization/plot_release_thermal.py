#!/usr/bin/env python3
"""
Visualize Release-to-Thermal Navigation Data

Creates an interactive map showing:
- Release points and thermal entry points connected by lines
- Color-coded by success (thermal found vs landed back)
- Bearing distribution rose diagram
- Filterable by runway and wind direction

Usage:
    python plot_release_thermal.py
    python plot_release_thermal.py --runway 30
    python plot_release_thermal.py --runway 30 --wind-dir 300 --wind-tolerance 30
"""

import sqlite3
import math
import argparse
import os
import sys
from typing import Optional

import folium
from folium.plugins import HeatMap
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from runway_constants import RUNWAY_LINES, AIRPORT_LAT, AIRPORT_LON


def fetch_data(db_path, runway=None, wind_dir=None, wind_tolerance=30):
    """Fetch release-to-thermal data from database."""
    conn = sqlite3.connect(db_path)

    filters = [
        "f.release_lat IS NOT NULL",
        "f.release_lon IS NOT NULL",
        "f.flight_outcome IN ('thermal_found', 'landed_back')",
        "f.start_methode = 'lier'",
    ]

    if runway:
        filters.append(f"f.runway = '{runway}'")

    if wind_dir is not None:
        dir_min = (wind_dir - wind_tolerance) % 360
        dir_max = (wind_dir + wind_tolerance) % 360
        if dir_min > dir_max:
            filters.append(f"(w.wdir >= {dir_min} OR w.wdir <= {dir_max})")
        else:
            filters.append(f"w.wdir BETWEEN {dir_min} AND {dir_max}")

    where = " AND ".join(filters)

    # Join with weather_data to get wind for the flight's date/hour
    query = f"""
        SELECT
            f.id, f.datum, f.runway,
            f.release_lat, f.release_lon, f.release_alt,
            f.thermal_entry_lat, f.thermal_entry_lon, f.thermal_entry_alt,
            f.release_to_thermal_bearing, f.release_to_thermal_distance_m,
            f.release_to_thermal_seconds,
            f.flight_duration_after_release, f.max_altitude,
            f.flight_outcome,
            w.wdir, w.wspd
        FROM flights f
        LEFT JOIN weather_data w ON f.datum = w.date AND w.hour = 12
        WHERE {where}
    """

    df = pd.read_sql_query(query, conn)
    conn.close()
    return df


def create_map(df, output_file, runway=None, wind_dir=None):
    """Create interactive release-to-thermal visualization."""

    m = folium.Map(location=[AIRPORT_LAT, AIRPORT_LON], zoom_start=14)

    # Separate thermal found vs landed back
    found = df[df['flight_outcome'] == 'thermal_found']
    landed = df[df['flight_outcome'] == 'landed_back']

    # --- Layer: Thermal entry heatmap ---
    if len(found) > 0:
        thermal_heat = found[['thermal_entry_lat', 'thermal_entry_lon']].dropna().values.tolist()
        if thermal_heat:
            heat_group = folium.FeatureGroup(name="Thermal Heatmap", show=True)
            HeatMap(
                thermal_heat,
                radius=15, blur=20, min_opacity=0.3, max_opacity=0.8,
                gradient={0.4: 'blue', 0.6: 'cyan', 0.7: 'lime', 0.8: 'yellow', 1.0: 'red'}
            ).add_to(heat_group)
            heat_group.add_to(m)

    # --- Layer: Release-to-thermal lines (successful) ---
    lines_group = folium.FeatureGroup(name="Release → Thermal (found)", show=True)
    for _, row in found.iterrows():
        if pd.notna(row['thermal_entry_lat']) and pd.notna(row['thermal_entry_lon']):
            folium.PolyLine(
                locations=[
                    [row['release_lat'], row['release_lon']],
                    [row['thermal_entry_lat'], row['thermal_entry_lon']]
                ],
                color='#2ca02c',
                weight=1.5,
                opacity=0.4,
                popup=(f"<b>Thermal found</b><br>"
                       f"Date: {row['datum']}<br>"
                       f"Runway: {row['runway']}<br>"
                       f"Bearing: {row['release_to_thermal_bearing']:.0f}°<br>"
                       f"Distance: {row['release_to_thermal_distance_m']:.0f}m<br>"
                       f"Time: {row['release_to_thermal_seconds']}s<br>"
                       f"Release alt: {row['release_alt']:.0f}m<br>"
                       f"Thermal alt: {row['thermal_entry_alt']:.0f}m")
            ).add_to(lines_group)
    lines_group.add_to(m)

    # --- Layer: Release points (landed back) ---
    landed_group = folium.FeatureGroup(name="Release → Landed back", show=False)
    for _, row in landed.iterrows():
        folium.CircleMarker(
            location=[row['release_lat'], row['release_lon']],
            radius=2,
            color='#d62728',
            fill=True,
            fillOpacity=0.3,
            popup=f"<b>Landed back</b><br>Date: {row['datum']}<br>Runway: {row['runway']}<br>Release alt: {row['release_alt']:.0f}m"
        ).add_to(landed_group)
    landed_group.add_to(m)

    # --- Layer: Thermal entry points ---
    entry_group = folium.FeatureGroup(name="Thermal entry points", show=False)
    for _, row in found.iterrows():
        if pd.notna(row['thermal_entry_lat']):
            folium.CircleMarker(
                location=[row['thermal_entry_lat'], row['thermal_entry_lon']],
                radius=3,
                color='#2ca02c',
                fill=True,
                fillColor='green',
                fillOpacity=0.5,
                popup=(f"<b>Thermal entry</b><br>"
                       f"Alt: {row['thermal_entry_alt']:.0f}m<br>"
                       f"From release: {row['release_to_thermal_bearing']:.0f}° / {row['release_to_thermal_distance_m']:.0f}m")
            ).add_to(entry_group)
    entry_group.add_to(m)

    # --- Runway lines ---
    for runway_id, runway_data in RUNWAY_LINES.items():
        is_active = runway and runway == runway_id
        folium.PolyLine(
            locations=[runway_data['start'], runway_data['end']],
            color=runway_data['color'],
            weight=4 if is_active else 2,
            opacity=1.0 if is_active else 0.5,
            dash_array=None if is_active else '5, 5',
            tooltip=runway_data['name']
        ).add_to(m)

        if is_active:
            folium.Marker(
                location=runway_data['start'],
                popup=f"<b>EHHV</b><br>{runway_data['name']}",
                tooltip="EHHV",
                icon=folium.Icon(color='green', icon='plane', prefix='fa')
            ).add_to(m)
            lier_html = '<div style="font-size:14px; font-weight:bold; color:#333; text-shadow:1px 1px 2px white;">L</div>'
            folium.Marker(
                location=runway_data['end'],
                popup=f"<b>Lier</b><br>{runway_data['name']}",
                tooltip="Lier",
                icon=folium.DivIcon(html=lier_html, icon_size=(16, 16), icon_anchor=(8, 8))
            ).add_to(m)

    # --- Bearing rose diagram (HTML overlay) ---
    if len(found) > 0 and 'release_to_thermal_bearing' in found.columns:
        bearings = found['release_to_thermal_bearing'].dropna()
        sectors = {}
        sector_labels = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
        for label in sector_labels:
            sectors[label] = 0
        for b in bearings:
            idx = round(b / 45) % 8
            sectors[sector_labels[idx]] += 1

        total_found = len(bearings)
        max_count = max(sectors.values()) if sectors else 1

        # Build SVG rose diagram
        cx, cy, r = 80, 80, 65
        svg_paths = []
        angles = [270, 315, 0, 45, 90, 135, 180, 225]  # SVG angles for N,NE,E,SE,S,SW,W,NW

        for i, label in enumerate(sector_labels):
            count = sectors[label]
            pct = count / max(total_found, 1)
            length = r * (count / max(max_count, 1))

            angle_rad = math.radians(angles[i])
            x = cx + length * math.cos(angle_rad)
            y = cy + length * math.sin(angle_rad)

            # Label position (just outside the circle)
            lx = cx + (r + 14) * math.cos(angle_rad)
            ly = cy + (r + 14) * math.sin(angle_rad)

            color_intensity = int(80 + 175 * (count / max(max_count, 1)))
            color = f"rgb(0, {color_intensity}, 0)"

            svg_paths.append(
                f'<line x1="{cx}" y1="{cy}" x2="{x:.0f}" y2="{y:.0f}" '
                f'stroke="{color}" stroke-width="{max(2, 8 * count / max(max_count, 1)):.0f}" stroke-linecap="round"/>'
            )
            svg_paths.append(
                f'<text x="{lx:.0f}" y="{ly:.0f}" text-anchor="middle" dominant-baseline="central" '
                f'font-size="10" font-weight="bold" fill="#333">{label}</text>'
            )
            svg_paths.append(
                f'<text x="{lx:.0f}" y="{ly + 11:.0f}" text-anchor="middle" font-size="8" fill="#666">'
                f'{100*pct:.0f}%</text>'
            )

        # Stats text
        median_dist = found['release_to_thermal_distance_m'].median()
        median_time = found['release_to_thermal_seconds'].median()
        success_rate = 100 * len(found) / max(len(df), 1)

        stats_html = (
            f'<div style="font-size:11px; margin-top:4px; color:#333;">'
            f'<b>Flights:</b> {len(df)} total<br>'
            f'<b>Thermal found:</b> {len(found)} ({success_rate:.0f}%)<br>'
            f'<b>Median distance:</b> {median_dist:.0f}m<br>'
            f'<b>Median time:</b> {median_time:.0f}s<br>'
        )
        if wind_dir is not None:
            stats_html += f'<b>Wind filter:</b> {wind_dir}°<br>'
        if runway:
            stats_html += f'<b>Runway:</b> {runway}<br>'
        stats_html += '</div>'

        rose_html = f'''
        <div style="position:fixed; top:10px; right:10px; z-index:9999;
                    background:white; padding:10px; border-radius:8px;
                    box-shadow: 0 2px 6px rgba(0,0,0,0.3); max-width:200px;">
            <div style="text-align:center; font-weight:bold; font-size:12px; margin-bottom:4px;">
                Direction to Thermal
            </div>
            <svg width="160" height="160" viewBox="0 0 160 160">
                <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#ddd" stroke-width="1"/>
                <circle cx="{cx}" cy="{cy}" r="{r*0.5:.0f}" fill="none" stroke="#eee" stroke-width="1"/>
                {"".join(svg_paths)}
                <circle cx="{cx}" cy="{cy}" r="3" fill="#333"/>
            </svg>
            {stats_html}
        </div>
        '''
        m.get_root().html.add_child(folium.Element(rose_html))

    # Layer control
    folium.LayerControl().add_to(m)

    m.save(output_file)
    print(f"\n✓ Map saved to: {output_file}")
    print(f"  Flights: {len(df)} ({len(found)} thermal found, {len(landed)} landed back)")


def main():
    parser = argparse.ArgumentParser(description='Visualize release-to-thermal navigation')
    parser.add_argument('--runway', type=str, choices=['07', '25', '12', '30', '18', '36'])
    parser.add_argument('--wind-dir', type=int, help='Filter by wind direction')
    parser.add_argument('--wind-tolerance', type=int, default=30, help='Wind direction tolerance (default: 30)')
    parser.add_argument('--output', default='output/release_to_thermal.html')
    parser.add_argument('--db-path', default='data/flights.db')
    args = parser.parse_args()

    df = fetch_data(args.db_path, runway=args.runway, wind_dir=args.wind_dir, wind_tolerance=args.wind_tolerance)

    if len(df) == 0:
        print("No data found with given filters.")
        return 1

    create_map(df, args.output, runway=args.runway, wind_dir=args.wind_dir)
    return 0


if __name__ == "__main__":
    exit(main())
