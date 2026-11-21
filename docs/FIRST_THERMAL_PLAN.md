# Plan: maximise the chance of the first thermal after a winch launch at EHHV

Written 2026-11-21. Status: **implemented 2026-11-21** — see section 19 for what was built,
where it deviates from this plan and why, and the measured prediction power.

## 1. Goal

After a winch launch at EHHV (release is low, median 284 m AGL), tell the pilot
**where to fly to have the highest chance of connecting with a first thermal**,
given the day's weather, date and time, using where thermals were found in the past.
Only thermals **within reach of the release** matter. The tool must also **state,
with statistics, how much predictive power it really has**.

Non-goals: cross-country thermal maps, a general weather forecast, live tracking.

## 2. Where we are now (measured on the current database)

Numbers come from a prototype detector (circling climb, 60 m+ gain, 1.5+ turns)
run over all 6.6 M fixes. They are approximate; Phase 1 makes them exact.

| Fact | Value |
|---|---|
| Winch launches with a usable track | 8,695 |
| Real connect rate (a genuine circling climb after release) | ~11% (956 flights, 218 days) |
| Flights currently labelled `thermal_found` that have a real climb | 939 of 3,976 (76% are false) |
| First real climb: height lost since release (p10 / median / p90) | 29 / 63 / 156 m |
| First real climb: distance from release (median / p90) | 785 m / 1,735 m |
| First real climb found within 3 minutes of release | 77% |
| Real climbs in aerotow flights (currently discarded) | 7,595, of which ~700 start below 500 m within 4 km |
| Connect rate by pilot, thermic days, 60 pilots with 30+ launches (p10 / median / p90 / best) | 7% / 15% / 26% / 43% |
| Days: share of all connects produced by the best 20% of days | 57% |
| Climb drift vs 10 m wind | ~0.9× wind speed, within ~27° of downwind |

Baseline predictive power of the current idea (leave-one-day-out, share of a day's
real low climbs inside the map's top 10% of area within 4 km; random = 10%):

| Map | Captured |
|---|---|
| Where gliders fly low without climbing (pure traffic) | 71% |
| All historical climbs | 71% |
| Climbs in similar wind (±45°) | 73% |
| Climbs from the same runway | 73% |

Conclusion: today's heatmaps are barely better than a traffic map. The pilot spread
(7% to 43%) confirms the experience that aces do something different, so there is a
signal to extract. The plan below is built to extract it and to measure it honestly.

## 3. Design principles

1. **Model the decision, not the density.** The unit of analysis is one winch launch:
   where the pilot searched after release, and whether that search ended in a climb.
   A spot is good when searching there *converts* often, not when many tracks cross it.
2. **Count every searcher, including the ones who failed.** Failed launches are the
   denominator. Without them, any map is a traffic map.
3. **Work in ground-source coordinates.** Shift each climb upwind to the place on the
   ground that produced it; shift back downwind for today's wind when displaying.
4. **Only what is reachable.** Every spot is judged from the release point of the
   runway in use, with today's wind, with enough height left to fly the circuit.
5. **Everything is validated on days the model has never seen.** No number is shown
   to the pilot that has not been backtested.

## 4. Phases

### Phase 1 — Trustworthy data (prerequisite for everything)

New module `src/thermal_detection/climb_detector.py`, replacing the logic in
`FindThermals_improved.py` and `extract_release_thermal.py`.

1.1 **Height above ground.** Subtract each flight's own ground reading (median of the
first fixes before take-off) so logger pressure offsets drop out.

1.2 **Launch end / release detection per launch type.**
- Winch: top of the first climb that starts on the ground (rate > 5 m/s phase), as now
  but on AGL.
- Aerotow (solves the "tow climb looks like a thermal" problem), in order of preference:
  a. Tug pairing: `flights.sleep_uuid` links 2,180 glider flights to the tug flight;
     about 800 tug flights have a track. Release = first moment the two tracks
     separate by more than ~150 m horizontally or 50 m vertically and stay apart.
  b. No tug track: release = end of the powered-climb signature, i.e. the first point
     after take-off where a sustained climb of roughly constant rate and airspeed
     (tow speed ~110–130 km/h, low turn rate) is followed by 20 s+ of descent
     with a speed change. Validate rule (b) against the flights where (a) is available
     and report its error (target: release time within 15 s for 90% of flights).
  c. Everything before release is flagged `phase = 'launch'` and never counted as lift.
- Self-launch (`zelf`, `sep`): same as (b); TMG stays excluded.

1.3 **Climb detection.** A climb is a segment with: height gain ≥ 60 m, duration ≥ 60 s,
cumulative turning ≥ 540°, ending when the glider drops 25 m below the segment's top.
Thresholds are tuned in 1.5.

1.4 **New tables** (keep the old columns until Phase 5 is accepted):
- `launches(flight_id, launch_type, runway, release_time, release_lat, release_lon,
  release_agl, connected, time_to_first_climb_s, height_lost_to_first_climb_m)`
- `climbs(flight_id, seq, start_time, lat0, lon0, agl0, agl1, gain, duration, rate,
  drift_dx, drift_dy, source_lat, source_lon, is_first, in_reach)`
- `search_tracks(flight_id, t, lat, lon, agl)` — the fixes between release and first
  climb or the decision to land (below circuit height), for winch flights only.

1.5 **Label audit.** Hand-check 100 random flights (50 connected, 50 not) on a quick
track plot; adjust thresholds until agreement is ≥ 95%. Record the result in this file.

1.6 **Time and data fixes.**
- Convert IGC times (UTC) to local time once, store both.
- Fix the logger `ground`/GPS outliers (a release at 65,100 m exists).

Deliverable: exact versions of the table in section 2.

### Phase 2 — Weather that describes convection

2.1 Backfill hourly history for EHHV from the Open-Meteo archive (verified available):
boundary-layer height, shortwave and direct radiation, low/mid cloud cover, 2 m
temperature and dew point, 100 m wind, soil moisture. Store in `weather_data` as new columns.
Note: CAPE and sensible heat flux are forecast-only, so do not train on them.

2.2 Derived features per launch: sun elevation, hours since sunrise, radiation of the
previous 1–2 hours (ground heating lags), temperature–dew point spread (cloud base
proxy), wind at 100 m as direction + speed, days since rain, month.

2.3 The forecast fetcher (`open_meteo_forecast.py`) is extended to return exactly the
same features for a future date, so training and prediction match.

### Phase 3 — The model: three layers

**Layer A — Is the day/hour workable? `P(connect | weather, time)`**
- One row per winch launch, target = `connected`.
- Gradient boosting or logistic regression on Phase 2 features plus runway.
- Purpose: tells you which launches are worth a search at all, and is the prior for
  layer B. Also gives the honest daily number ("today 14:00: 22%, club average 11%").

**Layer B — Where? Conversion rate per ground source**
- Grid of ground-source cells (start at 200 m; resolution is tuned by backtest)
  inside the reach area.
- For each cell: `searches` = number of winch launches whose search track passed over
  the cell's downwind-shifted position at a height where the thermal would be usable;
  `hits` = number of those that started a climb belonging to that source.
- Conversion rate = hits / searches, estimated with shrinkage (empirical Bayes /
  hierarchical model) so a cell with 3 visits does not show 100%.
- Conditioning: wind sector, wind speed band, time-of-day band, season, cloudiness.
  Fit as a model `P(hit | cell, conditions)` with partial pooling, so that conditions
  share strength; a cell only gets its own wind-specific value where data supports it.
- Extra evidence, lower weight: climbs from aerotow and later-in-flight climbs that
  start below ~600 m, drift-corrected to the same source grid. These add ~2–3× more
  thermal observations per source but have no failed-search denominator, so they enter
  as a prior on the cell, not as conversion counts. The weight is set by backtest.

**Layer C — What do the aces do? Route evidence**
- Rank pilots by connect rate **adjusted for layer A** (so nobody is an ace only for
  flying on good days) and for glider type and dual/solo.
- For the top group, extract the route from release to first climb per runway and wind
  sector; cluster the routes (3–5 typical lines per runway).
- Test whether following an ace route raises the conversion rate for *non-ace* pilots
  who happened to fly it. If yes, the routes become recommendations; if not, the ace
  effect is skill in centring, not location, and the report says so.
- Privacy: only pilot IDs are used internally; no names in outputs.

**Reach model (applies to B and C)**
- From the release point of the runway in use and the actual release height
  distribution (not 400 m): height available = release − circuit-entry height
  (default 180 m AGL, configurable).
- Still-air glide ratio per glider class (trainer ~25 effective, single-seater ~30
  effective, both derated), head/tailwind component from the 100 m wind, plus the
  height needed to fly back to the circuit entry point.
- Output: a reach polygon per runway/wind, and for every cell the height on arrival.
  A cell is "in reach" only if arrival height ≥ a minimum workable height
  (default 200 m AGL, tuned from the data: lowest heights at which climbs were
  actually started and held).

**Final recommendation**
- Score of a route = probability of at least one hit along a flyable path within the
  height budget (combine cells along the path, not just the single best cell), so the
  advice is "fly this line: source 1, then source 2, then join circuit".
- Output the top 2–3 lines with their backtested probability and sample size.

### Phase 4 — Proving the prediction power

All evaluation is **leave-one-day-out** (or blocked by week); never random splits,
because launches on one day share the same weather. Report with bootstrap 95%
confidence intervals over days. Script: `scripts/evaluate_prediction.py`, writing
`output/prediction_power_report.html`.

4.1 **Layer A (when):** AUC, Brier score, and a calibration plot (predicted vs observed
connect rate in 10 bins). Baselines: constant 11%; month + hour only.

4.2 **Layer B (where):**
- Capture curve: share of held-out first climbs inside the model's top 5/10/20% of
  reachable area. Baselines to beat: traffic map (71% at top-10%), all-climb map (71%),
  same-wind map (73%).
- The decisive test, **conversion lift**: among held-out launches, compare the connect
  rate of pilots whose search track went through the model's top-ranked cells with
  those whose track did not, *within the same day and hour* (stratified, so weather
  cancels out). Report the rate ratio with confidence interval and a permutation
  p-value. This is the number that answers "does following the map help".
- Same test restricted to non-ace pilots, to show the gain is not just "aces go there".

4.3 **Layer C (aces):** permutation test on pilot labels to show the pilot spread
(7%–43%) exceeds chance given layer A; then the route-following test from Phase 3.

4.4 **Ablations:** each ingredient switched off in turn (drift correction, wind
conditioning, aerotow evidence, shrinkage, grid size) with the change in capture and
lift. Anything that does not help is removed.

4.5 **Sample-size honesty:** per runway × wind sector, show the number of launches and
connects, and mark combinations with too little data ("no reliable advice for runway 18
in NE wind") rather than guessing.

4.6 **Acceptance criteria.** The new model replaces the old maps only if, on held-out
days: top-10% capture is significantly above the 73% baseline **and** conversion lift
> 1 with a 95% interval excluding 1. If it fails, the report says so and the tool shows
layer A plus the plain rate map with sample counts.

4.7 **Prospective check.** From go-live, log each daily forecast; after each flying
day compare with what happened. A running scorecard keeps the claim honest across seasons.

### Phase 5 — Display

One page per launch window (`output/first_thermal_<date>_<window>.html`):
- Header: runway in use, wind, "chance of connecting this hour: X% (club average 11%)".
- Map: reach polygon; ground sources coloured by conversion rate; the same sources
  drawn shifted downwind at 250 m for today's wind ("look here"); the 2–3 recommended
  lines with probability and number of historical launches behind them.
- Each popup: hits / searches, confidence interval, conditions it applies to.
- A "what the aces did in these conditions" toggle showing the clustered routes.
- A link to the prediction-power report. No "confidence %" without a backtest behind it.

Retire `thermal_predictor_advanced.py`, `ml_thermal_predictor.py`,
`ml_thermal_model.py`, `thermal_forecast.py` and the terrain enrichment once Phase 5
passes 4.6. Terrain may return later as a feature of the source cells, using polygon
edges instead of centroids, and only if an ablation shows it helps.

## 5. Order of work and size

| Step | Content | Depends on | Rough size |
|---|---|---|---|
| 1 | Phase 1: detector, aerotow release, tables, audit | — | 2–3 days |
| 2 | Phase 4 skeleton: evaluation script with today's baselines | 1 | 1 day |
| 3 | Phase 2: weather backfill and features | — | 1 day |
| 4 | Layer A | 1, 3 | 1 day |
| 5 | Reach model | 1 | 1 day |
| 6 | Layer B incl. drift correction | 1, 5 | 3 days |
| 7 | Layer C (aces) | 4, 6 | 2 days |
| 8 | Full prediction-power report, ablations | 2, 4, 6, 7 | 2 days |
| 9 | Phase 5 display, daily script, retire old code | 8 | 2 days |

The evaluation script comes second on purpose: every later step is judged by it.

## 6. Risks

- **Data volume.** ~950 real winch first climbs over six runways and several wind
  sectors is thin. Mitigation: partial pooling, aerotow/later climbs as prior, and
  explicit "not enough data" states. More seasons improve it automatically.
- **Confounding.** Aces fly better gliders on better days. Mitigation: all comparisons
  stratified by day/hour and adjusted for glider type and dual/solo.
- **Drift model error.** Wind at height is taken from a model, climb rate varies.
  Mitigation: estimate drift from each climb's own track where the climb is long
  enough; ablation 4.4 checks the correction earns its place.
- **The effect may be mostly skill, not place.** Then the honest product is layer A
  plus ace routes as training material. Phase 4 decides; the plan does not assume the answer.
- **Aerotow release detection without tug track** may be imprecise. Mitigation:
  validated against paired flights; flights failing the check contribute climbs only
  from 2 minutes after the estimated release.

## 7. Open choices (defaults chosen, change if you disagree)

- Circuit-entry height 180 m AGL and minimum workable height 200 m AGL.
- "Ace" = top 20% of pilots with 30+ launches on thermic days after adjustment.
- Reach uses two glider classes (two-seat trainer, single-seater).

---

# Implementation details

Sections 8–15 are the build instructions for the phases above. All constants live in
one new file, `src/config.py`, so that the evaluation script can vary them.
Prototype code that produced the numbers in section 2 is kept in `docs/prototype/`
(`prototype_climbs.py`, `prototype_analyze.py`); reuse it, do not start from zero.

## 8. Conventions used everywhere

**Local coordinates.** Field centre `LAT0 = 52.193650`, `LON0 = 5.145217`.

```
x_east  = (lon − LON0) · 111000 · cos(LAT0)      [m]
y_north = (lat − LAT0) · 111000                  [m]
```

**Wind vector.** Meteorological direction `D` is where the wind comes *from*.
The vector the air moves *to*, in m/s, with speed `S` in km/h:

```
wx = −sin(D) · S / 3.6        wy = −cos(D) · S / 3.6
```

**Time.** `igc_data.time` is UTC. Add `time_utc_s` (seconds since midnight, with
midnight wrap handled) and compute local time with `zoneinfo("Europe/Amsterdam")`
from `flights.datum`. All weather joins are done in UTC.

**Day key.** `flights.datum`. Every split, bootstrap and permutation is by day.

**`src/config.py` (initial values)**

```python
# detection
DRAWDOWN_M = 25; MIN_GAIN_M = 60; MIN_DUR_S = 60; MIN_TURN_DEG = 540
GAP_S = 30; MIN_STEP_M = 3; LAUNCH_START_MAX_AGL = 80; LAUNCH_MIN_GAIN = 100
# aerotow
TOW_SEP_H_M = 150; TOW_SEP_V_M = 50; TOW_SEP_HOLD_S = 20
# search / reach
CIRCUIT_ENTRY_AGL = 180; MIN_WORK_AGL = 200; SEARCH_END_AGL = 150
GLIDE = {"trainer": 25, "single": 30}; V_GLIDE_MS = {"trainer": 25, "single": 26}
CIRCLING_SINK_MS = 0.9
# source grid
CELL_M = 200; MATCH_RADIUS_M = 200; REACH_RADIUS_M = 3000; SMOOTH_SIGMA_M = 200
# shrinkage (tuned in evaluation)
K_CELL = 5.0; M_COND = 20.0; LAMBDA_AEROTOW = 0.3
```

## 9. Phase 1 in detail — `src/thermal_detection/climb_detector.py`

### 9.1 Loading
One query per run, ordered by `flight_id, sequence`, read with pandas and grouped
(38 s for the whole database in the prototype). Process per flight as numpy arrays
`t, x, y, alt`. Skip flights with fewer than 30 fixes. Drop fixes with altitude
outside −100…6000 m (removes the 65,100 m outlier).

### 9.2 Height above ground
`ground = median(alt[fixes before take-off])`, where take-off is the first fix with
ground speed > 40 km/h. Fallback when the log starts airborne: field elevation 1 m and
flag `agl_uncertain = 1`. `agl = alt − ground`.

### 9.3 Turn accumulation
```
hdg[i]  = atan2(dx[i], dy[i])                       # track over ground
dh[i]   = wrap180(hdg[i+1] − hdg[i]); dh = 0 where step < MIN_STEP_M
cumturn = cumsum(dh)
```
Circling in wind still accumulates 360° per circle in ground track, so no wind
correction is needed here.

### 9.4 Segmentation (zigzag on AGL)
```
i_min = i_max = 0
for i in 1..n−1:
    if t[i] − t[i−1] > GAP_S:            close(i_min, i_max); restart at i
    if agl[i] > agl[i_max]:              i_max = i
    elif agl[i_max] − agl[i] > DRAWDOWN_M: close(i_min, i_max); restart at i
    if agl[i] < agl[i_min] and agl[i_max] − agl[i_min] < DRAWDOWN_M: restart at i
close(i_min, i_max)
```
Each closed segment `(a, b)` is a candidate climb from its low point to its top.

### 9.5 Launch end per launch type
- **Winch (`lier`)**: first segment with `agl[a] < LAUNCH_START_MAX_AGL` and
  `gain > LAUNCH_MIN_GAIN`; release index = `b`. Sanity: release AGL in 150–600 m and
  mean climb rate of the segment > 5 m/s, else `launch_quality = 'suspect'`.
- **Aerotow (`sleep`) with tug track** (`flights.sleep_uuid = tug.uuid`, tug has
  `igc_data`): interpolate the tug track onto the glider's time stamps; release = first
  index `i` after take-off where horizontal separation > `TOW_SEP_H_M` or vertical
  separation > `TOW_SEP_V_M` and this stays true for `TOW_SEP_HOLD_S`. Check the clocks
  first: cross-correlate the two altitude series over ±60 s and apply the best lag.
- **Aerotow without tug track, and `zelf`, `sep`, `sep-a`**: powered-climb signature.
  Compute 20 s rolling climb rate `vz20` and its 60 s rolling standard deviation, and
  the turn rate. From take-off, the tow continues while `vz20 > 0.7 m/s` and
  `std60(vz20) < 0.8 m/s` and `|turn rate| < 8 °/s`. Release = first index where
  `vz20 < 0` for 15 s, or the turn rate exceeds 12 °/s for 10 s (the release turn).
  A real thermal entered straight off tow is cut too late or too early by this rule;
  therefore for these flights climbs only count from **120 s after** the estimated release.
- **Validation of the fallback rule**: run it on all flights that also have a tug track;
  report median and p90 of |release time difference|. Target: p90 ≤ 15 s. If it fails,
  raise the 120 s guard instead of trusting the rule.
- `tmg`, `tmg-a`, `car`, tug aircraft: excluded.

### 9.6 Climb acceptance
For every segment after the launch end:
`gain ≥ MIN_GAIN_M`, `dur ≥ MIN_DUR_S`, `|cumturn[b] − cumturn[a]| ≥ MIN_TURN_DEG`.
Store start position, `agl0`, `agl1`, `gain`, `dur`, `rate = gain/dur`, drift
`(x[b]−x[a], y[b]−y[a])`, and the circle-centre track (60 s rolling mean of x, y).
The first accepted climb of the flight gets `is_first = 1`.

### 9.7 Search track (winch only)
Fixes from release until the first of: start of first climb; `agl < SEARCH_END_AGL`;
600 s after release. Stored thinned to one fix per 2 s.
`connected = 1` if the first climb starts inside this window.

### 9.8 Ground source of a climb (drift correction)
```
w_air   = rate + CIRCLING_SINK_MS                  # updraft speed of the air
wind    = own drift (drift / dur) if dur ≥ 120 s
          else 0.5 · (wind_10m + wind_100m)        # mean wind in the layer below
source  = (x0, y0) − wind · agl0 / w_air
```
Clamp the shift to 1,500 m. Store `source_x, source_y, shift_m`.

### 9.9 Schema
```sql
CREATE TABLE launches (
  flight_id INTEGER PRIMARY KEY, launch_type TEXT, runway TEXT, glider_class TEXT,
  pilot_id INTEGER, dual INTEGER, release_time_utc_s REAL, release_x REAL, release_y REAL,
  release_agl REAL, launch_quality TEXT, agl_uncertain INTEGER,
  connected INTEGER, time_to_first_climb_s REAL, height_lost_to_first_climb_m REAL,
  detector_version TEXT);
CREATE TABLE climbs (
  id INTEGER PRIMARY KEY, flight_id INTEGER, seq INTEGER, start_time_utc_s REAL,
  x0 REAL, y0 REAL, agl0 REAL, agl1 REAL, gain REAL, duration REAL, rate REAL,
  drift_x REAL, drift_y REAL, source_x REAL, source_y REAL, shift_m REAL,
  is_first INTEGER, s_since_release REAL);
CREATE TABLE search_tracks (flight_id INTEGER, t_utc_s REAL, x REAL, y REAL, agl REAL);
CREATE INDEX idx_climbs_flight ON climbs(flight_id);
CREATE INDEX idx_search_flight ON search_tracks(flight_id);
```
`glider_class`: map `flights.type` → `trainer` (DG-1000, ASK-21, Duo Discus, any
two-seater) or `single`. The run is idempotent: a `detector_version` string is the hash
of the config values; a changed hash rebuilds the three tables.

CLI: `python -m src.thermal_detection.climb_detector --db data/flights.db [--force]`.

### 9.10 Audit tool — `scripts/audit_labels.py`
Draws for a random flight a map track (coloured by climb/search/launch) and an
altitude–time plot, asks `y/n` for "label correct?", writes `data/label_audit.csv`.
100 flights, stratified 50/50. Report agreement and each disagreement's cause here.

### 9.11 Tests — `tests/test_climb_detector.py`
- Synthetic winch launch + straight glide → no climb, `connected = 0`.
- Synthetic winch + 3 circles climbing 90 m in 120 s → one climb, correct start.
- Synthetic pull-up of 15 m in 5 s → no climb.
- Synthetic S-turns with ±10 m height changes → no climb
  (real example: flight 122666, a 5-minute circuit currently labelled `thermal_found`).
- Synthetic tow: glider and tug together to 500 m, then diverging → release within 5 s.
- Drifting circles in 20 km/h wind → source lies upwind by `wind · agl0 / w_air` ± 50 m.
- Midnight wrap and a 60 s logging gap.

## 10. Phase 2 in detail — weather

### 10.1 Backfill — `scripts/backfill_convection_weather.py`
One request for the whole history (verified working for these variables):

```
GET https://archive-api.open-meteo.com/v1/archive
  latitude=52.19 longitude=5.15 start_date=2024-03-01 end_date=<yesterday> timezone=UTC
  hourly=boundary_layer_height,shortwave_radiation,direct_radiation,cloud_cover_low,
         cloud_cover_mid,temperature_2m,dew_point_2m,wind_speed_100m,wind_direction_100m,
         wind_speed_10m,wind_direction_10m,precipitation,soil_moisture_0_to_7cm
```
New table `weather_conv(date, hour_utc, <one column per variable>)`, primary key
`(date, hour_utc)`. Not available in the archive: `cape`, `sensible_heat_flux`,
`lifted_index` — do not use them.

To verify during implementation: whether Open-Meteo's *historical forecast* endpoint
gives the same variables from the forecast model. If yes, train on that instead, so
training and prediction data come from the same model; otherwise accept the
archive/forecast mismatch and measure it in the prospective check (4.7).

### 10.2 Forecast — extend `src/weather/open_meteo_forecast.py`
Same variable list against `https://api.open-meteo.com/v1/forecast` (soil moisture
there is `soil_moisture_0_to_1cm`; map it with a fitted linear relation or drop the
feature if ablation shows no value). Return rows in the `weather_conv` layout.

### 10.3 Feature builder — `src/prediction/features.py`
`build_features(date, time_utc, runway) -> dict`, used by both training and prediction:

| Feature | Definition |
|---|---|
| `blh` | boundary-layer height, linear interpolation between hours |
| `sw_now`, `sw_prev2h` | shortwave radiation now; mean of previous 2 hours |
| `direct_frac` | direct / shortwave (0 when shortwave < 20) |
| `cloud_low`, `cloud_mid` | % |
| `spread` | T2m − dew point; cloud base estimate = 125 · spread [m] |
| `w100_spd`, `w100_sin`, `w100_cos` | wind at 100 m |
| `shear` | w100 speed − w10 speed |
| `headwind_rwy` | wind component along runway heading |
| `sun_elev` | solar elevation from date/time/position (formula, no extra library) |
| `h_since_sunrise` | hours |
| `rain_24h`, `days_since_rain` | from precipitation (rain = >1 mm/day) |
| `soil_moist` | as given |
| `doy_sin`, `doy_cos` | day of year |
| `wstar_proxy` | (blh · max(sw_prev2h − 100, 0))^(1/3), a rough thermal-strength index |

## 11. Layer A in detail — `src/prediction/layer_a.py`

- Rows: winch launches with `launch_quality != 'suspect'`. Target `connected`.
- Extra features: `glider_class`, `dual`, `release_agl` (prediction uses the runway's
  median release height).
- Model: `HistGradientBoostingClassifier(max_depth=3, max_iter=150, learning_rate=0.05,
  min_samples_leaf=50)`; alternative baseline: logistic regression on standardised features.
- Out-of-fold predictions with `GroupKFold(n_splits=10, groups=day)`; calibrate with
  isotonic regression fitted on the out-of-fold predictions.
- Store per launch the out-of-fold probability `pA` in `launches.pA`. **Every later
  step uses this out-of-fold value**, never an in-sample one.
- Saved artefact: `data/models/layer_a.pkl` (model, calibrator, feature list, config hash).

## 12. Reach model in detail — `src/prediction/reach.py`

Inputs: release point `R` and height `h_rel`, wind vector `w` (mean of 10 m and 100 m),
glider class (`E` = glide ratio, `V` = glide speed), circuit entry point `C_rwy`.

Height lost flying from A to B through moving air:
```
d      = |B − A|;  u = (B − A)/d
w_t    = w · u                      # tailwind component
w_c    = |w − w_t·u|                # crosswind component
V_g    = sqrt(V² − w_c²) + w_t      # ground speed; unreachable if V_g ≤ 2 m/s
loss   = (d / V_g) · (V / E)
```
For every source cell `s`, the place to look is the thermal position at search height
(iterate twice: `P = s + w · h_P / w_air_typ`, `w_air_typ` = 2.0 m/s):
```
h_P      = h_rel − loss(R → P)
h_return = h_P − loss(P → C_rwy) − 30          # 30 m = two search turns
in_reach = h_P ≥ MIN_WORK_AGL  and  h_return ≥ CIRCUIT_ENTRY_AGL
```
- `C_rwy` per runway is taken from data: the median position where non-connected
  flights from that runway pass `CIRCUIT_ENTRY_AGL` descending.
- `MIN_WORK_AGL` check against data: the 5th percentile of `agl0` of connected first climbs.
- Validation: at least 95% of historical first-climb positions must fall inside the
  reach computed for their own flight's wind and release height; if not, the glide
  numbers are too pessimistic and are adjusted.
- Output: boolean mask and `h_P` per cell; polygon (contour of the mask) for the map.

Tests: no wind → reach is symmetric around the line R–C; 30 km/h headwind shrinks
reach upwind and extends it downwind; a cell beyond `h_rel · E` is never in reach.

## 13. Layer B in detail — `src/prediction/layer_b.py`

### 13.1 Source grid
Square cells of `CELL_M` covering `REACH_RADIUS_M` around the field centre
(30 × 30 cells at 200 m). Cell index from `floor((x + REACH_RADIUS_M)/CELL_M)`.

### 13.2 "Did this launch search this source?" (the matching rule)
For every search-track fix `(p, agl)` of a launch with `agl ≥ MIN_WORK_AGL`:
```
tested_source = p − wind_flight · agl / 2.0
```
A launch **searched** cell `c` if at least one `tested_source` lies within
`MATCH_RADIUS_M` of the cell centre. A launch counts once per cell.
A launch **hit** cell `c` if it is connected and the `source` of its first climb lies
within `MATCH_RADIUS_M` of the cell centre (nearest cell wins; a hit cell is always
also counted as searched).

Result: table `cell_trials(flight_id, cell, hit)` — the core dataset of the where-model.

### 13.3 Weather-neutral quality of a cell: observed / expected
The chance of a hit depends first on the day. Remove that with layer A:
```
q        = Σ hits / Σ_over_trials pA(launch)             # global scale: hits per unit of pA per cell visit
E_c      = q · Σ_{launches that searched c} pA(launch)   # expected hits if c were an average cell
O_c      = hits in c
lift_c   = (O_c + K_CELL) / (E_c + K_CELL)               # gamma–Poisson shrinkage to 1.0
```
`lift_c > 1` means: searching here converts more often than searching elsewhere on
equally good days. Interval: gamma quantiles of `Gamma(O_c + K, E_c + K)`.
Before the ratio, smooth `O` and `E` with a Gaussian of `SMOOTH_SIGMA_M`
(neighbouring cells share information). `K_CELL` and the sigma are tuned in 14.6.

### 13.4 Conditioning (wind, time, season, cloud)
Condition bins `k`: wind sector at 100 m (8 × 45°, overlapping ±45° windows), wind
speed (< 10, 10–20, > 20 km/h), time (before 11, 11–14, after 14 UTC), season
(Mar–May, Jun–Aug, Sep–Nov). Not the full cross product: each dimension separately,
plus wind sector × runway. For each:
```
lift_{c,k} = (O_{c,k} + M_COND · lift_c) / (E_{c,k} + M_COND)     # shrink towards the cell's own value
```
Today's value for a cell combines the dimensions multiplicatively relative to the
cell's base: `lift_today(c) = lift_c · Π_k (lift_{c,k} / lift_c)`, capped to [0.2, 5].
The evaluation decides which dimensions stay (ablation per dimension).

### 13.5 Aerotow and later climbs as prior
All accepted climbs that are not winch first climbs, with `agl0 < 600 m` and source
inside the grid, binned to cells and smoothed → density `A_c`, normalised to mean 1.
They replace the neutral prior:
```
prior_c = 1 + LAMBDA_AEROTOW · (A_c − 1)                 # clipped to [0.5, 2]
lift_c  = (O_c + K_CELL · prior_c) / (E_c + K_CELL)
```
`LAMBDA_AEROTOW = 0` switches it off; tuned in 14.6.

### 13.6 Probability for today
```
base      = pA_today · q                                # chance per searched average cell today
p_today(c) = min(0.9, base · lift_today(c))            for cells in reach, else 0
```

### 13.7 Leave-one-day-out without refitting
`O`, `E`, `A` are sums over launches, so keep per-day arrays and subtract the held-out
day before computing lifts. A full leave-one-day-out pass is then a few seconds.

## 14. Layer C and route recommendation — `src/prediction/layer_c.py`, `routes.py`

### 14.1 Pilot skill, adjusted
```
skill_p = (connects_p + K) / (Σ pA over p's launches + K),  K = 3
```
after first refitting layer A with `glider_class` and `dual` (already features), so
that glider and day quality are removed. "Ace" = top 20% of pilots with ≥ 30 launches.
Significance of the spread: permute pilot IDs among launches *within the same day*
2,000 times; compare the variance of `skill_p` with the permutation distribution.

### 14.2 Route extraction
For each connected or non-connected winch launch: the search track resampled to 20
points equally spaced in path length, expressed relative to the release point and
rotated so the runway heading points up (so runways can share data where sensible).
Cluster per runway × wind sector with k-means (k = 3…5, chosen by silhouette) on the
40-dimensional vectors. Store cluster centre lines and membership.

### 14.3 Is it place or skill?
Per cluster: connect rate of aces and of non-aces, each divided by expected
(`Σ pA`). The route "works" if non-aces on that route have observed/expected clearly
above non-aces on other routes (stratified test of 15.3). Report both numbers.

### 14.4 Recommended lines for today
Candidate paths = ace cluster centre lines for today's runway and wind sector, plus
generated paths: from release, greedy/beam search (beam 20) over in-reach cells,
each step ≥ 300 m apart, respecting the height budget of section 12 at every step and
ending with enough height to reach `C_rwy`.
```
P(path) = 1 − Π_{cells on path} (1 − p_today(c))
```
Return the top 3 paths that differ by more than 300 m mean separation, each with
`P(path)`, its interval (from the lift intervals), and the number of historical
launches that searched its cells in similar conditions.

## 15. Phase 4 in detail — `scripts/evaluate_prediction.py`

Output: `output/prediction_power_report.html` plus `output/prediction_power.json`
(all numbers, so later runs can be compared). Uses matplotlib for figures.

### 15.1 Resampling
- Held-out unit: day. Bootstrap: resample days with replacement, 2,000 times,
  percentile 95% intervals.
- Permutations: 5,000, always within strata (day × hour block of 2 h).

### 15.2 Layer A metrics
AUC, Brier score, Brier skill score vs the constant rate, reliability diagram (10
quantile bins), and observed connect rate per predicted-probability tercile.
Baselines: constant; logistic on month + hour.

### 15.3 Layer B metrics
1. **Capture@k** (k = 5, 10, 20% of in-reach area): share of held-out winch first
   climbs whose source cell is in the top k% by `p_today`. Same for the baselines
   recomputed on the new labels: traffic, all climbs, same wind, same runway.
   Difference to the best baseline with paired bootstrap over days.
2. **Conversion lift (main result).** For each held-out launch compute
   `best_p = max p_today(c)` over the cells it searched (map fitted without that day).
   Split launches into "searched a top-decile cell" vs not. Within each stratum
   (day × 2-hour block) form the 2 × 2 table (top / not top × connected / not);
   combine with the Mantel–Haenszel risk ratio, 95% interval by day bootstrap,
   p-value by permuting the top/not label within strata.
   Also a dose–response table: connect rate by quintile of `best_p`.
3. The same as (2) for non-ace pilots only, and for each runway separately.
4. **Route metric:** connect rate of launches whose track followed a recommended
   line (mean distance to the line < 250 m) vs not, stratified as in (2).

### 15.4 Layer C metrics
Permutation result of 14.1; table of 14.3.

### 15.5 Power statement
Before trusting a null result: report the minimum detectable risk ratio at 80% power
for the available number of strata and connects (simulation: inject a known lift into
permuted data and count detections). The report must say, for example, "with this data
a lift of 1.4 or more would be detected; smaller effects cannot be confirmed yet".

### 15.6 Tuning and ablation grid
Tuned by leave-one-day-out on the **first 70% of days** only; the last 30% of days are
a final test set looked at once:
`CELL_M ∈ {150, 200, 300}`, `MATCH_RADIUS_M ∈ {150, 200, 300}`,
`SMOOTH_SIGMA_M ∈ {0, 150, 250}`, `K_CELL ∈ {2, 5, 10}`, `M_COND ∈ {10, 20, 50}`,
`LAMBDA_AEROTOW ∈ {0, 0.3, 0.6}`, drift correction on/off, each conditioning dimension
on/off. Objective: conversion lift, tie-break on capture@10.
The report lists every ablation with its change and interval.

### 15.7 Coverage table
Per runway × wind sector: launches, connects, cells with `E_c ≥ 3`, and a verdict
`reliable / thin / no advice` (thin: < 150 launches or < 15 connects; no advice:
< 50 launches). The display honours this verdict.

### 15.8 Prospective log
`scripts/daily_forecast.py` appends each forecast (date, window, runway, pA, top
cells, top paths, config hash) to `data/forecast_log.jsonl`. `evaluate_prediction.py
--prospective` scores logged forecasts against flights that arrived afterwards, using
the same metrics.

## 16. Phase 5 in detail — `src/prediction/first_thermal_map.py`

- Entry: `python scripts/daily_forecast.py [--date D] [--window morning|midday|afternoon]
  [--runway R] [--glider trainer|single]`. Runway defaults to the one implied by the
  wind (existing `determine_runway_from_wind`).
- Steps: forecast → features → `pA` → reach → `p_today` → paths → folium map → log.
- Map layers (folium `FeatureGroup`s, toggled):
  1. Reach polygon (line) and the no-return zone outside it (light grey).
  2. Ground sources: one rectangle per in-reach cell, colour by `lift_today`
     (diverging scale centred on 1.0), opacity by evidence (`E_c`).
  3. "Look here at 250 m": the same cells shifted downwind, as circles sized by `p_today`.
  4. Recommended lines 1–3 with arrowheads and labels "38% (27–49%), 112 launches".
  5. Ace routes (cluster centre lines).
  6. Runways, winch, circuit entry point, wind arrow at 100 m.
- Header box: date, window, runway, wind, `pA` with interval, club average, coverage
  verdict, and the headline numbers from the latest prediction-power report.
- Popups: hits, searches, expected, lift with interval, which conditions apply.
- If the coverage verdict is `no advice`: show only layer 1, layer A and a plain
  message, no ranked spots.

## 17. File overview

| New | Purpose |
|---|---|
| `src/config.py` | all constants |
| `src/thermal_detection/climb_detector.py` | Phase 1 |
| `scripts/audit_labels.py` | label audit |
| `scripts/backfill_convection_weather.py` | weather history |
| `src/prediction/features.py` | shared feature builder |
| `src/prediction/layer_a.py`, `reach.py`, `layer_b.py`, `layer_c.py`, `routes.py` | models |
| `scripts/evaluate_prediction.py` | prediction-power report |
| `src/prediction/first_thermal_map.py` | display |
| `tests/` | unit tests per module (pytest, added to requirements) |

| Changed | How |
|---|---|
| `src/weather/open_meteo_forecast.py` | convection variables |
| `scripts/daily_forecast.py` | calls the new pipeline, writes the forecast log |
| `scripts/update_flights.py` | runs the new detector after import |
| `requirements.txt` | add scikit-learn, scipy, pytest |

| Retired after acceptance (4.6) | |
|---|---|
| `FindThermals_improved.py`, `extract_release_thermal.py`, `thermal_predictor_advanced.py`, `ml_thermal_model.py`, `ml_thermal_predictor.py`, `thermal_forecast.py`, `terrain_enrichment.py` | columns `first_thermal`, `flight_outcome`, `release_*`, `thermal_entry_*` stay until then |

## 18. Definition of done per step

| Step | Done when |
|---|---|
| 1 Detector | tests pass; audit agreement ≥ 95%; tow-release fallback p90 error ≤ 15 s (or guard raised); section 2 table regenerated |
| 2 Evaluation skeleton | report reproduces the four baselines on new labels with intervals |
| 3 Weather | no missing hours on flying days; features identical from archive and forecast code paths for an overlapping date |
| 4 Layer A | calibration within ±3 points per bin; Brier skill > 0 with interval excluding 0 |
| 5 Reach | ≥ 95% of historical first climbs inside their own reach |
| 6 Layer B | `cell_trials` built; leave-one-day-out pass < 1 minute; lifts with intervals |
| 7 Layer C | permutation result and place-vs-skill table in the report |
| 8 Report | all of section 15 present; final 30% test set evaluated once; acceptance decision written into this file |
| 9 Display | daily script produces the page for all six runways; `no advice` path tested; old scripts retired |

---

# 19. Implementation status and results (2026-11-21)

All phases are implemented. Numbers below are from `output/prediction_power_report.html`
(regenerate with `scripts/evaluate_prediction.py`); "test days" = the last 30% of flying
days by date (2026-04-18 → 2026-11-04, 91 days), never used for tuning.

## 19.1 What was built

| Phase | Files |
|---|---|
| Config | `src/config.py` |
| 1 Detector, audit | `src/thermal_detection/climb_detector.py`, `scripts/audit_labels.py`, `data/label_audit.csv` |
| 2 Weather | `scripts/backfill_convection_weather.py` (table `weather_conv`), `src/weather/open_meteo_forecast.py`, `src/prediction/features.py` |
| 3 Models | `src/prediction/layer_a.py`, `reach.py`, `layer_b.py`, `layer_c.py`, `routes.py`, `dataset.py` |
| 4 Evaluation | `src/prediction/evaluation.py`, `scripts/evaluate_prediction.py` (`--tune`, `--prospective`) |
| 5 Display | `src/prediction/first_thermal_map.py`, `scripts/daily_forecast.py` (`--legacy` keeps the old maps) |
| Pipeline | `scripts/train_first_thermal.py`, step 7 in `scripts/update_flights.py` |
| Tests | `tests/` — 31 tests (detector on synthetic flights, features, reach, layer B maths, statistics, routes) |

## 19.2 Deviations from the plan, and why

1. **Tracks are cut to the logbook flight.** 117 winch flights shared one logger file with
   another flight, and 222 tracks did not match the logbook time. The detector splits a
   file into flights and keeps the take-off within 20 min of the logbook start (UTC via
   `zoneinfo`); otherwise the flight is skipped (`track_status`).
2. **Aerotow pairing.** The tug (PH-GOZ) has the same, wrong-time logger file attached to
   most tows, so only 172 tows could be paired with a tug track. The logger clock offset is
   estimated from horizontal distance (altitude alone gave 2 s errors = 70 m false
   separation). The fallback rule misses the 15 s target (median error 8.5 s, p90 103 s),
   but almost all errors are late, which only drops climbs; 3 of 172 (1.7%) were more than
   120 s early. The 120 s guard was kept rather than raised; a tow climb would also need
   1.5 full circles to be counted.
3. **Gap rule from the audit.** The audit (100 flights, 97% agreement) found that all 3
   errors were logging gaps or tracks ending in the air. Such launches are now excluded
   (`gap_in_search` 279, `track_ends_airborne` 30).
4. **Weather source.** Training uses Open-Meteo's *historical-forecast* endpoint (same
   models as the live forecast; has CAPE and heat flux). Boundary-layer height is missing
   there before 2024-09 and is filled from the reanalysis archive; 12% of hours stay empty
   (the gradient-boosting model handles missing values).
5. **Reach thresholds from data.** Plan defaults covered only 77% of real first climbs.
   Changes: minimum workable height 180 m (data: 5th percentile of first-climb start,
   181 m), eight circuit-entry points per runway instead of one (pilots join the circuit in
   different places), no 30 m search allowance. Coverage is now 96.4% (target ≥ 95%), with
   the 180 m circuit-entry floor unchanged. Note: pilots achieve an effective glide of only
   13.8 from release to first climb, so map heights are optimistic.
6. **The plan's conversion-lift test (15.3.2) is biased and was replaced.** Successful
   flights stop searching when they climb, so they touch fewer cells; any "did you touch a
   top cell in the first minute" exposure therefore looks harmful (first result: RR 0.51).
   A "decision point" version was biased the other way (RR 2.5–4, an artefact). Replaced by:
   - **concordance within visited cells**: among the cells a pilot searched, was the
     thermal in the cell the map ranks highest? Conditioning on the path removes both
     traffic and the stop-early effect (null = 0.5);
   - **direction test**: bearing at τ seconds after release vs the model's best of 8
     sectors, launches that connected before τ+10 s excluded, Mantel–Haenszel stratified by
     day × 2 h.
7. **"Expected first thermal" map added** (search traffic from the same runway in similar
   wind × per-cell lift), as the model's answer to the capture question.
8. **Layer C routes** use the first 30 s after release (the decision, before almost anyone
   connects) instead of whole search tracks.
9. **Tuning objective** = concordance (tie-break capture@10), coordinate-wise, on the first
   70% of days only; the direction rule (τ, method) was also chosen on those days.
   Tuning result: drift correction **off** (concordance 0.369 → 0.300), 150 m cells,
   300 m smoothing, K = 10, M = 10, λ = 0.3, wind-direction conditioning dropped (no gain).
10. **Disclosure.** Exploratory analyses while building looked at all days, including the
    test days, so the test set is not fully pristine. The forecast log
    (`data/forecast_log.jsonl`, scored with `--prospective`) is the untouched check.

## 19.3 Results

| Question | Result (test days unless stated) | Verdict |
|---|---|---|
| Labels correct? | audit 97/100; all 3 errors were logging gaps, now excluded | ✓ |
| Real connect rate | 1,002 of 8,190 clean winch launches (12.2%) on 301 days; median release 281 m | — |
| **When** (layer A) | AUC 0.782 vs 0.670 for month + hour; Brier skill 0.112 (0.087–0.135); calibration within 1.3 points in every decile; low/middle/high tercile observed 1.5% / 9.8% / 25.4% | ✓ strong |
| Reach | 96.4% of first climbs inside their own reach | ✓ |
| **Where — concordance** | thermal found in the best third of the cells the pilot searched **63.5%** of the time (chance 33%); mean percentile 0.294 (0.254–0.335), p ≈ 10⁻²⁸, n = 241. Plain climb density 0.380, traffic 0.568 | ✓ strong |
| Where — capture@10 | expected-first-thermal map 71.0% (63.8–78.2) vs best baseline (same-runway first climbs) 70.5%; difference +0.4 points (−2.3 to +3.4) | ✗ ties baseline |
| **Which way to turn** | pre-chosen rule (30 s, map-based): RR 1.14 (0.66–1.88), p = 0.31; smallest detectable RR 1.75. The 30 s history rule flips between periods (0.78 on tuning days, 2.44 on test days) | ✗ no reliable effect |
| **Aces** (layer C) | 76 pilots with ≥ 30 launches; aces connect 1.63× expected, others 0.87×; spread vs chance p = 0.0005 | ✓ skill is real |
| Place or skill? | non-aces on the aces' preferred first-30-s route: RR 0.89 (0.75–1.06), p = 0.89 | skill, not place |

## 19.4 Acceptance decision (plan 4.6)

**Degraded.** Capture does not beat the best baseline and following the model's direction
does not measurably raise the connect rate, so per 4.6 the forecast page shows layer A, the
per-cell rate map with sample counts, where first thermals are usually found, the reach
and the direction history — but no ranked "fly here" lines (`data/models/acceptance.json`).
The recommended-line code (`routes.py`) is built and tested and switches on automatically
when a future evaluation accepts it. The old scripts are **not** retired (4.6 not passed);
they stay reachable via `scripts/daily_forecast.py --legacy`.

What this means in the air: the forecast reliably tells **whether** an hour is worth a
search, and the rate map tells **which of the places you can reach** have converted most
often when searched. The data does not support a single "turn this way after release"
instruction, and the aces' edge is skill rather than a secret direction.

## 19.5 Next steps

1. Let the prospective log run for a season, then score it with
   `scripts/evaluate_prediction.py --prospective`.
2. Re-run `--tune` and the report each winter; more seasons shrink the detectable effect
   (currently RR 1.75).
3. Better tug tracks (a separate logger file per tow) would allow many more aerotow climbs
   to be used as evidence.
4. Terrain could return as a cell feature (polygon edges, not
   centroids) and keep it only if the concordance ablation improves.
