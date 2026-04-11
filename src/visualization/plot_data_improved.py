#!/usr/bin/env python3
"""
Improved Flight Data Visualization

Creates an interactive heatmap showing where lift was found, excluding:
- Winch launch altitude (< 200m)
- Aerotow plane (PH-GOZ)
- Aerotow flights (showing tow climb, not thermals)
- Optionally filter by wind conditions, time of day, altitude range

Features:
- Multiple markers (airport, winch release area, common thermal locations)
- Legend with statistics
- Configurable parameters via CLI
- Better filtering

Usage:
    python plot_data_improved.py
    python plot_data_improved.py --min-alt 200 --max-alt 600
    python plot_data_improved.py --wind-dir 300 --wind-tolerance 20
"""

import sqlite3
import math
import sys
import os
import argparse
from datetime import datetime
from typing import Optional, Tuple
import pandas as pd
import folium
from folium.plugins import HeatMap

# Add path for runway constants
sys.path.insert(0, os.path.dirname(__file__))
from runway_constants import RUNWAY_LINES, AIRPORT_LAT as RUNWAY_AIRPORT_LAT, AIRPORT_LON as RUNWAY_AIRPORT_LON


# Flying site configuration
EHHV_LAT = 52.1932
EHHV_LON = 5.1528
EHHV_ELEVATION = 1  # meters MSL


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points in kilometers."""
    R = 6371  # Earth radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate bearing from point 1 to point 2 in degrees."""
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lon = math.radians(lon2 - lon1)

    y = math.sin(delta_lon) * math.cos(lat2_rad)
    x = (math.cos(lat1_rad) * math.sin(lat2_rad) -
         math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(delta_lon))

    bearing = math.degrees(math.atan2(y, x))
    return (bearing + 360) % 360


def get_cardinal_direction(bearing: float) -> str:
    """Convert bearing to cardinal direction."""
    directions = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
    index = round(bearing / 22.5) % 16
    return directions[index]


def fetch_thermal_data(
    db_path: str = "data/flights.db",
    min_alt: int = 200,
    max_alt: int = 600,
    min_vs: float = 0.5,
    radius_km: float = 10,
    wind_dir: Optional[int] = None,
    wind_tolerance: int = 30,
    wind_speed_min: Optional[int] = None,
    wind_speed_max: Optional[int] = None,
    time_window: Optional[str] = None,
    runway: Optional[str] = None,
    use_first_thermal: bool = True
) -> Tuple[pd.DataFrame, dict]:
    """
    Fetch thermal data from database with filters.

    Returns:
        DataFrame with thermal data and statistics dict
    """
    conn = sqlite3.connect(db_path)

    # Build query
    filters = [
        f"i.altitude >= {min_alt}",
        f"i.altitude <= {max_alt}",
        f"i.vertical_speed >= {min_vs}",
        "f.registratie NOT IN ('PH-GOZ', 'PH GOZ')",  # Exclude aerotow plane
        "f.start_methode != 'sleep'"  # Exclude aerotow flights
    ]

    # Runway filter
    if runway:
        filters.append(f"f.runway = '{runway}'")

    # Use first_thermal flag if requested
    if use_first_thermal:
        filters.append("i.first_thermal = 1")

    # Wind filters
    if wind_dir is not None:
        dir_min = (wind_dir - wind_tolerance) % 360
        dir_max = (wind_dir + wind_tolerance) % 360

        if dir_min > dir_max:  # Wraparound case
            filters.append(f"(i.wind_dir >= {dir_min} OR i.wind_dir <= {dir_max})")
        else:
            filters.append(f"i.wind_dir BETWEEN {dir_min} AND {dir_max}")
        filters.append("i.wind_dir IS NOT NULL")

    if wind_speed_min is not None and wind_speed_max is not None:
        filters.append(f"i.wind_spd BETWEEN {wind_speed_min} AND {wind_speed_max}")
        filters.append("i.wind_spd IS NOT NULL")

    # Time window filter (midday = all day)
    if time_window == 'morning':
        filters.append("CAST(substr(i.time, 1, 2) AS INTEGER) BETWEEN 9 AND 11")
    elif time_window == 'afternoon':
        filters.append("CAST(substr(i.time, 1, 2) AS INTEGER) BETWEEN 15 AND 17")

    where_clause = " AND ".join(filters)

    query = f"""
    SELECT
        i.latitude,
        i.longitude,
        i.altitude,
        i.vertical_speed,
        i.wind_dir,
        i.wind_spd,
        i.time,
        i.flight_id,
        f.registratie,
        f.callsign
    FROM igc_data i
    JOIN flights f ON i.flight_id = f.id
    WHERE {where_clause}
    """

    df = pd.read_sql_query(query, conn)

    # Filter by radius
    df['distance'] = df.apply(
        lambda row: haversine_distance(EHHV_LAT, EHHV_LON, row['latitude'], row['longitude']),
        axis=1
    )
    df = df[df['distance'] <= radius_km].copy()

    # Calculate bearing
    df['bearing'] = df.apply(
        lambda row: calculate_bearing(EHHV_LAT, EHHV_LON, row['latitude'], row['longitude']),
        axis=1
    )

    # Statistics
    stats = {
        'total_points': len(df),
        'unique_flights': df['flight_id'].nunique(),
        'avg_altitude': df['altitude'].mean(),
        'avg_vertical_speed': df['vertical_speed'].mean(),
        'avg_distance': df['distance'].mean()
    }

    if 'wind_dir' in df.columns and df['wind_dir'].notna().any():
        stats['avg_wind_dir'] = df['wind_dir'].mean()
        stats['avg_wind_speed'] = df['wind_spd'].mean()

    conn.close()

    return df, stats


def create_improved_heatmap(
    df: pd.DataFrame,
    stats: dict,
    output_file: str = "output/heatmap_improved.html",
    title: str = "Thermal Activity Heatmap",
    runway: Optional[str] = None
):
    """
    Create enhanced heatmap with markers and statistics.
    """
    # Create map
    m = folium.Map(location=[EHHV_LAT, EHHV_LON], zoom_start=13)

    # Add heatmap
    heat_data = df[['latitude', 'longitude', 'vertical_speed']].values.tolist()
    HeatMap(
        heat_data,
        radius=8,
        blur=10,
        min_opacity=0.3,
        max_opacity=0.8,
        gradient={0.4: 'blue', 0.6: 'cyan', 0.7: 'lime', 0.8: 'yellow', 1.0: 'red'}
    ).add_to(m)

    # Search radius circle
    folium.Circle(
        location=[EHHV_LAT, EHHV_LON],
        radius=10000,  # 10km
        popup="10km search radius",
        color='blue',
        fill=False,
        weight=1,
        dashArray='5, 5'
    ).add_to(m)

    # Add runway lines (winch launch directions)
    airport_marker_placed = False
    for runway_id, runway_data in RUNWAY_LINES.items():
        is_active = runway and runway == runway_id
        if is_active:
            weight = 4
            opacity = 1.0
            dash_array = None
        else:
            weight = 2
            opacity = 0.6
            dash_array = '5, 5'

        folium.PolyLine(
            locations=[runway_data['start'], runway_data['end']],
            color=runway_data['color'],
            weight=weight,
            opacity=opacity,
            dash_array=dash_array,
            popup=f"<b>{runway_data['name']}</b><br>Bearing: {runway_data['bearing']:.1f}°<br>Winch launch direction",
            tooltip=runway_data['name']
        ).add_to(m)

        # Add airport marker at start and lier marker at end for active runway
        if is_active:
            folium.Marker(
                location=runway_data['start'],
                popup=f"<b>Hilversum Airport (EHHV)</b><br>{runway_data['name']}<br>Winch Launch",
                tooltip="EHHV",
                icon=folium.Icon(color='green', icon='plane', prefix='fa')
            ).add_to(m)
            airport_marker_placed = True

            lier_html = '<div style="font-size:14px; font-weight:bold; color:#333; text-shadow:1px 1px 2px white;">L</div>'
            folium.Marker(
                location=runway_data['end'],
                popup=f"<b>Lier</b><br>{runway_data['name']}",
                tooltip="Lier",
                icon=folium.DivIcon(html=lier_html, icon_size=(16, 16), icon_anchor=(8, 8))
            ).add_to(m)

    # Fallback airport marker if no active runway
    if not airport_marker_placed:
        folium.Marker(
            location=[EHHV_LAT, EHHV_LON],
            popup="<b>Hilversum Airport (EHHV)</b>",
            tooltip="EHHV",
            icon=folium.Icon(color='green', icon='plane', prefix='fa')
        ).add_to(m)

    # Add wind direction arrow if wind data available
    if 'avg_wind_dir' in stats:
        wind_dir = stats['avg_wind_dir']
        wind_speed = stats['avg_wind_speed']
        arrow_lat = RUNWAY_AIRPORT_LAT
        arrow_lon = RUNWAY_AIRPORT_LON
        arrow_html = f'''<div style="
            font-size: 28px;
            transform: rotate({wind_dir}deg);
            transform-origin: center;
            color: #2166AC;
            text-shadow: 1px 1px 2px white;
            font-weight: bold;
        ">⬇</div>'''
        folium.Marker(
            location=[arrow_lat, arrow_lon],
            icon=folium.DivIcon(html=arrow_html, icon_size=(32, 32), icon_anchor=(16, 16)),
            popup=f"<b>Wind</b><br>{wind_dir:.0f}° at {wind_speed:.1f} km/h",
            tooltip=f"Wind: {wind_dir:.0f}° / {wind_speed:.1f} km/h"
        ).add_to(m)

    # Find hotspot centers (simple grid-based)
    # Group by 100m cells and find top 5
    cell_size = 100  # meters
    cell_size_lat = cell_size / 111000
    cell_size_lon = cell_size / (111000 * math.cos(math.radians(EHHV_LAT)))

    df['grid_lat'] = (df['latitude'] / cell_size_lat).astype(int)
    df['grid_lon'] = (df['longitude'] / cell_size_lon).astype(int)

    hotspots = df.groupby(['grid_lat', 'grid_lon']).agg({
        'latitude': 'mean',
        'longitude': 'mean',
        'vertical_speed': 'mean',
        'flight_id': 'count'
    }).reset_index()
    hotspots.columns = ['grid_lat', 'grid_lon', 'lat', 'lon', 'avg_vs', 'count']
    hotspots = hotspots.sort_values('count', ascending=False).head(5)

    # Add hotspot markers
    for i, row in hotspots.iterrows():
        dist = haversine_distance(EHHV_LAT, EHHV_LON, row['lat'], row['lon'])
        bearing = calculate_bearing(EHHV_LAT, EHHV_LON, row['lat'], row['lon'])

        folium.CircleMarker(
            location=[row['lat'], row['lon']],
            radius=8,
            popup=f"""
            <b>Hotspot #{i+1}</b><br>
            Thermal count: {int(row['count'])}<br>
            Avg climb: {row['avg_vs']:.1f} m/s<br>
            Distance: {dist:.1f} km<br>
            Bearing: {bearing:.0f}° ({get_cardinal_direction(bearing)})
            """,
            tooltip=f"Hotspot #{i+1}",
            color='red',
            fill=True,
            fillColor='red',
            fillOpacity=0.6
        ).add_to(m)

    # Add statistics legend
    legend_html = f"""
    <div style="position: fixed;
                top: 10px; right: 10px; width: 280px;
                background-color: white;
                border: 2px solid grey;
                z-index: 9999;
                font-size: 14px;
                padding: 10px;
                border-radius: 5px;
                box-shadow: 2px 2px 6px rgba(0,0,0,0.3);">
    <h4 style="margin-top: 0;">{title}</h4>
    <b>Statistics:</b><br>
    • Data points: {stats['total_points']:,}<br>
    • Unique flights: {stats['unique_flights']:,}<br>
    • Avg altitude: {stats['avg_altitude']:.0f}m<br>
    • Avg climb: {stats['avg_vertical_speed']:.1f} m/s<br>
    • Avg distance: {stats['avg_distance']:.1f} km<br>
    """

    if 'avg_wind_dir' in stats:
        legend_html += f"""
        • Avg wind: {stats['avg_wind_dir']:.0f}° at {stats['avg_wind_speed']:.1f} km/h<br>
        """

    legend_html += """
    <br><b>Legend:</b><br>
    <span style="color: green;">✈</span> Airport (EHHV)<br>
    <span style="color: orange;">●</span> Winch release zone<br>
    <span style="color: red;">●</span> Top 5 hotspots<br>
    <span style="color: red;">█</span> High thermal activity<br>
    <span style="color: blue;">█</span> Low thermal activity
    </div>
    """

    m.get_root().html.add_child(folium.Element(legend_html))

    # Save map
    m.save(output_file)
    print(f"\n✓ Enhanced heatmap saved to: {output_file}")
    print(f"\nStatistics:")
    print(f"  Data points: {stats['total_points']:,}")
    print(f"  Unique flights: {stats['unique_flights']:,}")
    print(f"  Avg altitude: {stats['avg_altitude']:.0f}m")
    print(f"  Avg climb rate: {stats['avg_vertical_speed']:.1f} m/s")


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Create improved thermal activity heatmap'
    )

    parser.add_argument('--min-alt', type=int, default=200,
                       help='Minimum altitude (m) - default: 200 (excludes winch launch)')
    parser.add_argument('--max-alt', type=int, default=600,
                       help='Maximum altitude (m) - default: 600')
    parser.add_argument('--min-vs', type=float, default=0.5,
                       help='Minimum vertical speed (m/s) - default: 0.5')
    parser.add_argument('--radius', type=float, default=10,
                       help='Search radius (km) - default: 10')
    parser.add_argument('--wind-dir', type=int,
                       help='Filter by wind direction (degrees)')
    parser.add_argument('--wind-tolerance', type=int, default=30,
                       help='Wind direction tolerance (degrees) - default: 30')
    parser.add_argument('--wind-speed-min', type=int,
                       help='Minimum wind speed (km/h)')
    parser.add_argument('--wind-speed-max', type=int,
                       help='Maximum wind speed (km/h)')
    parser.add_argument('--time-window', choices=['morning', 'midday', 'afternoon'],
                       help='Filter by time of day')
    parser.add_argument('--runway', type=str, choices=['07', '25', '12', '30', '18', '36'],
                       help='Filter by runway (07, 25, 12, 30, 18, 36)')
    parser.add_argument('--no-first-thermal', action='store_true',
                       help='Include all thermal data, not just first thermals')
    parser.add_argument('--output', default='output/heatmap_improved.html',
                       help='Output HTML file')
    parser.add_argument('--db-path', default='data/flights.db',
                       help='Path to database')

    return parser.parse_args()


def main():
    """Main execution."""
    args = parse_arguments()

    print("="*60)
    print("THERMAL ACTIVITY HEATMAP")
    print("="*60)
    print(f"\nFilters:")
    print(f"  Altitude: {args.min_alt}-{args.max_alt}m")
    print(f"  Min vertical speed: {args.min_vs} m/s")
    print(f"  Radius: {args.radius} km")
    print(f"  Exclude aerotow plane & flights: Yes")
    print(f"  Use first_thermal flag: {not args.no_first_thermal}")

    if args.wind_dir:
        print(f"  Wind direction: {args.wind_dir}° ±{args.wind_tolerance}°")
    if args.wind_speed_min and args.wind_speed_max:
        print(f"  Wind speed: {args.wind_speed_min}-{args.wind_speed_max} km/h")
    if args.time_window:
        print(f"  Time window: {args.time_window}")
    if args.runway:
        print(f"  Runway: {args.runway}")

    print("\nFetching data...")

    # Fetch data
    df, stats = fetch_thermal_data(
        db_path=args.db_path,
        min_alt=args.min_alt,
        max_alt=args.max_alt,
        min_vs=args.min_vs,
        radius_km=args.radius,
        wind_dir=args.wind_dir,
        wind_tolerance=args.wind_tolerance,
        wind_speed_min=args.wind_speed_min,
        wind_speed_max=args.wind_speed_max,
        time_window=args.time_window,
        runway=args.runway,
        use_first_thermal=not args.no_first_thermal
    )

    if df.empty:
        print("\n⚠ No data found matching the filters!")
        return 1

    # Create title
    title_parts = ["Thermal Activity"]
    if args.time_window:
        title_parts.append(f"({args.time_window.capitalize()})")
    if args.wind_dir:
        title_parts.append(f"Wind: {args.wind_dir}°")

    title = " ".join(title_parts)

    # Create map
    create_improved_heatmap(df, stats, args.output, title, runway=args.runway)

    print(f"\n✓ Done! Open {args.output} in your browser.")

    return 0


if __name__ == "__main__":
    exit(main())
