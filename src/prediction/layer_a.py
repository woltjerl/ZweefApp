#!/usr/bin/env python3
"""
Layer A — is the day/hour workable?  P(connect | weather, time, glider)  (plan section 11)

Trains a gradient-boosting classifier on clean winch launches. Out-of-fold
predictions (GroupKFold by day) are calibrated with isotonic regression that is
itself fitted out-of-fold, and stored per launch in `launches.pA`. Every later
step (layers B and C, evaluation) uses these out-of-fold values only.

Usage:
    python -m src.prediction.layer_a
"""

import argparse
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.model_selection import GroupKFold

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import config as C  # noqa: E402
from src.prediction.dataset import connect, winch_launches  # noqa: E402
from src.prediction.features import FEATURES  # noqa: E402

FEATS_A = FEATURES + ['two_seat', 'dual', 'release_agl']
MODEL_PATH = C.MODEL_DIR / 'layer_a.pkl'


def make_model():
    return HistGradientBoostingClassifier(max_depth=3, max_iter=150, learning_rate=0.05,
                                          min_samples_leaf=50, l2_regularization=1.0, random_state=0)


def oof_predictions(L: pd.DataFrame, feats=FEATS_A, n_splits=10, model_fn=make_model):
    """Out-of-fold raw and calibrated probabilities, grouped by day."""
    X = L[feats].to_numpy(float)
    y = L.connected.to_numpy(int)
    groups = L.datum.to_numpy()
    folds = np.zeros(len(L), int)
    raw = np.zeros(len(L))
    for k, (tr, te) in enumerate(GroupKFold(n_splits=n_splits).split(X, y, groups)):
        m = model_fn()
        m.fit(X[tr], y[tr])
        raw[te] = m.predict_proba(X[te])[:, 1]
        folds[te] = k
    cal = np.zeros(len(L))
    for k in range(n_splits):
        te = folds == k
        iso = IsotonicRegression(out_of_bounds='clip', y_min=0.001, y_max=0.999)
        iso.fit(raw[~te], y[~te])
        cal[te] = iso.predict(raw[te])
    return raw, cal


def baseline_oof(L: pd.DataFrame, n_splits=10):
    """Baseline: logistic regression on month and hour only (circular)."""
    B = pd.DataFrame({
        'ms': np.sin(2 * np.pi * L.month / 12), 'mc': np.cos(2 * np.pi * L.month / 12),
        'hs': np.sin(2 * np.pi * L.hour_utc / 24), 'hc': np.cos(2 * np.pi * L.hour_utc / 24)})
    L2 = L.assign(**B)
    return oof_predictions(L2, ['ms', 'mc', 'hs', 'hc'], n_splits,
                           lambda: LogisticRegression(max_iter=1000))


def metrics(y, p):
    base = np.full(len(y), y.mean())
    b, b0 = brier_score_loss(y, p), brier_score_loss(y, base)
    return {'auc': float(roc_auc_score(y, p)), 'brier': float(b), 'brier_constant': float(b0),
            'brier_skill': float(1 - b / b0), 'n': int(len(y)), 'rate': float(y.mean())}


def train(conn, write_db=True, verbose=True):
    L = winch_launches(conn)
    raw, cal = oof_predictions(L)
    m = metrics(L.connected.to_numpy(), cal)
    if verbose:
        print(f"Layer A on {len(L)} launches / {L.datum.nunique()} days: out-of-fold AUC {m['auc']:.3f}, "
              f"Brier skill {m['brier_skill']:.3f}")
    # final model + calibrator on all data (calibrator from out-of-fold raw predictions)
    model = make_model().fit(L[FEATS_A].to_numpy(float), L.connected.to_numpy(int))
    iso = IsotonicRegression(out_of_bounds='clip', y_min=0.001, y_max=0.999).fit(raw, L.connected)
    release_agl = L.groupby('runway').release_agl.median().to_dict()
    bundle = {'model': model, 'calibrator': iso, 'features': FEATS_A, 'metrics': m,
              'release_agl_by_runway': release_agl, 'release_agl_all': float(L.release_agl.median()),
              'club_rate': float(L.connected.mean()), 'trained': pd.Timestamp.now().isoformat(),
              'detector_version': L.detector_version.iloc[0]}
    C.MODEL_DIR.mkdir(parents=True, exist_ok=True)
    with open(MODEL_PATH, 'wb') as f:
        pickle.dump(bundle, f)
    if write_db:
        conn.execute("UPDATE launches SET pA = NULL")
        conn.executemany("UPDATE launches SET pA = ? WHERE flight_id = ?",
                         list(zip(cal.tolist(), L.flight_id.astype(int).tolist())))
        conn.commit()
    L['pA'] = cal
    L['pA_raw'] = raw
    return L, bundle


def load():
    with open(MODEL_PATH, 'rb') as f:
        return pickle.load(f)


def predict(bundle, feats: pd.DataFrame, two_seat=0, dual=0, release_agl=None, runway=None):
    """Calibrated P(connect) for rows of a feature table (from features.build_features)."""
    X = feats.copy()
    X['two_seat'] = two_seat
    X['dual'] = dual
    if release_agl is None:
        release_agl = bundle['release_agl_by_runway'].get(runway, bundle['release_agl_all'])
    X['release_agl'] = release_agl
    raw = bundle['model'].predict_proba(X[bundle['features']].to_numpy(float))[:, 1]
    return bundle['calibrator'].predict(raw)


def main():
    p = argparse.ArgumentParser(description='Train layer A (connect probability)')
    p.add_argument('--db', default=str(C.DB_PATH))
    args = p.parse_args()
    conn = connect(args.db)
    train(conn)
    print(f"✓ saved {MODEL_PATH}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
