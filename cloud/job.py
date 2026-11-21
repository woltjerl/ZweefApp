#!/usr/bin/env python3
"""
Nightly briefing job (Cloud Run job, started by Cloud Scheduler at 00:05 Dutch time).

1. download the history pack (PACK_URI, e.g. gs://<project>-thermal-data/pack)
2. fetch the Open-Meteo forecast and build the briefing site for today (Dutch date)
3. upload it to the public site bucket (SITE_URI, e.g. gs://<project>-thermal-briefing):
   the day's pages first, index.html last, so the front page never links to missing pages

Test locally without Google Cloud:
    python cloud/job.py --pack output/briefing_pack --site /tmp/briefing_site
"""

import argparse
import os
import sqlite3
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TZ = ZoneInfo('Europe/Amsterdam')
NO_CACHE = {'index.html', 'latest.json'}


def _split(uri):
    bucket, _, prefix = uri[len('gs://'):].partition('/')
    return bucket, prefix.strip('/')


def download_pack(uri, dest: Path):
    from google.cloud import storage
    bucket, prefix = _split(uri)
    n = 0
    for blob in storage.Client().list_blobs(bucket, prefix=f"{prefix}/" if prefix else None):
        rel = blob.name[len(prefix) + 1:] if prefix else blob.name
        if not rel or rel.endswith('/'):
            continue
        f = dest / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(f))
        n += 1
    if n == 0:
        raise SystemExit(f"No history pack found at {uri} (run scripts/publish_briefing_pack.py)")
    return n


def upload_site(site: Path, uri):
    from google.cloud import storage
    bucket_name, prefix = _split(uri)
    bucket = storage.Client().bucket(bucket_name)
    files = sorted((p for p in site.rglob('*') if p.is_file()), key=lambda p: (p.parent == site, str(p)))
    for p in files:
        rel = p.relative_to(site).as_posix()
        blob = bucket.blob(f"{prefix}/{rel}" if prefix else rel)
        blob.cache_control = 'no-cache' if rel in NO_CACHE else 'public, max-age=300'
        ctype = 'text/html; charset=utf-8' if p.suffix == '.html' else 'application/json'
        blob.upload_from_filename(str(p), content_type=ctype)
    return len(files)


def main():
    p = argparse.ArgumentParser(description='Build and publish the daily thermal briefing')
    p.add_argument('--pack', default=os.environ.get('PACK_URI'), help='gs://bucket/prefix or a local folder')
    p.add_argument('--site', default=os.environ.get('SITE_URI'), help='gs://bucket[/prefix] or a local folder')
    p.add_argument('--day', help='YYYY-MM-DD (default: today in Dutch time + DAY_OFFSET)')
    p.add_argument('--day-offset', type=int, default=int(os.environ.get('DAY_OFFSET', '0')))
    args = p.parse_args()
    if not args.pack or not args.site:
        p.error('set --pack/--site or the PACK_URI/SITE_URI environment variables')

    t0 = time.time()
    work = Path(tempfile.mkdtemp(prefix='briefing_'))
    if args.pack.startswith('gs://'):
        pack = work / 'pack'
        n = download_pack(args.pack, pack)
        print(f"Downloaded {n} pack files from {args.pack} ({time.time() - t0:.0f} s)", flush=True)
    else:
        pack = Path(args.pack)

    from src.briefing_site.env import use_pack
    use_pack(pack)                                            # before src.config is imported
    from src import config as C
    from src.briefing_site.site import build_site

    day = date.fromisoformat(args.day) if args.day else datetime.now(TZ).date() + timedelta(days=args.day_offset)
    to_bucket = args.site.startswith('gs://')
    out = work / 'site' if to_bucket else Path(args.site)
    conn = sqlite3.connect(str(C.DB_PATH))
    info = build_site(conn, day, out, log=lambda m: print(m, flush=True))
    if to_bucket:
        n = upload_site(out, args.site)
        print(f"Uploaded {n} files to {args.site}", flush=True)
    print(f"Briefing for {day} done in {time.time() - t0:.0f} s: {info['day']['rating']} · "
          + ', '.join(f"{w} rwy {v['odds'][0][0]}" for w, v in info['windows'].items()), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
