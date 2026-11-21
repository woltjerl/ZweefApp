#!/usr/bin/env python3
"""
Daily First-Thermal Forecast (docs/FIRST_THERMAL_PLAN.md, section 16)

For a date and time window: chance to connect after a winch launch (layer A),
reach from today's release, per-cell connect rates for today's conditions
(layer B), direction history and ace routes (layer C), and — only when the
evaluation accepted them — recommended search lines. Writes
output/first_thermal_<date>_<window>.html and logs the forecast to
data/forecast_log.jsonl for the prospective check.

Usage:
    python scripts/daily_forecast.py
    python scripts/daily_forecast.py morning --runway 25 --glider two_seat
    python scripts/daily_forecast.py --date 2026-03-30 afternoon
    python scripts/daily_forecast.py --all-runways
    python scripts/daily_forecast.py --legacy        # the old heatmaps
"""

import sys
import os
import subprocess
import argparse
from datetime import date, timedelta

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src', 'weather'))
from open_meteo_forecast import get_forecast_for_window


def determine_runway_from_wind(wind_dir: int) -> str:
    """
    Determine expected runway from wind direction.

    Hilversum runways (based on actual winch positions):
    - 07/25: ~070°/~250°
    - 12/30: ~120°/~300° (winch position differs from runway heading)
    - 18/36: ~180°/~000°

    Returns runway in use (takeoff into the wind).
    """
    # Normalize to 0-360
    wind_dir = wind_dir % 360

    # Determine which runway - takeoff into the wind
    # Ranges match actual observed winch headings
    if 20 <= wind_dir < 95:
        return '07'  # East wind -> takeoff to east
    elif 95 <= wind_dir < 155:
        return '12'  # Southeast wind -> takeoff to southeast
    elif 155 <= wind_dir < 215:
        return '18'  # South wind -> takeoff to south
    elif 215 <= wind_dir < 275:
        return '25'  # West wind -> takeoff to west
    elif 275 <= wind_dir < 335:
        return '30'  # Northwest wind -> takeoff to northwest
    else:  # 335-20
        return '36'  # North wind -> takeoff to north


def get_forecast(target_date: date, time_window: str = 'midday'):
    """
    Get weather forecast for target date and time window using Open-Meteo API.

    Returns:
        dict with wind_dir, wind_speed, temp
    """
    print(f"Fetching weather forecast for {target_date} ({time_window})...")

    try:
        result = get_forecast_for_window(target_date, time_window)
        if not result:
            print(f"WARNING: No forecast data for {time_window} time window on {target_date}")
            return None

        return {
            'wind_dir': int(result['wind_dir']),
            'wind_speed': int(result['wind_speed']),
            'temp': result['temp'],
        }

    except Exception as e:
        print(f"ERROR fetching forecast: {e}")
        return None


def run_ml_prediction(target_date: date, time_window: str, forecast: dict, runway: str = None, output_suffix: str = ""):
    """Run ML-based thermal prediction."""
    print("\n" + "="*60)
    print("GENERATING ML THERMAL PREDICTION")
    print("="*60 + "\n")

    output_file = f"output/thermal_prediction_ml_{time_window}{output_suffix}.html"

    cmd = [
        '.venv/bin/python',
        'src/prediction/ml_thermal_predictor.py',
        '--date', str(target_date),
        '--time-window', time_window,
        '--output', output_file
    ]

    if runway:
        cmd.extend(['--runway', runway])

    result = subprocess.run(cmd, capture_output=False)

    if result.returncode == 0:
        print(f"\n✓ ML prediction saved to: {output_file}")
        return output_file
    else:
        print(f"\n✗ ML prediction failed with code {result.returncode}")
        return None


def run_advanced_prediction(target_date: date, time_window: str, runway: str = None, output_suffix: str = ""):
    """Run terrain-aware advanced prediction."""
    print("\n" + "="*60)
    print("GENERATING ADVANCED TERRAIN-AWARE PREDICTION")
    print("="*60 + "\n")

    output_file = f"output/thermal_prediction_advanced_{time_window}{output_suffix}.html"

    cmd = [
        '.venv/bin/python',
        'src/prediction/thermal_predictor_advanced.py',
        '--date', str(target_date),
        '--time-window', time_window,
        '--output', output_file
    ]

    if runway:
        cmd.extend(['--runway', runway])

    result = subprocess.run(cmd, capture_output=False)

    if result.returncode == 0:
        print(f"\n✓ Advanced prediction saved to: {output_file}")
        return output_file
    else:
        print(f"\n✗ Advanced prediction failed with code {result.returncode}")
        return None


def run_historical_heatmap(time_window: str, output_suffix: str = ""):
    """Run historical heatmap (all data, no wind filter)."""
    print("\n" + "="*60)
    print("GENERATING HISTORICAL HEATMAP (ALL CONDITIONS)")
    print("="*60 + "\n")

    output_file = f"output/heatmap_all_{time_window}{output_suffix}.html"

    cmd = [
        '.venv/bin/python',
        'src/visualization/plot_data_improved.py',
        '--time-window', time_window,
        '--min-alt', '200',
        '--max-alt', '600',
        '--output', output_file
    ]

    result = subprocess.run(cmd, capture_output=False)

    if result.returncode == 0:
        print(f"\n✓ Historical heatmap saved to: {output_file}")
        return output_file
    else:
        print(f"\n✗ Historical heatmap failed with code {result.returncode}")
        return None


def run_wind_filtered_heatmap(forecast: dict, time_window: str, runway: str = None, output_suffix: str = ""):
    """Run heatmap filtered by tomorrow's wind conditions."""
    print("\n" + "="*60)
    print("GENERATING WIND-FILTERED HEATMAP (MATCHING TOMORROW)")
    print("="*60 + "\n")

    wind_dir = forecast['wind_dir']
    wind_speed = forecast['wind_speed']

    print(f"Filtering for wind: {wind_dir}° at {wind_speed} km/h")
    if runway:
        print(f"Runway filter: {runway}")

    output_file = f"output/heatmap_wind_{time_window}{output_suffix}.html"

    cmd = [
        '.venv/bin/python',
        'src/visualization/plot_data_improved.py',
        '--time-window', time_window,
        '--wind-dir', str(wind_dir),
        '--wind-tolerance', '30',
        '--wind-speed-min', str(max(0, wind_speed - 10)),
        '--wind-speed-max', str(wind_speed + 10),
        '--min-alt', '200',
        '--max-alt', '600',
        '--output', output_file
    ]

    if runway:
        cmd.extend(['--runway', runway])

    result = subprocess.run(cmd, capture_output=False)

    if result.returncode == 0:
        print(f"\n✓ Wind-filtered heatmap saved to: {output_file}")
        return output_file
    else:
        print(f"\n✗ Wind-filtered heatmap failed with code {result.returncode}")
        return None


def print_summary(target_date: date, time_window: str, forecast: dict, runway: str, outputs: dict):
    """Print summary of generated forecasts."""
    print("\n" + "="*60)
    print("FORECAST GENERATION COMPLETE!")
    print("="*60 + "\n")

    print(f"📅 Date: {target_date}")
    print(f"⏰ Time window: {time_window.upper()}")

    if forecast:
        print(f"\n🌤️  Tomorrow's Weather:")
        print(f"   Wind: {forecast['wind_dir']}° at {forecast['wind_speed']} km/h")
        if forecast['temp']:
            print(f"   Temperature: {forecast['temp']}°C")
        if runway:
            print(f"   Expected runway: {runway}")

    print(f"\n📊 Generated Maps:\n")

    map_order = [
        ('ml', '1️⃣  ML Prediction',
         'Machine learning: success probability (will I find thermal?) + recommended direction'),
        ('wind_filtered', '2️⃣  Wind-Filtered Heatmap (RECOMMENDED)',
         'Historical thermal locations matching tomorrow\'s wind'),
        ('advanced', '3️⃣  Advanced Prediction',
         'Terrain-aware, confidence-scored, physics-based'),
        ('historical', '4️⃣  All Historical Data',
         'Complete thermal database, all conditions')
    ]

    for key, title, description in map_order:
        if outputs.get(key):
            print(f"{title}")
            print(f"   File: {outputs[key]}")
            print(f"   → {description}\n")

    if runway:
        print(f"ℹ️  Data filtered for runway {runway} flights only")
    print("ℹ️  Excludes: winch launch (<200m), aerotow plane, aerotow flights\n")

    print("="*60)
    print("HOW TO USE THESE MAPS")
    print("="*60 + "\n")

    print("🎯 BEST STRATEGY:\n")
    print("1. Check ML Success Probability (map #1)")
    print("   • Will I find a thermal? (e.g., 34% chance)")
    print("   • Recommended direction to fly from release\n")

    print("2. Use Wind-Filtered Heatmap (map #2) for WHERE")
    print("   • Shows where thermals historically occurred with this wind")
    print("   • Red areas = proven thermal locations")
    print("   • Fly toward dense red zones within glide range\n")

    print("3. Cross-check with Advanced Prediction (map #3)")
    print("   • Physics-based terrain scoring")
    print("   • Look for overlap with heatmap\n")

    print("4. Final decision:")
    print("   • Fly ML recommended direction toward heatmap hotspots")
    print("   • Stay within 2km glide range initially\n")

    print("="*60)
    print("FLIGHT STRATEGY")
    print("="*60 + "\n")

    print("After winch release (~400m):")
    print("  ✓ Turn toward closest high-confidence area")
    print("  ✓ Stay within gliding range of airport")
    print("  ✓ Search for lift in red zones")
    print("  ✓ Once established, consider distant hotspots\n")

    print("To view maps:")
    for key in ['ml', 'advanced', 'wind_filtered', 'historical']:
        if outputs.get(key):
            print(f"  open {outputs[key]}")

    print("\nGood luck with your flying! 🪂\n")


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='First-thermal forecast for winch launches at EHHV',
        epilog='Examples:\n'
               '  python scripts/daily_forecast.py\n'
               '  python scripts/daily_forecast.py morning --runway 25\n'
               '  python scripts/daily_forecast.py --date 2026-03-30 afternoon --glider two_seat\n'
               '  python scripts/daily_forecast.py --all-runways\n'
               '  python scripts/daily_forecast.py --legacy      # old heatmaps (before the new pipeline)\n',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument('time_window', nargs='?',
                       choices=['morning', 'midday', 'afternoon'],
                       default='midday',
                       help='Time window (default: midday)')
    parser.add_argument('--date', type=str,
                       help='Target date (YYYY-MM-DD), default: tomorrow')
    parser.add_argument('--runway', choices=['07', '12', '18', '25', '30', '36'],
                       help='Runway in use (default: from the forecast surface wind)')
    parser.add_argument('--glider', choices=['single', 'two_seat'], default='single',
                       help='Glider class for reach and connect chance (default: single)')
    parser.add_argument('--dual', action='store_true', help='Flying with an instructor/second pilot')
    parser.add_argument('--all-runways', action='store_true', help='Produce a page for each of the six runways')
    parser.add_argument('--no-log', action='store_true', help='Do not append to the forecast log')
    parser.add_argument('--legacy', action='store_true',
                       help='Generate the old heatmaps instead (scripts are kept until the new model is accepted)')
    parser.add_argument('--include-all', action='store_true',
                       help='(legacy) Include all-conditions heatmap (not wind-filtered)')
    parser.add_argument('--output-suffix', default='',
                       help='Suffix for output filenames')

    return parser.parse_args()


def run_first_thermal(target_date, args):
    """New pipeline: layer A + reach + layer B + layer C → output/first_thermal_<date>_<window>[_rwy].html"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from src.prediction.dataset import connect
    from src.prediction.first_thermal_map import build_forecast, log_forecast, render_map
    from src.weather.open_meteo_forecast import fetch_convection_forecast

    conn = connect()
    rows = fetch_convection_forecast(past_days=3, forecast_days=8)
    runways = ['07', '12', '18', '25', '30', '36'] if args.all_runways else [args.runway]
    outputs = []
    for rw in runways:
        fc = build_forecast(conn, target_date, args.time_window, rw, args.glider, int(args.dual), weather_rows=rows)
        suffix = f"_{fc['runway']}" if args.all_runways else ''
        out = f"output/first_thermal_{target_date}_{args.time_window}{suffix}{args.output_suffix}.html"
        os.makedirs('output', exist_ok=True)
        render_map(fc, out)
        if not args.no_log and not args.all_runways:
            log_forecast(fc)
        h = fc['pA_history']
        print(f"\n{target_date} {args.time_window} · runway {fc['runway']} · {args.glider}")
        print(f"  Wind 100 m {fc['weather']['w100_dir']:.0f}° {fc['weather']['w100_spd']:.0f} km/h, "
              f"boundary layer {fc['weather']['blh']:.0f} m, cloud base ≈ {fc['weather']['cloudbase']:.0f} m")
        print(f"  Chance to connect: {100 * fc['pA']:.0f}%  (club average {100 * h['club_rate']:.0f}%; "
              f"similar past forecasts {100 * h['observed']:.0f}%, n={h['n']})")
        d = fc['direction']
        names = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
        note = '' if fc['acceptance'].get('decision') == 'full' else ' — history only, not validated as advice'
        print(f"  Best direction historically ({d['tau']} s after release): {names[d['best']]} "
              f"(obs/exp {d['oe'][d['best']]:.2f}, {d['n'][d['best']]} launches){note}")
        print(f"  Data: {fc['coverage']['launches']} launches, {fc['coverage']['connects']} connected "
              f"({fc['coverage']['verdict']}); model status: {fc['acceptance'].get('decision')}")
        for k, ln in enumerate(fc['lines']):
            print(f"  Line {k + 1}: {100 * ln['p']:.0f}% ({100 * ln['p_lo']:.0f}–{100 * ln['p_hi']:.0f}%)")
        print(f"  ✓ {out}")
        outputs.append(out)
    return outputs


def main():
    """Main execution."""
    args = parse_arguments()

    # Determine target date
    if args.date:
        try:
            target_date = date.fromisoformat(args.date)
        except ValueError:
            print(f"ERROR: Invalid date format: {args.date}")
            print("Use YYYY-MM-DD format")
            return 1
    else:
        target_date = date.today() + timedelta(days=1)

    if not args.legacy:
        try:
            run_first_thermal(target_date, args)
        except FileNotFoundError as e:
            print(f"ERROR: model files missing ({e}). Run: .venv/bin/python scripts/train_first_thermal.py")
            return 1
        except ValueError as e:
            print(f"ERROR: {e}")
            return 1
        return 0

    print("="*60)
    print("DAILY THERMAL FORECAST GENERATOR")
    print("="*60 + "\n")
    print(f"Target date: {target_date}")
    print(f"Time window: {args.time_window.upper()}\n")

    # Get weather forecast
    forecast = get_forecast(target_date, args.time_window)

    if not forecast:
        print("\n⚠️  Could not fetch weather forecast")
        print("Continuing with map generation anyway...\n")

    # Ask user which runway to use
    runway = None
    if forecast and forecast.get('wind_dir') is not None:
        expected_runway = determine_runway_from_wind(forecast['wind_dir'])
        print("\n" + "="*60)
        print("RUNWAY SELECTION")
        print("="*60)
        print(f"Wind forecast: {forecast['wind_dir']}° at {forecast['wind_speed']} km/h")
        print(f"Expected runway: {expected_runway}")
        print("\nAvailable runways: 07, 25, 12, 30, 18, 36")

        response = input(f"\nWhich runway will be in use? [{expected_runway}]: ").strip().upper()

        if response == "":
            runway = expected_runway
        elif response in ['07', '25', '12', '30', '18', '36']:
            runway = response
        else:
            print(f"Invalid runway '{response}', using expected: {expected_runway}")
            runway = expected_runway

        print(f"\n✓ Using runway {runway} for thermal predictions\n")

    # Track generated outputs
    outputs = {}

    # 1. ML prediction (if models exist)
    ml_model_path = os.path.join('data', 'models', 'thermal_ml_models.pkl')
    if os.path.exists(ml_model_path) and forecast:
        output = run_ml_prediction(target_date, args.time_window, forecast, runway, args.output_suffix)
        if output:
            outputs['ml'] = output
    else:
        print("\n  ML models not found, skipping ML prediction")
        print("  Run: .venv/bin/python src/prediction/ml_thermal_model.py --evaluate --force\n")

    # 2. Advanced prediction (always run)
    output = run_advanced_prediction(target_date, args.time_window, runway, args.output_suffix)
    if output:
        outputs['advanced'] = output

    # 3. Wind-filtered heatmap (if forecast available)
    if forecast:
        output = run_wind_filtered_heatmap(forecast, args.time_window, runway, args.output_suffix)
        if output:
            outputs['wind_filtered'] = output

    # 4. All-conditions heatmap (optional, not wind-filtered)
    if args.include_all:
        output = run_historical_heatmap(args.time_window, args.output_suffix)
        if output:
            outputs['historical'] = output

    # Print summary
    print_summary(target_date, args.time_window, forecast, runway, outputs)

    return 0


if __name__ == "__main__":
    exit(main())
