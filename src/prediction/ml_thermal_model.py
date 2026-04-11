#!/usr/bin/env python3
"""
ML Thermal Prediction - Model Training

Trains three machine learning models from historical flight data:

1. Success model: P(finding thermal | weather, runway)
   - Binary classifier: will I find a thermal after winch release?

2. Navigation model: bearing + distance to thermal
   - Two regressors predicting where to fly after release

3. Spatial model: P(thermal at location | terrain, weather)
   - Grid-based predictor using terrain features + weather
   - Pre-computes terrain grid for fast prediction

Usage:
    python ml_thermal_model.py                  # Train all models
    python ml_thermal_model.py --evaluate       # Train with detailed evaluation
    python ml_thermal_model.py --force          # Retrain even if models exist
"""

import sqlite3
import math
import os
import sys
import pickle
import argparse
import time
from datetime import datetime

import numpy as np
import pandas as pd
import requests
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.inspection import permutation_importance
from sklearn.metrics import (
    accuracy_score, classification_report, roc_auc_score,
    mean_absolute_error, r2_score
)

# Airport center (from GPS data analysis)
AIRPORT_LAT = 52.193650
AIRPORT_LON = 5.145217

# Grid parameters
GRID_SIZE_M = 50        # Cell size in meters
MAX_RADIUS_KM = 4.0     # Reachable range from winch release
DISTANCE_CAP_KM = 10.0  # Cap terrain distances at 10km

# Terrain type categories (fixed order for encoding)
TERRAIN_TYPES = ['field', 'forest', 'forest_edge', 'urban', 'urban_edge', 'water']


def circular_encode(degrees):
    """Encode angle in degrees as (sin, cos) pair."""
    rad = np.radians(degrees)
    return np.sin(rad), np.cos(rad)


def circular_encode_series(series):
    """Encode pandas Series of angles as sin/cos columns."""
    rad = np.radians(series.astype(float))
    return np.sin(rad), np.cos(rad)


def haversine_distance(lat1, lon1, lat2, lon2):
    """Distance in km between two points."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def bearing_from_airport(lat, lon):
    """Bearing in degrees from airport to point."""
    lat1 = math.radians(AIRPORT_LAT)
    lat2 = math.radians(lat)
    dlon = math.radians(lon - AIRPORT_LON)
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


class ThermalMLModel:
    """Machine learning models for thermal prediction."""

    def __init__(self, db_path='data/flights.db', model_dir='data/models'):
        self.db_path = db_path
        self.model_dir = model_dir

        # Models
        self.success_model = None
        self.bearing_model = None
        self.distance_model = None
        self.spatial_model = None

        # Pre-computed data
        self.terrain_grid = None
        self.grid_meta = None  # Grid geometry metadata

        # Feature info
        self.feature_importances = {}
        self.metrics = {}
        self.training_date = None

    # ── Feature Engineering ──

    def _build_flight_features(self, df):
        """Build feature matrix from flight-level data."""
        X = pd.DataFrame()

        # Wind (circular)
        X['wind_dir_sin'], X['wind_dir_cos'] = circular_encode_series(df['wind_dir'])
        X['wind_speed'] = df['wind_spd']

        # Weather
        X['temperature'] = df['temp'].fillna(df['temp'].median())
        X['humidity'] = df['rhum'].fillna(df['rhum'].median())
        X['pressure'] = df['pres'].fillna(df['pres'].median())

        # Time (circular)
        month = pd.to_datetime(df['datum']).dt.month
        X['month_sin'], X['month_cos'] = circular_encode_series(month * 30)

        hour = df['flight_time'].str[:2].astype(float)
        X['hour_sin'], X['hour_cos'] = circular_encode_series(hour * 15)

        # Runway (one-hot)
        for rwy in ['07', '12', '18', '25', '30', '36']:
            X[f'runway_{rwy}'] = (df['runway'] == rwy).astype(int)

        return X

    def _build_spatial_features(self, df):
        """Build feature matrix for spatial thermal prediction.

        Strategy: Learn "where did pilots find thermals given wind + position"
        Primary features: GPS position + wind direction/speed
        Secondary: altitude, time of day

        Note: Positions are quantized to 100m grid to create smooth predictions.
        """
        X = pd.DataFrame()

        # Position (lat/lon offsets in meters from airport)
        # Quantize to 100m grid to smooth predictions
        north_m = (df['latitude'] - AIRPORT_LAT) * 111000
        east_m = (df['longitude'] - AIRPORT_LON) * 111000 * math.cos(math.radians(AIRPORT_LAT))
        X['north_m'] = (north_m / 100).round() * 100  # Snap to 100m grid
        X['east_m'] = (east_m / 100).round() * 100

        # Wind vector components (this captures wind-dependent thermal patterns)
        wind_rad = np.radians(df['wind_dir'])
        X['wind_north'] = df['wind_spd'] * np.cos(wind_rad)  # km/h northward component
        X['wind_east'] = df['wind_spd'] * np.sin(wind_rad)   # km/h eastward component

        # Wind-adjusted position (where wind would drift a thermal from this spot)
        # This helps the model learn "thermal at X with wind Y looks like thermal at X' with wind Z"
        drift_factor = 0.1  # 10% coupling
        X['position_wind_north'] = X['north_m'] + X['wind_north'] * drift_factor
        X['position_wind_east'] = X['east_m'] + X['wind_east'] * drift_factor

        # Wind direction and speed (circular encoding + magnitude)
        X['wind_dir_sin'], X['wind_dir_cos'] = circular_encode_series(df['wind_dir'])
        X['wind_speed'] = df['wind_spd']

        # Altitude
        X['altitude_m'] = df['altitude']

        # Temperature
        X['temperature'] = df['weather_temp'].fillna(df['weather_temp'].median())

        # Time of day (circular)
        hour = df['time'].str[:2].astype(float)
        X['hour_sin'], X['hour_cos'] = circular_encode_series(hour * 15)

        # Month (seasonal)
        month = pd.to_datetime(df['datum']).dt.month
        X['month_sin'], X['month_cos'] = circular_encode_series(month * 30)

        return X

    # ── Data Loading ──

    def _load_flight_data(self):
        """Load flight-level data for success + navigation models."""
        conn = sqlite3.connect(self.db_path)
        df = pd.read_sql_query("""
            SELECT
                f.runway,
                f.flight_outcome,
                f.release_to_thermal_bearing,
                f.release_to_thermal_distance_m,
                f.datum,
                f.start_tijd as flight_time,
                AVG(i.wind_dir) as wind_dir,
                AVG(i.wind_spd) as wind_spd,
                AVG(i.weather_temp) as temp,
                AVG(i.weather_rhum) as rhum,
                AVG(i.weather_pres) as pres
            FROM flights f
            JOIN igc_data i ON f.id = i.flight_id
            WHERE f.release_thermal_processed = 1
                AND f.flight_outcome IN ('thermal_found', 'landed_back')
                AND f.start_methode = 'lier'
                AND f.runway IS NOT NULL
                AND i.altitude BETWEEN 200 AND 500
                AND i.wind_dir IS NOT NULL
            GROUP BY f.id
            HAVING wind_dir IS NOT NULL AND wind_spd IS NOT NULL
        """, conn)
        conn.close()
        print(f"  Loaded {len(df)} flights ({(df['flight_outcome']=='thermal_found').sum()} thermal, "
              f"{(df['flight_outcome']=='landed_back').sum()} landed back)")
        return df

    def _load_spatial_data(self):
        """Load spatial data: thermal entry points from winch release.

        Uses release-to-thermal data which captures only thermals found
        within gliding range (~1.5km median) from winch release.
        """
        conn = sqlite3.connect(self.db_path)

        # Positive: Thermal entry points from winch launches
        print("  Loading thermal entry points (reachable from winch)...")
        thermals = pd.read_sql_query("""
            SELECT
                f.thermal_entry_lat as latitude,
                f.thermal_entry_lon as longitude,
                f.thermal_entry_alt as altitude,
                NULL as vertical_speed,
                w.wdir as wind_dir,
                w.wspd as wind_spd,
                w.temp as weather_temp,
                f.start_tijd as time,
                f.datum,
                1 as is_thermal
            FROM flights f
            LEFT JOIN weather_data w ON DATE(f.datum) = DATE(w.date)
                AND CAST(substr(f.start_tijd, 1, 2) AS INTEGER) = w.hour
            WHERE f.flight_outcome = 'thermal_found'
                AND f.thermal_entry_lat IS NOT NULL
                AND f.start_methode = 'lier'
                AND f.registratie NOT IN ('PH-GOZ', 'PH GOZ')
                AND f.release_to_thermal_distance_m <= 2500
        """, conn)
        print(f"    {len(thermals)} thermal entry points within 2.5km")

        # Negative: GPS points from early winch phase (200-400m altitude)
        # These are locations pilots flew through but landed back without thermal
        n_target = len(thermals)
        print(f"  Loading ~{n_target} non-thermal GPS points...")
        non_thermals = pd.read_sql_query(f"""
            SELECT
                i.latitude, i.longitude, i.altitude, i.vertical_speed,
                i.wind_dir, i.wind_spd, i.weather_temp,
                i.time, f.datum,
                0 as is_thermal
            FROM igc_data i
            JOIN flights f ON i.flight_id = f.id
            WHERE i.altitude BETWEEN 200 AND 400
                AND i.wind_dir IS NOT NULL
                AND f.flight_outcome = 'landed_back'
                AND f.start_methode = 'lier'
                AND f.registratie NOT IN ('PH-GOZ', 'PH GOZ')
            ORDER BY RANDOM()
            LIMIT {n_target}
        """, conn)
        print(f"    {len(non_thermals)} non-thermal points (from landed-back flights)")
        conn.close()

        df = pd.concat([thermals, non_thermals], ignore_index=True)
        df['is_thermal'] = df['is_thermal'].astype(int)

        # Fill missing wind data with median
        df['wind_dir'] = df['wind_dir'].fillna(df['wind_dir'].median())
        df['wind_spd'] = df['wind_spd'].fillna(df['wind_spd'].median())

        return df

    def _enrich_terrain_from_grid(self, df):
        """Look up terrain features for GPS points from the terrain grid."""
        print("    Enriching non-thermal points with terrain from grid...")
        meta = self.grid_meta
        cell_lat = meta['cell_lat']
        cell_lon = meta['cell_lon']
        lat_min = meta['lat_min']
        lon_min = meta['lon_min']

        # Vectorized grid cell assignment
        glats = ((df['latitude'].values - lat_min) / cell_lat).astype(int)
        glons = ((df['longitude'].values - lon_min) / cell_lon).astype(int)

        # Pre-build lookup arrays
        n = len(df)
        terrain_types = np.empty(n, dtype=object)
        dist_forest = np.full(n, DISTANCE_CAP_KM)
        bear_forest = np.zeros(n)
        dist_water = np.full(n, DISTANCE_CAP_KM)
        bear_water = np.zeros(n)
        dist_urban = np.full(n, DISTANCE_CAP_KM)
        bear_urban = np.zeros(n)
        terrain_types[:] = 'field'

        for i in range(n):
            cell = self.terrain_grid.get((glats[i], glons[i]))
            if cell:
                terrain_types[i] = cell['terrain_type']
                dist_forest[i] = cell['distance_to_forest']
                bear_forest[i] = cell['bearing_to_forest']
                dist_water[i] = cell['distance_to_water']
                bear_water[i] = cell['bearing_to_water']
                dist_urban[i] = cell['distance_to_urban']
                bear_urban[i] = cell['bearing_to_urban']

        df = df.copy()
        df['terrain_type'] = terrain_types
        df['distance_to_forest'] = dist_forest
        df['bearing_to_forest'] = bear_forest
        df['distance_to_water'] = dist_water
        df['bearing_to_water'] = bear_water
        df['distance_to_urban'] = dist_urban
        df['bearing_to_urban'] = bear_urban
        return df

    def _fetch_osm_features(self, feature_type, osm_tag):
        """Fetch features from OpenStreetMap Overpass API with retry."""
        # 6km radius: covers 4km flight radius + 2km buffer for nearest-feature calc
        lat_offset = 6.0 / 111.0
        lon_offset = 6.0 / (111.0 * math.cos(math.radians(AIRPORT_LAT)))
        south = AIRPORT_LAT - lat_offset
        north = AIRPORT_LAT + lat_offset
        west = AIRPORT_LON - lon_offset
        east = AIRPORT_LON + lon_offset

        tag_filter = "".join([f'["{k}"="{v}"]' for k, v in osm_tag.items()])
        query = f"""
        [out:json][timeout:120];
        (
          way{tag_filter}({south},{west},{north},{east});
          relation{tag_filter}({south},{west},{north},{east});
        );
        out center;
        """

        for attempt in range(10):
            try:
                response = requests.post(
                    "https://overpass-api.de/api/interpreter",
                    data=query, timeout=180
                )
                if response.status_code in (429, 504, 503, 502):
                    wait = 30 * (attempt + 1)
                    print(f"    Server returned {response.status_code}, retrying in {wait}s... (attempt {attempt+1}/10)")
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                wait = 30 * (attempt + 1)
                print(f"    {type(e).__name__}, retrying in {wait}s... (attempt {attempt+1}/10)")
                time.sleep(wait)
        else:
            raise RuntimeError(f"Failed to fetch {feature_type} after 10 attempts")

        data = response.json()

        features = []
        for el in data.get('elements', []):
            if 'center' in el:
                features.append((el['center']['lat'], el['center']['lon']))
            elif 'lat' in el and 'lon' in el:
                features.append((el['lat'], el['lon']))

        print(f"    {len(features)} {feature_type}")
        return features

    def _osm_cache_path(self):
        return os.path.join(self.model_dir, 'osm_features_cache.pkl')

    def _fetch_all_osm_terrain(self):
        """Fetch all terrain feature types from OSM with intermediate caching.

        Each feature type is saved after fetching so progress is not lost
        if a subsequent request fails.
        """
        os.makedirs(self.model_dir, exist_ok=True)
        cache_path = self._osm_cache_path()

        # Load existing cache
        cache = {}
        if os.path.exists(cache_path):
            with open(cache_path, 'rb') as f:
                cache = pickle.load(f)
            print(f"  Loaded cached OSM features: {list(cache.keys())}")

        queries = [
            ('forests_wood',   "forests",              {"natural": "wood"}),
            ('forests_tree',   "tree rows",            {"natural": "tree_row"}),
            ('water_natural',  "water (natural)",      {"natural": "water"}),
            ('water_reservoir',"water (reservoir)",     {"landuse": "reservoir"}),
            ('urban_resid',    "residential areas",    {"landuse": "residential"}),
            ('urban_comm',     "commercial areas",     {"landuse": "commercial"}),
            ('urban_indust',   "industrial areas",     {"landuse": "industrial"}),
        ]

        print("  Fetching terrain from OpenStreetMap...")
        for key, desc, tag in queries:
            if key in cache:
                print(f"    {desc}: {len(cache[key])} (cached)")
                continue

            features = self._fetch_osm_features(desc, tag)
            cache[key] = features

            # Save intermediate result
            with open(cache_path, 'wb') as f:
                pickle.dump(cache, f)
            print(f"    Saved to cache ({len(cache)}/{len(queries)} complete)")

            # Long wait between requests to avoid throttling
            time.sleep(15)

        forests = cache['forests_wood'] + cache['forests_tree']
        water = cache['water_natural'] + cache['water_reservoir']
        urban = cache['urban_resid'] + cache['urban_comm'] + cache['urban_indust']

        print(f"    Total: {len(forests)} forests, {len(water)} water, {len(urban)} urban")
        return forests, water, urban

    @staticmethod
    def _nearest_features_batch(cell_lats, cell_lons, features, batch_size=5000):
        """Vectorized nearest-feature calculation, batched for memory safety.

        Processes cells in chunks to avoid creating huge distance matrices.
        Returns: (distances_km, bearings_deg) arrays of shape (n_cells,)
        """
        n = len(cell_lats)
        if not features:
            return np.full(n, 999.0), np.zeros(n)

        feat_arr = np.array(features)
        feat_lats_rad = np.radians(feat_arr[:, 0])
        feat_lons_rad = np.radians(feat_arr[:, 1])

        all_dists = np.empty(n)
        all_bearings = np.empty(n)

        for start in range(0, n, batch_size):
            end = min(start + batch_size, n)
            batch_lats_rad = np.radians(cell_lats[start:end])
            batch_lons_rad = np.radians(cell_lons[start:end])
            b = end - start

            # Haversine: (batch, 1) vs (1, n_features)
            dlat = feat_lats_rad[np.newaxis, :] - batch_lats_rad[:, np.newaxis]
            dlon = feat_lons_rad[np.newaxis, :] - batch_lons_rad[:, np.newaxis]
            a = (np.sin(dlat/2)**2 +
                 np.cos(batch_lats_rad[:, np.newaxis]) *
                 np.cos(feat_lats_rad[np.newaxis, :]) *
                 np.sin(dlon/2)**2)
            dists_km = 6371 * 2 * np.arctan2(np.sqrt(a), np.sqrt(1-a))

            nearest_idx = np.argmin(dists_km, axis=1)
            all_dists[start:end] = dists_km[np.arange(b), nearest_idx]

            # Bearing to nearest feature
            n_feat_lats = feat_lats_rad[nearest_idx]
            n_feat_lons = feat_lons_rad[nearest_idx]
            dlon_n = n_feat_lons - batch_lons_rad
            y = np.sin(dlon_n) * np.cos(n_feat_lats)
            x = (np.cos(batch_lats_rad) * np.sin(n_feat_lats) -
                 np.sin(batch_lats_rad) * np.cos(n_feat_lats) * np.cos(dlon_n))
            all_bearings[start:end] = (np.degrees(np.arctan2(y, x)) + 360) % 360

        return all_dists, all_bearings

    @staticmethod
    def _classify_terrain_array(dist_forest, dist_water, dist_urban):
        """Classify terrain type for arrays of distances."""
        n = len(dist_forest)
        types = np.full(n, 'field', dtype=object)
        types[dist_urban < 0.5] = 'urban_edge'
        types[dist_forest < 0.5] = 'forest_edge'
        types[dist_urban < 0.2] = 'urban'
        types[dist_water < 0.2] = 'water'
        types[dist_forest < 0.2] = 'forest'
        return types

    def _build_terrain_grid(self, grid_size_m=10):
        """Build high-resolution terrain grid from OSM data.

        Args:
            grid_size_m: Cell size in meters (default 10m for high-res storage)
        """
        forests, water, urban = self._fetch_all_osm_terrain()

        lat_range = MAX_RADIUS_KM / 111.0
        lon_range = MAX_RADIUS_KM / (111.0 * math.cos(math.radians(AIRPORT_LAT)))

        cell_lat = grid_size_m / 111000.0
        cell_lon = grid_size_m / (111000.0 * math.cos(math.radians(AIRPORT_LAT)))
        lat_min = AIRPORT_LAT - lat_range
        lon_min = AIRPORT_LON - lon_range

        n_lat = int(2 * lat_range / cell_lat) + 1
        n_lon = int(2 * lon_range / cell_lon) + 1

        # Generate all cell centers within radius
        print(f"  Generating {grid_size_m}m grid: {n_lat}x{n_lon} potential cells...")
        centers = []
        indices = []
        for glat in range(n_lat):
            clat = lat_min + (glat + 0.5) * cell_lat
            for glon in range(n_lon):
                clon = lon_min + (glon + 0.5) * cell_lon
                dist = haversine_distance(AIRPORT_LAT, AIRPORT_LON, clat, clon)
                if dist <= MAX_RADIUS_KM:
                    centers.append((clat, clon, dist))
                    indices.append((glat, glon))

        cell_lats = np.array([c[0] for c in centers])
        cell_lons = np.array([c[1] for c in centers])
        cell_dists = np.array([c[2] for c in centers])
        n_cells = len(centers)
        print(f"    {n_cells} cells within {MAX_RADIUS_KM}km radius")

        # Vectorized nearest-feature calculations (batched for memory)
        print(f"  Computing distances to {len(forests)} forests...")
        df_km, df_bear = self._nearest_features_batch(cell_lats, cell_lons, forests)
        print(f"  Computing distances to {len(water)} water bodies...")
        dw_km, dw_bear = self._nearest_features_batch(cell_lats, cell_lons, water)
        print(f"  Computing distances to {len(urban)} urban areas...")
        du_km, du_bear = self._nearest_features_batch(cell_lats, cell_lons, urban)

        # Classify terrain
        terrain_types = self._classify_terrain_array(df_km, dw_km, du_km)

        # Build grid dict
        print(f"  Building grid dict...")
        terrain_grid = {}
        for i in range(n_cells):
            glat, glon = indices[i]
            terrain_grid[(glat, glon)] = {
                'lat': float(cell_lats[i]),
                'lon': float(cell_lons[i]),
                'terrain_type': str(terrain_types[i]),
                'distance_to_forest': float(df_km[i]),
                'distance_to_water': float(dw_km[i]),
                'distance_to_urban': float(du_km[i]),
                'bearing_to_forest': float(df_bear[i]),
                'bearing_to_water': float(dw_bear[i]),
                'bearing_to_urban': float(du_bear[i]),
                'dist_from_airport': float(cell_dists[i]),
            }

        self.grid_meta = {
            'cell_size_m': grid_size_m,
            'cell_lat': cell_lat,
            'cell_lon': cell_lon,
            'lat_min': lat_min,
            'lon_min': lon_min,
        }

        # Print terrain distribution
        from collections import Counter
        counts = Counter(terrain_types)
        print(f"    Terrain: {dict(counts)}")
        print(f"    {n_cells} grid cells at {grid_size_m}m resolution")
        return terrain_grid

    def _build_terrain_grid_from_db(self):
        """Build terrain grid from existing enriched GPS points in the database.

        Fallback when OSM API is unavailable. Uses the ~109K thermal points
        that already have terrain data from a previous terrain_enrichment run.
        """
        print("  Building terrain grid from database (no OSM needed)...")
        conn = sqlite3.connect(self.db_path)

        lat_range = MAX_RADIUS_KM / 111.0
        lon_range = MAX_RADIUS_KM / (111.0 * math.cos(math.radians(AIRPORT_LAT)))

        df = pd.read_sql_query("""
            SELECT latitude, longitude, terrain_type,
                   distance_to_forest, bearing_to_forest,
                   distance_to_water, bearing_to_water,
                   distance_to_urban, bearing_to_urban
            FROM igc_data
            WHERE terrain_type IS NOT NULL
                AND latitude BETWEEN ? AND ?
                AND longitude BETWEEN ? AND ?
        """, conn, params=[
            AIRPORT_LAT - lat_range, AIRPORT_LAT + lat_range,
            AIRPORT_LON - lon_range, AIRPORT_LON + lon_range
        ])
        conn.close()
        print(f"    {len(df)} enriched GPS points")

        cell_lat = GRID_SIZE_M / 111000.0
        cell_lon = GRID_SIZE_M / (111000.0 * math.cos(math.radians(AIRPORT_LAT)))
        lat_min = AIRPORT_LAT - lat_range
        lon_min = AIRPORT_LON - lon_range

        df['grid_lat'] = ((df['latitude'] - lat_min) / cell_lat).astype(int)
        df['grid_lon'] = ((df['longitude'] - lon_min) / cell_lon).astype(int)

        terrain_grid = {}
        for (glat, glon), group in df.groupby(['grid_lat', 'grid_lon']):
            center_lat = lat_min + (glat + 0.5) * cell_lat
            center_lon = lon_min + (glon + 0.5) * cell_lon

            dist = haversine_distance(AIRPORT_LAT, AIRPORT_LON, center_lat, center_lon)
            if dist > MAX_RADIUS_KM:
                continue

            terrain_grid[(glat, glon)] = {
                'lat': center_lat,
                'lon': center_lon,
                'terrain_type': group['terrain_type'].mode().iloc[0],
                'distance_to_forest': float(group['distance_to_forest'].median()),
                'distance_to_water': float(group['distance_to_water'].median()),
                'distance_to_urban': float(group['distance_to_urban'].median()),
                'bearing_to_forest': float(group['bearing_to_forest'].median()),
                'bearing_to_water': float(group['bearing_to_water'].median()),
                'bearing_to_urban': float(group['bearing_to_urban'].median()),
                'dist_from_airport': dist,
            }

        self.grid_meta = {
            'cell_lat': cell_lat,
            'cell_lon': cell_lon,
            'lat_min': lat_min,
            'lon_min': lon_min,
        }

        from collections import Counter
        counts = Counter(c['terrain_type'] for c in terrain_grid.values())
        print(f"    Terrain: {dict(counts)}")
        print(f"    {len(terrain_grid)} grid cells from DB")
        return terrain_grid

    # ── Model Training ──

    def _train_success_model(self, df, evaluate=False):
        """Train binary classifier: P(thermal found | weather, runway)."""
        print("\n── Training Success Model ──")

        X = self._build_flight_features(df)
        y = (df['flight_outcome'] == 'thermal_found').astype(int)

        feature_names = list(X.columns)

        if evaluate:
            # Time-based split: last 20% by date
            dates = pd.to_datetime(df['datum'])
            cutoff = dates.quantile(0.8)
            train_mask = dates <= cutoff
            X_train, X_test = X[train_mask], X[~train_mask]
            y_train, y_test = y[train_mask], y[~train_mask]
            print(f"  Train: {len(X_train)}, Test: {len(X_test)}")
        else:
            X_train, y_train = X, y
            X_test, y_test = None, None

        model = HistGradientBoostingClassifier(
            max_iter=200,
            max_depth=6,
            learning_rate=0.1,
            min_samples_leaf=20,
            random_state=42
        )
        model.fit(X_train.values, y_train.values)

        if evaluate and X_test is not None and len(X_test) > 0:
            y_pred = model.predict(X_test.values)
            y_prob = model.predict_proba(X_test.values)[:, 1]
            acc = accuracy_score(y_test, y_pred)
            auc = roc_auc_score(y_test, y_prob)
            print(f"  Accuracy: {acc:.3f}")
            print(f"  AUC-ROC:  {auc:.3f}")
            print(classification_report(y_test, y_pred, target_names=['landed_back', 'thermal_found']))
            self.metrics['success'] = {'accuracy': acc, 'auc': auc}

        # Retrain on all data
        if evaluate:
            model.fit(X.values, y.values)

        # Feature importance (permutation-based)
        perm = permutation_importance(model, X.values, y.values, n_repeats=5, random_state=42, n_jobs=-1)
        importances = dict(zip(feature_names, perm.importances_mean))
        self.feature_importances['success'] = dict(sorted(importances.items(), key=lambda x: -x[1])[:10])
        print("  Top features:", list(self.feature_importances['success'].keys())[:5])

        self.success_model = model
        return model

    def _train_navigation_model(self, df, evaluate=False):
        """Train regressors for bearing and distance to thermal."""
        print("\n── Training Navigation Model ──")

        # Only thermal-found flights with valid navigation data
        mask = (df['flight_outcome'] == 'thermal_found') & df['release_to_thermal_bearing'].notna()
        df_nav = df[mask].copy()
        print(f"  {len(df_nav)} flights with navigation data")

        X = self._build_flight_features(df_nav)

        # Encode bearing as sin/cos for regression (handles 360° wraparound)
        bearing_sin, bearing_cos = circular_encode_series(df_nav['release_to_thermal_bearing'])
        y_bearing_sin = bearing_sin.values
        y_bearing_cos = bearing_cos.values
        y_distance = df_nav['release_to_thermal_distance_m'].values

        feature_names = list(X.columns)

        if evaluate:
            dates = pd.to_datetime(df_nav['datum'])
            cutoff = dates.quantile(0.8)
            train_mask = dates <= cutoff
            X_train, X_test = X[train_mask], X[~train_mask]
            idx_train, idx_test = train_mask.values, ~train_mask.values
        else:
            X_train = X
            idx_train = np.ones(len(X), dtype=bool)
            idx_test = None

        # Bearing model (predicts sin/cos)
        bearing_sin_model = HistGradientBoostingRegressor(
            max_iter=200, max_depth=6, learning_rate=0.1, random_state=42
        )
        bearing_cos_model = HistGradientBoostingRegressor(
            max_iter=200, max_depth=6, learning_rate=0.1, random_state=42
        )
        bearing_sin_model.fit(X_train.values, y_bearing_sin[idx_train])
        bearing_cos_model.fit(X_train.values, y_bearing_cos[idx_train])

        # Distance model
        distance_model = HistGradientBoostingRegressor(
            max_iter=200, max_depth=6, learning_rate=0.1, random_state=42
        )
        distance_model.fit(X_train.values, y_distance[idx_train])

        if evaluate and idx_test is not None:
            # Bearing evaluation
            pred_sin = bearing_sin_model.predict(X_test.values)
            pred_cos = bearing_cos_model.predict(X_test.values)
            pred_bearing = (np.degrees(np.arctan2(pred_sin, pred_cos)) + 360) % 360
            actual_bearing = df_nav.loc[~train_mask, 'release_to_thermal_bearing'].values

            # Angular error
            bearing_errors = np.abs(pred_bearing - actual_bearing)
            bearing_errors = np.minimum(bearing_errors, 360 - bearing_errors)
            mae_bearing = np.mean(bearing_errors)
            print(f"  Bearing MAE: {mae_bearing:.1f}°")

            # Distance evaluation
            pred_dist = distance_model.predict(X_test.values)
            mae_dist = mean_absolute_error(y_distance[~train_mask.values], pred_dist)
            r2_dist = r2_score(y_distance[~train_mask.values], pred_dist)
            print(f"  Distance MAE: {mae_dist:.0f}m, R²: {r2_dist:.3f}")
            self.metrics['navigation'] = {
                'bearing_mae': mae_bearing,
                'distance_mae': mae_dist,
                'distance_r2': r2_dist
            }

        # Retrain on all data
        if evaluate:
            bearing_sin_model.fit(X.values, y_bearing_sin)
            bearing_cos_model.fit(X.values, y_bearing_cos)
            distance_model.fit(X.values, y_distance)

        self.bearing_model = {
            'sin': bearing_sin_model,
            'cos': bearing_cos_model,
        }
        self.distance_model = distance_model

        # Feature importance (permutation-based, from distance model)
        perm = permutation_importance(distance_model, X.values, y_distance, n_repeats=5, random_state=42, n_jobs=-1)
        importances = dict(zip(feature_names, perm.importances_mean))
        self.feature_importances['navigation'] = dict(sorted(importances.items(), key=lambda x: -x[1])[:10])

    def _train_spatial_model(self, df, evaluate=False):
        """Train spatial thermal probability model."""
        print("\n── Training Spatial Model ──")

        X = self._build_spatial_features(df)
        y = df['is_thermal'].values

        feature_names = list(X.columns)

        if evaluate:
            dates = pd.to_datetime(df['datum'])
            cutoff = dates.quantile(0.8)
            train_mask = dates <= cutoff
            X_train, X_test = X[train_mask], X[~train_mask]
            y_train, y_test = y[train_mask.values], y[~train_mask.values]
            print(f"  Train: {len(X_train)}, Test: {len(X_test)}")
        else:
            X_train, y_train = X, y
            X_test, y_test = None, None

        # Shallow model for smooth spatial predictions
        model = HistGradientBoostingClassifier(
            max_iter=100,
            max_depth=4,  # Shallow trees = smooth boundaries
            learning_rate=0.2,
            min_samples_leaf=100,  # Many samples per leaf = smooth
            random_state=42
        )
        model.fit(X_train.values, y_train)

        if evaluate and X_test is not None and len(X_test) > 0:
            y_pred = model.predict(X_test.values)
            y_prob = model.predict_proba(X_test.values)[:, 1]
            acc = accuracy_score(y_test, y_pred)
            auc = roc_auc_score(y_test, y_prob)
            print(f"  Accuracy: {acc:.3f}")
            print(f"  AUC-ROC:  {auc:.3f}")
            print(classification_report(y_test, y_pred, target_names=['no_thermal', 'thermal']))
            self.metrics['spatial'] = {'accuracy': acc, 'auc': auc}

        # Retrain on all data
        if evaluate:
            model.fit(X.values, y)

        # Feature importance (permutation-based)
        perm = permutation_importance(model, X.values, y, n_repeats=5, random_state=42, n_jobs=-1)
        importances = dict(zip(feature_names, perm.importances_mean))
        self.feature_importances['spatial'] = dict(sorted(importances.items(), key=lambda x: -x[1])[:10])
        print("  Top features:", list(self.feature_importances['spatial'].keys())[:5])

        self.spatial_model = model

    # ── Main Training Entry Point ──

    def train(self, evaluate=False):
        """Train all models."""
        print("=" * 60)
        print("ML THERMAL MODEL TRAINING")
        print("=" * 60)
        self.training_date = datetime.now().isoformat()

        # 1. Flight-level models
        print("\nLoading flight data...")
        flight_df = self._load_flight_data()
        self._train_success_model(flight_df, evaluate)
        self._train_navigation_model(flight_df, evaluate)

        # 2. Spatial model (requires terrain grid for negative sample enrichment)
        has_grid = self.terrain_grid is not None or self.load_terrain_grid()
        if has_grid:
            print("\nLoading spatial data...")
            spatial_df = self._load_spatial_data()
            self._train_spatial_model(spatial_df, evaluate)
        else:
            print("\n  Skipping spatial model (no terrain grid)")
            print("  Run --build-grid or --build-grid-from-db first, then retrain")

        print("\n" + "=" * 60)
        print("TRAINING COMPLETE")
        print("=" * 60)

        if self.metrics:
            print("\nModel Performance:")
            if 'success' in self.metrics:
                m = self.metrics['success']
                print(f"  Success model:    AUC={m['auc']:.3f}, Acc={m['accuracy']:.3f}")
            if 'navigation' in self.metrics:
                m = self.metrics['navigation']
                print(f"  Navigation model: Bearing MAE={m['bearing_mae']:.1f}°, Distance MAE={m['distance_mae']:.0f}m")
            if 'spatial' in self.metrics:
                m = self.metrics['spatial']
                print(f"  Spatial model:    AUC={m['auc']:.3f}, Acc={m['accuracy']:.3f}")

        print(f"\nFeature Importances (spatial model):")
        for feat, imp in list(self.feature_importances.get('spatial', {}).items())[:8]:
            print(f"  {feat:30s} {imp:.4f}")

    # ── Terrain Grid Persistence ──

    def _terrain_grid_path(self):
        return os.path.join(self.model_dir, 'terrain_grid.pkl')

    def save_terrain_grid(self):
        """Save terrain grid to disk (separate from models, fetched once)."""
        os.makedirs(self.model_dir, exist_ok=True)
        path = self._terrain_grid_path()
        with open(path, 'wb') as f:
            pickle.dump({'terrain_grid': self.terrain_grid, 'grid_meta': self.grid_meta}, f)
        size_mb = os.path.getsize(path) / 1024 / 1024
        print(f"  Terrain grid saved to {path} ({size_mb:.1f} MB)")

    def load_terrain_grid(self):
        """Load cached terrain grid from disk."""
        path = self._terrain_grid_path()
        if not os.path.exists(path):
            return False
        with open(path, 'rb') as f:
            data = pickle.load(f)
        self.terrain_grid = data['terrain_grid']
        self.grid_meta = data['grid_meta']
        print(f"  Loaded cached terrain grid: {len(self.terrain_grid)} cells")
        return True

    def ensure_terrain_grid(self):
        """Load terrain grid from cache, or build from OSM if not cached."""
        if self.load_terrain_grid():
            return
        print("  No cached terrain grid found, fetching from OSM...")
        self.terrain_grid = self._build_terrain_grid()
        self.save_terrain_grid()

    # ── Model Persistence ──

    def save(self):
        """Save ML models to disk (terrain grid saved separately)."""
        os.makedirs(self.model_dir, exist_ok=True)

        data = {
            'success_model': self.success_model,
            'bearing_model': self.bearing_model,
            'distance_model': self.distance_model,
            'spatial_model': self.spatial_model,
            'feature_importances': self.feature_importances,
            'metrics': self.metrics,
            'training_date': self.training_date,
        }

        path = os.path.join(self.model_dir, 'thermal_ml_models.pkl')
        with open(path, 'wb') as f:
            pickle.dump(data, f)

        size_mb = os.path.getsize(path) / 1024 / 1024
        print(f"\nModels saved to {path} ({size_mb:.1f} MB)")

    @classmethod
    def load(cls, model_dir='data/models', db_path='data/flights.db'):
        """Load trained models + terrain grid from disk."""
        path = os.path.join(model_dir, 'thermal_ml_models.pkl')
        if not os.path.exists(path):
            raise FileNotFoundError(f"No trained models found at {path}. Run training first.")

        with open(path, 'rb') as f:
            data = pickle.load(f)

        model = cls(db_path=db_path, model_dir=model_dir)
        model.success_model = data['success_model']
        model.bearing_model = data['bearing_model']
        model.distance_model = data['distance_model']
        model.spatial_model = data['spatial_model']
        model.feature_importances = data['feature_importances']
        model.metrics = data['metrics']
        model.training_date = data['training_date']

        # Load terrain grid (cached separately, optional for spatial predictions)
        if not model.load_terrain_grid():
            print("  Warning: no terrain grid found. Spatial predictions unavailable.")
            print("  Run: python ml_thermal_model.py --build-grid-from-db")

        return model

    # ── Prediction ──

    def predict_success(self, wind_dir, wind_speed, temp, humidity, pressure, month, hour, runway):
        """Predict probability of finding a thermal."""
        features = {}
        features['wind_dir_sin'], features['wind_dir_cos'] = circular_encode(wind_dir)
        features['wind_speed'] = wind_speed
        features['temperature'] = temp if temp is not None else 15.0
        features['humidity'] = humidity if humidity is not None else 70.0
        features['pressure'] = pressure if pressure is not None else 1013.0
        features['month_sin'], features['month_cos'] = circular_encode(month * 30)
        features['hour_sin'], features['hour_cos'] = circular_encode(hour * 15)
        for rwy in ['07', '12', '18', '25', '30', '36']:
            features[f'runway_{rwy}'] = 1 if runway == rwy else 0

        X = np.array([[features[k] for k in sorted(features.keys())]])
        # Ensure consistent column ordering
        cols = ['hour_cos', 'hour_sin', 'humidity', 'month_cos', 'month_sin',
                'pressure', 'runway_07', 'runway_12', 'runway_18', 'runway_25',
                'runway_30', 'runway_36', 'temperature', 'wind_dir_cos',
                'wind_dir_sin', 'wind_speed']
        X = np.array([[features[c] for c in cols]])
        prob = self.success_model.predict_proba(X)[0, 1]
        return prob

    def predict_navigation(self, wind_dir, wind_speed, temp, humidity, pressure, month, hour, runway):
        """Predict bearing and distance to thermal."""
        features = {}
        features['wind_dir_sin'], features['wind_dir_cos'] = circular_encode(wind_dir)
        features['wind_speed'] = wind_speed
        features['temperature'] = temp if temp is not None else 15.0
        features['humidity'] = humidity if humidity is not None else 70.0
        features['pressure'] = pressure if pressure is not None else 1013.0
        features['month_sin'], features['month_cos'] = circular_encode(month * 30)
        features['hour_sin'], features['hour_cos'] = circular_encode(hour * 15)
        for rwy in ['07', '12', '18', '25', '30', '36']:
            features[f'runway_{rwy}'] = 1 if runway == rwy else 0

        cols = ['hour_cos', 'hour_sin', 'humidity', 'month_cos', 'month_sin',
                'pressure', 'runway_07', 'runway_12', 'runway_18', 'runway_25',
                'runway_30', 'runway_36', 'temperature', 'wind_dir_cos',
                'wind_dir_sin', 'wind_speed']
        X = np.array([[features[c] for c in cols]])

        bearing_sin = self.bearing_model['sin'].predict(X)[0]
        bearing_cos = self.bearing_model['cos'].predict(X)[0]
        bearing = (np.degrees(np.arctan2(bearing_sin, bearing_cos)) + 360) % 360
        distance = max(100, self.distance_model.predict(X)[0])

        return bearing, distance

    def predict_grid(self, wind_dir, wind_speed, temp, month, hour, runway=None, prediction_cell_m=50):
        """Predict thermal probability for each grid cell.

        Args:
            wind_dir, wind_speed, temp, month, hour: Weather conditions
            runway: Active runway (e.g., '30') to filter by reachability
            prediction_cell_m: Grid resolution for predictions

        Returns list of dicts: [{lat, lon, probability, terrain_type, dist_from_airport}, ...]
        """
        if not self.terrain_grid:
            raise ValueError("No terrain grid available. Train the model first.")

        # Get release point (lier) position for reachability calculation
        release_lat, release_lon = AIRPORT_LAT, AIRPORT_LON
        if runway:
            from runway_constants import RUNWAY_LINES
            if runway in RUNWAY_LINES:
                release_lat, release_lon = RUNWAY_LINES[runway]['end']

        # Max glide distance from 400m release altitude
        # Glide ratio ~30:1, release at 400m, need ~100m margin = 9km theoretical
        # But realistically with sink + wind + safety margin: 2.5km
        MAX_GLIDE_KM = 2.5

        # Subsample if terrain grid is higher resolution than needed
        grid_cell_m = self.grid_meta.get('cell_size_m', GRID_SIZE_M)
        step = max(1, int(prediction_cell_m / grid_cell_m))

        results = []

        for (glat, glon), cell in self.terrain_grid.items():
            # Skip cells for subsampling
            if step > 1 and (glat % step != 0 or glon % step != 0):
                continue

            # Filter by reachability from release point
            dist_from_release = haversine_distance(release_lat, release_lon, cell['lat'], cell['lon'])
            if dist_from_release > MAX_GLIDE_KM:
                continue
            # Build feature vector for this cell
            features = {}

            # Position offsets from airport (quantized to 100m grid)
            north_m = (cell['lat'] - AIRPORT_LAT) * 111000
            east_m = (cell['lon'] - AIRPORT_LON) * 111000 * math.cos(math.radians(AIRPORT_LAT))
            features['north_m'] = round(north_m / 100) * 100
            features['east_m'] = round(east_m / 100) * 100

            # Wind vector components
            wind_rad = math.radians(wind_dir)
            wind_north = wind_speed * math.cos(wind_rad)
            wind_east = wind_speed * math.sin(wind_rad)
            features['wind_north'] = wind_north
            features['wind_east'] = wind_east

            # Wind-adjusted position
            drift_factor = 0.1
            features['position_wind_north'] = features['north_m'] + wind_north * drift_factor
            features['position_wind_east'] = features['east_m'] + wind_east * drift_factor

            # Wind (circular + magnitude)
            features['wind_dir_sin'], features['wind_dir_cos'] = circular_encode(wind_dir)
            features['wind_speed'] = wind_speed

            # Altitude (predict at 300m cruise altitude)
            features['altitude_m'] = 300.0

            # Temperature
            features['temperature'] = temp if temp is not None else 15.0

            # Time
            features['hour_sin'], features['hour_cos'] = circular_encode(hour * 15)
            features['month_sin'], features['month_cos'] = circular_encode(month * 30)

            results.append({
                'lat': cell['lat'],
                'lon': cell['lon'],
                'terrain_type': cell['terrain_type'],
                'dist_from_airport': cell['dist_from_airport'],
                'dist_from_release': dist_from_release,
                'features': features,
            })

        # Batch predict
        if not results:
            return []

        # Build feature matrix with consistent column ordering
        col_order = sorted(results[0]['features'].keys())
        X = np.array([[r['features'][c] for c in col_order] for r in results])
        probs = self.spatial_model.predict_proba(X)[:, 1]

        for i, r in enumerate(results):
            r['probability'] = float(probs[i])
            del r['features']  # Clean up

        return results


def main():
    parser = argparse.ArgumentParser(description='Train ML thermal prediction models')
    parser.add_argument('--build-grid', action='store_true',
                        help='Fetch terrain from OSM and build grid (run once)')
    parser.add_argument('--build-grid-from-db', action='store_true',
                        help='Build terrain grid from existing DB data (no OSM needed)')
    parser.add_argument('--evaluate', action='store_true',
                        help='Run with train/test evaluation')
    parser.add_argument('--force', action='store_true',
                        help='Retrain even if models exist')
    parser.add_argument('--db', default='data/flights.db', help='Database path')
    parser.add_argument('--model-dir', default='data/models', help='Model output directory')
    args = parser.parse_args()

    model = ThermalMLModel(db_path=args.db, model_dir=args.model_dir)

    # Build terrain grid only (10m resolution from OSM)
    if args.build_grid:
        print("=" * 60)
        print("BUILDING 10m TERRAIN GRID FROM OPENSTREETMAP")
        print("=" * 60)
        model.terrain_grid = model._build_terrain_grid(grid_size_m=10)
        model.save_terrain_grid()
        return 0

    if args.build_grid_from_db:
        print("=" * 60)
        print("BUILDING TERRAIN GRID FROM DATABASE")
        print("=" * 60)
        model.terrain_grid = model._build_terrain_grid_from_db()
        model.save_terrain_grid()
        return 0

    # Train models
    model_path = os.path.join(args.model_dir, 'thermal_ml_models.pkl')
    if os.path.exists(model_path) and not args.force:
        print(f"Models already exist at {model_path}")
        print("Use --force to retrain")
        return 0

    model.train(evaluate=args.evaluate)
    model.save()
    return 0


if __name__ == '__main__':
    sys.exit(main())
