# ZweefApp — first thermal after a winch launch at EHHV

Uses ~9,000 logged winch launches from Hilversum (EHHV) to answer three questions for a
given day, hour and runway:

1. **When:** what is the chance of connecting with a thermal after release? (layer A)
2. **Where:** within reach of the low release, which places have converted into a climb
   most often when searched, in similar wind and season? (layer B)
3. **Who:** do the club's best thermal pilots go somewhere different, and does it help
   others to follow them? (layer C)

Every claim the forecast page makes is backed by a prediction-power report computed on
days the model never saw. See `docs/FIRST_THERMAL_PLAN.md` for the design and the
results, and `output/prediction_power_report.html` for the latest numbers.

## What it can and cannot tell you (held-out test days, Apr–Nov 2026)

| | Result |
|---|---|
| Chance to connect this hour | AUC 0.78, well calibrated (predicted low/middle/high tercile: observed 1.5% / 9.8% / 25.4%) |
| Which reachable places convert when searched | thermal in the map's best third of the cells you search 63.5% of the time (chance 33%) |
| One "turn this way after release" instruction | no reliable effect (RR 1.14, 0.66–1.88) |
| Do the aces know a secret spot? | aces connect 1.6× expected, but others following their route gain nothing: it is skill |

So the page shows the chance, the rate map with sample counts, where first thermals are
usually found and the reach — not ranked "fly here" lines (plan 4.6 not passed).

## Daily use

```bash
# tomorrow, 12–15 h local, runway from the forecast wind
.venv/bin/python scripts/daily_forecast.py
open output/first_thermal_<date>_midday.html

# other windows / runway / glider class
.venv/bin/python scripts/daily_forecast.py morning --runway 25 --glider two_seat
.venv/bin/python scripts/daily_forecast.py --date 2026-05-02 afternoon
.venv/bin/python scripts/daily_forecast.py --all-runways
```

The page shows: the chance to connect (and how often past launches with a similar
forecast actually connected), hourly chances, the reach from today's release with
circuit height left, the per-cell connect rate for today's conditions with sample counts,
where first thermals are usually found from this runway in this wind, the history of
initial directions, and the typical early routes of the best pilots. Ranked "fly here"
lines are only drawn when the evaluation accepted them (`data/models/acceptance.json`).
Each forecast is appended to `data/forecast_log.jsonl` for a prospective check.

## Briefing website (hosted daily)

A static website with today's briefing, rebuilt every night at 00:05 Dutch time:
the day's thermal forecast for EHHV (strength, height, cumulus, chance per hour) and one
briefing map per time window (9–12, 12–15, 15–18 h) and runway. The runway buttons are
ordered by how likely each runway is in the forecast wind (history of runway choices: most
likely runway right 69% of day-windows, top two 91%); the most likely one opens first.

```bash
.venv/bin/python scripts/build_briefing_site.py                    # today, local build
.venv/bin/python scripts/build_briefing_site.py --day 2026-07-28 --past --me "Woltjer"
open output/briefing_site/index.html
```

Hosting on Google Cloud (about €0 a month within the free tiers): see
[cloud/README.md](cloud/README.md). After `update_flights.py`, publish the new history with
`--publish-pack gs://<project>-thermal-data/pack` (or `scripts/publish_briefing_pack.py`).

## Updating

```bash
.venv/bin/python scripts/update_flights.py          # fetch new flights, then retrain (step 7)
.venv/bin/python scripts/train_first_thermal.py     # only retrain
.venv/bin/python scripts/evaluate_prediction.py     # regenerate the prediction-power report
.venv/bin/python scripts/evaluate_prediction.py --tune         # re-tune (first 70% of days only)
.venv/bin/python scripts/evaluate_prediction.py --prospective  # score logged forecasts
```

## Pipeline

| Step | Module | Output |
|---|---|---|
| Convection weather history (Open-Meteo historical forecast) | `scripts/backfill_convection_weather.py` | table `weather_conv` |
| Launch and climb detection (winch, aerotow with tug pairing, self-launch) | `src/thermal_detection/climb_detector.py` | tables `launches`, `climbs`, `search_tracks` |
| Old label columns kept in step with the detector | `src/thermal_detection/sync_legacy_columns.py` | `flights.flight_outcome`, `release_*`, `thermal_entry_*`, `igc_data.first_thermal` |
| Label audit | `scripts/audit_labels.py` | `data/label_audit.csv` |
| Layer A: chance to connect | `src/prediction/layer_a.py` | `launches.pA`, `data/models/layer_a.pkl` |
| Reach | `src/prediction/reach.py` | used by B and the page |
| Layer B: per-cell rates, direction history | `src/prediction/layer_b.py` | `data/models/layer_b.pkl` |
| Layer C: pilot skill, early routes | `src/prediction/layer_c.py` | `data/models/layer_c.pkl` |
| Recommended lines | `src/prediction/routes.py` | page (only if accepted) |
| Evaluation | `scripts/evaluate_prediction.py`, `src/prediction/evaluation.py` | report, `acceptance.json` |
| Forecast page | `src/prediction/first_thermal_map.py`, `scripts/daily_forecast.py` | `output/first_thermal_*.html` |

All tunable constants are in `src/config.py`. Tests: `.venv/bin/python -m pytest tests/`.

## Airspace violations

```bash
.venv/bin/python scripts/find_airspace_violations.py                     # defaults below
.venv/bin/python scripts/find_airspace_violations.py --min-duration 60 --vertical-margin 150ft --horizontal-margin 100m
.venv/bin/python scripts/find_airspace_violations.py --centre ehhv        # circle around the field instead
.venv/bin/python scripts/find_airspace_violations.py --centre 52.15,5.45 --radius-km 40
.venv/bin/python scripts/find_airspace_violations.py --map 110245        # one flight: track + barogram
.venv/bin/python scripts/find_airspace_violations.py --year 2026 --suffix 2026_anonymised --anonymise --details-only --event-pages violation-only
    # anonymised 2026 details page: pilot_1, pilot_2, … (pilot_1 = most violations), own event tracks and CSV
.venv/bin/python scripts/find_airspace_violations.py --year 2026 --event-pages violation-only
    # separate 2026 edition: airspace_violations_report_2026.html, …_map_2026.html, …_details_2026.html, …_2026.csv and a
    # detailed track for every 2026 violation in airspace_events_2026/ (main outputs and database untouched)
.venv/bin/python scripts/find_airspace_violations.py --update-airspace   # fetch the latest Dutch airspace file
```

A violation is a stretch of at least `--min-duration` (30 s) inside airspace of a checked
class (A, B, C, D, P, R), at least `--vertical-margin` (50 m) above its floor and below its
ceiling and `--horizontal-margin` (0 m) inside its boundary. Only flying inside the measuring
circle counts: by default a 50 km circle placed where it covers the most flying time while
keeping EHHV inside (it ends up ~44 km east, over the Veluwe; 89% of all flying time vs 81%
for a circle around the field). Altitudes are the GPS altitudes (AMSL) in the logs; flight
levels use that day's QNH.

Not everything inside airspace is a violation:
- **time-dependent classes** (AIP SUP 03/2022, Regeling luchtverkeersdienstverlening): Lelystad
  TMA 3/4/5 and Nieuw Milligen TMA B and C2/E FL055–FL065 are class D only 18:00–22:00 local
  (class E otherwise); Nieuw Milligen C2/E FL065–FL095 is class D on weekdays and only
  18:00–22:00 on weekends/holidays. Outside those windows nothing is counted (`--no-time-rules` to switch off);
- **unverified** — areas activated by NOTAM: TRA/TSA (e.g. EHTRA 80 Deelen Hoog), areas tagged
  `<notam>`, and restricted areas without an H24 tag (e.g. EHR 9 Harskamp, EHR 3A Oldebroek):
  compare with the NOTAMs of that day (`--intermittent-regex`, `--count-intermittent`);
- **possible** — CTR Deelen always (crossing clearance can be requested from Dutch Mil, `--clearance-regex`),
  and military TMAs/CTRs on weekends and Dutch public holidays (`--part-time-regex`);
- **exempt** — inside a gliding sector (e.g. Terlet inside CTR Deelen).
Schiphol TMAs (class A), EHP 25 Drakensteyn (prohibited, H24) and Lelystad TMA 2 (class D) have no
time or weekend rules.

Outputs: `output/airspace_violations_report.html` (overview without names: tiles; "how high can I go"
floor map; events per airspace; per weekday, month, year and launch method; severity; worst cases),
`output/airspace_violations_details.html` (every event with pilot in command, second pilot, aircraft,
time, airspace, depth and why it got its status; per-pilot summary; filter, search and sort),
`output/airspace_events/event_<id>.html` (detailed track of one event: map zoomed on the violation with
entry/exit times, altitude vs. floor and margin, distance inside the boundary, every fix; made for the
`--event-top` (10) longest violations plus the (possible) violations of `--event-pilot NAME` as pilot in command; `--event-pages
violations|all` for more). Clicking a row in the details report opens its event track (or the whole flight)
in an overlay,
`output/airspace_violations_map.html` (interactive, same base map as the forecast page:
airspace and every event as a track segment, click for the flight page),
`output/airspace_maps/flight_<id>.html` (track + barogram with the airspace floor, one per flight
with an event), `output/airspace_violations.csv`, and the tables `airspace_checks` /
`airspace_violations`. The report contains pilot names — use `--anonymise` before sharing it.
The airspace file (SoaringWeb, via the XCSoar data repository) is unofficial — check the AIP.

## Data collection

```bash
cp .env.example .env            # GlidingApp credentials
.venv/bin/python src/data_collection/fetch_igc_files.py
.venv/bin/python src/data_collection/store_icg_data.py
```

## Legacy

The earlier heatmaps and terrain-scored predictors (`thermal_predictor_advanced.py`,
`ml_thermal_predictor.py`, …) are still available via `scripts/daily_forecast.py --legacy`;
they now read the corrected labels, because the climb detector writes its results into the
old columns too. The old detectors (`FindThermals_improved.py`, `extract_release_thermal.py`)
refuse to run without `--legacy-overwrite`; the previous labels are kept in the tables
`legacy_labels_backup` and `legacy_first_thermal_backup`
(`python -m src.thermal_detection.sync_legacy_columns --restore` puts them back).
The old maps' "confidence" numbers were similarity scores, not probabilities; see the plan
document for why they were replaced.

## Maps

All folium pages use the Dutch government base map (PDOK BRT-Achtergrondkaart, open data,
no key) via `src/visualization/basemap.py`. OpenStreetMap's volunteer tile servers block
local HTML files and CARTO needs an API key. The layer menu also offers PDOK grey, PDOK
aerial photo and an Esri street map (useful outside the Netherlands, where PDOK is blank).

## Safety

Probabilities, not guarantees. Reach uses a still-air glide; real pilots lose height
faster while searching, so heights on the map are optimistic. Always keep circuit height.
