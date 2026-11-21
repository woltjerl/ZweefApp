"""
Fetch weather forecasts from Open-Meteo API.

Open-Meteo provides free 7-day hourly forecasts (no API key required).
Use this for future dates instead of Meteostat (which only has historical data).
"""

import requests
from datetime import date
from typing import Optional, Dict, List


# Hilversum Airport coordinates
EHHV_LAT = 52.191
EHHV_LON = 5.146

# Time window hour ranges
TIME_RANGES = {
    'morning': (9, 12),
    'midday': (12, 15),
    'afternoon': (15, 18),
}


def fetch_open_meteo_hourly(target_date: date) -> List[Dict]:
    """
    Fetch hourly forecast data from Open-Meteo for a given date.

    Returns list of dicts with keys: time, temp, wdir, wspd, rhum
    (matching HilversumWeatherFetcher output format).
    """
    resp = requests.get('https://api.open-meteo.com/v1/forecast', params={
        'latitude': EHHV_LAT,
        'longitude': EHHV_LON,
        'hourly': 'temperature_2m,wind_speed_10m,wind_direction_10m,relative_humidity_2m,surface_pressure',
        'forecast_days': 7,
        'timezone': 'Europe/Amsterdam',
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    hourly = data['hourly']
    target_str = str(target_date)
    records = []

    for i, ts in enumerate(hourly['time']):
        if not ts.startswith(target_str):
            continue
        records.append({
            'time': ts,
            'temp': hourly['temperature_2m'][i],
            'wdir': hourly['wind_direction_10m'][i],
            'wspd': hourly['wind_speed_10m'][i],
            'rhum': hourly.get('relative_humidity_2m', [None] * len(hourly['time']))[i],
            'pres': hourly.get('surface_pressure', [None] * len(hourly['time']))[i],
        })

    return records


# ── Convection variables (first-thermal pipeline) ──
# Same variables for history (historical-forecast API) and prediction (forecast API),
# so the model is trained on the same kind of data it is later fed.
CONVECTION_VARS = {
    'boundary_layer_height': 'blh',
    'cape': 'cape',
    'sensible_heat_flux': 'shf',
    'shortwave_radiation': 'sw',
    'direct_radiation': 'direct',
    'cloud_cover_low': 'cl_low',
    'cloud_cover_mid': 'cl_mid',
    'temperature_2m': 't2m',
    'dew_point_2m': 'td2m',
    'wind_speed_100m': 'ws100',
    'wind_direction_100m': 'wd100',
    'wind_speed_10m': 'ws10',
    'wind_direction_10m': 'wd10',
    'precipitation': 'precip',
    'soil_moisture_0_to_1cm': 'soil',
}
CONVECTION_COLUMNS = list(CONVECTION_VARS.values())

ENDPOINTS = {
    'forecast': 'https://api.open-meteo.com/v1/forecast',
    'historical_forecast': 'https://historical-forecast-api.open-meteo.com/v1/forecast',
    'archive': 'https://archive-api.open-meteo.com/v1/archive',
}


def fetch_convection(endpoint: str, start_date: Optional[date] = None, end_date: Optional[date] = None,
                     variables: Optional[Dict[str, str]] = None, past_days: int = 0,
                     forecast_days: int = 7, timeout: int = 120) -> List[Dict]:
    """
    Fetch hourly convection variables (UTC) from an Open-Meteo endpoint.

    Returns a list of dicts: {'date': 'YYYY-MM-DD', 'hour_utc': int, <short names>...}.
    """
    variables = variables or CONVECTION_VARS
    params = {
        'latitude': EHHV_LAT,
        'longitude': EHHV_LON,
        'hourly': ','.join(variables.keys()),
        'timezone': 'UTC',
    }
    if start_date is not None:
        params['start_date'] = str(start_date)
        params['end_date'] = str(end_date or start_date)
    else:
        params['forecast_days'] = forecast_days
        if past_days:
            params['past_days'] = past_days

    resp = requests.get(ENDPOINTS[endpoint], params=params, timeout=timeout)
    resp.raise_for_status()
    hourly = resp.json()['hourly']

    rows = []
    for i, ts in enumerate(hourly['time']):
        row = {'date': ts[:10], 'hour_utc': int(ts[11:13])}
        for api_name, short in variables.items():
            vals = hourly.get(api_name)
            row[short] = vals[i] if vals is not None else None
        rows.append(row)
    return rows


def fetch_convection_forecast(past_days: int = 3, forecast_days: int = 7) -> List[Dict]:
    """Convection variables for the coming days (plus a few past days for rain history)."""
    return fetch_convection('forecast', past_days=past_days, forecast_days=forecast_days, timeout=30)


def get_forecast_for_window(target_date: date, time_window: str) -> Optional[Dict]:
    """
    Get averaged forecast for a specific date and time window.

    Returns dict with wind_dir, wind_speed, temp, humidity, pressure
    or None if no data available.
    """
    hour_start, hour_end = TIME_RANGES[time_window]

    records = fetch_open_meteo_hourly(target_date)

    wind_dirs = []
    wind_speeds = []
    temps = []
    humidities = []
    pressures = []

    for r in records:
        hour = int(r['time'].split('T')[1].split(':')[0])
        if hour_start <= hour < hour_end:
            if r['wdir'] is not None:
                wind_dirs.append(r['wdir'])
            if r['wspd'] is not None:
                wind_speeds.append(r['wspd'])
            if r['temp'] is not None:
                temps.append(r['temp'])
            if r.get('rhum') is not None:
                humidities.append(r['rhum'])
            if r.get('pres') is not None:
                pressures.append(r['pres'])

    if not wind_dirs or not wind_speeds:
        return None

    def avg(lst):
        return sum(lst) / len(lst) if lst else None

    return {
        'wind_dir': avg(wind_dirs),
        'wind_speed': avg(wind_speeds),
        'temp': round(avg(temps), 1) if temps else None,
        'humidity': round(avg(humidities), 1) if humidities else None,
        'pressure': round(avg(pressures), 1) if pressures else None,
    }
