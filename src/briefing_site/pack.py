"""
History pack for the hosted briefing: everything the nightly job needs from the 1 GB database
and the model folder, in a small folder (~15 MB) without pilot names.

    pack/
      history.db        launches (all), first climbs, runway history, meta (your pilot ids, climb calibration)
      models/           layer_a.pkl, layer_b.pkl, layer_c.pkl, tuned_params.json, acceptance.json
      NL-ASP.txt        airspace file

Pilot ids are the logbook's numeric ids; names never leave your machine. Your own ids are only
included when you pass `me` (for the "your first thermals" layer and your connect rate).
"""

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src import config as C
from src.briefing_site.day_forecast import thermal_calibration
from src.briefing_site.env import AIRSPACE_NAME, PACK_DB
from src.briefing_site.runway_odds import runway_history

MODEL_FILES = ('layer_a.pkl', 'layer_b.pkl', 'layer_c.pkl', 'tuned_params.json', 'acceptance.json')


def my_pilot_ids(conn, name):
    if not name:
        return []
    rows = conn.execute("SELECT DISTINCT gezagvoerder_id FROM flights WHERE gezagvoerder_naam LIKE ?",
                        (f"%{name}%",)).fetchall()
    return sorted({int(r[0]) for r in rows if r[0] is not None})


def export_pack(out_dir, db_path=None, me=None, model_dir=None, airspace=None) -> dict:
    out = Path(out_dir)
    (out / 'models').mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(db_path or C.DB_PATH))
    launches = pd.read_sql_query("SELECT * FROM launches", src)
    climbs = pd.read_sql_query("SELECT * FROM climbs WHERE is_first = 1", src)
    hist = runway_history(src)
    cal = thermal_calibration(src)
    me_ids = my_pilot_ids(src, me)
    if me and not me_ids:
        raise ValueError(f"No pilot found in the logbook matching {me!r}")
    src.close()

    tmp = out / (PACK_DB + '.tmp')
    tmp.unlink(missing_ok=True)
    dst = sqlite3.connect(str(tmp))
    launches.to_sql('launches', dst, index=False)
    climbs.to_sql('climbs', dst, index=False)
    hist.to_sql('runway_history', dst, index=False)
    meta = {'made_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'launches': len(launches), 'first_climbs': len(climbs), 'runway_history': len(hist),
            'last_day': str(launches.datum.max()), 'me_ids': me_ids, 'thermal_cal': cal,
            'detector': C.config_hash()}
    pd.DataFrame([{'key': k, 'value': json.dumps(v)} for k, v in meta.items()]).to_sql('meta', dst, index=False)
    dst.execute("CREATE INDEX idx_l_flight ON launches(flight_id)")
    dst.execute("CREATE INDEX idx_c_flight ON climbs(flight_id)")
    dst.commit()
    dst.execute("VACUUM")
    dst.close()
    tmp.replace(out / PACK_DB)

    mdir = Path(model_dir or C.MODEL_DIR)
    for f in MODEL_FILES:
        if not (mdir / f).exists():
            raise FileNotFoundError(f"{mdir / f} is missing: run scripts/train_first_thermal.py first")
        shutil.copy2(mdir / f, out / 'models' / f)
    shutil.copy2(Path(airspace or C.AIRSPACE_FILE), out / AIRSPACE_NAME)
    return meta


def read_meta(conn) -> dict:
    try:
        return {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM meta")}
    except sqlite3.OperationalError:
        return {}
