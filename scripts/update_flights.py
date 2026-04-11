#!/usr/bin/env python3
"""
Update Flight Database

Fetches new flights from GlidingApp, stores them in the database,
and runs all processing steps:
  1. Fetch IGC files from GlidingApp API
  2. Store flights and GPS data in SQLite database
  3. Thermal detection (winch release + first thermal)
  4. Runway detection (from GPS takeoff track)
  5. Release-to-thermal extraction (bearing, distance, time from release to thermal)
  6. Weather data update (fetches weather for date range, even if no flights)

By default, steps 3-5 only process new/unprocessed flights.
Use --force to recalculate all flights.

Usage:
    python scripts/update_flights.py                    # fetch new since last flight in DB
    python scripts/update_flights.py --from 2026-03-01  # fetch from specific date
    python scripts/update_flights.py --days 7           # fetch last 7 days
    python scripts/update_flights.py --force            # recalculate all derived data
"""

import subprocess
import sqlite3
import argparse
from datetime import date, timedelta


def get_last_flight_date(db_path: str) -> date:
    """Get the date of the most recent flight in the database."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(datum) FROM flights")
    row = cursor.fetchone()
    conn.close()
    if row and row[0]:
        return date.fromisoformat(row[0])
    return date(2024, 1, 1)


def get_flight_count(db_path: str) -> int:
    """Get total flight count in the database."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM flights")
    count = cursor.fetchone()[0]
    conn.close()
    return count


def main():
    parser = argparse.ArgumentParser(
        description='Fetch new flights, store in DB, detect thermals/runways, and extract release-to-thermal data',
    )
    parser.add_argument('--from', dest='from_date', type=str,
                       help='Fetch from date (YYYY-MM-DD), default: last flight in DB')
    parser.add_argument('--to', type=str,
                       help='Fetch to date (YYYY-MM-DD), default: today')
    parser.add_argument('--days', type=int,
                       help='Fetch last N days (e.g. --days 7)')
    parser.add_argument('--db-path', default='data/flights.db',
                       help='Path to database')
    parser.add_argument('--force', '-f', action='store_true',
                       help='Force recalculation of thermals, runways, and release-to-thermal for all flights')
    args = parser.parse_args()

    db_path = args.db_path

    # Determine end date
    if args.to:
        fetch_to = args.to
    elif args.days:
        fetch_to = str(date.today())
    else:
        fetch_to = str(date.today())

    # Determine start date
    if args.days:
        fetch_from = str(date.today() - timedelta(days=args.days))
    elif args.from_date:
        fetch_from = args.from_date
    else:
        last = get_last_flight_date(db_path)
        # Re-fetch the last day too - more flights may have been added
        fetch_from = str(last - timedelta(days=1))

    flights_before = get_flight_count(db_path)

    print("="*60)
    print("UPDATE FLIGHT DATABASE")
    print("="*60)
    print(f"\n  Database:    {db_path}")
    print(f"  Flights:     {flights_before}")
    print(f"  Fetch range: {fetch_from} -> {fetch_to}\n")

    # Step 1: Fetch IGC files
    if fetch_from > fetch_to:
        print("Database is already up to date, no new flights to fetch.\n")
    else:
        print("-"*60)
        print("STEP 1: Fetching IGC files from GlidingApp")
        print("-"*60 + "\n")

        result = subprocess.run([
            '.venv/bin/python', 'src/data_collection/fetch_igc_files.py',
            '--min-date', fetch_from,
            '--max-date', fetch_to,
        ])
        if result.returncode != 0:
            print(f"\nWARNING: Fetch failed (exit code {result.returncode})\n")
        else:
            print("\n✓ Fetch complete\n")

        # Step 2: Store in database
        print("-"*60)
        print("STEP 2: Storing flights in database")
        print("-"*60 + "\n")

        result = subprocess.run([
            '.venv/bin/python', 'src/data_collection/store_icg_data.py',
            '--db-path', db_path,
            '--resume',
        ])
        if result.returncode != 0:
            print(f"\nWARNING: Store failed (exit code {result.returncode})\n")
        else:
            print("\n✓ Store complete\n")

    # Step 3: Thermal detection (auto-skips if nothing changed)
    print("-"*60)
    print("STEP 3: Thermal detection")
    print("-"*60 + "\n")

    thermal_cmd = [
        '.venv/bin/python', 'src/thermal_detection/FindThermals_improved.py',
        '--yes', '--db-path', db_path,
    ]
    if args.force:
        thermal_cmd.append('--force')

    result = subprocess.run(thermal_cmd)
    if result.returncode != 0:
        print(f"\nWARNING: Thermal detection failed (exit code {result.returncode})\n")

    # Step 4: Runway detection
    print("-"*60)
    print("STEP 4: Runway detection")
    print("-"*60 + "\n")

    runway_cmd = [
        '.venv/bin/python', 'src/data_collection/detect_runway.py',
        '--db-path', db_path,
    ]
    if args.force:
        runway_cmd.append('--force')

    result = subprocess.run(runway_cmd)
    if result.returncode != 0:
        print(f"\nWARNING: Runway detection failed (exit code {result.returncode})\n")

    # Step 5: Release-to-thermal extraction
    print("-"*60)
    print("STEP 5: Release-to-thermal extraction")
    print("-"*60 + "\n")

    release_cmd = [
        '.venv/bin/python', 'src/data_collection/extract_release_thermal.py',
        '--db-path', db_path,
    ]
    if args.force:
        release_cmd.append('--force')

    result = subprocess.run(release_cmd)
    if result.returncode != 0:
        print(f"\nWARNING: Release-to-thermal extraction failed (exit code {result.returncode})\n")

    # Step 6: Update weather data for date range
    print("-"*60)
    print("STEP 6: Update weather data")
    print("-"*60 + "\n")

    weather_cmd = [
        '.venv/bin/python', 'scripts/update_weather.py',
        '--from', fetch_from,
        '--to', fetch_to,
        '--db-path', db_path,
    ]

    result = subprocess.run(weather_cmd)
    if result.returncode != 0:
        print(f"\nWARNING: Weather update failed (exit code {result.returncode})\n")
    else:
        print("\n✓ Weather data updated\n")

    # Summary
    flights_after = get_flight_count(db_path)
    new_flights = flights_after - flights_before

    print("\n" + "="*60)
    print("UPDATE COMPLETE")
    print("="*60)
    print(f"\n  Flights before: {flights_before}")
    print(f"  Flights after:  {flights_after}")
    print(f"  New flights:    {new_flights}")
    print(f"\nTo generate tomorrow's forecast:")
    print(f"  .venv/bin/python scripts/daily_forecast.py\n")

    return 0


if __name__ == "__main__":
    exit(main())
