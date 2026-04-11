#!/usr/bin/env python3
"""
Update Weather Database

Fetches weather data from Meteostat and stores it in the database.
Unlike update_flights.py, this works independently of flight data.

Usage:
    python scripts/update_weather.py                    # fetch today
    python scripts/update_weather.py --date 2026-03-29  # fetch specific date
    python scripts/update_weather.py --from 2026-03-20 --to 2026-03-29  # date range
    python scripts/update_weather.py --force            # re-fetch even if already in DB
"""

import sys
import os
import sqlite3
import argparse
import logging
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src', 'weather'))
from HilversumWeatherFetcher import HilversumWeatherFetcher


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    return logging.getLogger(__name__)


def ensure_weather_table(conn: sqlite3.Connection):
    """Ensure weather_data table exists."""
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS weather_data (
            date TEXT NOT NULL,
            hour INTEGER NOT NULL,
            temp REAL,
            dwpt REAL,
            rhum REAL,
            prcp REAL,
            snow REAL,
            wdir REAL,
            wspd REAL,
            wpgt REAL,
            pres REAL,
            tsun REAL,
            coco REAL,
            PRIMARY KEY (date, hour)
        )
    """)
    conn.commit()


def weather_exists(conn: sqlite3.Connection, day_str: str) -> bool:
    """Check if weather data already exists for a given day."""
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM weather_data WHERE date = ?", (day_str,))
    count = c.fetchone()[0]
    return count > 0


def store_weather_for_day(
    conn: sqlite3.Connection,
    day: date,
    logger: logging.Logger,
    force: bool = False
) -> bool:
    """
    Fetch and store weather data for a single day.

    Args:
        conn: Database connection
        day: Date to fetch
        logger: Logger instance
        force: Re-fetch even if data exists

    Returns:
        True if successful, False otherwise
    """
    day_str = day.isoformat()

    # Check if already exists (unless force)
    if not force and weather_exists(conn, day_str):
        logger.info(f"  {day_str}: Already in database (use --force to re-fetch)")
        return True

    # Fetch weather data
    try:
        logger.info(f"  {day_str}: Fetching from Meteostat...")
        fetcher = HilversumWeatherFetcher(logger=logger)
        weather_list = fetcher.fetch_wind(day)

        if not weather_list:
            logger.warning(f"  {day_str}: No weather data returned")
            return False

        # Parse and store
        c = conn.cursor()
        records_added = 0

        for record in weather_list:
            try:
                # Extract hour from ISO timestamp (e.g., "2026-03-29T12:00:00")
                hour = int(record["time"][11:13])

                c.execute("""
                    INSERT OR REPLACE INTO weather_data
                    (date, hour, temp, dwpt, rhum, prcp, snow, wdir, wspd, wpgt, pres, tsun, coco)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    day_str, hour,
                    record.get("temp"), record.get("dwpt"), record.get("rhum"),
                    record.get("prcp"), record.get("snow"), record.get("wdir"),
                    record.get("wspd"), record.get("wpgt"), record.get("pres"),
                    record.get("tsun"), record.get("coco")
                ))
                records_added += 1

            except (KeyError, ValueError, IndexError) as e:
                logger.warning(f"  {day_str}: Invalid weather record: {e}")

        conn.commit()
        logger.info(f"  {day_str}: ✓ Stored {records_added} hourly records")
        return True

    except Exception as e:
        logger.error(f"  {day_str}: ✗ Failed to fetch weather: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description='Fetch and store weather data in database',
        epilog='Examples:\n'
               '  python scripts/update_weather.py\n'
               '  python scripts/update_weather.py --date 2026-03-29\n'
               '  python scripts/update_weather.py --from 2026-03-20 --to 2026-03-29\n'
               '  python scripts/update_weather.py --force\n',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument('--date', type=str,
                       help='Single date to fetch (YYYY-MM-DD), default: today')
    parser.add_argument('--from', dest='from_date', type=str,
                       help='Start date for range (YYYY-MM-DD)')
    parser.add_argument('--to', dest='to_date', type=str,
                       help='End date for range (YYYY-MM-DD)')
    parser.add_argument('--db-path', default='data/flights.db',
                       help='Path to database (default: data/flights.db)')
    parser.add_argument('--force', '-f', action='store_true',
                       help='Re-fetch even if data already exists')
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='Verbose logging')

    args = parser.parse_args()
    logger = setup_logging(args.verbose)

    # Determine date range
    if args.date:
        try:
            start_date = date.fromisoformat(args.date)
            end_date = start_date
        except ValueError:
            logger.error(f"Invalid date format: {args.date}")
            return 1
    elif args.from_date and args.to_date:
        try:
            start_date = date.fromisoformat(args.from_date)
            end_date = date.fromisoformat(args.to_date)
        except ValueError as e:
            logger.error(f"Invalid date format: {e}")
            return 1
    elif args.from_date:
        try:
            start_date = date.fromisoformat(args.from_date)
            end_date = date.today()
        except ValueError:
            logger.error(f"Invalid date format: {args.from_date}")
            return 1
    else:
        # Default: today
        start_date = date.today()
        end_date = start_date

    if start_date > end_date:
        logger.error(f"Start date {start_date} is after end date {end_date}")
        return 1

    # Connect to database
    try:
        conn = sqlite3.connect(args.db_path)
        ensure_weather_table(conn)
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        return 1

    # Process date range
    print("="*60)
    print("UPDATE WEATHER DATABASE")
    print("="*60)
    print(f"\n  Database:   {args.db_path}")
    print(f"  Date range: {start_date} to {end_date}")
    print(f"  Force:      {args.force}\n")

    current_date = start_date
    success_count = 0
    skip_count = 0
    fail_count = 0

    while current_date <= end_date:
        if store_weather_for_day(conn, current_date, logger, args.force):
            if not args.force and weather_exists(conn, current_date.isoformat()):
                skip_count += 1
            success_count += 1
        else:
            fail_count += 1

        current_date += timedelta(days=1)

    conn.close()

    # Summary
    total_days = (end_date - start_date).days + 1
    print("\n" + "="*60)
    print("UPDATE COMPLETE")
    print("="*60)
    print(f"\n  Total days:     {total_days}")
    print(f"  Successful:     {success_count}")
    print(f"  Skipped:        {skip_count}")
    print(f"  Failed:         {fail_count}\n")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    exit(main())
