#!/usr/bin/env python3
"""
Smart Thermal Prediction for Tomorrow's Flying

This tool analyzes historical thermal data under similar wind conditions to tomorrow's
forecast and creates an interactive map showing where you're most likely to find
thermals after winch launch.

Usage:
    python thermal_forecast.py
    python thermal_forecast.py --date 2026-03-28
    python thermal_forecast.py --wind-dir 180 --wind-speed 15
    python thermal_forecast.py --time-window morning
"""

import sqlite3
import argparse
import math
import sys
import os
from datetime import datetime, date, timedelta
from typing import Dict, List, Tuple, Optional
import folium
from folium.plugins import HeatMap
import pandas as pd

# Add parent directories to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'weather'))
from HilversumWeatherFetcher import HilversumWeatherFetcher


# Flying site configuration
EHHV_LAT = 52.1932
EHHV_LON = 5.1528
WINCH_RELEASE_ALTITUDE = 400  # meters
SEARCH_RADIUS_KM = 10


class ThermalPredictor:
    """Predict thermal locations based on weather forecast and historical data."""

    def __init__(self, db_path: str = "data/flights.db"):
        """Initialize predictor with database connection."""
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)

    def get_forecast_for_date(self, target_date: date) -> Dict:
        """
        Get weather forecast for target date.

        Args:
            target_date: Date to get forecast for

        Returns:
            Dictionary with wind_dir, wind_speed, temp for different times
        """
        print(f"Fetching weather forecast for {target_date}...")
        fetcher = HilversumWeatherFetcher()
        weather_data = fetcher.fetch_wind(target_date)

        if not weather_data:
            raise ValueError(f"No weather data available for {target_date}")

        # Group by time window
        morning = [w for w in weather_data if 9 <= int(w['time'][11:13]) < 12]
        midday = [w for w in weather_data if 12 <= int(w['time'][11:13]) < 15]
        afternoon = [w for w in weather_data if 15 <= int(w['time'][11:13]) < 18]

        def avg_wind(data_list):
            if not data_list:
                return None, None
            dirs = [d['wdir'] for d in data_list if d['wdir'] is not None]
            spds = [d['wspd'] for d in data_list if d['wspd'] is not None]
            return (sum(dirs) / len(dirs) if dirs else None,
                    sum(spds) / len(spds) if spds else None)

        morning_dir, morning_spd = avg_wind(morning)
        midday_dir, midday_spd = avg_wind(midday)
        afternoon_dir, afternoon_spd = avg_wind(afternoon)

        return {
            'date': target_date,
            'morning': {'wind_dir': morning_dir, 'wind_speed': morning_spd, 'hours': '09:00-12:00'},
            'midday': {'wind_dir': midday_dir, 'wind_speed': midday_spd, 'hours': '12:00-15:00'},
            'afternoon': {'wind_dir': afternoon_dir, 'wind_speed': afternoon_spd, 'hours': '15:00-18:00'},
            'all_data': weather_data
        }

    def find_similar_conditions(
        self,
        wind_dir: float,
        wind_speed: float,
        time_window: Optional[str] = None,
        dir_tolerance: float = 30,
        speed_tolerance: float = 5
    ) -> pd.DataFrame:
        """
        Find historical thermal data under similar wind conditions.

        Args:
            wind_dir: Target wind direction (degrees)
            wind_speed: Target wind speed (km/h)
            time_window: 'morning', 'midday', 'afternoon', or None for all
            dir_tolerance: Wind direction tolerance (degrees)
            speed_tolerance: Wind speed tolerance (km/h)

        Returns:
            DataFrame with thermal locations and statistics
        """
        print(f"\nSearching for thermals in similar conditions:")
        print(f"  Wind: {wind_dir:.0f}° at {wind_speed:.1f} km/h")
        print(f"  Tolerance: ±{dir_tolerance}° and ±{speed_tolerance} km/h")

        # Calculate wind direction range (handling 0/360 wraparound)
        dir_min = (wind_dir - dir_tolerance) % 360
        dir_max = (wind_dir + dir_tolerance) % 360

        # Build time filter
        time_filter = ""
        if time_window == 'morning':
            time_filter = "AND CAST(substr(time, 1, 2) AS INTEGER) BETWEEN 9 AND 11"
        elif time_window == 'midday':
            time_filter = "AND CAST(substr(time, 1, 2) AS INTEGER) BETWEEN 12 AND 14"
        elif time_window == 'afternoon':
            time_filter = "AND CAST(substr(time, 1, 2) AS INTEGER) BETWEEN 15 AND 17"

        # Handle wraparound for wind direction
        if dir_min > dir_max:  # Wraparound case (e.g., 350-10)
            wind_condition = f"(wind_dir >= {dir_min} OR wind_dir <= {dir_max})"
        else:
            wind_condition = f"wind_dir BETWEEN {dir_min} AND {dir_max}"

        query = f"""
        SELECT
            i.latitude,
            i.longitude,
            i.altitude,
            i.vertical_speed,
            i.wind_dir,
            i.wind_spd,
            i.time,
            i.flight_id
        FROM igc_data i
        JOIN flights f ON i.flight_id = f.id
        WHERE i.first_thermal = 1
          AND i.altitude < {WINCH_RELEASE_ALTITUDE + 200}
          AND i.altitude > {WINCH_RELEASE_ALTITUDE - 200}
          AND {wind_condition}
          AND i.wind_spd BETWEEN {wind_speed - speed_tolerance} AND {wind_speed + speed_tolerance}
          AND i.wind_dir IS NOT NULL
          AND i.wind_spd IS NOT NULL
          AND f.registratie NOT IN ('PH-GOZ', 'PH GOZ')
          {time_filter}
        """

        df = pd.read_sql_query(query, self.conn)
        print(f"  Found {len(df)} thermal points from historical data")

        return df

    def calculate_thermal_hotspots(
        self,
        df: pd.DataFrame,
        grid_size_m: int = 100
    ) -> List[Tuple[float, float, float]]:
        """
        Calculate thermal hotspot locations with intensity.

        Args:
            df: DataFrame with thermal locations
            grid_size_m: Grid cell size in meters

        Returns:
            List of (lat, lon, intensity) tuples
        """
        if df.empty:
            return []

        # Filter to search radius
        df = df.copy()
        df['distance'] = df.apply(
            lambda row: self._haversine(EHHV_LAT, EHHV_LON, row['latitude'], row['longitude']),
            axis=1
        )
        df = df[df['distance'] <= SEARCH_RADIUS_KM]

        # Create grid
        cell_size_lat = grid_size_m / 111000  # degrees
        cell_size_lon = grid_size_m / (111000 * math.cos(math.radians(EHHV_LAT)))

        lat_min, lat_max = df['latitude'].min(), df['latitude'].max()
        lon_min, lon_max = df['longitude'].min(), df['longitude'].max()

        df['grid_x'] = ((df['longitude'] - lon_min) // cell_size_lon).astype(int)
        df['grid_y'] = ((df['latitude'] - lat_min) // cell_size_lat).astype(int)

        # Count thermals per grid cell
        grid_counts = df.groupby(['grid_x', 'grid_y']).size().reset_index(name='count')

        # Convert back to coordinates with intensity
        hotspots = []
        max_count = grid_counts['count'].max()

        for _, row in grid_counts.iterrows():
            grid_x, grid_y, count = row['grid_x'], row['grid_y'], row['count']

            # Center of grid cell
            cell_lat = lat_min + (grid_y + 0.5) * cell_size_lat
            cell_lon = lon_min + (grid_x + 0.5) * cell_size_lon

            # Normalize intensity (0-1)
            intensity = count / max_count

            # Only include significant hotspots
            if count >= 3:  # At least 3 thermal points
                hotspots.append((cell_lat, cell_lon, intensity))

        return hotspots

    def create_map(
        self,
        forecast: Dict,
        time_window: str = 'midday',
        output_file: str = 'output/thermal_forecast.html'
    ):
        """
        Create interactive thermal prediction map.

        Args:
            forecast: Weather forecast dictionary
            time_window: 'morning', 'midday', or 'afternoon'
            output_file: Output HTML filename
        """
        print(f"\n{'='*60}")
        print(f"THERMAL FORECAST FOR {forecast['date']}")
        print(f"Time window: {time_window.upper()}")
        print(f"{'='*60}")

        # Get forecast for time window
        window_forecast = forecast[time_window]
        wind_dir = window_forecast['wind_dir']
        wind_speed = window_forecast['wind_speed']

        if wind_dir is None or wind_speed is None:
            print(f"ERROR: No wind forecast available for {time_window}")
            return

        print(f"\nForecasted conditions ({window_forecast['hours']}):")
        print(f"  Wind: {wind_dir:.0f}° at {wind_speed:.1f} km/h")

        # Find similar historical conditions
        df = self.find_similar_conditions(wind_dir, wind_speed, time_window)

        if df.empty:
            print("\nWARNING: No historical data found for these conditions!")
            print("Consider widening search criteria.")
            return

        # Calculate statistics
        unique_flights = df['flight_id'].nunique()
        avg_altitude = df['altitude'].mean()
        print(f"\nStatistics:")
        print(f"  Thermal points: {len(df)}")
        print(f"  From {unique_flights} different flights")
        print(f"  Average thermal altitude: {avg_altitude:.0f}m")

        # Calculate hotspots
        hotspots = self.calculate_thermal_hotspots(df)
        print(f"  Identified {len(hotspots)} thermal hotspots")

        # Create map
        m = folium.Map(location=[EHHV_LAT, EHHV_LON], zoom_start=12)

        # Add heatmap of thermal points
        heat_data = [[row['latitude'], row['longitude'], 1.0] for _, row in df.iterrows()]
        HeatMap(
            heat_data,
            radius=15,
            blur=20,
            min_opacity=0.3,
            gradient={0.4: 'blue', 0.6: 'cyan', 0.7: 'lime', 0.8: 'yellow', 1.0: 'red'}
        ).add_to(m)

        # Add hotspot markers
        for lat, lon, intensity in sorted(hotspots, key=lambda x: x[2], reverse=True)[:10]:
            distance = self._haversine(EHHV_LAT, EHHV_LON, lat, lon)
            bearing = self._calculate_bearing(EHHV_LAT, EHHV_LON, lat, lon)

            # Color based on intensity
            if intensity > 0.7:
                color = 'red'
                priority = 'HIGH'
            elif intensity > 0.4:
                color = 'orange'
                priority = 'MEDIUM'
            else:
                color = 'blue'
                priority = 'LOW'

            folium.CircleMarker(
                location=[lat, lon],
                radius=8 + intensity * 10,
                popup=f"""
                <b>Thermal Hotspot ({priority} probability)</b><br>
                Distance: {distance:.1f} km<br>
                Bearing: {bearing:.0f}°<br>
                Thermal frequency: {intensity:.0%}<br>
                <i>Based on {unique_flights} historical flights</i>
                """,
                color=color,
                fill=True,
                fillColor=color,
                fillOpacity=0.6,
                weight=2
            ).add_to(m)

        # Add airport marker
        folium.Marker(
            [EHHV_LAT, EHHV_LON],
            popup="<b>EHHV - Hilversum Airport</b><br>Winch launch location",
            tooltip="EHHV",
            icon=folium.Icon(color='green', icon='plane', prefix='fa')
        ).add_to(m)

        # Add wind direction arrow
        self._add_wind_arrow(m, EHHV_LAT, EHHV_LON, wind_dir, wind_speed)

        # Add legend
        legend_html = f"""
        <div style="position: fixed;
                    top: 10px; right: 10px;
                    background-color: white;
                    border: 2px solid grey;
                    border-radius: 5px;
                    padding: 10px;
                    font-size: 14px;
                    z-index: 1000;">
            <h4 style="margin-top: 0;">Thermal Forecast</h4>
            <b>Date:</b> {forecast['date']}<br>
            <b>Time:</b> {window_forecast['hours']}<br>
            <b>Wind:</b> {wind_dir:.0f}° @ {wind_speed:.1f} km/h<br>
            <hr>
            <b>Historical data:</b><br>
            • {len(df)} thermal points<br>
            • {unique_flights} flights<br>
            • {len(hotspots)} hotspots<br>
            <hr>
            <b>Markers:</b><br>
            <span style="color: red;">●</span> High probability<br>
            <span style="color: orange;">●</span> Medium probability<br>
            <span style="color: blue;">●</span> Low probability<br>
        </div>
        """
        m.get_root().html.add_child(folium.Element(legend_html))

        # Save map
        m.save(output_file)
        print(f"\n✓ Map saved to: {output_file}")

        # Print recommendations
        self._print_recommendations(hotspots, wind_dir)

    def _add_wind_arrow(self, m, lat, lon, wind_dir, wind_speed):
        """Add wind direction arrow to map."""
        # Calculate arrow endpoint (1 km in wind direction)
        distance_km = min(2.0, wind_speed / 10)  # Scale with wind speed
        bearing = wind_dir

        lat_rad = math.radians(lat)
        lon_rad = math.radians(lon)
        bearing_rad = math.radians(bearing)

        lat2 = math.asin(math.sin(lat_rad) * math.cos(distance_km / 6371) +
                         math.cos(lat_rad) * math.sin(distance_km / 6371) * math.cos(bearing_rad))
        lon2 = lon_rad + math.atan2(math.sin(bearing_rad) * math.sin(distance_km / 6371) * math.cos(lat_rad),
                                     math.cos(distance_km / 6371) - math.sin(lat_rad) * math.sin(lat2))

        lat2 = math.degrees(lat2)
        lon2 = math.degrees(lon2)

        folium.PolyLine(
            locations=[[lat, lon], [lat2, lon2]],
            color='black',
            weight=4,
            opacity=0.8,
            popup=f"Wind: {wind_dir:.0f}° @ {wind_speed:.1f} km/h"
        ).add_to(m)

        # Add arrowhead
        folium.RegularPolygonMarker(
            location=[lat2, lon2],
            fill_color='black',
            number_of_sides=3,
            radius=10,
            rotation=wind_dir,
            popup=f"Wind from {wind_dir:.0f}°"
        ).add_to(m)

    def _print_recommendations(self, hotspots: List[Tuple], wind_dir: float):
        """Print practical recommendations for pilot."""
        print(f"\n{'='*60}")
        print("RECOMMENDATIONS FOR TOMORROW")
        print(f"{'='*60}")

        if not hotspots:
            print("⚠ No strong thermal hotspots identified.")
            print("Consider flying conservatively and stay within gliding range.")
            return

        # Sort by intensity
        top_hotspots = sorted(hotspots, key=lambda x: x[2], reverse=True)[:3]

        print("\n🎯 TOP 3 THERMAL HOTSPOTS:")
        for i, (lat, lon, intensity) in enumerate(top_hotspots, 1):
            distance = self._haversine(EHHV_LAT, EHHV_LON, lat, lon)
            bearing = self._calculate_bearing(EHHV_LAT, EHHV_LON, lat, lon)

            print(f"\n  {i}. Hotspot at {lat:.4f}, {lon:.4f}")
            print(f"     • Bearing: {bearing:.0f}° from EHHV")
            print(f"     • Distance: {distance:.1f} km")
            print(f"     • Thermal probability: {intensity:.0%}")
            print(f"     • Strategy: Head {self._bearing_to_direction(bearing)} after release")

        print(f"\n💨 WIND STRATEGY:")
        print(f"  • Wind from {wind_dir:.0f}° ({self._bearing_to_direction(wind_dir)})")
        print(f"  • Thermals likely downwind of trigger points")
        print(f"  • Consider wind drift during climb")

        print(f"\n✈ FLIGHT PLANNING:")
        print(f"  • Release altitude: ~{WINCH_RELEASE_ALTITUDE}m")
        print(f"  • First thermal search: {top_hotspots[0][2]:.0%} chance in marked areas")
        print(f"  • Stay within gliding range until first thermal")
        print(f"  • Have a backup plan if first thermal fails")

    def _haversine(self, lat1, lon1, lat2, lon2):
        """Calculate distance between two points in km."""
        R = 6371
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    def _calculate_bearing(self, lat1, lon1, lat2, lon2):
        """Calculate bearing from point 1 to point 2."""
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
        dlon = lon2 - lon1
        x = math.sin(dlon) * math.cos(lat2)
        y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
        bearing = math.atan2(x, y)
        return (math.degrees(bearing) + 360) % 360

    def _bearing_to_direction(self, bearing):
        """Convert bearing to cardinal direction."""
        directions = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                     'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
        index = round(bearing / 22.5) % 16
        return directions[index]

    def close(self):
        """Close database connection."""
        self.conn.close()


def main():
    """Main execution."""
    parser = argparse.ArgumentParser(
        description='Generate thermal prediction map for tomorrow',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        '--date',
        type=str,
        help='Target date (YYYY-MM-DD), default: tomorrow'
    )

    parser.add_argument(
        '--time-window',
        type=str,
        choices=['morning', 'midday', 'afternoon'],
        default='midday',
        help='Time window for predictions (default: midday)'
    )

    parser.add_argument(
        '--wind-dir',
        type=float,
        help='Override wind direction (degrees)'
    )

    parser.add_argument(
        '--wind-speed',
        type=float,
        help='Override wind speed (km/h)'
    )

    parser.add_argument(
        '--output',
        type=str,
        default='thermal_forecast.html',
        help='Output HTML file (default: thermal_forecast.html)'
    )

    args = parser.parse_args()

    # Determine target date
    if args.date:
        target_date = datetime.strptime(args.date, '%Y-%m-%d').date()
    else:
        target_date = date.today() + timedelta(days=1)

    print(f"{'='*60}")
    print(f"THERMAL FORECAST GENERATOR")
    print(f"{'='*60}")
    print(f"Target date: {target_date}")
    print(f"Time window: {args.time_window}")

    # Initialize predictor
    predictor = ThermalPredictor()

    try:
        # Get forecast or use manual input
        if args.wind_dir and args.wind_speed:
            print("\nUsing manual wind input...")
            forecast = {
                'date': target_date,
                args.time_window: {
                    'wind_dir': args.wind_dir,
                    'wind_speed': args.wind_speed,
                    'hours': '09:00-18:00'
                }
            }
        else:
            forecast = predictor.get_forecast_for_date(target_date)

        # Create map
        predictor.create_map(forecast, args.time_window, args.output)

        print(f"\n✓ SUCCESS! Open {args.output} in your browser.")
        print(f"  Good luck with your flying tomorrow! 🪂")

    finally:
        predictor.close()


if __name__ == "__main__":
    main()
