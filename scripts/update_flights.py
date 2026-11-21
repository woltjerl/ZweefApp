#!/usr/bin/env python3
"""
Update Flight Database

Fetches new flights from GlidingApp, stores them in the database,
and runs all processing steps:
  1. Fetch IGC files from GlidingApp API
  2. Store flights and GPS data in SQLite database (day folders in the fetch range are
     re-checked, so flights added later to a day and logger files uploaded after the
     flight was first stored are picked up)
  3. Runway detection (from GPS takeoff track)
  4. Weather data update (fetches weather for date range, even if no flights)
  5. First-thermal pipeline (scripts/train_first_thermal.py; skip with --skip-models):
     convection weather, climb detector (launches, climbs, search tracks; also writes
     flights.flight_outcome / release_* / thermal_entry_* and igc_data.first_thermal),
     layers A/B/C
  6. Optional: publish the history pack for the hosted briefing (--publish-pack gs://…/pack,
     or set ZWEEF_PACK_URI; see cloud/README.md)

The old thermal detection (FindThermals_improved.py) and release-to-thermal extraction
(extract_release_thermal.py) are replaced by the climb detector in step 5.

By default, steps 3 and 5 only process new flights.
Use --force to recalculate all flights.

Usage:
    python scripts/update_flights.py                    # fetch new since last flight in DB
    python scripts/update_flights.py --from 2026-03-01  # fetch from specific date
    python scripts/update_flights.py --days 7           # fetch last 7 days
    python scripts/update_flights.py --force            # recalculate all derived data
"""

import os
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


def reopen_folders(db_path: str, date_from: str, date_to: str) -> int:
    """Remove processing_log entries for day folders in the range so --resume processes them again."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM processing_log WHERE substr(folder_name, 1, 10) BETWEEN ? AND ?",
            (date_from, date_to))
        conn.commit()
        return cur.rowcount
    except sqlite3.OperationalError:
        return 0          # no processing_log yet
    finally:
        conn.close()


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
        description='Fetch new flights, store in DB, detect runways, and run the first-thermal pipeline',
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
                       help='Force recalculation of runways and climb detection for all flights')
    parser.add_argument('--skip-models', action='store_true',
                       help='Do not run the first-thermal pipeline (step 5)')
    parser.add_argument('--publish-pack', default=os.environ.get('ZWEEF_PACK_URI'),
                       help='gs://bucket/prefix: upload the briefing history pack afterwards (env ZWEEF_PACK_URI)')
    parser.add_argument('--me', default=os.environ.get('ZWEEF_ME'),
                       help='your logbook name for the "you" layer in the pack (env ZWEEF_ME)')
    parser.add_argument('--gcloud-account', default=os.environ.get('ZWEEF_GCLOUD_ACCOUNT'),
                       help='gcloud account for the upload (env ZWEEF_GCLOUD_ACCOUNT)')
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

        # Days in the fetch range were downloaded again into the same folders (date_dagid);
        # re-open them so flights added to those days since the last run are stored too.
        # Storing skips flights that are already in the database.
        reopened = reopen_folders(db_path, fetch_from, fetch_to)
        if reopened:
            print(f"  Re-checking {reopened} day folder(s) in {fetch_from} → {fetch_to} for new flights\n")

        result = subprocess.run([
            '.venv/bin/python', 'src/data_collection/store_icg_data.py',
            '--db-path', db_path,
            '--resume',
        ])
        if result.returncode != 0:
            print(f"\nWARNING: Store failed (exit code {result.returncode})\n")
        else:
            print("\n✓ Store complete\n")

    # Step 3: Runway detection (before the climb detector, which copies the runway)
    print("-"*60)
    print("STEP 3: Runway detection")
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

    # Step 4: Update weather data for date range
    print("-"*60)
    print("STEP 4: Update weather data")
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

    # Step 5: First-thermal pipeline (convection weather, climb detector, layers A/B/C)
    if not args.skip_models:
        print("-"*60)
        print("STEP 5: First-thermal pipeline (weather, climb detector, layers A/B/C)")
        print("-"*60 + "\n")
        ft_cmd = ['.venv/bin/python', 'scripts/train_first_thermal.py', '--db-path', db_path]
        if args.force:
            ft_cmd.append('--force-detector')
        result = subprocess.run(ft_cmd)
        if result.returncode != 0:
            print(f"\nWARNING: First-thermal training finished with warnings (exit code {result.returncode})\n")
        else:
            print("\n✓ First-thermal models updated\n")

    # Step 6: history pack for the hosted briefing
    if args.publish_pack:
        print("-"*60)
        print("STEP 6: Publish the briefing history pack")
        print("-"*60 + "\n")
        pack_cmd = ['.venv/bin/python', 'scripts/publish_briefing_pack.py', '--db-path', db_path,
                    '--uri', args.publish_pack]
        if args.me:
            pack_cmd += ['--me', args.me]
        if args.gcloud_account:
            pack_cmd += ['--account', args.gcloud_account]
        result = subprocess.run(pack_cmd)
        if result.returncode != 0:
            print(f"\nWARNING: Publishing the pack failed (exit code {result.returncode})\n")

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
