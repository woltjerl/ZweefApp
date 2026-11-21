"""
Point the project at a history pack instead of the full database. Kept free of project
imports: set these variables before anything imports src.config (which reads them once).
"""

import os
import sys
from pathlib import Path

PACK_DB = 'history.db'
AIRSPACE_NAME = 'NL-ASP.txt'


def pack_env(pack_dir) -> dict:
    p = Path(pack_dir).resolve()
    return {'ZWEEF_DB': str(p / PACK_DB), 'ZWEEF_MODEL_DIR': str(p / 'models'),
            'ZWEEF_AIRSPACE': str(p / AIRSPACE_NAME)}


def use_pack(pack_dir):
    if 'src.config' in sys.modules:
        raise RuntimeError('use_pack() must run before src.config is imported')
    os.environ.update(pack_env(pack_dir))
