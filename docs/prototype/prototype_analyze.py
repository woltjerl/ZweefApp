import sys, pickle, math
import numpy as np, pandas as pd
S = sys.argv[1]
C = pd.read_pickle(S + '/climbs.pkl'); F = pd.read_pickle(S + '/flights.pkl')
expo = pickle.load(open(S + '/expo.pkl', 'rb'))
pd.set_option('display.width', 200)
C['r0'] = np.hypot(C.x0, C.y0)
print('== climbs total', len(C), 'on', C.datum.nunique(), 'days; flights with >=1 climb', C.flight_id.nunique())
print(C.groupby('start_methode').size())
print('start AGL bands:\n', pd.cut(C.agl0, [0, 200, 300, 400, 500, 700, 1000, 3000]).value_counts().sort_index())
print('median gain', C.gain.median(), 'rate', C.rate.median().round(2), 'dur', C.dur.median())

W = F[F.start_methode == 'lier'].copy()
print('\n== winch flights', len(W), ' release AGL median', W.rel_agl.median(), 'p10/p90', W.rel_agl.quantile([.1, .9]).values)
W['connected'] = W.nclimb > 0
print('true connect rate (>=1 circling climb of 60m+):', W.connected.mean().round(3), '=', W.connected.sum())
print('existing label vs real climb:\n', pd.crosstab(W.flight_outcome.fillna('none'), W.connected))
print('duration after release, median min: connected', (W[W.connected].dur_after.median() / 60).round(1),
      ' not', (W[~W.connected].dur_after.median() / 60).round(1))

# first climbs of winch flights (the "where do I go after release" set)
FC = C[(C.start_methode == 'lier') & (C['first'] == 1)].copy()
FC = FC.join(W.set_index('flight_id')[['rel_x', 'rel_y', 'rel_agl']], on='flight_id')
FC['d_rel'] = np.hypot(FC.x0 - FC.rel_x, FC.y0 - FC.rel_y)
print('\n== first climbs after winch:', len(FC), 'days', FC.datum.nunique())
print('start AGL quantiles', FC.agl0.quantile([.1, .25, .5, .75, .9]).round().values)
print('dist from release quantiles (m)', FC.d_rel.quantile([.1, .25, .5, .75, .9]).round().values)
print('secs since release quantiles', FC.since_launch.quantile([.1, .25, .5, .75, .9]).round().values)
print('by runway:\n', FC.groupby('runway').size())

# connect rate vs conditions
W['hour'] = (W.t_launch // 3600).astype(int)
W['month'] = pd.to_datetime(W.datum).dt.month
print('\nconnect rate by UTC hour:\n', W.groupby('hour').connected.agg(['mean', 'size']).round(2).loc[7:17])
print('connect rate by month:\n', W.groupby('month').connected.agg(['mean', 'size']).round(2))
print('connect rate by coco (meteostat condition code):\n', W.groupby('coco').connected.agg(['mean', 'size']).round(2).head(8))
print('connect rate by wind speed:\n', W.groupby(pd.cut(W.wind_spd, [0, 5, 10, 15, 20, 30, 60])).connected.agg(['mean', 'size']).round(2))
dd = W.groupby('datum').connected.agg(['mean', 'size'])
dd = dd[dd['size'] >= 10]
print('day-level connect rate (days with >=10 launches): n', len(dd), ' quantiles', dd['mean'].quantile([.1, .25, .5, .75, .9]).round(2).values)
print('share of all connects coming from top 20% days:', (W[W.connected].groupby('datum').size().sort_values(ascending=False).head(int(W.datum.nunique() * .2)).sum() / W.connected.sum()).round(2))

# ---------- backtest: where ----------
# target: low climbs near the field (start < 500 m AGL, within 4 km), any launch type
R = 4000.0; G = 100.0
T = C[(C.agl0 < 500) & (C.r0 < R)].copy()
print('\n== low climbs within 4km:', len(T), 'days', T.datum.nunique(), ' (winch first climbs among them:', ((T.start_methode == 'lier') & (T['first'] == 1)).sum(), ')')
nb = int(2 * R / G)
def gidx(x, y):
    return np.clip(((x + R) // G).astype(int), 0, nb - 1), np.clip(((y + R) // G).astype(int), 0, nb - 1)
cx = (np.arange(nb) + .5) * G - R
XX, YY = np.meshgrid(cx, cx, indexing='ij')
disc = np.hypot(XX, YY) < R
from scipy.ndimage import gaussian_filter
def smooth(H, sig=2.0):
    return gaussian_filter(H, sig, mode='constant')
def hist(df, w=None):
    ix, iy = gidx(df.x0.values, df.y0.values)
    H = np.zeros((nb, nb)); np.add.at(H, (ix, iy), 1 if w is None else w); return H
# exposure per day
fday = F.set_index('flight_id').datum
E_day = {}
for fid, e in expo.items():
    d = fday.get(fid)
    H = E_day.setdefault(d, np.zeros((nb, nb)))
    if len(e)==0: continue
    ix = np.array([k[0] for k in e.index], dtype=int); iy = np.array([k[1] for k in e.index], dtype=int)
    ix = ix + nb // 2; iy = iy + nb // 2
    ok = (ix >= 0) & (ix < nb) & (iy >= 0) & (iy < nb)
    np.add.at(H, (ix[ok], iy[ok]), e.values[ok])
E_all = sum(E_day.values())
H_all = hist(T)
days = sorted(T.datum.unique())
T['wsec'] = (((T.wind_dir.fillna(0) + 22.5) % 360) // 45).astype(int)

def angdiff(a, b):
    d = np.abs(a - b) % 360
    return np.minimum(d, 360 - d)

def topfrac_hits(score, pts, frac):
    s = np.where(disc, score, -np.inf)
    thr = np.quantile(s[disc], 1 - frac)
    ix, iy = gidx(pts.x0.values, pts.y0.values)
    return (s[ix, iy] >= thr).sum()

res = {k: {0.05: 0, 0.10: 0, 0.20: 0} for k in ['exposure(traffic)', 'climbs all', 'climbs same wind ±45', 'climbs same wind ±45 + speed±8',
                                             'rate=climbs/exposure', 'rate same wind', 'climbs same runway']}
ntest = 0
for d in days:
    te = T[T.datum == d]
    tr = T[T.datum != d]
    if te.wind_dir.isna().all():
        continue
    wd = te.wind_dir.median(); wsp = te.wind_spd.median()
    Ed = E_all - E_day.get(d, 0)
    Hd = hist(tr)
    sw = tr[angdiff(tr.wind_dir, wd) <= 45]
    sws = sw[np.abs(sw.wind_spd - wsp) <= 8]
    # exposure in same wind: sum over train days with similar wind
    preds = {
        'exposure(traffic)': smooth(Ed),
        'climbs all': smooth(Hd),
        'climbs same wind ±45': smooth(hist(sw)) + 1e-3 * smooth(Hd),
        'climbs same wind ±45 + speed±8': smooth(hist(sws)) + 1e-3 * smooth(Hd),
        'rate=climbs/exposure': smooth(Hd) / (smooth(Ed) + 600.0),
        'rate same wind': (smooth(hist(sw)) + 0.2 * smooth(Hd) * len(sw) / max(len(tr), 1)) / (smooth(Ed) + 600.0),
    }
    rw = te.runway.mode()
    if len(rw):
        sr = tr[tr.runway == rw.iloc[0]]
        preds['climbs same runway'] = smooth(hist(sr)) + 1e-3 * smooth(Hd)
    for k, P in preds.items():
        for fr in res[k]:
            res[k][fr] += topfrac_hits(P, te, fr)
    ntest += len(te)
print('\nLeave-one-day-out: share of the day\'s real low climbs that fall inside the model\'s top X% of area (4 km disc). Uniform = X.')
out = pd.DataFrame(res).T / ntest
print((out * 100).round(1).to_string())
print('n test climbs', ntest)

# hotspots: top cells by smoothed count and by rate
Hs = smooth(H_all, 1.5); Rt = smooth(H_all, 2) / (smooth(E_all, 2) / 3600 + 0.5)  # climbs per low-search-hour
def top(M, n=8, mind=600):
    M = np.where(disc, M, -1).copy(); out = []
    while len(out) < n:
        i, j = np.unravel_index(np.argmax(M), M.shape)
        out.append((cx[i], cx[j], M[i, j]))
        M[np.hypot(XX - cx[i], YY - cx[j]) < mind] = -1
    return out
print('\nTop spots by climb count (east m, north m from field centre):')
for x, y, v in top(Hs): print(f'  E{x:+6.0f} N{y:+6.0f}  lat {52.193650 + y / 111000:.4f} lon {5.145217 + x / (111000 * math.cos(math.radians(52.19365))):.4f}  dens {v:.2f}')
print('Top spots by climbs per hour of low searching there:')
for x, y, v in top(Rt): print(f'  E{x:+6.0f} N{y:+6.0f}  lat {52.193650 + y / 111000:.4f} lon {5.145217 + x / (111000 * math.cos(math.radians(52.19365))):.4f}  {v:.1f}/h')
print('overall: climbs', len(T), 'per low-search hour in disc', (len(T) / (E_all[disc].sum() / 3600)).round(2), ' total low-search hours', (E_all[disc].sum() / 3600).round(0))
np.save(S + '/H_all.npy', H_all); np.save(S + '/E_all.npy', E_all)

# drift: does climb displacement follow wind?
Cd = C[(C.dur > 120) & C.wind_dir.notna() & (C.wind_spd > 8)]
wr = np.radians(Cd.wind_dir)
wx, wy = -np.sin(wr) * Cd.wind_spd / 3.6, -np.cos(wr) * Cd.wind_spd / 3.6  # m/s, direction wind blows TO
vx, vy = Cd.dxdrift / Cd.dur, Cd.dydrift / Cd.dur
along = (vx * wx + vy * wy) / (np.hypot(wx, wy))
print('\ndrift of circling climbs along 10m-wind direction: median m/s', along.median().round(2), ' vs 10 m wind speed median', (Cd.wind_spd.median() / 3.6).round(2),
      ' ratio median', (along / (Cd.wind_spd / 3.6)).median().round(2))
drift_dir = np.degrees(np.arctan2(vx, vy)) % 360
print('median |drift dir - downwind dir| deg:', angdiff(drift_dir, (Cd.wind_dir + 180) % 360).median().round(0))
