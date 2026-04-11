#!/usr/bin/env python3
"""
Runway Detection for Hilversum Airport (EHHV)

Detects which runway (7/25, 12/30, 18/36) was used for each flight
by analyzing the initial GPS track during takeoff.

Hilversum Runways:
    - Runway 07/25: 070° / 250° magnetic heading
    - Runway 12/30: 120° / 300° magnetic heading
    - Runway 18/36: 180° / 000° magnetic heading

Usage:
    python detect_runway.py                    # detect for all flights
    python detect_runway.py --force            # redetect even if already set
    python detect_runway.py --dry-run          # show what would be detected
"""

import sqlite3
import math
import argparse
from typing import Optional, Tuple, List
from tqdm import tqdm


class RunwayDetector:
    # Runway definitions (actual winch headings observed at EHHV)
    # Note: Gliding winch positions differ slightly from runway headings
    RUNWAYS = {
        '07': (50, 82),    # Runway 07: ~070° (50-82°)
        '25': (220, 275),  # Runway 25: ~250° (220-275°)
        '12': (82, 150),   # Runway 12: ~120° (82-150°) - widened to reduce overlap with 07
        '30': (275, 335),  # Runway 30: ~300° (275-335°) - winch position differs
        '18': (155, 205),  # Runway 18: ~180° (155-205°)
        '36': (335, 50),   # Runway 36: ~360°/000° (335-50°, wraps around)
    }

    def __init__(self, db_path: str = "data/flights.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()

    def _add_runway_column(self):
        """Add runway column to flights table if it doesn't exist."""
        try:
            self.cursor.execute("ALTER TABLE flights ADD COLUMN runway TEXT")
            self.conn.commit()
            print("Added 'runway' column to flights table")
        except sqlite3.OperationalError:
            pass  # Column already exists

    def _add_runway_processed_column(self):
        """Add runway_processed column to flights table if it doesn't exist."""
        try:
            self.cursor.execute("ALTER TABLE flights ADD COLUMN runway_processed INTEGER DEFAULT 0")
            self.conn.commit()
            print("Added 'runway_processed' column to flights table")
        except sqlite3.OperationalError:
            pass  # Column already exists

    def mark_existing_as_processed(self):
        """Mark all flights as processed without detecting runway (skip baseline)."""
        self._add_runway_column()
        self._add_runway_processed_column()

        self.cursor.execute("UPDATE flights SET runway_processed = 1 WHERE runway_processed = 0")
        count = self.cursor.rowcount
        self.conn.commit()

        print(f"Marked {count} existing flights as processed (skipped baseline runway detection)")
        print("Future flights will be detected incrementally.")

    def calculate_bearing(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate initial bearing between two GPS points (in degrees)."""
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        dlon = math.radians(lon2 - lon1)

        x = math.sin(dlon) * math.cos(lat2_rad)
        y = math.cos(lat1_rad) * math.sin(lat2_rad) - \
            math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon)

        bearing = math.atan2(x, y)
        bearing_deg = math.degrees(bearing)

        # Normalize to 0-360
        return (bearing_deg + 360) % 360

    def detect_runway_from_track(self, flight_id: int) -> Optional[str]:
        """
        Detect runway from GPS track during takeoff.

        Strategy:
        1. Find first 20 GPS points where altitude is increasing (takeoff roll + initial climb)
        2. Calculate bearing for each consecutive pair
        3. Average the bearings
        4. Match to nearest runway
        """
        # Get IGC data for takeoff phase (first 60 seconds, altitude 0-300m)
        self.cursor.execute("""
            SELECT latitude, longitude, altitude, vertical_speed
            FROM igc_data
            WHERE flight_id = ?
              AND altitude < 300
            ORDER BY sequence
            LIMIT 60
        """, (flight_id,))

        points = self.cursor.fetchall()

        if len(points) < 5:
            return None

        # Find points during takeoff (positive vertical speed or altitude increasing)
        takeoff_points = []
        prev_alt = 0
        for lat, lon, alt, vs in points:
            if lat and lon and alt is not None:
                if alt > prev_alt or (vs and vs > 0.5):
                    takeoff_points.append((lat, lon, alt))
                    if len(takeoff_points) >= 20:
                        break
                prev_alt = alt

        if len(takeoff_points) < 3:
            return None

        # Calculate bearings between consecutive points
        bearings = []
        for i in range(len(takeoff_points) - 1):
            lat1, lon1, _ = takeoff_points[i]
            lat2, lon2, _ = takeoff_points[i + 1]
            bearing = self.calculate_bearing(lat1, lon1, lat2, lon2)
            bearings.append(bearing)

        if not bearings:
            return None

        # Average bearing (handling circular mean for angles)
        avg_bearing = self._circular_mean(bearings)

        # Match to runway
        return self._match_runway(avg_bearing)

    def _circular_mean(self, angles: List[float]) -> float:
        """Calculate circular mean of angles (handles 360° wrap-around)."""
        sin_sum = sum(math.sin(math.radians(a)) for a in angles)
        cos_sum = sum(math.cos(math.radians(a)) for a in angles)
        mean_rad = math.atan2(sin_sum, cos_sum)
        mean_deg = math.degrees(mean_rad)
        return (mean_deg + 360) % 360

    def _match_runway(self, bearing: float) -> Optional[str]:
        """Match bearing to nearest runway."""
        for runway, (min_hdg, max_hdg) in self.RUNWAYS.items():
            if min_hdg > max_hdg:  # Wraps around 360° (runway 36)
                if bearing >= min_hdg or bearing <= max_hdg:
                    return runway
            else:
                if min_hdg <= bearing <= max_hdg:
                    return runway
        return None

    def detect_all_runways(self, force: bool = False, null_only: bool = False, dry_run: bool = False):
        """Detect runway for all flights."""
        if not dry_run:
            self._add_runway_column()
            self._add_runway_processed_column()

        # Reset processed flag if force
        if force and not dry_run:
            self.cursor.execute("UPDATE flights SET runway_processed = 0")
            self.conn.commit()

        # Count total flights and already processed
        self.cursor.execute("SELECT COUNT(*) FROM flights")
        total_flights = self.cursor.fetchone()[0]

        if null_only:
            self.cursor.execute("SELECT COUNT(*) FROM flights WHERE runway IS NOT NULL")
            already_processed = self.cursor.fetchone()[0]
        else:
            self.cursor.execute("SELECT COUNT(*) FROM flights WHERE runway_processed = 1")
            already_processed = self.cursor.fetchone()[0]

        # Get flights that need runway detection
        if null_only:
            query = "SELECT id, start_methode, registratie FROM flights WHERE runway IS NULL"
            mode = "NULL-ONLY"
        elif force or dry_run:
            query = "SELECT id, start_methode, registratie FROM flights WHERE runway_processed = 0"
            mode = "FORCE" if force else "DRY-RUN"
        else:
            query = "SELECT id, start_methode, registratie FROM flights WHERE runway_processed = 0"
            mode = "INCREMENTAL"

        self.cursor.execute(query)
        flights = self.cursor.fetchall()

        if not flights:
            print(f"✓ All {total_flights} flights already have runway detected.")
            return

        print(f"Runway Detection ({mode})")
        print(f"  Total flights in DB: {total_flights}")
        print(f"  Already processed:   {already_processed}")
        print(f"  To process:          {len(flights)}\n")

        stats = {
            '07': 0, '25': 0,
            '12': 0, '30': 0,
            '18': 0, '36': 0,
            'unknown': 0
        }

        for flight_id, start_methode, registratie in tqdm(flights, desc="Detecting runways"):
            # Skip aerotow plane
            if registratie in ('PH-GOZ', 'PH GOZ'):
                if not dry_run:
                    self.cursor.execute(
                        "UPDATE flights SET runway_processed = 1 WHERE id = ?",
                        (flight_id,)
                    )
                continue

            runway = self.detect_runway_from_track(flight_id)

            if runway:
                stats[runway] += 1
            else:
                stats['unknown'] += 1

            if not dry_run:
                self.cursor.execute(
                    "UPDATE flights SET runway = ?, runway_processed = 1 WHERE id = ?",
                    (runway, flight_id)
                )

        if not dry_run:
            self.conn.commit()

        # Print statistics
        print("\n" + "="*60)
        print("RUNWAY DETECTION COMPLETE")
        print("="*60)
        print(f"\nProcessed {len(flights)} flights:")
        print(f"  Runway 07: {stats['07']:5d} flights")
        print(f"  Runway 25: {stats['25']:5d} flights")
        print(f"  Runway 12: {stats['12']:5d} flights")
        print(f"  Runway 30: {stats['30']:5d} flights")
        print(f"  Runway 18: {stats['18']:5d} flights")
        print(f"  Runway 36: {stats['36']:5d} flights")
        print(f"  Unknown:   {stats['unknown']:5d} flights\n")

        # Group by runway pair
        print("Summary by runway pair:")
        print(f"  07/25: {stats['07'] + stats['25']:5d} flights")
        print(f"  12/30: {stats['12'] + stats['30']:5d} flights")
        print(f"  18/36: {stats['18'] + stats['36']:5d} flights")
        print("="*60)

    def close(self):
        """Close database connection."""
        self.conn.close()


def main():
    parser = argparse.ArgumentParser(
        description='Detect runway for Hilversum flights'
    )
    parser.add_argument('--force', '-f', action='store_true',
                       help='Redetect all flights (ignore runway_processed flag)')
    parser.add_argument('--null-only', action='store_true',
                       help='Only process flights where runway IS NULL')
    parser.add_argument('--skip-baseline', action='store_true',
                       help='Mark all existing flights as processed (skip baseline, only detect new flights)')
    parser.add_argument('--dry-run', action='store_true',
                       help='Show what would be detected without updating database')
    parser.add_argument('--db-path', default='data/flights.db',
                       help='Path to database')
    args = parser.parse_args()

    detector = RunwayDetector(db_path=args.db_path)

    if args.skip_baseline:
        detector.mark_existing_as_processed()
    else:
        detector.detect_all_runways(force=args.force, null_only=args.null_only, dry_run=args.dry_run)

    detector.close()

    if not args.skip_baseline:
        print("\nRunway data added to flights table.")
        print("You can now filter predictions by runway in visualization scripts.")


if __name__ == "__main__":
    main()
