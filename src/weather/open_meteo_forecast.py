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
