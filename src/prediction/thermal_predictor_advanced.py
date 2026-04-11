#!/usr/bin/env python3
"""
Advanced Terrain-Aware Thermal Prediction

Uses terrain features, weather conditions, and historical data to predict
where you'll find your first thermal after winch launch.

Scoring system considers:
- Wind direction/speed match (35% weight)
- Terrain context and downwind effects (30% weight)
- Temperature and atmospheric conditions (20% weight)
- Time of day (10% weight)
- Historical frequency (5% weight)

Usage:
    python thermal_predictor_advanced.py
    python thermal_predictor_advanced.py --date 2026-03-28
    python thermal_predictor_advanced.py --time-window midday
"""

import sqlite3
import argparse
import math
import sys
import os
from datetime import datetime, date, timedelta
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
import folium
from folium.plugins import HeatMap
import pandas as pd

# Add path for runway constants
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'visualization'))
from runway_constants import RUNWAY_LINES

# Add parent directories to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'weather'))
from open_meteo_forecast import fetch_open_meteo_hourly


# Flying site configuration
EHHV_LAT = 52.1932
EHHV_LON = 5.1528
WINCH_RELEASE_ALTITUDE = 400  # meters
SEARCH_RADIUS_KM = 10


@dataclass
class ThermalHotspot:
    """Represents a predicted thermal hotspot with confidence score."""
    lat: float
    lon: float
    confidence: float  # 0-100
    thermal_count: int
    avg_climb_rate: float
    distance_km: float
    bearing: float
    terrain_type: str
    reasoning: List[str]


class AdvancedThermalPredictor:
    """Terrain-aware thermal prediction with weighted scoring."""

    def __init__(self, db_path: str = "data/flights.db"):
        """Initialize predictor with database connection."""
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)

    def haversine_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance between two points in kilometers."""
        R = 6371
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)

        a = (math.sin(delta_lat / 2) ** 2 +
             math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

    def calculate_bearing(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate bearing from point 1 to point 2 in degrees."""
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lon = math.radians(lon2 - lon1)

        y = math.sin(delta_lon) * math.cos(lat2_rad)
        x = (math.cos(lat1_rad) * math.sin(lat2_rad) -
             math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(delta_lon))

        bearing = math.degrees(math.atan2(y, x))
        return (bearing + 360) % 360

    def get_cardinal_direction(self, bearing: float) -> str:
        """Convert bearing to cardinal direction."""
        directions = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                     'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
        index = round(bearing / 22.5) % 16
        return directions[index]

    def get_forecast_for_date(self, target_date: date) -> Dict:
        """Get weather forecast for target date using Open-Meteo."""
        print(f"Fetching weather forecast for {target_date}...\n")

        try:
            records = fetch_open_meteo_hourly(target_date)

            if not records:
                raise ValueError("No weather data returned")

            # Organize by time windows
            forecast = {
                'date': str(target_date),
                'morning': {'hours': '09:00-12:00', 'wind_dir': None, 'wind_speed': None, 'temp': None},
                'midday': {'hours': '12:00-15:00', 'wind_dir': None, 'wind_speed': None, 'temp': None},
                'afternoon': {'hours': '15:00-18:00', 'wind_dir': None, 'wind_speed': None, 'temp': None}
            }

            # Calculate averages for each window
            for record in records:
                hour = int(record['time'].split('T')[1].split(':')[0])

                window = None
                if 9 <= hour < 12:
                    window = 'morning'
                elif 12 <= hour < 15:
                    window = 'midday'
                elif 15 <= hour < 18:
                    window = 'afternoon'

                if window and record.get('wdir') is not None and record.get('wspd') is not None:
                    if forecast[window]['wind_dir'] is None:
                        forecast[window]['wind_dir'] = []
                        forecast[window]['wind_speed'] = []
                        forecast[window]['temp'] = []

                    forecast[window]['wind_dir'].append(record['wdir'])
                    forecast[window]['wind_speed'].append(record['wspd'])
                    if record.get('temp') is not None:
                        forecast[window]['temp'].append(record['temp'])

            # Average the values
            for window in ['morning', 'midday', 'afternoon']:
                if forecast[window]['wind_dir']:
                    forecast[window]['wind_dir'] = sum(forecast[window]['wind_dir']) / len(forecast[window]['wind_dir'])
                    forecast[window]['wind_speed'] = sum(forecast[window]['wind_speed']) / len(forecast[window]['wind_speed'])
                    if forecast[window]['temp']:
                        forecast[window]['temp'] = sum(forecast[window]['temp']) / len(forecast[window]['temp'])

            return forecast

        except Exception as e:
            print(f"ERROR: Could not fetch weather forecast: {e}")
            raise

    def calculate_wind_score(
        self, forecast_wind_dir: float, forecast_wind_speed: float,
        thermal_wind_dir: float, thermal_wind_speed: float
    ) -> Tuple[float, str]:
        """
        Calculate wind similarity score (0-1).

        Returns:
            (score, reasoning)
        """
        # Direction similarity (handle wraparound)
        dir_diff = abs(forecast_wind_dir - thermal_wind_dir)
        if dir_diff > 180:
            dir_diff = 360 - dir_diff

        # Speed similarity
        speed_diff = abs(forecast_wind_speed - thermal_wind_speed)

        # Score calculation
        dir_score = max(0, 1 - (dir_diff / 45.0))  # Perfect within 0°, zero at 45°
        speed_score = max(0, 1 - (speed_diff / 10.0))  # Perfect within 0 km/h, zero at 10 km/h

        # Combined (direction more important)
        wind_score = 0.7 * dir_score + 0.3 * speed_score

        reasoning = f"Wind match: {wind_score*100:.0f}% (dir ±{dir_diff:.0f}°, speed ±{speed_diff:.1f} km/h)"
        return (wind_score, reasoning)

    def calculate_terrain_score(
        self, terrain_type: str, bearing_to_forest: float, bearing_to_urban: float,
        forecast_wind_dir: float, time_window: str
    ) -> Tuple[float, str]:
        """
        Calculate terrain-based thermal probability score.

        Considers:
        - Thermal triggers (forest edges, urban edges)
        - Downwind effects (thermals form downwind of heat sources)
        - Time-of-day effects (fields vs urban)

        Returns:
            (score, reasoning)
        """
        score = 0.5  # Base score
        reasons = []

        # Forest edge bonus (good thermal triggers)
        if terrain_type == "forest_edge":
            score += 0.2
            reasons.append("Forest edge (thermal trigger)")

            # Downwind of forest bonus
            wind_to_forest_diff = abs(forecast_wind_dir - bearing_to_forest)
            if wind_to_forest_diff > 180:
                wind_to_forest_diff = 360 - wind_to_forest_diff

            # Thermal forms DOWNWIND (opposite direction) of forest
            # So if wind is FROM 270° (west), thermal is EAST (90°) of forest
            # Forest is at bearing 270° from thermal, wind is from 270°
            downwind_angle = abs(180 - wind_to_forest_diff)

            if downwind_angle < 45:  # Within 45° of downwind
                downwind_bonus = 0.3 * (1 - downwind_angle / 45.0)
                score += downwind_bonus
                reasons.append(f"Downwind of forest ({downwind_bonus*100:.0f}% bonus)")

        # Urban edge bonus
        elif terrain_type == "urban_edge":
            # Urban areas better in morning/midday (slower to heat)
            if time_window in ['morning', 'midday']:
                score += 0.15
                reasons.append("Urban edge (good in morning/midday)")
            else:
                score += 0.05

            # Downwind of urban
            wind_to_urban_diff = abs(forecast_wind_dir - bearing_to_urban)
            if wind_to_urban_diff > 180:
                wind_to_urban_diff = 360 - wind_to_urban_diff
            downwind_angle = abs(180 - wind_to_urban_diff)

            if downwind_angle < 45:
                downwind_bonus = 0.2 * (1 - downwind_angle / 45.0)
                score += downwind_bonus
                reasons.append(f"Downwind of urban ({downwind_bonus*100:.0f}% bonus)")

        # Open fields
        elif terrain_type == "field":
            # Fields better in afternoon (heat up quickly)
            if time_window == 'afternoon':
                score += 0.15
                reasons.append("Open field (good in afternoon)")
            elif time_window == 'midday':
                score += 0.1
                reasons.append("Open field")
            else:
                score += 0.05

        # Near water penalty (sink zone)
        elif terrain_type == "water":
            score -= 0.3
            reasons.append("Near water (sink zone)")

        # Forest interior (less reliable)
        elif terrain_type == "forest":
            score -= 0.1
            reasons.append("Forest interior (less reliable)")

        score = max(0, min(1, score))  # Clamp to 0-1
        reasoning = "Terrain: " + ", ".join(reasons) if reasons else "Terrain: neutral"
        return (score, reasoning)

    def calculate_temperature_score(
        self, forecast_temp: Optional[float], thermal_temp: Optional[float]
    ) -> Tuple[float, str]:
        """
        Calculate temperature similarity score.

        Returns:
            (score, reasoning)
        """
        if forecast_temp is None or thermal_temp is None:
            return (0.5, "Temperature: unknown")

        temp_diff = abs(forecast_temp - thermal_temp)
        score = max(0, 1 - (temp_diff / 10.0))  # Perfect match, zero at 10°C difference

        reasoning = f"Temperature match: {score*100:.0f}% (±{temp_diff:.1f}°C)"
        return (score, reasoning)

    def find_similar_thermals_with_scoring(
        self,
        forecast_wind_dir: float,
        forecast_wind_speed: float,
        forecast_temp: Optional[float],
        time_window: str,
        runway: Optional[str] = None
    ) -> pd.DataFrame:
        """
        Find similar thermals with weighted confidence scores.

        Returns:
            DataFrame with thermal locations and confidence scores
        """
        print(f"Searching for thermals in similar conditions:")
        print(f"  Wind: {forecast_wind_dir:.0f}° at {forecast_wind_speed:.1f} km/h")
        if forecast_temp:
            print(f"  Temperature: {forecast_temp:.1f}°C")
        print(f"  Time window: {time_window}")

        # Broader query - we'll score everything
        dir_tolerance = 45  # Wider search
        speed_tolerance = 8

        dir_min = (forecast_wind_dir - dir_tolerance) % 360
        dir_max = (forecast_wind_dir + dir_tolerance) % 360

        # Time window filter (midday = all day)
        time_filter = ""
        if time_window == 'morning':
            time_filter = "AND CAST(substr(time, 1, 2) AS INTEGER) BETWEEN 9 AND 11"
        elif time_window == 'afternoon':
            time_filter = "AND CAST(substr(time, 1, 2) AS INTEGER) BETWEEN 15 AND 17"

        # Handle wraparound
        if dir_min > dir_max:
            wind_condition = f"(wind_dir >= {dir_min} OR wind_dir <= {dir_max})"
        else:
            wind_condition = f"wind_dir BETWEEN {dir_min} AND {dir_max}"

        # Runway filter
        runway_filter = f"AND f.runway = '{runway}'" if runway else ""

        query = f"""
        SELECT
            i.latitude,
            i.longitude,
            i.altitude,
            i.vertical_speed,
            i.wind_dir,
            i.wind_spd,
            i.weather_temp,
            i.distance_to_forest,
            i.bearing_to_forest,
            i.distance_to_urban,
            i.bearing_to_urban,
            i.terrain_type,
            i.time,
            i.flight_id
        FROM igc_data i
        JOIN flights f ON i.flight_id = f.id
        WHERE i.first_thermal = 1
          AND i.altitude < 600
          AND i.altitude > 200
          AND {wind_condition}
          AND i.wind_spd BETWEEN {forecast_wind_speed - speed_tolerance} AND {forecast_wind_speed + speed_tolerance}
          AND i.wind_dir IS NOT NULL
          AND i.wind_spd IS NOT NULL
          AND i.terrain_type IS NOT NULL
          AND f.registratie NOT IN ('PH-GOZ', 'PH GOZ')
          AND f.start_methode != 'sleep'
          {runway_filter}
          {time_filter}
        """

        df = pd.read_sql_query(query, self.conn)
        print(f"  Found {len(df)} thermal points\n")

        if df.empty:
            return df

        # Calculate confidence scores for each point
        confidence_scores = []
        reasoning_list = []

        for _, row in df.iterrows():
            # Wind score (35% weight)
            wind_score, wind_reason = self.calculate_wind_score(
                forecast_wind_dir, forecast_wind_speed,
                row['wind_dir'], row['wind_spd']
            )

            # Terrain score (30% weight)
            terrain_score, terrain_reason = self.calculate_terrain_score(
                row['terrain_type'] or 'field',
                row['bearing_to_forest'] or 0,
                row['bearing_to_urban'] or 0,
                forecast_wind_dir,
                time_window
            )

            # Temperature score (20% weight)
            temp_score, temp_reason = self.calculate_temperature_score(
                forecast_temp, row['weather_temp']
            )

            # Time-of-day score (10% weight)
            # Already filtered by time_window, so give base score
            time_score = 0.8

            # Historical frequency bonus (5% weight)
            # Higher climb rate = better thermal
            climb_bonus = min(1.0, (row['vertical_speed'] or 1.0) / 3.0)

            # Weighted total
            confidence = (
                0.35 * wind_score +
                0.30 * terrain_score +
                0.20 * temp_score +
                0.10 * time_score +
                0.05 * climb_bonus
            )

            confidence_scores.append(confidence * 100)  # Convert to percentage
            reasoning_list.append([wind_reason, terrain_reason, temp_reason])

        df['confidence'] = confidence_scores
        df['reasoning'] = reasoning_list

        return df

    def calculate_thermal_hotspots(
        self, df: pd.DataFrame, forecast_wind_dir: float
    ) -> List[ThermalHotspot]:
        """
        Calculate thermal hotspots with confidence scores.

        Groups nearby points and calculates weighted confidence.
        """
        if df.empty:
            return []

        # Filter to search radius
        df = df.copy()
        df['distance'] = df.apply(
            lambda row: self.haversine_distance(EHHV_LAT, EHHV_LON, row['latitude'], row['longitude']),
            axis=1
        )
        df = df[df['distance'] <= SEARCH_RADIUS_KM]

        # Grid-based clustering (100m cells)
        cell_size_m = 100
        cell_size_lat = cell_size_m / 111000
        cell_size_lon = cell_size_m / (111000 * math.cos(math.radians(EHHV_LAT)))

        lat_min, lat_max = df['latitude'].min(), df['latitude'].max()
        lon_min, lon_max = df['longitude'].min(), df['longitude'].max()

        df['grid_x'] = ((df['longitude'] - lon_min) // cell_size_lon).astype(int)
        df['grid_y'] = ((df['latitude'] - lat_min) // cell_size_lat).astype(int)

        # Aggregate by grid cell
        hotspots = []

        for (grid_x, grid_y), group in df.groupby(['grid_x', 'grid_y']):
            # Cell center
            cell_lat = lat_min + (grid_y + 0.5) * cell_size_lat
            cell_lon = lon_min + (grid_x + 0.5) * cell_size_lon

            # Weighted confidence (higher confidence points contribute more)
            weights = group['confidence'].values
            avg_confidence = (weights ** 2).sum() / weights.sum()  # Quadratic weighting

            # Statistics
            thermal_count = len(group)
            avg_climb_rate = group['vertical_speed'].mean()
            distance_km = self.haversine_distance(EHHV_LAT, EHHV_LON, cell_lat, cell_lon)
            bearing = self.calculate_bearing(EHHV_LAT, EHHV_LON, cell_lat, cell_lon)

            # Most common terrain type
            terrain_type = group['terrain_type'].mode()[0] if not group['terrain_type'].empty else 'field'

            # Aggregate reasoning
            reasoning = group.iloc[0]['reasoning']  # Take first point's reasoning

            hotspot = ThermalHotspot(
                lat=cell_lat,
                lon=cell_lon,
                confidence=avg_confidence,
                thermal_count=thermal_count,
                avg_climb_rate=avg_climb_rate,
                distance_km=distance_km,
                bearing=bearing,
                terrain_type=terrain_type,
                reasoning=reasoning
            )

            # Only include significant hotspots
            if thermal_count >= 3 and avg_confidence >= 40:  # At least 3 points, 40% confidence
                hotspots.append(hotspot)

        # Sort by confidence
        hotspots.sort(key=lambda h: h.confidence, reverse=True)

        return hotspots

    def create_map(
        self,
        forecast: Dict,
        time_window: str,
        output_file: str = 'output/thermal_prediction_advanced.html',
        runway: Optional[str] = None
    ):
        """Create interactive thermal prediction map with confidence scores."""
        print(f"\n{'='*60}")
        print(f"ADVANCED THERMAL PREDICTION FOR {forecast['date']}")
        print(f"Time window: {time_window.upper()}")
        if runway:
            print(f"Runway filter: {runway}")
        print(f"{'='*60}")

        window_forecast = forecast[time_window]
        wind_dir = window_forecast['wind_dir']
        wind_speed = window_forecast['wind_speed']
        temp = window_forecast.get('temp')

        if wind_dir is None or wind_speed is None:
            print(f"ERROR: No wind forecast available for {time_window}")
            return

        print(f"\nForecasted conditions ({window_forecast['hours']}):")
        print(f"  Wind: {wind_dir:.0f}° ({self.get_cardinal_direction(wind_dir)}) at {wind_speed:.1f} km/h")
        if temp:
            print(f"  Temperature: {temp:.1f}°C")

        # Find similar thermals with scoring
        df = self.find_similar_thermals_with_scoring(wind_dir, wind_speed, temp, time_window, runway)

        if df.empty:
            print("\nWARNING: No historical data found for these conditions!")
            return

        # Calculate hotspots
        hotspots = self.calculate_thermal_hotspots(df, wind_dir)

        if not hotspots:
            print("\nWARNING: No significant hotspots found!")
            return

        print(f"Identified {len(hotspots)} high-confidence thermal hotspots")

        # Create map
        m = folium.Map(location=[EHHV_LAT, EHHV_LON], zoom_start=13)

        # Add heatmap (weighted by confidence)
        heat_data = [[row['latitude'], row['longitude'], row['confidence']/100.0] for _, row in df.iterrows()]
        HeatMap(
            heat_data,
            radius=15,
            blur=20,
            min_opacity=0.3,
            gradient={0.4: 'blue', 0.6: 'cyan', 0.7: 'lime', 0.8: 'yellow', 1.0: 'red'}
        ).add_to(m)

        # Add hotspot markers (top 10)
        for hotspot in hotspots[:10]:
            # Color based on confidence
            if hotspot.confidence >= 80:
                color = 'red'
                priority = 'VERY HIGH'
            elif hotspot.confidence >= 70:
                color = 'orange'
                priority = 'HIGH'
            elif hotspot.confidence >= 60:
                color = 'yellow'
                priority = 'GOOD'
            else:
                color = 'lightblue'
                priority = 'MODERATE'

            reasoning_html = "<br>".join([f"• {r}" for r in hotspot.reasoning])

            folium.CircleMarker(
                location=[hotspot.lat, hotspot.lon],
                radius=8 + (hotspot.confidence / 10),
                popup=f"""
                <b>Thermal Hotspot ({priority})</b><br>
                <b>Confidence: {hotspot.confidence:.0f}%</b><br>
                <hr>
                Distance: {hotspot.distance_km:.1f} km<br>
                Bearing: {hotspot.bearing:.0f}° ({self.get_cardinal_direction(hotspot.bearing)})<br>
                Thermal count: {hotspot.thermal_count}<br>
                Avg climb: {hotspot.avg_climb_rate:.1f} m/s<br>
                Terrain: {hotspot.terrain_type}<br>
                <hr>
                <b>Why this spot:</b><br>
                {reasoning_html}
                """,
                color=color,
                fill=True,
                fillColor=color,
                fillOpacity=0.7,
                weight=2
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
                opacity = 0.5
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

        # Add wind direction arrow
        # Arrow points in the direction the wind is going TO (meteorological: wind_dir is where it comes FROM)
        arrow_bearing = wind_dir  # direction wind comes from
        # Place arrow near the airport, offset slightly
        from runway_constants import AIRPORT_LAT, AIRPORT_LON
        arrow_lat = AIRPORT_LAT
        arrow_lon = AIRPORT_LON
        # CSS rotation: 0° = up (north), clockwise
        arrow_html = f'''<div style="
            font-size: 28px;
            transform: rotate({arrow_bearing}deg);
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

        # Save map
        m.save(output_file)
        print(f"\n✓ Map saved to: {output_file}")

        # Print top recommendations
        self.print_recommendations(hotspots[:5], wind_dir, wind_speed)

    def print_recommendations(
        self, hotspots: List[ThermalHotspot], wind_dir: float, wind_speed: float
    ):
        """Print top thermal recommendations."""
        print(f"\n{'='*60}")
        print("TOP THERMAL RECOMMENDATIONS")
        print("="*60)

        for i, hotspot in enumerate(hotspots, 1):
            print(f"\n🎯 HOTSPOT #{i} - {hotspot.confidence:.0f}% CONFIDENCE")
            print(f"   Location: {hotspot.lat:.4f}, {hotspot.lon:.4f}")
            print(f"   Bearing: {hotspot.bearing:.0f}° ({self.get_cardinal_direction(hotspot.bearing)}) from EHHV")
            print(f"   Distance: {hotspot.distance_km:.1f} km")
            print(f"   Expected climb: {hotspot.avg_climb_rate:.1f} m/s")
            print(f"   Terrain: {hotspot.terrain_type}")
            print(f"   Based on {hotspot.thermal_count} historical thermals")
            print(f"\n   Why this spot:")
            for reason in hotspot.reasoning:
                print(f"     • {reason}")

        print(f"\n{'='*60}")
        print("FLIGHT STRATEGY")
        print("="*60)
        print(f"💨 Wind: {wind_dir:.0f}° ({self.get_cardinal_direction(wind_dir)}) at {wind_speed:.1f} km/h")
        print(f"\n📍 After winch release (~400m):")
        print(f"   1. Turn toward {self.get_cardinal_direction(hotspots[0].bearing)} ({hotspots[0].bearing:.0f}°)")
        print(f"   2. Fly {hotspots[0].distance_km:.1f} km")
        print(f"   3. Search {hotspots[0].terrain_type} area")
        print(f"\n⚠️  Backup plans:")
        for i, hotspot in enumerate(hotspots[1:3], 2):
            print(f"   {i}. {self.get_cardinal_direction(hotspot.bearing)} ({hotspot.bearing:.0f}°), {hotspot.distance_km:.1f} km")
        print(f"\n✈️  Stay within gliding range until first thermal!")
        print("="*60 + "\n")

    def close(self):
        """Close database connection."""
        self.conn.close()


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Advanced terrain-aware thermal prediction'
    )
    tomorrow = date.today() + timedelta(days=1)

    parser.add_argument('--date', type=str, default=str(tomorrow),
                       help='Target date (YYYY-MM-DD), default: tomorrow')
    parser.add_argument('--time-window', choices=['morning', 'midday', 'afternoon'],
                       default='midday', help='Time window for predictions')
    parser.add_argument('--runway', type=str, choices=['07', '25', '12', '30', '18', '36'],
                       help='Filter by runway (07, 25, 12, 30, 18, 36)')
    parser.add_argument('--output', type=str, default='output/thermal_prediction_advanced.html',
                       help='Output HTML file')

    return parser.parse_args()


def main():
    """Main execution."""
    args = parse_arguments()

    print("="*60)
    print("ADVANCED TERRAIN-AWARE THERMAL PREDICTION")
    print("="*60)
    print(f"Target date: {args.date}")
    print(f"Time window: {args.time_window}")
    if args.runway:
        print(f"Runway filter: {args.runway}")
    print("="*60 + "\n")

    # Parse date
    try:
        target_date = datetime.strptime(args.date, '%Y-%m-%d').date()
    except ValueError:
        print("ERROR: Invalid date format. Use YYYY-MM-DD")
        return 1

    # Create predictor
    predictor = AdvancedThermalPredictor()

    try:
        # Get forecast
        forecast = predictor.get_forecast_for_date(target_date)

        # Create prediction map
        predictor.create_map(forecast, args.time_window, args.output, args.runway)

        print(f"\n✓ SUCCESS! Open {args.output} in your browser.")
        print("  Good luck with your flying! 🪂\n")

    except Exception as e:
        print(f"\nERROR: {e}")
        return 1
    finally:
        predictor.close()

    return 0


if __name__ == "__main__":
    exit(main())
