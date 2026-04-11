"""
IGC File Fetcher for GlidingApp API

This script fetches IGC (GPS track) files from the GlidingApp API for a specified
date range. It handles authentication, filters days with actual flight data, and
downloads all IGC files with proper rate limiting.

Configuration:
    All credentials and configuration should be stored in a .env file.
    See .env.example for required variables.

Usage:
    python fetch_igc_files.py --min-date 2025-08-13 --max-date 2025-09-02
    python fetch_igc_files.py --min-date 2025-08-13 --max-date 2025-09-02 --verbose

Requirements:
    - python-dotenv
    - requests (already used by gliding_api.py)

Security:
    - Never commit the .env file to version control
    - Keep credentials secure and rotate them regularly
    - Use token-based auth when possible (more secure than password)
"""

import os
import sys
import time
import argparse
import logging
from datetime import datetime
from dotenv import load_dotenv
import requests

sys.path.insert(0, os.path.dirname(__file__))
from gliding_api import GlidingAPI


class Config:
    """Configuration management using environment variables."""

    def __init__(self):
        """Load configuration from environment variables."""
        # Load .env file
        load_dotenv()

        # API Configuration
        self.base_url = os.getenv('GLIDING_APP_BASE_URL', 'https://admin.gliding.app')
        self.version = os.getenv('GLIDING_APP_VERSION', '4.1.2')

        # Authentication
        self.email = os.getenv('GLIDING_APP_EMAIL')
        self.password = os.getenv('GLIDING_APP_PASSWORD')
        self.client_secret = os.getenv('GLIDING_APP_CLIENT_SECRET')
        self.access_token = os.getenv('GLIDING_APP_ACCESS_TOKEN')
        self.use_token_auth = os.getenv('GLIDING_APP_USE_TOKEN_AUTH', 'false').lower() == 'true'

        # Download Configuration
        self.output_directory = os.getenv('OUTPUT_DIRECTORY', 'icg_files')
        self.rate_limit_seconds = float(os.getenv('RATE_LIMIT_SECONDS', '4'))

    def validate(self):
        """
        Validate required configuration values.

        Raises:
            ValueError: If required configuration is missing
        """
        errors = []

        if self.use_token_auth and not self.access_token:
            errors.append("GLIDING_APP_ACCESS_TOKEN is required when using token authentication")

        if not self.use_token_auth:
            if not self.email:
                errors.append("GLIDING_APP_EMAIL is required for login authentication")
            if not self.password:
                errors.append("GLIDING_APP_PASSWORD is required for login authentication")
            if not self.client_secret:
                errors.append("GLIDING_APP_CLIENT_SECRET is required for login authentication")

        if errors:
            raise ValueError("Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors))


def setup_logging(log_level=logging.INFO):
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
            logging.FileHandler('logs/fetch_igc_files.log', mode='a')
        ]
    )
    return logging.getLogger(__name__)


def validate_date_range(min_date: str, max_date: str, date_format: str = "%Y-%m-%d"):
    """
    Validate and parse date range.

    Args:
        min_date: Start date string
        max_date: End date string
        date_format: Expected date format (default: YYYY-MM-DD)

    Returns:
        Tuple of (min_date_obj, max_date_obj) as date objects

    Raises:
        ValueError: If dates are invalid or range is invalid
    """
    try:
        min_dt = datetime.strptime(min_date, date_format).date()
    except ValueError as e:
        raise ValueError(f"Invalid min_date '{min_date}': {e}")

    try:
        max_dt = datetime.strptime(max_date, date_format).date()
    except ValueError as e:
        raise ValueError(f"Invalid max_date '{max_date}': {e}")

    if min_dt > max_dt:
        raise ValueError(f"min_date ({min_date}) must be before or equal to max_date ({max_date})")

    # Warn if range is very large (more than 365 days)
    days_diff = (max_dt - min_dt).days
    if days_diff > 365:
        logger = logging.getLogger(__name__)
        logger.warning(f"Date range is large ({days_diff} days). This may take a long time.")

    return min_dt, max_dt


def get_token(gliding_app: GlidingAPI, config: Config, logger: logging.Logger):
    """
    Get access token either from config or by performing login.

    Args:
        gliding_app: GlidingAPI instance
        config: Configuration object
        logger: Logger instance

    Returns:
        Tuple of (access_token, success: bool)
    """
    if config.use_token_auth:
        logger.info("Using pre-configured access token")
        return config.access_token, True

    logger.info("Attempting login authentication")
    try:
        login_response = gliding_app.login(
            email=config.email,
            password=config.password,
            client_secret=config.client_secret
        )
        access_token = login_response.get("access_token")
        if not access_token:
            logger.error("No 'access_token' found in login response")
            return None, False

        logger.info("Successfully logged in and retrieved access token")
        return access_token, True

    except requests.exceptions.RequestException as e:
        logger.error(f"Network error during login: {e}")
        return None, False
    except Exception as e:
        logger.error(f"Unexpected error during login: {e}")
        return None, False


def find_days_in_range_with_minutes(
    days_data: dict,
    min_date: str,
    max_date: str,
    logger: logging.Logger = None
):
    """
    Find days within date range that have flights with total minutes > 0.

    Args:
        days_data: Dictionary containing 'days' and 'totals' from API
        min_date: Start date in YYYY-MM-DD format
        max_date: End date in YYYY-MM-DD format
        logger: Optional logger instance

    Returns:
        List of dag_ids that match criteria

    Raises:
        ValueError: If days_data is missing required keys
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    # Validate input structure
    if not isinstance(days_data, dict):
        raise ValueError("days_data must be a dictionary")
    if "days" not in days_data:
        raise ValueError("days_data missing 'days' key")
    if "totals" not in days_data:
        raise ValueError("days_data missing 'totals' key")

    logger.debug(f"Processing {len(days_data.get('days', []))} days")
    logger.debug(f"Processing {len(days_data.get('totals', []))} total entries")

    # Convert min_date, max_date from strings to datetime for easy comparison
    fmt = "%Y-%m-%d"
    min_dt = datetime.strptime(min_date, fmt).date()
    max_dt = datetime.strptime(max_date, fmt).date()

    # 1) Build a dictionary of day_id -> date
    day_dates = {}
    for day_entry in days_data.get("days", []):
        dag_id = day_entry["dag_id"]
        # parse the 'datum' into a date
        d_str = day_entry["datum"]  # e.g. "2025-11-02"
        d_dt = datetime.strptime(d_str, fmt).date()
        day_dates[dag_id] = d_dt

    # 2) Build a dictionary of day_id -> total_minutes
    day_minutes = {}
    for total_entry in days_data.get("totals", []):
        dag_id = total_entry["dag_id"]
        minutes = total_entry.get("minutes") or 0
        if dag_id not in day_minutes:
            day_minutes[dag_id] = 0
        day_minutes[dag_id] += minutes

    # 3) Filter for day_id with date in range [min_dt, max_dt] AND total_minutes > 0
    matching_dag_ids = []
    for dag_id, date_val in day_dates.items():
        if min_dt <= date_val <= max_dt:
            total_mins = day_minutes.get(dag_id, 0)
            if total_mins > 0:
                matching_dag_ids.append(dag_id)

    logger.info(f"Found {len(matching_dag_ids)} matching days")
    return matching_dag_ids


def fetch_files(min_date: str, max_date: str, config: Config, logger: logging.Logger):
    """
    Fetch IGC files for flights within the specified date range.

    Args:
        min_date: Start date in YYYY-MM-DD format
        max_date: End date in YYYY-MM-DD format
        config: Configuration object
        logger: Logger instance

    Returns:
        Dictionary with statistics: {
            'days_processed': int,
            'days_failed': int,
            'days_skipped': int
        }

    Raises:
        ValueError: If date validation fails
        RuntimeError: If authentication fails
    """
    # Validate date range
    try:
        min_dt, max_dt = validate_date_range(min_date, max_date)
    except ValueError as e:
        logger.error(f"Date validation failed: {e}")
        raise

    # Initialize API client
    gliding_app = GlidingAPI(
        base_url=config.base_url,
        version=config.version
    )

    # Get access token
    access_token, success = get_token(gliding_app, config, logger)
    if not success or not access_token:
        raise RuntimeError("Failed to obtain access token")

    # Fetch days data
    try:
        logger.info("Fetching days data from API")
        days_data = gliding_app.get_days(access_token)
        logger.info("Successfully retrieved days data")
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error fetching days data: {e}")
        raise
    except Exception as e:
        logger.error(f"Error getting days data: {e}")
        raise

    # Find days in range with flights
    try:
        results = find_days_in_range_with_minutes(
            days_data,
            min_date=min_date,
            max_date=max_date,
            logger=logger
        )
        logger.info(f"Found {len(results)} days in range with flights")
    except Exception as e:
        logger.error(f"Error filtering days: {e}")
        raise

    # Process each day
    stats = {
        'days_processed': 0,
        'days_failed': 0,
        'days_skipped': 0
    }

    for idx, day_entry in enumerate(days_data["days"]):
        if day_entry["dag_id"] not in results:
            stats['days_skipped'] += 1
            continue

        dag_id = day_entry["dag_id"]
        datum = day_entry.get("datum", "unknown")

        logger.info(f"Processing day {stats['days_processed'] + 1}/{len(results)}: {datum} (dag_id={dag_id})")

        try:
            gliding_app.download_all_igcs_for_day(
                day_id=dag_id,
                access_token=access_token
            )
            stats['days_processed'] += 1
            logger.info(f"Successfully processed day {datum}")

            # Rate limiting between days
            if stats['days_processed'] < len(results):  # Don't sleep after last day
                logger.debug(f"Rate limiting: waiting {config.rate_limit_seconds} seconds")
                time.sleep(config.rate_limit_seconds)

        except requests.exceptions.RequestException as e:
            logger.error(f"Network error downloading IGCs for day {datum}: {e}")
            stats['days_failed'] += 1
        except Exception as e:
            logger.error(f"Unexpected error processing day {datum}: {e}")
            stats['days_failed'] += 1

    logger.info(f"Processing complete. Stats: {stats}")
    return stats


def parse_arguments():
    """
    Parse command line arguments.

    Returns:
        Namespace with parsed arguments
    """
    parser = argparse.ArgumentParser(
        description='Fetch IGC files from GlidingApp API',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --min-date 2025-08-13 --max-date 2025-09-02
  %(prog)s --min-date 2025-08-13 --max-date 2025-09-02 --verbose
  %(prog)s --min-date 2025-08-13 --max-date 2025-09-02 --output-dir ./my_files
        """
    )

    parser.add_argument(
        '--min-date',
        type=str,
        required=True,
        help='Start date in YYYY-MM-DD format'
    )

    parser.add_argument(
        '--max-date',
        type=str,
        required=True,
        help='End date in YYYY-MM-DD format'
    )

    parser.add_argument(
        '--output-dir',
        type=str,
        help='Output directory for downloaded files (overrides .env)'
    )

    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose logging (DEBUG level)'
    )

    parser.add_argument(
        '--quiet', '-q',
        action='store_true',
        help='Quiet mode - only show errors (ERROR level)'
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
        # Load and validate configuration
        logger.info("Loading configuration")
        config = Config()

        # Override output directory if specified
        if args.output_dir:
            config.output_directory = args.output_dir
            logger.info(f"Output directory overridden to: {args.output_dir}")

        # Validate configuration
        config.validate()

        # Execute fetch
        logger.info(f"Starting fetch from {args.min_date} to {args.max_date}")
        stats = fetch_files(args.min_date, args.max_date, config, logger)

        # Print summary
        logger.info("=" * 50)
        logger.info("FETCH COMPLETE")
        logger.info(f"  Days processed: {stats['days_processed']}")
        logger.info(f"  Days failed: {stats['days_failed']}")
        logger.info(f"  Days skipped: {stats['days_skipped']}")
        logger.info("=" * 50)

        # Exit with error code if any failures
        if stats['days_failed'] > 0:
            sys.exit(1)

    except ValueError as e:
        logger.error(f"Configuration or validation error: {e}")
        sys.exit(1)
    except RuntimeError as e:
        logger.error(f"Runtime error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.warning("Process interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
