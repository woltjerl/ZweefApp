#!/usr/bin/env python3
"""
Export the history pack for the hosted briefing and (optionally) upload it to Cloud Storage.
Run after scripts/update_flights.py so the nightly job uses the latest flights and models
(update_flights.py --publish-pack does this for you).

Usage:
    python scripts/publish_briefing_pack.py                                  # export to output/briefing_pack
    python scripts/publish_briefing_pack.py --uri gs://<project>-thermal-data/pack --me "Woltjer"
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config as C  # noqa: E402
from src.briefing_site.pack import export_pack  # noqa: E402


def main():
    p = argparse.ArgumentParser(description='Export (and upload) the briefing history pack')
    p.add_argument('--out', default=str(ROOT / 'output' / 'briefing_pack'))
    p.add_argument('--db-path', default=str(C.DB_PATH))
    p.add_argument('--me', help='your name in the logbook: adds your pilot ids for the "you" layer '
                                '(only ids, no names; the site then shows your own connect rate publicly)')
    p.add_argument('--uri', help='gs://bucket/prefix to upload to (needs gcloud)')
    p.add_argument('--project', help='Google Cloud project for the upload')
    p.add_argument('--account', help='gcloud account for the upload (if not your active one)')
    args = p.parse_args()

    meta = export_pack(args.out, db_path=args.db_path, me=args.me)
    size = sum(f.stat().st_size for f in Path(args.out).rglob('*') if f.is_file()) / 1e6
    print(f"✓ pack {args.out}: {meta['launches']} launches, {meta['first_climbs']} first climbs, "
          f"last day {meta['last_day']}, {len(meta['me_ids'])} pilot id(s) for 'you', {size:.1f} MB")
    if args.uri:
        if not shutil.which('gcloud'):
            p.error('gcloud is not installed')
        cmd = ['gcloud', 'storage', 'rsync', args.out, args.uri, '--recursive',
               '--delete-unmatched-destination-objects']
        if args.project:
            cmd.append(f'--project={args.project}')
        if args.account:
            cmd.append(f'--account={args.account}')
        r = subprocess.run(cmd)
        if r.returncode != 0:
            return r.returncode
        print(f"✓ uploaded to {args.uri}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
