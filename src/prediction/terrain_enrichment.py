"""
Terrain Enrichment for Thermal Prediction

Fetches terrain features from OpenStreetMap and enriches the thermal database
with geographic context for better predictions.

Features added:
- Distance to nearest forest
- Distance to nearest water body
- Distance to nearest urban area
- Terrain type at location
- Bearing from terrain features (for wind effects)

Usage:
    python terrain_enrichment.py
"""

import sqlite3
import math
import time
from typing import Tuple, List, Optional, Dict
from tqdm import tqdm
import requests
import json


class TerrainEnricher:
    """Enrich thermal database with terrain features from OpenStreetMap."""

    # Search radius around Hilversum (km)
    SEARCH_RADIUS_KM = 15
    EHHV_LAT = 52.1932
    EHHV_LON = 5.1528

    def __init__(self, db_path: str = "data/flights.db"):
        """Initialize with database connection."""
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()

        # Cache for terrain features
        self.forests = []
        self.water_bodies = []
        self.urban_areas = []

    def haversine_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance between two points in kilometers."""
        R = 6371  # Earth radius in km

        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)

        a = (math.sin(delta_lat / 2) ** 2 +
             math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        return R * c

    def calculate_bearing(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate bearing from point 1 to point 2 in degrees."""
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lon = math.radians(lon2 - lon1)

        y = math.sin(delta_lon) * math.cos(lat2_rad)
        x = (math.cos(lat1_rad) * math.sin(lat2_rad) -
             math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(delta_lon))

        bearing = math.degrees(math.atan2(y, x))
        return (bearing + 360) % 360

    def fetch_osm_features(self, feature_type: str, osm_tag: Dict[str, str]) -> List[Tuple[float, float]]:
        """
        Fetch features from OpenStreetMap using Overpass API.

        Args:
            feature_type: Description of feature for logging
            osm_tag: OSM tag dictionary, e.g., {"natural": "wood"}

        Returns:
            List of (lat, lon) tuples for feature centroids
        """
        # Calculate bounding box
        lat_offset = self.SEARCH_RADIUS_KM / 111.0  # degrees
        lon_offset = self.SEARCH_RADIUS_KM / (111.0 * math.cos(math.radians(self.EHHV_LAT)))

        south = self.EHHV_LAT - lat_offset
        north = self.EHHV_LAT + lat_offset
        west = self.EHHV_LON - lon_offset
        east = self.EHHV_LON + lon_offset

        # Build Overpass query
        tag_filter = "".join([f'["{k}"="{v}"]' for k, v in osm_tag.items()])
        query = f"""
        [out:json][timeout:60];
        (
          way{tag_filter}({south},{west},{north},{east});
          relation{tag_filter}({south},{west},{north},{east});
        );
        out center;
        """

        print(f"Fetching {feature_type} from OpenStreetMap...")

        try:
            response = requests.post(
                "https://overpass-api.de/api/interpreter",
                data=query,
                timeout=60
            )
            response.raise_for_status()
            data = response.json()

            features = []
            for element in data.get('elements', []):
                if 'center' in element:
                    features.append((element['center']['lat'], element['center']['lon']))
                elif 'lat' in element and 'lon' in element:
                    features.append((element['lat'], element['lon']))

            print(f"  Found {len(features)} {feature_type}")
            return features

        except Exception as e:
            print(f"  ERROR fetching {feature_type}: {e}")
            return []

    def fetch_all_terrain_features(self):
        """Fetch all terrain features from OpenStreetMap."""
        print("\n" + "="*60)
        print("FETCHING TERRAIN FEATURES FROM OPENSTREETMAP")
        print("="*60)

        # Forests and woods
        self.forests = self.fetch_osm_features(
            "forests",
            {"natural": "wood"}
        )
        time.sleep(1)  # Rate limiting

        # Also get tree lines and tree rows
        tree_rows = self.fetch_osm_features(
            "tree rows",
            {"natural": "tree_row"}
        )
        self.forests.extend(tree_rows)
        time.sleep(1)

        # Water bodies
        water_natural = self.fetch_osm_features(
            "water bodies (natural)",
            {"natural": "water"}
        )
        time.sleep(1)

        water_landuse = self.fetch_osm_features(
            "water bodies (landuse)",
            {"landuse": "reservoir"}
        )
        self.water_bodies = water_natural + water_landuse
        time.sleep(1)

        # Urban/residential areas
        residential = self.fetch_osm_features(
            "residential areas",
            {"landuse": "residential"}
        )
        time.sleep(1)

        commercial = self.fetch_osm_features(
            "commercial areas",
            {"landuse": "commercial"}
        )
        time.sleep(1)

        industrial = self.fetch_osm_features(
            "industrial areas",
            {"landuse": "industrial"}
        )
        self.urban_areas = residential + commercial + industrial

        print("\n" + "="*60)
        print("TERRAIN FEATURES LOADED")
        print("="*60)
        print(f"Forests: {len(self.forests)}")
        print(f"Water bodies: {len(self.water_bodies)}")
        print(f"Urban areas: {len(self.urban_areas)}")
        print("="*60 + "\n")

    def find_nearest_feature(
        self, lat: float, lon: float, features: List[Tuple[float, float]]
    ) -> Tuple[float, float]:
        """
        Find distance and bearing to nearest feature.

        Returns:
            (distance_km, bearing_degrees)
        """
        if not features:
            return (999.0, 0.0)  # No feature found

        min_distance = float('inf')
        nearest_bearing = 0.0

        for feat_lat, feat_lon in features:
            distance = self.haversine_distance(lat, lon, feat_lat, feat_lon)
            if distance < min_distance:
                min_distance = distance
                nearest_bearing = self.calculate_bearing(lat, lon, feat_lat, feat_lon)

        return (min_distance, nearest_bearing)

    def classify_terrain_type(
        self, dist_forest: float, dist_water: float, dist_urban: float
    ) -> str:
        """
        Classify terrain type based on proximity to features.

        Args:
            dist_forest: Distance to nearest forest (km)
            dist_water: Distance to nearest water (km)
            dist_urban: Distance to nearest urban (km)

        Returns:
            Terrain type string
        """
        # Very close to feature
        if dist_forest < 0.2:
            return "forest"
        if dist_water < 0.2:
            return "water"
        if dist_urban < 0.2:
            return "urban"

        # Near feature edge (thermal triggers!)
        if dist_forest < 0.5:
            return "forest_edge"
        if dist_urban < 0.5:
            return "urban_edge"

        # Open field
        return "field"

    def add_terrain_columns(self):
        """Add terrain feature columns to igc_data table."""
        print("Adding terrain columns to igc_data table...")

        columns_to_add = [
            ("distance_to_forest", "REAL"),
            ("bearing_to_forest", "REAL"),
            ("distance_to_water", "REAL"),
            ("bearing_to_water", "REAL"),
            ("distance_to_urban", "REAL"),
            ("bearing_to_urban", "REAL"),
            ("terrain_type", "TEXT"),
        ]

        for col_name, col_type in columns_to_add:
            try:
                self.cursor.execute(f"ALTER TABLE igc_data ADD COLUMN {col_name} {col_type}")
                print(f"  Added column: {col_name}")
            except sqlite3.OperationalError:
                print(f"  Column already exists: {col_name}")

        self.conn.commit()

    def enrich_thermal_points(self):
        """Enrich all thermal points with terrain features."""
        print("\n" + "="*60)
        print("ENRICHING THERMAL POINTS WITH TERRAIN DATA")
        print("="*60)

        # Get all thermal points that need enrichment
        self.cursor.execute("""
            SELECT id, latitude, longitude
            FROM igc_data
            WHERE first_thermal = 1
              AND distance_to_forest IS NULL
        """)

        thermal_points = self.cursor.fetchall()
        print(f"Found {len(thermal_points)} thermal points to enrich\n")

        if not thermal_points:
            print("No thermal points need enrichment.")
            return

        # Process in batches
        batch_size = 1000
        for i in tqdm(range(0, len(thermal_points), batch_size), desc="Enriching batches"):
            batch = thermal_points[i:i+batch_size]

            updates = []
            for point_id, lat, lon in batch:
                # Find nearest features
                dist_forest, bear_forest = self.find_nearest_feature(lat, lon, self.forests)
                dist_water, bear_water = self.find_nearest_feature(lat, lon, self.water_bodies)
                dist_urban, bear_urban = self.find_nearest_feature(lat, lon, self.urban_areas)

                # Classify terrain
                terrain_type = self.classify_terrain_type(dist_forest, dist_water, dist_urban)

                updates.append((
                    dist_forest, bear_forest,
                    dist_water, bear_water,
                    dist_urban, bear_urban,
                    terrain_type,
                    point_id
                ))

            # Batch update
            self.cursor.executemany("""
                UPDATE igc_data
                SET distance_to_forest = ?,
                    bearing_to_forest = ?,
                    distance_to_water = ?,
                    bearing_to_water = ?,
                    distance_to_urban = ?,
                    bearing_to_urban = ?,
                    terrain_type = ?
                WHERE id = ?
            """, updates)

            self.conn.commit()

        print("\n✓ Terrain enrichment complete!")

    def print_statistics(self):
        """Print statistics about terrain features."""
        print("\n" + "="*60)
        print("TERRAIN ENRICHMENT STATISTICS")
        print("="*60)

        self.cursor.execute("""
            SELECT
                terrain_type,
                COUNT(*) as count,
                AVG(distance_to_forest) as avg_dist_forest,
                AVG(distance_to_urban) as avg_dist_urban
            FROM igc_data
            WHERE first_thermal = 1
              AND terrain_type IS NOT NULL
            GROUP BY terrain_type
            ORDER BY count DESC
        """)

        print("\nThermal distribution by terrain type:")
        print(f"{'Terrain Type':<20} {'Count':<10} {'Avg Dist Forest':<18} {'Avg Dist Urban':<15}")
        print("-" * 60)

        for terrain_type, count, avg_forest, avg_urban in self.cursor.fetchall():
            print(f"{terrain_type:<20} {count:<10} {avg_forest:<18.2f} {avg_urban:<15.2f}")

        print("="*60 + "\n")

    def close(self):
        """Close database connection."""
        self.conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Terrain enrichment for thermal prediction')
    parser.add_argument('--yes', '-y', action='store_true', help='Skip confirmation prompt')
    parser.add_argument('--db-path', default='data/flights.db', help='Path to database')
    args = parser.parse_args()

    print("="*60)
    print("TERRAIN ENRICHMENT FOR THERMAL PREDICTION")
    print("="*60)
    print("\nThis will:")
    print("  1. Fetch terrain features from OpenStreetMap")
    print("     - Forests and tree lines")
    print("     - Water bodies")
    print("     - Urban/residential areas")
    print("  2. Add terrain columns to igc_data table")
    print("  3. Calculate distances and bearings for all thermal points")
    print("  4. Classify terrain types")
    print("\nNote: This makes ~6 API calls to OpenStreetMap (rate-limited)")
    print("      and processes all thermal points in the database.")
    print("="*60 + "\n")

    if not args.yes:
        response = input("Proceed? (yes/no): ")
        if response.lower() not in ['yes', 'y']:
            print("Cancelled.")
            exit(0)

    enricher = TerrainEnricher(db_path=args.db_path)

    # Fetch terrain features from OSM
    enricher.fetch_all_terrain_features()

    # Add columns to database
    enricher.add_terrain_columns()

    # Enrich all thermal points
    enricher.enrich_thermal_points()

    # Show statistics
    enricher.print_statistics()

    enricher.close()

    print("✓ Done! Now run FindThermals_improved.py (if not done yet)")
    print("  then thermal_predictor_advanced.py for terrain-aware predictions.")
