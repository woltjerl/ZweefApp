"""
IGC Flight Data Storage System

Processes IGC (GPS track) files from GlidingApp downloads and stores them in SQLite
with enriched weather data, speed calculations, and fix-to-fix relationships.

Database Tables:
    - flights: Flight metadata from JSON
    - igc_data: GPS fixes with computed speed/vertical speed and weather
    - weather_data: Hourly weather cache (date, hour as PRIMARY KEY)
    - processing_log: Track processed folders for resume capability

Usage:
    python store_icg_data.py
    python store_icg_data.py --input-dir icg_files --db-path flights.db --verbose
    python store_icg_data.py --resume --verbose
    python store_icg_data.py --dry-run

Features:
    - Automatic weather data caching (reduces API calls)
    - Progress indicators for long-running operations
    - Resume capability for interrupted runs
    - Graceful error handling (continues on failures)
    - Statistics summary at completion
    - Configurable via CLI arguments

Requirements:
    - igc_parser
    - HilversumWeatherFetcher
    - tqdm
"""

import os
import sys
import json
import sqlite3
import datetime
import math
import argparse
import logging
from typing import Optional, Dict, List, Any
from tqdm import tqdm

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'thermal_detection'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'weather'))
from igc_parser import IGCParser
from HilversumWeatherFetcher import HilversumWeatherFetcher


class Config:
    """Configuration for IGC data storage."""

    def __init__(self):
        """Initialize with default values."""
        self.db_path = "data/flights.db"
        self.input_dir = "data/icg_files"
        self.resume = False
        self.strict_mode = False
        self.dry_run = False

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> 'Config':
        """
        Create Config from command line arguments.

        Args:
            args: Parsed command line arguments

        Returns:
            Config instance
        """
        config = cls()
        config.db_path = args.db_path
        config.input_dir = args.input_dir
        config.resume = args.resume
        config.strict_mode = args.strict
        config.dry_run = args.dry_run
        return config

    def validate(self) -> List[str]:
        """
        Validate configuration values.

        Returns:
            List of error messages (empty if valid)
        """
        errors = []

        if not os.path.isdir(self.input_dir):
            errors.append(f"Input directory does not exist: {self.input_dir}")

        if self.dry_run:
            # Dry run doesn't need writable DB
            pass
        elif os.path.exists(self.db_path):
            if not os.access(self.db_path, os.W_OK):
                errors.append(f"Database not writable: {self.db_path}")
        else:
            # Check if parent directory is writable
            parent_dir = os.path.dirname(self.db_path) or "."
            if not os.access(parent_dir, os.W_OK):
                errors.append(f"Cannot create database at: {self.db_path}")

        return errors


class ProcessingStats:
    """Track statistics during processing."""

    def __init__(self):
        """Initialize counters."""
        self.folders_processed = 0
        self.folders_skipped = 0
        self.folders_failed = 0
        self.flights_processed = 0
        self.flights_skipped = 0
        self.flights_failed = 0
        self.igc_fixes_processed = 0
        self.weather_days_fetched = 0
        self.weather_days_cached = 0
        self.weather_days_failed = 0

    def print_summary(self, logger: logging.Logger):
        """Print formatted summary."""
        logger.info("=" * 60)
        logger.info("PROCESSING COMPLETE")
        logger.info(f"  Folders processed: {self.folders_processed}")
        logger.info(f"  Folders skipped: {self.folders_skipped}")
        logger.info(f"  Folders failed: {self.folders_failed}")
        logger.info(f"  Flights processed: {self.flights_processed}")
        logger.info(f"  Flights skipped: {self.flights_skipped}")
        logger.info(f"  Flights failed: {self.flights_failed}")
        logger.info(f"  IGC fixes processed: {self.igc_fixes_processed}")
        logger.info(f"  Weather days fetched: {self.weather_days_fetched}")
        logger.info(f"  Weather days cached: {self.weather_days_cached}")
        logger.info(f"  Weather days failed: {self.weather_days_failed}")
        logger.info("=" * 60)


def setup_logging(log_level: int = logging.INFO) -> logging.Logger:
    """
    Configure logging for the application.

    Args:
        log_level: Logging level (default: INFO)

    Returns:
        Logger instance
    """
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('logs/store_icg_data.log', mode='a')
        ]
    )
    return logging.getLogger(__name__)


def create_tables(conn: sqlite3.Connection):
    """
    Create the flights, igc_data, weather_data, and processing_log tables.

    Args:
        conn: Database connection
    """
    c = conn.cursor()

    # Flights table: store all fields from the JSON flight record.
    c.execute("""
        CREATE TABLE IF NOT EXISTS flights (
            id INTEGER PRIMARY KEY,
            uuid TEXT,
            datum TEXT,
            date_created TEXT,
            date_updated TEXT,
            volg_nummer INTEGER,
            is_deleted INTEGER,
            is_locked INTEGER,
            dag_id INTEGER,
            is_prive INTEGER,
            vertrek_vliegveld TEXT,
            aankomst_vliegveld TEXT,
            kist_id INTEGER,
            callsign TEXT,
            registratie TEXT,
            type TEXT,
            flarm TEXT,
            gezagvoerder_id INTEGER,
            gezagvoerder_naam TEXT,
            tweede_inzittende_id INTEGER,
            tweede_inzittende_naam TEXT,
            betalend_lid_id INTEGER,
            is_fis INTEGER,
            start_methode TEXT,
            sleep_uuid TEXT,
            category TEXT,
            is_training INTEGER,
            is_examen INTEGER,
            is_profcheck INTEGER,
            is_overland INTEGER,
            afstand INTEGER,
            starts INTEGER,
            start_tijd TEXT,
            landings_tijd TEXT,
            vluchtduur INTEGER,
            blocktime INTEGER,
            start_ogn INTEGER,
            landings_ogn INTEGER,
            height TEXT,
            pic_time TEXT,
            dbo_time TEXT,
            bijzonderheden TEXT,
            notitie TEXT,
            igc TEXT,
            igc_visible INTEGER,
            signed TEXT
        )
    """)

    # igc_data table
    c.execute("""
        CREATE TABLE IF NOT EXISTS igc_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flight_id INTEGER,
            sequence INTEGER,
            time TEXT,
            latitude REAL,
            longitude REAL,
            altitude REAL,
            speed REAL,
            vertical_speed REAL,
            wind_dir REAL,
            wind_spd REAL,
            weather_temp REAL,
            weather_dwpt REAL,
            weather_rhum REAL,
            weather_prcp REAL,
            weather_snow REAL,
            weather_wpgt REAL,
            weather_pres REAL,
            weather_tsun REAL,
            weather_coco REAL,
            previous_fix_id INTEGER,
            next_fix_id INTEGER,
            FOREIGN KEY(flight_id) REFERENCES flights(id)
        )
    """)

    # weather_data table
    c.execute("""
        CREATE TABLE IF NOT EXISTS weather_data (
            date TEXT,
            hour INTEGER,
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

    # processing_log table for resume capability
    c.execute("""
        CREATE TABLE IF NOT EXISTS processing_log (
            folder_name TEXT PRIMARY KEY,
            processed_at TEXT,
            status TEXT,
            error_message TEXT
        )
    """)

    conn.commit()


def get_processed_folders(conn: sqlite3.Connection) -> set:
    """
    Get set of already-processed folder names.

    Args:
        conn: Database connection

    Returns:
        Set of folder names that have been processed
    """
    c = conn.cursor()
    c.execute("SELECT folder_name FROM processing_log WHERE status = 'completed'")
    return {row[0] for row in c.fetchall()}


def mark_folder_processed(
    conn: sqlite3.Connection,
    folder_name: str,
    status: str,
    error_message: Optional[str] = None
):
    """
    Mark folder as processed in log.

    Args:
        conn: Database connection
        folder_name: Name of folder
        status: 'completed', 'failed', or 'skipped'
        error_message: Optional error message
    """
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO processing_log
        (folder_name, processed_at, status, error_message)
        VALUES (?, ?, ?, ?)
    """, (
        folder_name,
        datetime.datetime.now().isoformat(),
        status,
        error_message
    ))
    conn.commit()


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great-circle distance between two points on Earth.

    Args:
        lat1: Latitude of first point
        lon1: Longitude of first point
        lat2: Latitude of second point
        lon2: Longitude of second point

    Returns:
        Distance in kilometers
    """
    R = 6371  # Earth radius in kilometers
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def convert_value(value: Any) -> Any:
    """
    Convert dictionaries or lists to JSON strings so they can be stored in SQLite.

    Args:
        value: Value to convert

    Returns:
        JSON string if dict/list, otherwise unchanged value
    """
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


def fetch_weather_for_day(
    conn: sqlite3.Connection,
    day_str: str,
    logger: logging.Logger,
    stats: ProcessingStats,
    strict_mode: bool = False
) -> Optional[Dict[int, Dict[str, Any]]]:
    """
    Check if weather data for the given day already exists in the DB.
    If not, fetch it using HilversumWeatherFetcher and store one record per hour.

    Args:
        conn: Database connection
        day_str: Date string in YYYY-MM-DD format
        logger: Logger instance
        stats: Statistics tracker
        strict_mode: If True, raise on weather fetch failure

    Returns:
        Dictionary keyed by hour (0-23) with weather data, or None on failure

    Raises:
        RuntimeError: If strict_mode=True and weather fetch fails
    """
    c = conn.cursor()
    c.execute("""
        SELECT hour, temp, dwpt, rhum, prcp, snow, wdir, wspd, wpgt, pres, tsun, coco
        FROM weather_data WHERE date = ?
    """, (day_str,))
    rows = c.fetchall()

    weather_by_hour = {}
    if rows:
        logger.debug(f"Using cached weather data for {day_str}")
        stats.weather_days_cached += 1
        for row in rows:
            hour = row[0]
            weather_by_hour[hour] = {
                "temp": row[1], "dwpt": row[2], "rhum": row[3],
                "prcp": row[4], "snow": row[5], "wdir": row[6],
                "wspd": row[7], "wpgt": row[8], "pres": row[9],
                "tsun": row[10], "coco": row[11]
            }
        return weather_by_hour

    # Need to fetch weather data
    try:
        day_date = datetime.datetime.strptime(day_str, "%Y-%m-%d").date()
    except ValueError as e:
        logger.error(f"Invalid date format: {day_str}: {e}")
        return None

    try:
        logger.info(f"Fetching weather data for {day_str}")
        fetcher = HilversumWeatherFetcher(logger=logger)
        weather_list = fetcher.fetch_wind(day_date)

        if not weather_list:
            logger.warning(f"No weather data returned for {day_str}")
            stats.weather_days_failed += 1
            if strict_mode:
                raise RuntimeError(f"No weather data available for {day_str}")
            return {}

        # Build dictionary keyed by hour
        hourly_data = {}
        for record in weather_list:
            try:
                hour = int(record["time"][11:13])
                hourly_data[hour] = record
            except (KeyError, ValueError, IndexError) as e:
                logger.warning(f"Invalid time format in weather record: {e}")

        # Insert into database
        for hour, record in hourly_data.items():
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
            weather_by_hour[hour] = {
                "temp": record.get("temp"), "dwpt": record.get("dwpt"),
                "rhum": record.get("rhum"), "prcp": record.get("prcp"),
                "snow": record.get("snow"), "wdir": record.get("wdir"),
                "wspd": record.get("wspd"), "wpgt": record.get("wpgt"),
                "pres": record.get("pres"), "tsun": record.get("tsun"),
                "coco": record.get("coco")
            }

        conn.commit()
        stats.weather_days_fetched += 1
        logger.info(f"Successfully cached weather data for {day_str}")
        return weather_by_hour

    except RuntimeError as e:
        logger.error(f"Weather fetch failed for {day_str}: {e}")
        stats.weather_days_failed += 1
        if strict_mode:
            raise
        return {}
    except Exception as e:
        logger.error(f"Unexpected error fetching weather for {day_str}: {e}")
        stats.weather_days_failed += 1
        if strict_mode:
            raise RuntimeError(f"Weather fetch error: {e}") from e
        return {}


def process_directory(
    directory_path: str,
    conn: sqlite3.Connection,
    logger: logging.Logger,
    stats: ProcessingStats,
    strict_mode: bool = False
) -> bool:
    """
    Process a folder: open the JSON file and process each flight record.

    Args:
        directory_path: Path to folder
        conn: Database connection
        logger: Logger instance
        stats: Statistics tracker
        strict_mode: If True, abort on errors

    Returns:
        True if successful, False otherwise
    """
    folder_name = os.path.basename(directory_path)
    logger.info(f"Processing folder: {folder_name}")

    try:
        # Extract date from folder name
        day_str = folder_name.split('_')[0]

        # Validate date format
        try:
            datetime.datetime.strptime(day_str, "%Y-%m-%d")
        except ValueError:
            logger.warning(f"Invalid folder name format: {folder_name}")
            stats.folders_skipped += 1
            return False

        # Fetch weather data
        weather_by_hour = fetch_weather_for_day(
            conn, day_str, logger, stats, strict_mode
        )

        if weather_by_hour is None:
            logger.error(f"Failed to get weather data for {day_str}")
            stats.folders_failed += 1
            if strict_mode:
                raise RuntimeError(f"Weather data unavailable for {day_str}")
            return False

        # Find JSON file
        json_files = [
            f for f in os.listdir(directory_path)
            if f.lower().endswith('.json')
        ]

        if not json_files:
            logger.warning(f"No JSON file found in {directory_path}")
            stats.folders_skipped += 1
            return False

        json_file_path = os.path.join(directory_path, json_files[0])

        # Load JSON
        try:
            with open(json_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Error reading JSON from {json_file_path}: {e}")
            stats.folders_failed += 1
            return False

        # Process each flight
        all_flights = data.get("allFlights", [])
        logger.info(f"Found {len(all_flights)} flights in {folder_name}")

        for flight in tqdm(all_flights, desc=f"Flights in {folder_name}", leave=False):
            try:
                process_flight(
                    flight, directory_path, conn,
                    weather_by_hour, day_str, logger, stats
                )
            except Exception as e:
                logger.error(f"Error processing flight {flight.get('id')}: {e}")
                stats.flights_failed += 1
                if strict_mode:
                    raise

        stats.folders_processed += 1
        logger.info(f"Successfully processed folder: {folder_name}")
        return True

    except Exception as e:
        logger.error(f"Error processing directory {directory_path}: {e}")
        stats.folders_failed += 1
        if strict_mode:
            raise
        return False


def process_flight(
    flight: Dict[str, Any],
    directory_path: str,
    conn: sqlite3.Connection,
    weather_by_hour: Dict[int, Dict[str, Any]],
    day_str: str,
    logger: logging.Logger,
    stats: ProcessingStats
) -> bool:
    """
    Process a flight: insert flight data and parse associated IGC file (if available).

    Args:
        flight: Flight data dictionary
        directory_path: Path to folder containing IGC files
        conn: Database connection
        weather_by_hour: Weather data keyed by hour
        day_str: Date string in YYYY-MM-DD format
        logger: Logger instance
        stats: Statistics tracker

    Returns:
        True if successful, False if skipped/failed
    """
    c = conn.cursor()

    # Check if the flight is already in the database.
    flight_id = flight.get("id")
    c.execute("SELECT id FROM flights WHERE id = ?", (flight_id,))
    if c.fetchone() is not None:
        logger.debug(f"Flight {flight_id} already exists, skipping")
        stats.flights_skipped += 1
        return False

    try:
        # Fields to extract from the JSON record.
        fields = (
            "uuid", "id", "datum", "date_created", "date_updated", "volg_nummer", "is_deleted", "is_locked",
            "dag_id", "is_prive", "vertrek_vliegveld", "aankomst_vliegveld", "kist_id", "callsign", "registratie",
            "type", "flarm", "gezagvoerder_id", "gezagvoerder_naam", "tweede_inzittende_id", "tweede_inzittende_naam",
            "betalend_lid_id", "is_fis", "start_methode", "sleep_uuid", "category", "is_training", "is_examen",
            "is_profcheck", "is_overland", "afstand", "starts", "start_tijd", "landings_tijd", "vluchtduur",
            "blocktime", "start_ogn", "landings_ogn", "height", "pic_time", "dbo_time", "bijzonderheden", "notitie",
            "igc", "igc_visible", "signed"
        )
        flight_values = [convert_value(flight.get(field)) for field in fields]
        placeholders = ",".join(["?"] * len(fields))
        sql = f"INSERT INTO flights ({','.join(fields)}) VALUES ({placeholders})"
        c.execute(sql, flight_values)

        # Use the flight JSON "id" as flight_id.
        flight_db_id = flight.get("id")

        # Process the IGC file for the flight if available.
        igc_field = flight.get("igc")
        if igc_field:
            igc_filename = os.path.basename(igc_field)
            igc_file_path = os.path.join(directory_path, igc_filename)
            if os.path.exists(igc_file_path):
                with open(igc_file_path, "r", encoding="utf-8") as f:
                    igc_text = f.read()
                parser = IGCParser(century_threshold=80)
                try:
                    igc_result = parser.parse(igc_text)
                except Exception as e:
                    logger.error(f"Error parsing IGC file {igc_file_path}: {e}")
                    igc_result = None
                if igc_result:
                    fixes = igc_result.get("fixes", [])
                    fix_ids = []
                    previous_dt = None
                    previous_lat = None
                    previous_lon = None
                    previous_altitude = None

                    for seq, fix in enumerate(fixes):
                        fix_time_str = fix.get("time")
                        speed = None
                        vertical_speed = None

                        # Weather values to add.
                        weather_temp = None
                        weather_dwpt = None
                        weather_rhum = None
                        weather_prcp = None
                        weather_snow = None
                        weather_wdir = None
                        weather_wspd = None
                        weather_wpgt = None
                        weather_pres = None
                        weather_tsun = None
                        weather_coco = None

                        # Parse the fix time (assumes format HH:MM:SS).
                        try:
                            fix_time = datetime.datetime.strptime(fix_time_str, "%H:%M:%S").time() if fix_time_str else None
                        except Exception as e:
                            logger.error(f"Time parse error for fix {fix_time_str}: {e}")
                            fix_time = None

                        # Determine weather data for the fix based on the hour.
                        if fix_time:
                            fix_hour = int(fix_time_str.split(":")[0])
                            weather = weather_by_hour.get(fix_hour)
                            if weather:
                                weather_temp = weather.get("temp")
                                weather_dwpt = weather.get("dwpt")
                                weather_rhum = weather.get("rhum")
                                weather_prcp = weather.get("prcp")
                                weather_snow = weather.get("snow")
                                weather_wdir = weather.get("wdir")
                                weather_wspd = weather.get("wspd")
                                weather_wpgt = weather.get("wpgt")
                                weather_pres = weather.get("pres")
                                weather_tsun = weather.get("tsun")
                                weather_coco = weather.get("coco")

                        if previous_dt and fix_time:
                            current_dt = datetime.datetime.combine(datetime.date.today(), fix_time)
                            time_diff = (current_dt - previous_dt).total_seconds()
                            if 0 < time_diff <= 10:
                                distance = haversine(previous_lat, previous_lon, fix.get("latitude"), fix.get("longitude"))
                                speed = (distance / time_diff) * 3600
                                if previous_altitude is not None:
                                    vertical_speed = (fix.get("altitude") - previous_altitude) / time_diff
                        current_dt_val = (datetime.datetime.combine(datetime.date.today(), fix_time)
                                          if fix_time else None)

                        c.execute("""
                            INSERT INTO igc_data (
                                flight_id, sequence, time, latitude, longitude, altitude,
                                speed, vertical_speed, wind_dir, wind_spd,
                                weather_temp, weather_dwpt, weather_rhum, weather_prcp, weather_snow,
                                weather_wpgt, weather_pres, weather_tsun, weather_coco
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            flight_db_id,
                            seq,
                            fix_time_str,
                            fix.get("latitude"),
                            fix.get("longitude"),
                            fix.get("altitude"),
                            speed,
                            vertical_speed,
                            weather_wdir,
                            weather_wspd,
                            weather_temp,
                            weather_dwpt,
                            weather_rhum,
                            weather_prcp,
                            weather_snow,
                            weather_wpgt,
                            weather_pres,
                            weather_tsun,
                            weather_coco
                        ))
                        fix_id = c.lastrowid
                        fix_ids.append(fix_id)

                        previous_dt = current_dt_val
                        previous_lat = fix.get("latitude")
                        previous_lon = fix.get("longitude")
                        previous_altitude = fix.get("altitude")

                    # Update each fix record with references to the previous and next fixes.
                    for idx, fix_id in enumerate(fix_ids):
                        prev_id = fix_ids[idx - 1] if idx > 0 else None
                        next_id = fix_ids[idx + 1] if idx < len(fix_ids) - 1 else None
                        c.execute("""
                            UPDATE igc_data
                            SET previous_fix_id = ?, next_fix_id = ?
                            WHERE id = ?
                        """, (prev_id, next_id, fix_id))

                    stats.igc_fixes_processed += len(fix_ids)

        conn.commit()
        stats.flights_processed += 1
        return True

    except sqlite3.Error as e:
        logger.error(f"Database error processing flight {flight_id}: {e}")
        conn.rollback()
        stats.flights_failed += 1
        raise
    except Exception as e:
        logger.error(f"Unexpected error processing flight {flight_id}: {e}")
        conn.rollback()
        stats.flights_failed += 1
        raise


def parse_arguments() -> argparse.Namespace:
    """
    Parse command line arguments.

    Returns:
        Parsed arguments
    """
    parser = argparse.ArgumentParser(
        description='Store IGC flight data with weather enrichment',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s
  %(prog)s --db-path my_flights.db --input-dir ./data
  %(prog)s --resume --verbose
  %(prog)s --dry-run --verbose
  %(prog)s --strict
        """
    )

    parser.add_argument(
        '--db-path',
        type=str,
        default='data/flights.db',
        help='Path to SQLite database (default: flights.db)'
    )

    parser.add_argument(
        '--input-dir',
        type=str,
        default='data/icg_files',
        help='Input directory containing flight folders (default: icg_files)'
    )

    parser.add_argument(
        '--resume',
        action='store_true',
        help='Resume from previous run (skip already processed folders)'
    )

    parser.add_argument(
        '--strict',
        action='store_true',
        help='Strict mode: abort on any error (default: continue on errors)'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Dry run: show what would be processed without making changes'
    )

    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Verbose logging (DEBUG level)'
    )

    parser.add_argument(
        '--quiet', '-q',
        action='store_true',
        help='Quiet mode: only show errors'
    )

    return parser.parse_args()


def main():
    """Main execution function."""
    # Parse arguments
    args = parse_arguments()

    # Determine log level
    log_level = logging.INFO
    if args.verbose:
        log_level = logging.DEBUG
    elif args.quiet:
        log_level = logging.ERROR

    # Setup logging
    logger = setup_logging(log_level)

    try:
        # Create config
        logger.info("Loading configuration")
        config = Config.from_args(args)

        # Validate
        errors = config.validate()
        if errors:
            for error in errors:
                logger.error(f"Configuration error: {error}")
            sys.exit(1)

        # Dry run info
        if config.dry_run:
            logger.info("DRY RUN MODE - No changes will be made")

        # Connect to database
        logger.info(f"Connecting to database: {config.db_path}")
        conn = sqlite3.connect(config.db_path)

        # Create tables
        if not config.dry_run:
            create_tables(conn)
            logger.info("Database tables ready")

        # Get list of folders to process
        all_folders = sorted([
            f for f in os.listdir(config.input_dir)
            if os.path.isdir(os.path.join(config.input_dir, f))
        ])
        logger.info(f"Found {len(all_folders)} folders in {config.input_dir}")

        # Check for resume
        processed_folders = set()
        if config.resume:
            processed_folders = get_processed_folders(conn)
            logger.info(f"Resume mode: {len(processed_folders)} folders already processed")

        # Filter folders
        folders_to_process = [
            f for f in all_folders
            if f not in processed_folders
        ]
        logger.info(f"Will process {len(folders_to_process)} folders")

        if config.dry_run:
            logger.info("Dry run - folders that would be processed:")
            for folder in folders_to_process[:10]:  # Show first 10
                logger.info(f"  - {folder}")
            if len(folders_to_process) > 10:
                logger.info(f"  ... and {len(folders_to_process) - 10} more")
            sys.exit(0)

        # Process folders with progress bar
        stats = ProcessingStats()

        for folder in tqdm(folders_to_process, desc="Processing folders"):
            folder_path = os.path.join(config.input_dir, folder)

            try:
                success = process_directory(
                    folder_path, conn, logger, stats, config.strict_mode
                )

                if success:
                    mark_folder_processed(conn, folder, 'completed')
                else:
                    mark_folder_processed(conn, folder, 'skipped')

            except Exception as e:
                logger.error(f"Fatal error processing {folder}: {e}")
                mark_folder_processed(conn, folder, 'failed', str(e))
                if config.strict_mode:
                    raise

        # Close database
        conn.close()

        # Print summary
        stats.print_summary(logger)

        # Exit with error code if failures
        if stats.folders_failed > 0 or stats.flights_failed > 0:
            sys.exit(1)

    except KeyboardInterrupt:
        logger.warning("Process interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
