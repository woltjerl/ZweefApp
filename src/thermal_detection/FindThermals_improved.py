"""
Improved Thermal Detection Algorithm

Detects ALL thermal points at winch-reachable altitude (200-600m).
Marks all GPS points where lift exists within reach of winch launch.

Key improvements over original:
- Altitude-based winch release detection (finds actual release altitude)
- Requires sustained climb to detect thermal start (3+ consecutive points with vs > 1.0 m/s)
- Marks ALL points in first thermal while altitude is winch-reachable (< 600m)
- Includes ALL circling positions with lift (not just entry point)
- Minimum vertical speed threshold (0.5 m/s to count as lift)
- Excludes aerotow plane (PH-GOZ) and aerotow flights (showing tow climb, not thermals)

Rationale:
If a glider finds lift at location X,Y at altitude 350m (reachable from 400m winch),
that's valuable prediction data - regardless of whether it's at thermal entry or
while circling. All low-altitude lift locations are useful for thermal prediction.
"""

import sqlite3
import hashlib
import json
from tqdm import tqdm
from datetime import datetime
from typing import List, Tuple, Optional


class FindThermalsImproved:
    # Configuration constants
    WINCH_LAUNCH_VS = 10.0  # m/s - minimum vertical speed to detect winch launch
    WINCH_RELEASE_ALT_MIN = 200  # m - minimum altitude for winch release
    WINCH_RELEASE_ALT_MAX = 600  # m - maximum altitude for winch release
    WINCH_REACHABLE_ALT = 600  # m - maximum altitude reachable from winch (mark points below this)
    MIN_THERMAL_START_VS = 1.0  # m/s - minimum vs to detect thermal start
    MIN_AVG_THERMAL_VS = 1.5  # m/s - minimum average climb rate to confirm thermal
    SUSTAINED_CLIMB_POINTS = 3  # consecutive points needed to confirm thermal start
    MIN_LIFT_VS = 0.5  # m/s - minimum vs to count as "lift" while in thermal
    THERMAL_TIMEOUT = 15  # seconds - gap without lift before thermal ends

    def __init__(self, db_path="data/flights.db"):
        """Initialize with database connection."""
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()

    def _config_hash(self) -> str:
        """Hash of all configuration constants. Changes when any constant is modified."""
        config = {
            'WINCH_LAUNCH_VS': self.WINCH_LAUNCH_VS,
            'WINCH_RELEASE_ALT_MIN': self.WINCH_RELEASE_ALT_MIN,
            'WINCH_RELEASE_ALT_MAX': self.WINCH_RELEASE_ALT_MAX,
            'WINCH_REACHABLE_ALT': self.WINCH_REACHABLE_ALT,
            'MIN_THERMAL_START_VS': self.MIN_THERMAL_START_VS,
            'MIN_AVG_THERMAL_VS': self.MIN_AVG_THERMAL_VS,
            'SUSTAINED_CLIMB_POINTS': self.SUSTAINED_CLIMB_POINTS,
            'MIN_LIFT_VS': self.MIN_LIFT_VS,
            'THERMAL_TIMEOUT': self.THERMAL_TIMEOUT,
        }
        return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]

    def _get_flight_count(self) -> int:
        """Count flights (excluding aerotow) currently in the database."""
        self.cursor.execute("""
            SELECT COUNT(DISTINCT id) FROM flights
            WHERE registratie NOT IN ('PH-GOZ', 'PH GOZ')
              AND start_methode != 'sleep'
        """)
        return self.cursor.fetchone()[0]

    def _init_metadata_table(self):
        """Create metadata table if it doesn't exist."""
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS thermal_detection_metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        self.conn.commit()

    def _add_thermal_processed_column(self):
        """Add thermal_processed column to flights table if it doesn't exist."""
        try:
            self.cursor.execute("ALTER TABLE flights ADD COLUMN thermal_processed INTEGER DEFAULT 0")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

    def needs_update(self) -> Tuple[bool, str, bool]:
        """Check if thermal detection needs to run.

        Returns:
            (needs_update, reason, full_reprocess) - full_reprocess=True means config changed,
            full_reprocess=False means only new flights need processing.
        """
        self._init_metadata_table()
        self._add_thermal_processed_column()

        self.cursor.execute("SELECT key, value FROM thermal_detection_metadata")
        metadata = dict(self.cursor.fetchall())

        if not metadata:
            return True, "First run - no previous detection found", True

        current_config_hash = self._config_hash()
        current_flight_count = self._get_flight_count()

        prev_config_hash = metadata.get('config_hash')
        prev_flight_count = int(metadata.get('flight_count', '0'))

        if prev_config_hash != current_config_hash:
            return True, "Configuration constants changed - full reprocess", True

        if current_flight_count != prev_flight_count:
            diff = current_flight_count - prev_flight_count
            return True, f"New flights detected ({diff:+d}: {prev_flight_count} -> {current_flight_count})", False

        last_run = metadata.get('last_run', 'unknown')
        return False, f"Up to date (last run: {last_run}, {prev_flight_count} flights, config: {prev_config_hash})", False

    def save_run_metadata(self):
        """Save current state after a successful detection run."""
        self._init_metadata_table()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entries = {
            'config_hash': self._config_hash(),
            'flight_count': str(self._get_flight_count()),
            'last_run': now,
        }
        for key, value in entries.items():
            self.cursor.execute(
                "INSERT OR REPLACE INTO thermal_detection_metadata (key, value) VALUES (?, ?)",
                (key, value)
            )
        self.conn.commit()

    def add_first_thermal_column(self):
        """Add first_thermal column if it doesn't exist."""
        try:
            self.cursor.execute("ALTER TABLE igc_data ADD COLUMN first_thermal INTEGER DEFAULT 0")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

    def reset_first_thermal(self):
        """Reset all first_thermal flags to 0."""
        print("Resetting all first_thermal flags...")
        self.cursor.execute("UPDATE igc_data SET first_thermal = 0")
        self.conn.commit()

    def time_to_seconds(self, time_str: str) -> Optional[int]:
        """Convert HH:MM:SS to seconds since midnight."""
        try:
            t = datetime.strptime(time_str, "%H:%M:%S")
            return t.hour * 3600 + t.minute * 60 + t.second
        except Exception:
            return None

    def find_winch_launch(self, records: List[Tuple]) -> Optional[int]:
        """
        Find winch launch by detecting rapid vertical speed.

        Args:
            records: List of (id, sequence, time, altitude, vertical_speed) tuples

        Returns:
            Index of launch point, or None if not found
        """
        for idx, (rec_id, seq, time_str, alt, vs) in enumerate(records):
            if vs is not None and vs > self.WINCH_LAUNCH_VS:
                return idx
        return None

    def find_winch_release(self, records: List[Tuple], launch_index: int) -> Optional[int]:
        """
        Find winch release by detecting altitude peak in winch launch range.

        More reliable than looking for negative vertical speed.

        Args:
            records: List of (id, sequence, time, altitude, vertical_speed) tuples
            launch_index: Index where winch launch started

        Returns:
            Index of release point, or None if not found
        """
        max_alt = 0
        max_alt_index = None

        # Look for altitude peak in winch launch range
        for idx, (rec_id, seq, time_str, alt, vs) in enumerate(
            records[launch_index:], start=launch_index
        ):
            if alt is None:
                continue

            # Track maximum altitude
            if alt > max_alt:
                max_alt = alt
                max_alt_index = idx

            # If we're in the typical winch release range and altitude is dropping
            if (self.WINCH_RELEASE_ALT_MIN < alt < self.WINCH_RELEASE_ALT_MAX
                and vs is not None and vs < -1.0 and max_alt_index is not None):
                return max_alt_index

        return max_alt_index

    def detect_sustained_climb(
        self, records: List[Tuple], start_index: int
    ) -> Optional[int]:
        """
        Detect first sustained climb (thermal start).

        Requires: consecutive points with vertical_speed > MIN_THERMAL_START_VS

        Args:
            records: List of (id, sequence, time, altitude, vertical_speed) tuples
            start_index: Index to start searching from

        Returns:
            Index where thermal starts, or None if not found
        """
        consecutive_climb = 0
        climb_start_index = None
        climb_rates = []

        for idx, (rec_id, seq, time_str, alt, vs) in enumerate(
            records[start_index:], start=start_index
        ):
            if vs is None or alt is None:
                consecutive_climb = 0
                climb_start_index = None
                climb_rates = []
                continue

            # Check if climbing sufficiently
            if vs >= self.MIN_THERMAL_START_VS:
                if consecutive_climb == 0:
                    climb_start_index = idx

                consecutive_climb += 1
                climb_rates.append(vs)

                # Check if we have sustained climb
                if consecutive_climb >= self.SUSTAINED_CLIMB_POINTS:
                    avg_climb = sum(climb_rates) / len(climb_rates)

                    # Verify average climb rate is good enough
                    if avg_climb >= self.MIN_AVG_THERMAL_VS:
                        return climb_start_index
            else:
                # Reset if climb interrupted
                consecutive_climb = 0
                climb_start_index = None
                climb_rates = []

        return None

    def mark_thermal_segment(
        self, records: List[Tuple], thermal_start_index: int
    ) -> List[int]:
        """
        Mark all points in thermal segment at winch-reachable altitude.

        Marks points where:
        - Altitude < WINCH_REACHABLE_ALT (can reach from winch launch)
        - Vertical speed > MIN_LIFT_VS (rising air)
        - Within THERMAL_TIMEOUT seconds of last lift

        Args:
            records: List of (id, sequence, time, altitude, vertical_speed) tuples
            thermal_start_index: Index where thermal was detected

        Returns:
            List of record IDs to mark as first_thermal
        """
        thermal_ids = []
        last_lift_time = None

        for idx in range(thermal_start_index, len(records)):
            rec_id, seq, time_str, alt, vs = records[idx]

            # Parse time
            current_time = self.time_to_seconds(time_str)
            if current_time is None:
                continue

            # Check if we've left the thermal (timeout)
            if last_lift_time is not None:
                time_gap = current_time - last_lift_time
                if time_gap >= self.THERMAL_TIMEOUT:
                    break  # Thermal ended

            # Skip if altitude is too high (not winch-reachable)
            if alt is None or alt >= self.WINCH_REACHABLE_ALT:
                continue

            # Mark points with lift
            if vs is not None and vs >= self.MIN_LIFT_VS:
                thermal_ids.append(rec_id)
                last_lift_time = current_time
            # Also mark points with slight sink (part of circling)
            elif vs is not None and vs > -1.0 and last_lift_time is not None:
                # Allow some sink during circling, but update timeout
                thermal_ids.append(rec_id)

        return thermal_ids

    def update_first_thermal(self, incremental: bool = False):
        """
        Update first_thermal flags for flights.

        Args:
            incremental: If True, only process flights not yet analyzed.
                         If False, reset and reprocess all flights.
        """
        if not incremental:
            self.reset_first_thermal()
            # Reset processed flags
            self.cursor.execute("UPDATE flights SET thermal_processed = 0")
            self.conn.commit()

        # Get unprocessed flight_ids, excluding aerotow plane and aerotow flights
        self.cursor.execute("""
            SELECT DISTINCT f.id
            FROM flights f
            WHERE f.registratie NOT IN ('PH-GOZ', 'PH GOZ')
              AND f.start_methode != 'sleep'
              AND f.thermal_processed = 0
        """)
        flight_ids = [row[0] for row in self.cursor.fetchall()]

        if not flight_ids:
            print("No new flights to process.")
            return

        label = "Processing new flights" if incremental else "Detecting thermal locations"
        print(f"{'Incremental' if incremental else 'Full'} run: {len(flight_ids)} flights to process\n")

        stats = {
            'processed': 0,
            'thermals_found': 0,
            'total_thermal_points': 0,
            'no_launch': 0,
            'no_release': 0,
            'no_thermal': 0
        }

        for flight_id in tqdm(flight_ids, desc=label):
            # Get flight data with altitude
            self.cursor.execute("""
                SELECT id, sequence, time, altitude, vertical_speed
                FROM igc_data
                WHERE flight_id=?
                ORDER BY sequence
            """, (flight_id,))
            records = self.cursor.fetchall()

            # Mark flight as processed regardless of outcome
            self.cursor.execute("UPDATE flights SET thermal_processed = 1 WHERE id = ?", (flight_id,))

            if not records:
                continue

            stats['processed'] += 1

            # Step 1: Find winch launch
            launch_index = self.find_winch_launch(records)
            if launch_index is None:
                stats['no_launch'] += 1
                continue

            # Step 2: Find winch release (altitude-based)
            release_index = self.find_winch_release(records, launch_index)
            if release_index is None:
                stats['no_release'] += 1
                continue

            # Step 3: Detect first sustained thermal after release
            thermal_start_index = self.detect_sustained_climb(records, release_index)
            if thermal_start_index is None:
                stats['no_thermal'] += 1
                continue

            # Step 4: Mark ALL points in thermal at winch-reachable altitude
            thermal_ids = self.mark_thermal_segment(records, thermal_start_index)

            # Update database
            if thermal_ids:
                placeholders = ','.join(['?'] * len(thermal_ids))
                query = f"UPDATE igc_data SET first_thermal = 1 WHERE id IN ({placeholders})"
                self.cursor.execute(query, thermal_ids)
                stats['thermals_found'] += 1
                stats['total_thermal_points'] += len(thermal_ids)

        self.conn.commit()

        # Print statistics
        print("\n" + "="*60)
        print("THERMAL DETECTION COMPLETE")
        print("="*60)
        print(f"Flights processed: {stats['processed']}")
        print(f"Thermals found: {stats['thermals_found']}")
        print(f"Total thermal points marked: {stats['total_thermal_points']}")
        print(f"No winch launch: {stats['no_launch']}")
        print(f"No winch release: {stats['no_release']}")
        print(f"No thermal detected: {stats['no_thermal']}")

        # Calculate average points per thermal
        if stats['thermals_found'] > 0:
            avg_points = stats['total_thermal_points'] / stats['thermals_found']
            print(f"Average points per thermal: {avg_points:.1f}")
        print("="*60)

    def close(self):
        """Close database connection."""
        self.conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Improved thermal detection')
    parser.add_argument('--yes', '-y', action='store_true', help='Skip confirmation prompt')
    parser.add_argument('--force', '-f', action='store_true', help='Force re-run even if nothing changed')
    parser.add_argument('--db-path', default='data/flights.db', help='Path to database')
    args = parser.parse_args()

    finder = FindThermalsImproved(db_path=args.db_path)

    # Check if update is needed
    update_needed, reason, full_reprocess = finder.needs_update()

    if not update_needed and not args.force:
        print(f"Skipping thermal detection: {reason}")
        finder.close()
        exit(0)

    # --force always does full reprocess
    if args.force:
        full_reprocess = True

    mode = "FULL REPROCESS" if full_reprocess else "INCREMENTAL"
    print("="*60)
    print(f"THERMAL DETECTION ({mode})")
    print("="*60)
    print(f"\nReason: {reason}")

    if not args.yes:
        response = input("\nProceed? (yes/no): ")
        if response.lower() not in ['yes', 'y']:
            print("Cancelled.")
            finder.close()
            exit(0)

    finder.add_first_thermal_column()
    finder.update_first_thermal(incremental=not full_reprocess)
    finder.save_run_metadata()
    finder.close()

    print("\n✓ Done! Run thermal_forecast.py to see improved predictions.")
