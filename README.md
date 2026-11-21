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
| Label audit | `scripts/audit_labels.py` | `data/label_audit.csv` |
| Layer A: chance to connect | `src/prediction/layer_a.py` | `launches.pA`, `data/models/layer_a.pkl` |
| Reach | `src/prediction/reach.py` | used by B and the page |
| Layer B: per-cell rates, direction history | `src/prediction/layer_b.py` | `data/models/layer_b.pkl` |
| Layer C: pilot skill, early routes | `src/prediction/layer_c.py` | `data/models/layer_c.pkl` |
| Recommended lines | `src/prediction/routes.py` | page (only if accepted) |
| Evaluation | `scripts/evaluate_prediction.py`, `src/prediction/evaluation.py` | report, `acceptance.json` |
| Forecast page | `src/prediction/first_thermal_map.py`, `scripts/daily_forecast.py` | `output/first_thermal_*.html` |

All tunable constants are in `src/config.py`. Tests: `.venv/bin/python -m pytest tests/`.

## Data collection

```bash
cp .env.example .env            # GlidingApp credentials
.venv/bin/python src/data_collection/fetch_igc_files.py
.venv/bin/python src/data_collection/store_icg_data.py
```

## Legacy

The earlier heatmaps and terrain-scored predictors (`thermal_predictor_advanced.py`,
`ml_thermal_predictor.py`, `FindThermals_improved.py`, …) are still available via
`scripts/daily_forecast.py --legacy`. Their "confidence" numbers were similarity scores,
not probabilities; see the plan document for why they were replaced.

## Safety

Probabilities, not guarantees. Reach uses a still-air glide; real pilots lose height
faster while searching, so heights on the map are optimistic. Always keep circuit height.
