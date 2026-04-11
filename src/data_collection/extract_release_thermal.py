#!/usr/bin/env python3
"""
Extract Release-to-Thermal Navigation Data

For each winch launch flight, extracts:
- Winch release point (lat, lon, alt)
- First thermal entry point (lat, lon, alt)
- Bearing and distance from release to thermal
- Time elapsed between release and thermal
- Flight outcome (thermal found vs landed back)
- Maximum altitude reached

This data answers: "After winch release, which direction should I fly to find a thermal?"

Usage:
    python extract_release_thermal.py              # process all flights
    python extract_release_thermal.py --force      # reprocess all
    python extract_release_thermal.py --dry-run    # show stats without writing
"""

import sqlite3
import math
import argparse
from tqdm import tqdm
from datetime import datetime
from typing import Optional, Tuple, List


class ReleaseToThermalExtractor:
    # Same detection constants as FindThermals_improved
    WINCH_LAUNCH_VS = 10.0
    WINCH_RELEASE_ALT_MIN = 200
    WINCH_RELEASE_ALT_MAX = 600
    MIN_THERMAL_START_VS = 1.0
    MIN_AVG_THERMAL_VS = 1.5
    SUSTAINED_CLIMB_POINTS = 3

    def __init__(self, db_path: str = "data/flights.db"):
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()

    def _add_columns(self):
        """Add release-to-thermal columns to flights table."""
        columns = [
            ("release_lat", "REAL"),
            ("release_lon", "REAL"),
            ("release_alt", "REAL"),
            ("thermal_entry_lat", "REAL"),
            ("thermal_entry_lon", "REAL"),
            ("thermal_entry_alt", "REAL"),
            ("release_to_thermal_bearing", "REAL"),
            ("release_to_thermal_distance_m", "REAL"),
            ("release_to_thermal_seconds", "INTEGER"),
            ("flight_duration_after_release", "INTEGER"),
            ("max_altitude", "REAL"),
            ("flight_outcome", "TEXT"),
            ("release_thermal_processed", "INTEGER DEFAULT 0"),
        ]
        for col_name, col_type in columns:
            try:
                self.cursor.execute(f"ALTER TABLE flights ADD COLUMN {col_name} {col_type}")
                self.conn.commit()
            except sqlite3.OperationalError:
                pass

    def calculate_bearing(self, lat1, lon1, lat2, lon2):
        """Calculate bearing between two GPS points."""
        lat1_r = math.radians(lat1)
        lat2_r = math.radians(lat2)
        dlon = math.radians(lon2 - lon1)
        x = math.sin(dlon) * math.cos(lat2_r)
        y = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon)
        return (math.degrees(math.atan2(x, y)) + 360) % 360

    def haversine(self, lat1, lon1, lat2, lon2):
        """Calculate distance in meters between two GPS points."""
        R = 6371000
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    def time_to_seconds(self, time_str: str) -> Optional[int]:
        """Convert HH:MM:SS to seconds since midnight."""
        try:
            t = datetime.strptime(time_str, "%H:%M:%S")
            return t.hour * 3600 + t.minute * 60 + t.second
        except Exception:
            return None

    def find_winch_launch(self, records):
        """Find winch launch by detecting rapid vertical speed."""
        for idx, rec in enumerate(records):
            vs = rec[6]  # vertical_speed
            if vs is not None and vs > self.WINCH_LAUNCH_VS:
                return idx
        return None

    def find_winch_release(self, records, launch_index):
        """Find winch release by detecting altitude peak."""
        max_alt = 0
        max_alt_index = None
        for idx in range(launch_index, len(records)):
            alt = records[idx][5]  # altitude
            vs = records[idx][6]   # vertical_speed
            if alt is None:
                continue
            if alt > max_alt:
                max_alt = alt
                max_alt_index = idx
            if (self.WINCH_RELEASE_ALT_MIN < alt < self.WINCH_RELEASE_ALT_MAX
                and vs is not None and vs < -1.0 and max_alt_index is not None):
                return max_alt_index
        return max_alt_index

    def find_thermal_start(self, records, start_index):
        """Find first sustained thermal climb after release."""
        consecutive = 0
        start_idx = None
        rates = []
        for idx in range(start_index, len(records)):
            vs = records[idx][6]
            alt = records[idx][5]
            if vs is None or alt is None:
                consecutive = 0
                start_idx = None
                rates = []
                continue
            if vs >= self.MIN_THERMAL_START_VS:
                if consecutive == 0:
                    start_idx = idx
                consecutive += 1
                rates.append(vs)
                if consecutive >= self.SUSTAINED_CLIMB_POINTS:
                    if sum(rates) / len(rates) >= self.MIN_AVG_THERMAL_VS:
                        return start_idx
            else:
                consecutive = 0
                start_idx = None
                rates = []
        return None

    def process_flight(self, flight_id: int) -> dict:
        """
        Extract release-to-thermal data for a single flight.

        Returns dict with all extracted fields.
        """
        # Get full flight track with coordinates
        self.cursor.execute("""
            SELECT id, sequence, time, latitude, longitude, altitude, vertical_speed
            FROM igc_data
            WHERE flight_id = ?
            ORDER BY sequence
        """, (flight_id,))
        records = self.cursor.fetchall()

        result = {
            'release_lat': None, 'release_lon': None, 'release_alt': None,
            'thermal_entry_lat': None, 'thermal_entry_lon': None, 'thermal_entry_alt': None,
            'release_to_thermal_bearing': None, 'release_to_thermal_distance_m': None,
            'release_to_thermal_seconds': None,
            'flight_duration_after_release': None,
            'max_altitude': None,
            'flight_outcome': 'unknown',
        }

        if len(records) < 20:
            return result

        # Step 1: Find winch launch
        launch_idx = self.find_winch_launch(records)
        if launch_idx is None:
            return result

        # Step 2: Find winch release
        release_idx = self.find_winch_release(records, launch_idx)
        if release_idx is None:
            return result

        release_rec = records[release_idx]
        result['release_lat'] = release_rec[3]
        result['release_lon'] = release_rec[4]
        result['release_alt'] = release_rec[5]

        release_time = self.time_to_seconds(release_rec[2])

        # Max altitude in the flight
        max_alt = max((r[5] for r in records if r[5] is not None), default=None)
        result['max_altitude'] = max_alt

        # Flight duration after release
        last_time = None
        for rec in reversed(records):
            t = self.time_to_seconds(rec[2])
            if t is not None:
                last_time = t
                break
        if release_time is not None and last_time is not None:
            duration = last_time - release_time
            if duration < 0:
                duration += 86400  # handle midnight wrap
            result['flight_duration_after_release'] = duration

        # Step 3: Find first thermal
        thermal_idx = self.find_thermal_start(records, release_idx)

        if thermal_idx is None:
            # No thermal found - pilot landed back
            result['flight_outcome'] = 'landed_back'
            return result

        thermal_rec = records[thermal_idx]
        result['thermal_entry_lat'] = thermal_rec[3]
        result['thermal_entry_lon'] = thermal_rec[4]
        result['thermal_entry_alt'] = thermal_rec[5]
        result['flight_outcome'] = 'thermal_found'

        # Calculate bearing and distance from release to thermal
        if (result['release_lat'] and result['release_lon']
            and result['thermal_entry_lat'] and result['thermal_entry_lon']):
            result['release_to_thermal_bearing'] = self.calculate_bearing(
                result['release_lat'], result['release_lon'],
                result['thermal_entry_lat'], result['thermal_entry_lon']
            )
            result['release_to_thermal_distance_m'] = self.haversine(
                result['release_lat'], result['release_lon'],
                result['thermal_entry_lat'], result['thermal_entry_lon']
            )

        # Time from release to thermal
        thermal_time = self.time_to_seconds(thermal_rec[2])
        if release_time is not None and thermal_time is not None:
            seconds = thermal_time - release_time
            if seconds < 0:
                seconds += 86400
            result['release_to_thermal_seconds'] = seconds

        return result

    def run(self, force: bool = False, dry_run: bool = False):
        """Process all winch launch flights."""
        if not dry_run:
            self._add_columns()

        if force and not dry_run:
            self.cursor.execute("UPDATE flights SET release_thermal_processed = 0")
            self.conn.commit()

        # Get flights to process
        self.cursor.execute("""
            SELECT id FROM flights
            WHERE registratie NOT IN ('PH-GOZ', 'PH GOZ')
              AND start_methode != 'sleep'
              AND (release_thermal_processed = 0 OR release_thermal_processed IS NULL)
        """)
        flight_ids = [r[0] for r in self.cursor.fetchall()]

        self.cursor.execute("SELECT COUNT(*) FROM flights")
        total = self.cursor.fetchone()[0]

        if not flight_ids:
            print(f"All {total} flights already processed.")
            return

        print(f"Release-to-Thermal Extraction")
        print(f"  Total flights: {total}")
        print(f"  To process: {len(flight_ids)}")
        if dry_run:
            print(f"  Mode: DRY-RUN\n")
        else:
            print()

        stats = {
            'processed': 0,
            'thermal_found': 0,
            'landed_back': 0,
            'no_launch': 0,
            'unknown': 0,
            'bearings': [],
            'distances': [],
            'times': [],
            'release_alts': [],
        }

        for flight_id in tqdm(flight_ids, desc="Extracting release-to-thermal"):
            result = self.process_flight(flight_id)
            stats['processed'] += 1

            outcome = result['flight_outcome']
            if outcome == 'thermal_found':
                stats['thermal_found'] += 1
                if result['release_to_thermal_bearing'] is not None:
                    stats['bearings'].append(result['release_to_thermal_bearing'])
                if result['release_to_thermal_distance_m'] is not None:
                    stats['distances'].append(result['release_to_thermal_distance_m'])
                if result['release_to_thermal_seconds'] is not None:
                    stats['times'].append(result['release_to_thermal_seconds'])
            elif outcome == 'landed_back':
                stats['landed_back'] += 1
            else:
                if result['release_alt'] is None:
                    stats['no_launch'] += 1
                else:
                    stats['unknown'] += 1

            if result['release_alt'] is not None:
                stats['release_alts'].append(result['release_alt'])

            if not dry_run:
                self.cursor.execute("""
                    UPDATE flights SET
                        release_lat = ?, release_lon = ?, release_alt = ?,
                        thermal_entry_lat = ?, thermal_entry_lon = ?, thermal_entry_alt = ?,
                        release_to_thermal_bearing = ?, release_to_thermal_distance_m = ?,
                        release_to_thermal_seconds = ?,
                        flight_duration_after_release = ?,
                        max_altitude = ?, flight_outcome = ?,
                        release_thermal_processed = 1
                    WHERE id = ?
                """, (
                    result['release_lat'], result['release_lon'], result['release_alt'],
                    result['thermal_entry_lat'], result['thermal_entry_lon'], result['thermal_entry_alt'],
                    result['release_to_thermal_bearing'], result['release_to_thermal_distance_m'],
                    result['release_to_thermal_seconds'],
                    result['flight_duration_after_release'],
                    result['max_altitude'], result['flight_outcome'],
                    flight_id
                ))

        if not dry_run:
            self.conn.commit()

        # Print results
        print("\n" + "=" * 60)
        print("RELEASE-TO-THERMAL EXTRACTION COMPLETE")
        print("=" * 60)
        print(f"\nProcessed: {stats['processed']} flights")
        print(f"  Thermal found:  {stats['thermal_found']} ({100*stats['thermal_found']/max(stats['processed'],1):.0f}%)")
        print(f"  Landed back:    {stats['landed_back']} ({100*stats['landed_back']/max(stats['processed'],1):.0f}%)")
        print(f"  No winch launch:{stats['no_launch']}")
        print(f"  Unknown:        {stats['unknown']}")

        if stats['release_alts']:
            print(f"\nRelease altitude:")
            print(f"  Median: {sorted(stats['release_alts'])[len(stats['release_alts'])//2]:.0f}m")
            print(f"  Min: {min(stats['release_alts']):.0f}m  Max: {max(stats['release_alts']):.0f}m")

        if stats['distances']:
            dists = sorted(stats['distances'])
            print(f"\nDistance release → thermal:")
            print(f"  Median: {dists[len(dists)//2]:.0f}m")
            print(f"  25th percentile: {dists[len(dists)//4]:.0f}m")
            print(f"  75th percentile: {dists[3*len(dists)//4]:.0f}m")

        if stats['times']:
            times = sorted(stats['times'])
            print(f"\nTime release → thermal:")
            print(f"  Median: {times[len(times)//2]}s")
            print(f"  25th percentile: {times[len(times)//4]}s")
            print(f"  75th percentile: {times[3*len(times)//4]}s")

        if stats['bearings']:
            # Show bearing distribution in 45° sectors
            sectors = {'N': 0, 'NE': 0, 'E': 0, 'SE': 0, 'S': 0, 'SW': 0, 'W': 0, 'NW': 0}
            sector_names = list(sectors.keys())
            for b in stats['bearings']:
                idx = round(b / 45) % 8
                sectors[sector_names[idx]] += 1

            print(f"\nBearing release → thermal (direction pilots flew):")
            max_count = max(sectors.values())
            for name, count in sectors.items():
                bar = "#" * (count * 30 // max(max_count, 1))
                pct = 100 * count / len(stats['bearings'])
                print(f"  {name:2s}: {count:4d} ({pct:4.1f}%) {bar}")

        print("=" * 60)

    def close(self):
        self.conn.close()


def main():
    parser = argparse.ArgumentParser(description='Extract release-to-thermal navigation data')
    parser.add_argument('--force', '-f', action='store_true', help='Reprocess all flights')
    parser.add_argument('--dry-run', action='store_true', help='Show stats without writing')
    parser.add_argument('--db-path', default='data/flights.db', help='Path to database')
    args = parser.parse_args()

    extractor = ReleaseToThermalExtractor(db_path=args.db_path)
    extractor.run(force=args.force, dry_run=args.dry_run)
    extractor.close()


if __name__ == "__main__":
    main()
