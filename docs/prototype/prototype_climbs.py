"""Prototype: detect real circling climbs in all tracks, save to parquet/csv.

A climb = zigzag segment (ends when altitude drops DRAW m below running max)
with gain >= MIN_GAIN, duration >= MIN_DUR, and >= MIN_TURN degrees of
cumulative turning (true circling). The launch segment (starting near the
ground) is skipped. Also accumulates low-level 'search exposure' (seconds
flown below 500 m AGL, after launch, outside climbs) on a 100 m grid.
"""
import sqlite3, sys, math, pickle
import numpy as np
import pandas as pd

DB = sys.argv[1]
OUT = sys.argv[2]
LAT0, LON0 = 52.193650, 5.145217
MX = 111000.0
MY = 111000.0 * math.cos(math.radians(LAT0))
DRAW, MIN_GAIN, MIN_DUR, MIN_TURN = 25.0, 60.0, 60.0, 540.0

conn = sqlite3.connect(DB)
fl = pd.read_sql_query("""SELECT id, datum, start_methode, runway, registratie, flight_outcome,
    release_alt, max_altitude, flight_duration_after_release FROM flights""", conn)
fl = fl[~fl.registratie.isin(['PH-GOZ', 'PH GOZ'])]
fl = fl[fl.start_methode.isin(['lier', 'sleep', 'sep', 'sep-a', 'zelf'])].set_index('id')
print('loading fixes...', flush=True)
df = pd.read_sql_query("""SELECT flight_id, sequence, time, latitude, longitude, altitude,
    wind_dir, wind_spd, weather_temp, weather_coco FROM igc_data ORDER BY flight_id, sequence""", conn)
conn.close()
df = df[df.flight_id.isin(fl.index)].dropna(subset=['latitude', 'longitude', 'altitude', 'time'])
t = df.time.str
df['t'] = (t[0:2].astype(int) * 3600 + t[3:5].astype(int) * 60 + t[6:8].astype(int)).astype(float)
df['x'] = (df.longitude - LON0) * MY
df['y'] = (df.latitude - LAT0) * MX
print(len(df), 'fixes', df.flight_id.nunique(), 'flights', flush=True)

climbs = []
flights_out = []
GRID = 100.0
expo = {}  # (flight-level) (ix, iy) -> seconds low & not climbing, kept per flight for LODO

for fid, g in df.groupby('flight_id', sort=False):
    if len(g) < 30:
        continue
    tt = g.t.values.copy()
    tt[tt < tt[0]] += 86400
    x, y, alt = g.x.values, g.y.values, g.altitude.values.astype(float)
    ground = np.median(alt[:5])
    agl = alt - ground
    n = len(agl)
    # heading per step
    dx, dy = np.diff(x), np.diff(y)
    hdg = np.degrees(np.arctan2(dx, dy))
    dh = np.diff(hdg)
    dh = (dh + 180) % 360 - 180
    step = np.hypot(dx, dy)
    dh[(step[1:] < 3) | (step[:-1] < 3)] = 0  # ignore heading noise when ~stationary
    cumturn = np.concatenate([[0, 0], np.cumsum(dh)])
    # zigzag
    segs = []
    i_min = 0
    i_max = 0
    for i in range(1, n):
        if tt[i] - tt[i - 1] > 30:  # logging gap: close segment
            segs.append((i_min, i_max))
            i_min = i_max = i
            continue
        if agl[i] > agl[i_max]:
            i_max = i
        elif agl[i_max] - agl[i] > DRAW:
            segs.append((i_min, i_max))
            i_min = i_max = i
        if agl[i] < agl[i_min] and i_max == i_min:
            i_min = i_max = i
        elif agl[i] < agl[i_min]:
            # new low before drawdown trigger: only happens if gain < DRAW; restart
            if agl[i_max] - agl[i_min] < DRAW:
                i_min = i_max = i
    segs.append((i_min, i_max))
    # launch end = top of first segment that starts near the ground
    launch_end = None
    in_climb = np.zeros(n, bool)
    wd, ws = g.wind_dir.values[0], g.wind_spd.values[0]
    nclimb = 0
    first = None
    for (a, b) in segs:
        gain = agl[b] - agl[a]
        if launch_end is None:
            if agl[a] < 80 and gain > 100:
                launch_end = b
            continue
        dur = tt[b] - tt[a]
        if gain < MIN_GAIN or dur < MIN_DUR:
            continue
        turn = abs(cumturn[b] - cumturn[a])
        # also allow alternating-direction circling: use sum |dh| / 2 as fallback? keep strict
        if turn < MIN_TURN:
            continue
        in_climb[a:b + 1] = True
        nclimb += 1
        c = dict(flight_id=fid, t0=tt[a], x0=x[a], y0=y[a], agl0=agl[a], agl1=agl[b], gain=gain,
                 dur=dur, rate=gain / dur, xm=x[a:b + 1].mean(), ym=y[a:b + 1].mean(),
                 dxdrift=x[b] - x[a], dydrift=y[b] - y[a], turn=turn,
                 since_launch=tt[a] - tt[launch_end], wind_dir=wd, wind_spd=ws,
                 temp=g.weather_temp.values[0], coco=g.weather_coco.values[0])
        if first is None:
            first = c
            c['first'] = 1
        else:
            c['first'] = 0
        climbs.append(c)
    if launch_end is None:
        continue
    # exposure: after launch, below 500 m AGL, not in climb, above 80 m (excl. final/landing)
    idx = np.arange(n)
    m = (idx > launch_end) & (agl < 500) & (agl > 80) & (~in_climb)
    dt = np.diff(tt, prepend=tt[0])
    dt[dt > 30] = 0
    ix = np.floor(x[m] / GRID).astype(int)
    iy = np.floor(y[m] / GRID).astype(int)
    e = pd.Series(dt[m]).groupby([ix, iy]).sum()
    expo[fid] = e
    flights_out.append(dict(flight_id=fid, rel_agl=agl[launch_end], rel_x=x[launch_end], rel_y=y[launch_end],
                            max_agl=agl.max(), dur_after=tt[-1] - tt[launch_end], nclimb=nclimb,
                            wind_dir=wd, wind_spd=ws, temp=g.weather_temp.values[0], coco=g.weather_coco.values[0],
                            t_launch=tt[launch_end]))

C = pd.DataFrame(climbs).join(fl[['datum', 'start_methode', 'runway']], on='flight_id')
F = pd.DataFrame(flights_out).join(fl, on='flight_id')
C.to_pickle(OUT + '/climbs.pkl')
F.to_pickle(OUT + '/flights.pkl')
with open(OUT + '/expo.pkl', 'wb') as f:
    pickle.dump(expo, f)
print('climbs', len(C), 'flights with launch', len(F))
