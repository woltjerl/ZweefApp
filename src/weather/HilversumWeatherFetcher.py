"""
Weather Data Fetcher for Hilversum Airport

Fetches hourly weather data from Meteostat for locations near Hilversum Airport.
Includes automatic dew point calculation, retry logic, and robust error handling.

Usage:
    fetcher = HilversumWeatherFetcher(logger=logger)
    weather_data = fetcher.fetch_wind(date(2025, 3, 16))

Features:
    - Automatic retry logic with exponential backoff
    - Dew point calculation using Magnus formula
    - Pandas NA/NaN handling for SQLite compatibility
    - Comprehensive logging and error handling

Dependencies:
    - meteostat >= 2.1.4
    - pandas
"""

import math
import time
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from meteostat import Point, stations, hourly


class HilversumWeatherFetcher:
    """
    Fetch weather data from Meteostat for specified location.

    Automatically finds nearby weather stations and fetches hourly data
    with calculated dew points and error handling.
    """

    def __init__(
        self,
        latitude: float = 52.191,
        longitude: float = 5.146,
        station_limit: int = 10,
        logger: Optional[logging.Logger] = None
    ):
        """
        Initialize weather fetcher for specified location.

        Args:
            latitude: Location latitude (default: 52.191 - Hilversum)
            longitude: Location longitude (default: 5.146 - Hilversum)
            station_limit: Maximum number of nearby stations to query (default: 10)
            logger: Optional logger instance (creates default if not provided)

        Raises:
            ValueError: If no nearby weather stations found or invalid coordinates
        """
        self.logger = logger or logging.getLogger(__name__)

        # Validate coordinates
        if not (-90 <= latitude <= 90):
            raise ValueError(f"Invalid latitude: {latitude} (must be between -90 and 90)")
        if not (-180 <= longitude <= 180):
            raise ValueError(f"Invalid longitude: {longitude} (must be between -180 and 180)")

        # Create Point object for location
        self.logger.debug(f"Creating location point: lat={latitude}, lon={longitude}")
        location = Point(latitude, longitude)

        # Get nearby stations
        try:
            self.nearby_stations = stations.nearby(location).head(station_limit)
        except Exception as e:
            self.logger.error(f"Failed to query nearby stations: {e}")
            raise ValueError(f"Cannot query weather stations: {e}") from e

        if self.nearby_stations.empty:
            self.logger.error(f"No weather stations found near ({latitude}, {longitude})")
            raise ValueError("No nearby weather stations found")

        best_station = self.nearby_stations.iloc[0]
        self.station_id = self.nearby_stations.index[0]
        station_name = best_station["name"]

        distance_in_meters = best_station["distance"]
        distance_km = distance_in_meters / 1000.0

        self.logger.info(
            f"Using weather station '{station_name}' (ID: {self.station_id}), "
            f"located {distance_km:.2f} km from target location"
        )

    def _calculate_dew_point(self, temp: Optional[float], rhum: Optional[float]) -> Optional[float]:
        """
        Calculate dew point temperature using Magnus formula.

        Args:
            temp: Temperature in °C
            rhum: Relative humidity in %

        Returns:
            Dew point temperature in °C, or None if calculation fails

        Note:
            Uses Magnus-Tetens formula accurate to ±0.4°C for:
            - Temperature: -40°C to 50°C
            - Humidity: 1% to 100%
        """
        if temp is None or rhum is None:
            return None

        try:
            # Magnus formula constants
            a = 17.27
            b = 237.7

            alpha = ((a * temp) / (b + temp)) + math.log(rhum / 100.0)
            dwpt = (b * alpha) / (a - alpha)

            return round(dwpt, 1)
        except (ValueError, ZeroDivisionError) as e:
            self.logger.warning(f"Dew point calculation failed for temp={temp}, rhum={rhum}: {e}")
            return None

    def _safe_get(self, value: Any) -> Optional[float]:
        """
        Convert pandas NA/NaN to None for SQLite compatibility.

        Args:
            value: Value from pandas DataFrame

        Returns:
            Float value or None
        """
        if value is None:
            return None
        try:
            import pandas as pd
            if pd.isna(value):
                return None
            return float(value) if isinstance(value, (int, float)) else value
        except (TypeError, ValueError):
            return None

    def _parse_weather_dataframe(self, df) -> List[Dict[str, Any]]:
        """
        Parse weather dataframe into list of dictionaries.

        Args:
            df: Pandas DataFrame from meteostat

        Returns:
            List of weather data dictionaries with calculated dew points
        """
        weather_data = []
        for timestamp, row in df.iterrows():
            # Calculate dew point from temperature and humidity
            temp = self._safe_get(row.get("temp"))
            rhum = self._safe_get(row.get("rhum"))
            dwpt = self._calculate_dew_point(temp, rhum) if (temp is not None and rhum is not None) else None

            weather_data.append({
                "time": timestamp.to_pydatetime().isoformat(),
                "temp": temp,
                "dwpt": dwpt,  # Calculated, not provided by API
                "rhum": rhum,
                "prcp": self._safe_get(row.get("prcp")),
                "snow": self._safe_get(row.get("snwd")),  # snwd is snow water equivalent
                "wdir": self._safe_get(row.get("wdir")),
                "wspd": self._safe_get(row.get("wspd")),
                "wpgt": self._safe_get(row.get("wpgt")),
                "pres": self._safe_get(row.get("pres")),
                "tsun": self._safe_get(row.get("tsun")),
                "coco": self._safe_get(row.get("coco"))
            })
        return weather_data

    def fetch_wind(
        self,
        date,
        max_retries: int = 3,
        timeout: int = 30
    ) -> List[Dict[str, Any]]:
        """
        Fetch hourly weather data for a given date with retry logic.

        Args:
            date: Date to fetch (datetime.date or datetime.datetime)
            max_retries: Maximum number of retry attempts (default: 3)
            timeout: Request timeout in seconds (default: 30, currently unused by meteostat)

        Returns:
            List of dictionaries containing hourly weather data

        Raises:
            ValueError: If date is invalid
            RuntimeError: If all retry attempts fail

        Example:
            >>> fetcher = HilversumWeatherFetcher()
            >>> data = fetcher.fetch_wind(date(2025, 3, 16))
            >>> len(data)
            24
        """
        # Date validation and conversion
        if isinstance(date, datetime):
            day_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            try:
                day_start = datetime(date.year, date.month, date.day)
            except (AttributeError, ValueError) as e:
                self.logger.error(f"Invalid date format: {date}")
                raise ValueError(f"Invalid date format: {e}") from e

        day_end = day_start + timedelta(days=1) - timedelta(seconds=1)

        # Retry loop with exponential backoff
        for attempt in range(1, max_retries + 1):
            try:
                self.logger.debug(
                    f"Fetching weather data for {day_start.date()} "
                    f"(attempt {attempt}/{max_retries})"
                )

                # Note: meteostat doesn't directly support timeout parameter
                # but we wrap in try/except to handle network issues
                data = hourly(self.station_id, day_start, day_end)
                df = data.fetch()

                if df.empty:
                    self.logger.warning(
                        f"No weather data returned for {day_start.date()}"
                    )
                    return []

                weather_data = self._parse_weather_dataframe(df)
                self.logger.info(
                    f"Successfully fetched {len(weather_data)} hourly records "
                    f"for {day_start.date()}"
                )
                return weather_data

            except Exception as e:
                self.logger.warning(
                    f"Attempt {attempt}/{max_retries} failed for {day_start.date()}: {e}"
                )

                if attempt == max_retries:
                    self.logger.error(
                        f"All {max_retries} attempts failed for {day_start.date()}"
                    )
                    raise RuntimeError(
                        f"Failed to fetch weather data after {max_retries} attempts: {e}"
                    ) from e

                # Exponential backoff: 1s, 2s, 4s...
                wait_time = 2 ** (attempt - 1)
                self.logger.debug(f"Waiting {wait_time}s before retry")
                time.sleep(wait_time)

        # Should never reach here due to raise in loop
        return []


# Quick test
if __name__ == "__main__":
    from datetime import date

    # Setup logging for test
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    fetcher = HilversumWeatherFetcher()
    results = fetcher.fetch_wind(date(2025, 3, 16))

    print(f"\nFetched {len(results)} weather records:")
    for i, record in enumerate(results[:3]):  # Show first 3
        print(f"  {i+1}. {record}")
    if len(results) > 3:
        print(f"  ... and {len(results) - 3} more records")
