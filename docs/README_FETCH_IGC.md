# IGC File Fetcher

## Overview
This tool fetches IGC (GPS track) files from the GlidingApp API for specified date ranges.

## Setup

### 1. Install Dependencies
```bash
source .venv/bin/activate  # Activate virtual environment
pip install python-dotenv
```

### 2. Configure Environment Variables
Copy the example file and fill in your credentials:
```bash
cp .env.example .env
```

Edit `.env` and add your actual credentials:
- `GLIDING_APP_EMAIL`: Your GlidingApp email
- `GLIDING_APP_PASSWORD`: Your GlidingApp password
- `GLIDING_APP_CLIENT_SECRET`: API client secret
- `GLIDING_APP_ACCESS_TOKEN`: Pre-existing access token (optional)

### 3. Security Note
**IMPORTANT**: Never commit the `.env` file to version control. It contains sensitive credentials.

## Usage

### Basic Usage
```bash
python fetch_igc_files.py --min-date 2025-08-13 --max-date 2025-09-02
```

### Verbose Mode
```bash
python fetch_igc_files.py --min-date 2025-08-13 --max-date 2025-09-02 --verbose
```

### Custom Output Directory
```bash
python fetch_igc_files.py --min-date 2025-08-13 --max-date 2025-09-02 --output-dir ./my_files
```

### Quiet Mode (Errors Only)
```bash
python fetch_igc_files.py --min-date 2025-08-13 --max-date 2025-09-02 --quiet
```

## Features

### Security
- Credentials stored in `.env` file, not in code
- Environment variables for all sensitive data
- Token-based authentication option

### Reliability
- Comprehensive error handling
- Validation of date ranges
- Graceful degradation on failures
- Retry logic for network errors (inherited from gliding_api.py)

### Logging
- Detailed logging to both console and file (`fetch_igc_files.log`)
- Multiple log levels: DEBUG, INFO, ERROR
- Timestamps and structured messages

### Rate Limiting
- Configurable delays between day processing
- Prevents API throttling
- Default: 4 seconds between days

### Input Validation
- Date format validation
- Date range validation (min before max)
- Warning for large date ranges

## Output

### Directory Structure
```
icg_files/
├── 2025-08-13_1234/
│   ├── flights_2025-08-13.json
│   ├── flight1.igc
│   └── flight2.igc
└── 2025-08-14_1235/
    ├── flights_2025-08-14.json
    └── flight3.igc
```

### Log File
Execution logs are written to `fetch_igc_files.log`.

## Configuration Options

See `.env.example` for all available configuration options:

- `GLIDING_APP_BASE_URL`: API base URL (default: https://admin.gliding.app)
- `GLIDING_APP_VERSION`: API version (default: 4.1.2)
- `GLIDING_APP_EMAIL`: Your email for login
- `GLIDING_APP_PASSWORD`: Your password
- `GLIDING_APP_CLIENT_SECRET`: API client secret
- `GLIDING_APP_ACCESS_TOKEN`: Pre-existing token (optional)
- `GLIDING_APP_USE_TOKEN_AUTH`: Use token instead of login (true/false)
- `OUTPUT_DIRECTORY`: Where to save files (default: icg_files)
- `RATE_LIMIT_SECONDS`: Delay between days (default: 4)

## Error Handling

The script handles various error conditions:
- **Invalid dates**: Clear error message with format requirements
- **Date range issues**: Validation that min < max
- **Authentication failures**: Detailed logging of login issues
- **Network errors**: Retry and logging
- **Missing data**: Graceful skipping with logging

Exit codes:
- `0`: Success
- `1`: Error (configuration, runtime, or processing failures)
- `130`: Interrupted by user (Ctrl+C)

## Troubleshooting

### Authentication Errors
1. Verify credentials in `.env` file
2. Try using token-based auth instead of login
3. Check if access token is still valid

### Network Errors
1. Check internet connection
2. Verify API endpoint is accessible
3. Review rate limiting settings

### Date Range Issues
1. Ensure dates are in YYYY-MM-DD format
2. Verify min_date is before max_date
3. Check for typos in dates

## Development

### Running Tests
```bash
# Test with verbose logging
python fetch_igc_files.py --min-date 2025-08-13 --max-date 2025-08-13 --verbose

# Test with single day
python fetch_igc_files.py --min-date 2025-08-13 --max-date 2025-08-13
```

### Backward Compatibility
This refactored version maintains the same core functionality as the original script.
The main differences are:
- Credentials now in `.env` instead of hardcoded
- CLI arguments required instead of hardcoded in main
- Better error handling and logging
- Input validation

### Migration from Old Version
1. Extract credentials from old script
2. Add them to `.env` file
3. Update any scripts that call this one to use CLI arguments
4. Test with small date range first

## Programmatic Usage

You can also import and use the functions programmatically:

```python
from fetch_igc_files import fetch_files, Config, setup_logging

logger = setup_logging()
config = Config()
config.validate()
stats = fetch_files("2025-08-13", "2025-09-02", config, logger)
print(f"Processed {stats['days_processed']} days")
```
