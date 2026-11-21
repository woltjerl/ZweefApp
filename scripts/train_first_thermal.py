#!/usr/bin/env python3
"""
Rebuild everything the first-thermal forecast needs, in order:

  1. convection weather backfill (incremental)
  2. climb detector (incremental; full rebuild when detector settings change), then write
     its results into the old label columns (flights.flight_outcome, igc_data.first_thermal, …)
  3. layer A (connect probability; writes out-of-fold pA per launch)
  4. layer B artefact (per-cell rates; uses data/models/tuned_params.json if present)
  5. layer C (pilot skill, ace routes)
  6. optional: prediction-power report (--report)

Usage:
    python scripts/train_first_thermal.py
    python scripts/train_first_thermal.py --report
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def step(title, args):
    print('-' * 60)
    print(title)
    print('-' * 60, flush=True)
    r = subprocess.run([PY] + args, cwd=ROOT)
    if r.returncode != 0:
        print(f"\nWARNING: '{title}' failed (exit code {r.returncode})\n")
    return r.returncode == 0


def main():
    p = argparse.ArgumentParser(description='Train the first-thermal models')
    p.add_argument('--report', action='store_true', help='also regenerate the prediction-power report')
    p.add_argument('--force-detector', action='store_true', help='rebuild the climb tables from scratch')
    p.add_argument('--db-path', default=str(ROOT / 'data' / 'flights.db'))
    args = p.parse_args()

    ok = step('Convection weather (Open-Meteo)', ['scripts/backfill_convection_weather.py', '--db-path', args.db_path])
    det = ['-m', 'src.thermal_detection.climb_detector', '--db', args.db_path]
    if args.force_detector:
        det.append('--force')
    ok &= step('Climb detector', det)
    ok &= step('Old label columns ← climb detector', ['-m', 'src.thermal_detection.sync_legacy_columns',
                                                      '--db', args.db_path])
    ok &= step('Layer A: chance to connect', ['-m', 'src.prediction.layer_a', '--db', args.db_path])
    ok &= step('Layer B: per-cell rates', ['-m', 'src.prediction.layer_b', '--db', args.db_path])
    ok &= step('Layer C: pilot skill and routes', ['-m', 'src.prediction.layer_c', '--db', args.db_path])
    if args.report:
        ok &= step('Prediction-power report', ['scripts/evaluate_prediction.py', '--db', args.db_path])
    print('\n✓ done' if ok else '\nfinished with warnings')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
