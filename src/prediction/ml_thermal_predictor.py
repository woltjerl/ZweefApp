#!/usr/bin/env python3
"""
ML Thermal Prediction - Interactive Map Generator

Generates weather-conditional thermal probability maps using trained ML models.
Shows a spatial grid of thermal probability, recommended navigation direction,
and overall success probability.

Usage:
    python ml_thermal_predictor.py --date 2026-03-30
    python ml_thermal_predictor.py --date 2026-03-30 --runway 30
    python ml_thermal_predictor.py --date 2026-03-30 --time-window morning
"""

import argparse
import math
import os
import sys
from datetime import date, datetime, timedelta

import folium
import numpy as np

# Add paths for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'visualization'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'weather'))

from runway_constants import RUNWAY_LINES, AIRPORT_LAT, AIRPORT_LON
from open_meteo_forecast import get_forecast_for_window
from ml_thermal_model import ThermalMLModel, GRID_SIZE_M


def get_cardinal(bearing):
    """Convert bearing to cardinal direction."""
    dirs = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
            'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
    return dirs[round(bearing / 22.5) % 16]


def prob_to_color(prob):
    """Convert probability (0-1) to color hex string.

    Calibrated for typical thermal probabilities (30-60% range).
    """
    if prob >= 0.55:
        # Hot red (very high)
        return '#d73027'
    elif prob >= 0.50:
        # Orange-red (high)
        return '#fc8d59'
    elif prob >= 0.45:
        # Orange (elevated)
        return '#fee090'
    elif prob >= 0.40:
        # Yellow (moderate)
        return '#ffffbf'
    elif prob >= 0.35:
        # Light blue (baseline)
        return '#e0f3f8'
    else:
        # Blue (below baseline)
        return '#91bfdb'


def create_ml_prediction_map(
    model, wind_dir, wind_speed, temp, humidity, pressure,
    month, hour, runway, output_file, forecast_date=None
):
    """Create interactive HTML map with ML thermal predictions."""

    # ── Run predictions ──
    success_prob = model.predict_success(
        wind_dir, wind_speed, temp, humidity, pressure, month, hour, runway
    )
    nav_bearing, nav_distance = model.predict_navigation(
        wind_dir, wind_speed, temp, humidity, pressure, month, hour, runway
    )

    # Skip spatial grid - use wind-filtered heatmap instead for better results
    grid = []

    print(f"\n  Success probability: {success_prob:.0%}")
    print(f"  Recommended bearing: {nav_bearing:.0f}° ({get_cardinal(nav_bearing)})")
    print(f"  Recommended distance: {nav_distance:.0f}m")
    print(f"  Note: For spatial visualization, see wind-filtered heatmap")

    # ── Create map ──
    m = folium.Map(location=[AIRPORT_LAT, AIRPORT_LON], zoom_start=14)

    # Grid probability overlay
    pred_cell_m = 50  # prediction resolution
    cell_lat = pred_cell_m / 111000.0
    cell_lon = pred_cell_m / (111000.0 * math.cos(math.radians(AIRPORT_LAT)))
    half_lat = cell_lat / 2
    half_lon = cell_lon / 2

    grid_group = folium.FeatureGroup(name='ML Thermal Probability Grid')

    # Calculate probability threshold - only show top 30% of cells
    if grid:
        grid.sort(key=lambda c: c['probability'])
        probs = [c['probability'] for c in grid]
        p70 = np.percentile(probs, 70)
        threshold = max(0.35, p70)  # Show only cells above 35% OR top 30%
        print(f"  Showing cells with P(thermal) > {threshold:.1%} (top 30%)")
    else:
        threshold = 0.35

    for cell in grid:
        prob = cell['probability']
        if prob < threshold:
            continue

        color = prob_to_color(prob)
        opacity = 0.15 + 0.5 * min(1.0, prob)

        folium.Rectangle(
            bounds=[
                [cell['lat'] - half_lat, cell['lon'] - half_lon],
                [cell['lat'] + half_lat, cell['lon'] + half_lon]
            ],
            color=color,
            fill=True,
            fillColor=color,
            fillOpacity=opacity,
            weight=0.5,
            opacity=0.3,
            popup=(
                f"<b>Thermal Probability: {prob:.0%}</b><br>"
                f"Terrain: {cell['terrain_type']}<br>"
                f"Distance: {cell.get('dist_from_release', cell['dist_from_airport']):.1f} km from release"
            ),
        ).add_to(grid_group)
    grid_group.add_to(m)

    # Get release point for distance calculations
    release_lat, release_lon = AIRPORT_LAT, AIRPORT_LON
    if runway and runway in RUNWAY_LINES:
        release_lat, release_lon = RUNWAY_LINES[runway]['end']

    # Top hotspot markers
    top_cells = sorted(grid, key=lambda c: -c['probability'])[:8] if grid else []
    hotspot_group = folium.FeatureGroup(name='Top Thermal Hotspots')
    for i, cell in enumerate(top_cells):
        dist = cell.get('dist_from_release', cell['dist_from_airport'])
        bearing = math.degrees(math.atan2(
            (cell['lon'] - release_lon) * math.cos(math.radians(release_lat)),
            cell['lat'] - release_lat
        ))
        bearing = (bearing + 360) % 360

        folium.CircleMarker(
            location=[cell['lat'], cell['lon']],
            radius=10 - i * 0.8,
            color='darkred' if i < 3 else 'darkorange',
            fill=True,
            fillColor='red' if i < 3 else 'orange',
            fillOpacity=0.8,
            weight=2,
            popup=(
                f"<b>#{i+1} Thermal Hotspot</b><br>"
                f"<b>Probability: {cell['probability']:.0%}</b><br>"
                f"Terrain: {cell['terrain_type']}<br>"
                f"Bearing: {bearing:.0f}° ({get_cardinal(bearing)})<br>"
                f"Distance: {dist:.1f} km from release"
            ),
            tooltip=f"#{i+1}: {cell['probability']:.0%}"
        ).add_to(hotspot_group)
    hotspot_group.add_to(m)

    # Glide range circle (2.5km from release point)
    folium.Circle(
        location=[release_lat, release_lon],
        radius=2500,
        color='#666',
        weight=2,
        fill=False,
        opacity=0.4,
        dash_array='5, 10',
        popup="<b>Max glide range</b><br>2.5 km from release (400m altitude)",
        tooltip="Glide range limit"
    ).add_to(m)

    # Navigation arrow (from release point toward recommended bearing)
    arrow_group = folium.FeatureGroup(name='Recommended Direction')
    if runway and runway in RUNWAY_LINES:
        release_lat, release_lon = RUNWAY_LINES[runway]['end']  # Lier = release point
    else:
        release_lat, release_lon = AIRPORT_LAT, AIRPORT_LON

    # Arrow endpoint
    arrow_len_km = nav_distance / 1000.0
    end_lat = release_lat + (arrow_len_km / 111.0) * math.cos(math.radians(nav_bearing))
    end_lon = release_lon + (arrow_len_km / (111.0 * math.cos(math.radians(release_lat)))) * math.sin(math.radians(nav_bearing))

    folium.PolyLine(
        locations=[[release_lat, release_lon], [end_lat, end_lon]],
        color='#E91E63',
        weight=4,
        opacity=0.9,
        dash_array='10, 5',
        popup=(
            f"<b>ML Recommended Direction</b><br>"
            f"Bearing: {nav_bearing:.0f}° ({get_cardinal(nav_bearing)})<br>"
            f"Distance: {nav_distance:.0f}m<br>"
            f"Success probability: {success_prob:.0%}"
        ),
        tooltip=f"Fly {get_cardinal(nav_bearing)} ({nav_bearing:.0f}°), {nav_distance:.0f}m"
    ).add_to(arrow_group)

    # Arrowhead
    arrow_head_html = f'''<div style="
        font-size: 20px;
        transform: rotate({nav_bearing}deg);
        color: #E91E63;
        font-weight: bold;
        text-shadow: 1px 1px 2px white;
    ">▲</div>'''
    folium.Marker(
        location=[end_lat, end_lon],
        icon=folium.DivIcon(html=arrow_head_html, icon_size=(20, 20), icon_anchor=(10, 10)),
    ).add_to(arrow_group)
    arrow_group.add_to(m)

    # Runway lines
    runway_group = folium.FeatureGroup(name='Runways')
    for rwy_id, rwy_data in RUNWAY_LINES.items():
        is_active = runway and runway == rwy_id
        folium.PolyLine(
            locations=[rwy_data['start'], rwy_data['end']],
            color=rwy_data['color'],
            weight=4 if is_active else 2,
            opacity=1.0 if is_active else 0.4,
            dash_array=None if is_active else '5, 5',
            popup=f"<b>{rwy_data['name']}</b><br>Bearing: {rwy_data['bearing']:.1f}°",
            tooltip=rwy_data['name']
        ).add_to(runway_group)

        if is_active:
            folium.Marker(
                location=rwy_data['start'],
                popup=f"<b>EHHV</b><br>{rwy_data['name']}",
                tooltip="EHHV",
                icon=folium.Icon(color='green', icon='plane', prefix='fa')
            ).add_to(runway_group)

            lier_html = '<div style="font-size:14px; font-weight:bold; color:#333; text-shadow:1px 1px 2px white;">L</div>'
            folium.Marker(
                location=rwy_data['end'],
                popup=f"<b>Lier</b><br>{rwy_data['name']}",
                tooltip="Lier",
                icon=folium.DivIcon(html=lier_html, icon_size=(16, 16), icon_anchor=(8, 8))
            ).add_to(runway_group)
    runway_group.add_to(m)

    # Wind arrow at airfield center
    arrow_html = f'''<div style="
        font-size: 28px;
        transform: rotate({wind_dir}deg);
        transform-origin: center;
        color: #2166AC;
        text-shadow: 1px 1px 2px white;
        font-weight: bold;
    ">⬇</div>'''
    folium.Marker(
        location=[AIRPORT_LAT, AIRPORT_LON],
        icon=folium.DivIcon(html=arrow_html, icon_size=(32, 32), icon_anchor=(16, 16)),
        popup=f"<b>Wind</b><br>{wind_dir:.0f}° at {wind_speed:.1f} km/h",
        tooltip=f"Wind: {wind_dir:.0f}° / {wind_speed:.1f} km/h"
    ).add_to(m)

    # Info overlay (top-left)
    date_str = str(forecast_date) if forecast_date else 'N/A'

    # Model accuracy metrics
    metrics_html = ''
    if model.metrics:
        if 'success' in model.metrics:
            metrics_html += f"Success model AUC: {model.metrics['success']['auc']:.2f}<br>"
        if 'navigation' in model.metrics:
            metrics_html += f"Direction error: ±{model.metrics['navigation']['bearing_mae']:.0f}°<br>"

    info_html = f'''
    <div style="
        position: fixed; top: 10px; left: 60px; z-index: 1000;
        background: rgba(255,255,255,0.95); padding: 15px; border-radius: 8px;
        box-shadow: 0 2px 10px rgba(0,0,0,0.2); font-family: Arial; font-size: 13px;
        max-width: 320px;
    ">
        <b style="font-size:16px;">🤖 ML Thermal Prediction</b><br>
        <span style="color:#666;">Date: {date_str}</span><br><br>

        <b>Will I find a thermal?</b><br>
        <span style="font-size:24px; color:{'#2e7d32' if success_prob > 0.4 else '#d32f2f'};">
            {success_prob:.0%}
        </span>
        <span style="font-size:12px; color:#666;">
            probability
        </span><br><br>

        <b>Where should I fly?</b><br>
        <span style="font-size:16px; color:#E91E63;">
            {get_cardinal(nav_bearing)} ({nav_bearing:.0f}°)
        </span><br>
        <span style="color:#666;">Distance: {nav_distance:.0f}m from release</span><br><br>

        <b>Conditions:</b><br>
        Wind: {wind_dir:.0f}° at {wind_speed:.1f} km/h<br>
        {'Temp: ' + f'{temp:.1f}°C<br>' if temp else ''}
        Runway: {runway or 'any'}<br><br>

        <span style="color:#666; font-size:11px;">
        {metrics_html}
        Trained: {model.training_date[:10] if model.training_date else 'unknown'}<br>
        Models: 2,604 thermal flights, 3,222 landed back
        </span>
    </div>
    '''
    m.get_root().html.add_child(folium.Element(info_html))

    # Legend (bottom-right)
    legend_html = '''
    <div style="
        position: fixed; bottom: 30px; right: 30px; z-index: 1000;
        background: rgba(255,255,255,0.95); padding: 12px; border-radius: 8px;
        box-shadow: 0 2px 10px rgba(0,0,0,0.2); font-family: Arial; font-size: 12px;
    ">
        <b>Map Legend</b><br><br>
        <span style="color:#E91E63; font-size:16px;">▲</span> Recommended flight direction<br>
        <span style="color:#666;">- - -</span> Max glide range (2.5km)<br>
        <span style="color:#2166AC; font-size:16px;">⬇</span> Wind direction<br><br>
        <span style="color:#888; font-size:11px;">
        For spatial heatmap see<br>wind-filtered map
        </span>
    </div>
    '''
    m.get_root().html.add_child(folium.Element(legend_html))

    # Layer control
    folium.LayerControl(collapsed=False).add_to(m)

    m.save(output_file)
    print(f"\n  Map saved to: {output_file}")

    return {
        'success_probability': success_prob,
        'bearing': nav_bearing,
        'distance': nav_distance,
        'top_hotspots': top_cells[:5],
    }


def get_forecast(target_date, time_window):
    """Fetch weather forecast from Open-Meteo."""
    print(f"Fetching weather for {target_date} ({time_window})...")
    result = get_forecast_for_window(target_date, time_window)

    if not result:
        raise ValueError(f"No data for {time_window}")

    return result


def main():
    parser = argparse.ArgumentParser(description='ML thermal prediction map generator')
    tomorrow = date.today() + timedelta(days=1)

    parser.add_argument('--date', type=str, default=str(tomorrow), help='Target date (YYYY-MM-DD)')
    parser.add_argument('--time-window', choices=['morning', 'midday', 'afternoon'], default='midday')
    parser.add_argument('--runway', choices=['07', '12', '18', '25', '30', '36'])
    parser.add_argument('--output', default='output/thermal_prediction_ml.html')
    parser.add_argument('--model-dir', default='data/models')
    parser.add_argument('--db', default='data/flights.db')
    args = parser.parse_args()

    target_date = datetime.strptime(args.date, '%Y-%m-%d').date()

    print("=" * 60)
    print("ML THERMAL PREDICTION")
    print("=" * 60)
    print(f"Date: {target_date}")
    print(f"Time window: {args.time_window}")
    if args.runway:
        print(f"Runway: {args.runway}")

    # Load model
    print("\nLoading ML models...")
    model = ThermalMLModel.load(model_dir=args.model_dir, db_path=args.db)
    print(f"  Models trained: {model.training_date[:10] if model.training_date else 'unknown'}")
    print(f"  Grid cells: {len(model.terrain_grid) if model.terrain_grid else 'none (no terrain grid)'}")

    # Get forecast
    forecast = get_forecast(target_date, args.time_window)
    wind_dir = forecast['wind_dir']
    wind_speed = forecast['wind_speed']
    temp = forecast.get('temp')
    humidity = forecast.get('humidity')
    pressure = forecast.get('pressure')

    print(f"\nForecast: wind {wind_dir:.0f}° at {wind_speed:.1f} km/h"
          f"{f', temp {temp:.1f}°C' if temp else ''}")

    # Determine month/hour for prediction
    hour_map = {'morning': 10, 'midday': 13, 'afternoon': 16}
    hour = hour_map[args.time_window]

    # Run prediction
    print("\nGenerating predictions...")
    result = create_ml_prediction_map(
        model=model,
        wind_dir=wind_dir,
        wind_speed=wind_speed,
        temp=temp,
        humidity=humidity,
        pressure=pressure,
        month=target_date.month,
        hour=hour,
        runway=args.runway,
        output_file=args.output,
        forecast_date=target_date,
    )

    # Print summary
    print(f"\n{'='*60}")
    print("PREDICTION SUMMARY")
    print("=" * 60)
    print(f"Success probability: {result['success_probability']:.0%}")
    print(f"Best direction: {get_cardinal(result['bearing'])} ({result['bearing']:.0f}°)")
    print(f"Expected distance: {result['distance']:.0f}m")
    if result['top_hotspots']:
        print(f"\nTop hotspots:")
        for i, cell in enumerate(result['top_hotspots'], 1):
            dist = cell.get('dist_from_release', cell['dist_from_airport'])
            print(f"  #{i}: {cell['probability']:.0%} - {cell['terrain_type']}, "
                  f"{dist:.1f}km from release")
    print(f"\nOpen {args.output} in your browser.")

    return 0


if __name__ == '__main__':
    sys.exit(main())
