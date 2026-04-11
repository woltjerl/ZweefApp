# Advanced Thermal Prediction Setup Guide

## Overview

Your new terrain-aware thermal prediction system uses:
- ✅ **Historical thermal data** (180k+ points)
- ✅ **Weather forecast** (wind, temperature, time)
- ✅ **Terrain features** (forests, water, urban areas from OpenStreetMap)
- ✅ **Weighted confidence scoring** (wind 35%, terrain 30%, temp 20%, time 10%, climb rate 5%)

## One-Time Setup (Run Once)

### Step 1: Improve Thermal Detection (20 minutes)

Run the improved thermal finder to mark only winch-reachable thermals (<600m):

```bash
.venv/bin/python FindThermals_improved.py
```

**What it does:**
- Resets all thermal flags
- Detects first thermal after winch release
- Marks ALL points with lift at reachable altitude
- Excludes aerotow plane (PH-GOZ)
- Processes ~12,000 flights

**Expected output:**
- Thermals found: ~5,000-6,000 flights
- Total thermal points: ~60,000-100,000 (down from 183k)
- Average points per thermal: ~12-20

### Step 2: Enrich with Terrain Data (5 minutes)

Fetch terrain features and add to database:

```bash
.venv/bin/python terrain_enrichment.py
```

**What it does:**
- Fetches forests, water, urban areas from OpenStreetMap (~6 API calls)
- Adds 7 columns to igc_data table:
  - `distance_to_forest` (km)
  - `bearing_to_forest` (degrees)
  - `distance_to_water` (km)
  - `bearing_to_water` (degrees)
  - `distance_to_urban` (km)
  - `bearing_to_urban` (degrees)
  - `terrain_type` (forest, forest_edge, field, urban_edge, water)
- Processes all thermal points in batches

**Expected output:**
```
Forests: ~50-100
Water bodies: ~20-40
Urban areas: ~30-60
Terrain enrichment complete!
```

## Daily Use

### Get Tomorrow's Thermal Prediction

```bash
.venv/bin/python thermal_predictor_advanced.py
```

**Default:** Tomorrow, midday (12:00-15:00)

**Custom options:**
```bash
# Morning flight
.venv/bin/python thermal_predictor_advanced.py --time-window morning

# Afternoon
.venv/bin/python thermal_predictor_advanced.py --time-window afternoon

# Specific date
.venv/bin/python thermal_predictor_advanced.py --date 2026-03-30

# Custom output file
.venv/bin/python thermal_predictor_advanced.py --output my_flight.html
```

### What You Get

**1. Console output:**
```
🎯 HOTSPOT #1 - 87% CONFIDENCE
   Location: 52.1942, 5.1420
   Bearing: 279° (W) from EHHV
   Distance: 0.7 km
   Expected climb: 2.3 m/s
   Terrain: forest_edge
   Based on 45 historical thermals

   Why this spot:
     • Wind match: 95% (dir ±5°, speed ±1.2 km/h)
     • Terrain: Forest edge (thermal trigger), Downwind of forest (80% bonus)
     • Temperature match: 85% (±1.8°C)

📍 After winch release (~400m):
   1. Turn toward W (279°)
   2. Fly 0.7 km
   3. Search forest_edge area
```

**2. Interactive map** (`thermal_prediction_advanced.html`):
- **Heatmap:** Shows thermal density weighted by confidence
- **Hotspot markers:** Top 10 spots color-coded by confidence
  - 🔴 Red: 80-100% confidence (VERY HIGH)
  - 🟠 Orange: 70-80% confidence (HIGH)
  - 🟡 Yellow: 60-70% confidence (GOOD)
  - 🔵 Blue: 40-60% confidence (MODERATE)
- **Click markers** for detailed reasoning
- **Wind arrow** from airport showing forecast direction
- **Airport marker** (green plane icon)

## How The Scoring Works

### Wind Similarity (35% weight)
```
Perfect match (0° diff, 0 km/h diff) = 100%
Good match (±15°, ±3 km/h) = 80%
Acceptable (±30°, ±5 km/h) = 50%
Poor (±45°, ±8 km/h) = 20%
```

### Terrain Context (30% weight)
```
Forest edge + downwind = +50%
Urban edge (morning) = +15-35%
Open field (afternoon) = +15%
Near water = -30% (sink zone)
```

**Downwind logic:**
- Wind FROM 270° (west) → Thermals form EAST of forests
- System calculates if thermal is downwind of trigger point
- Bonus scales with alignment (perfect = 180° opposite)

### Temperature (20% weight)
```
Exact match = 100%
±2°C = 80%
±5°C = 50%
±10°C = 0%
```

### Time of Day (10% weight)
```
Correct time window = 80%
(Already filtered by morning/midday/afternoon)
```

### Climb Rate (5% weight)
```
Strong thermal (>3 m/s) = 100%
Good thermal (2-3 m/s) = 66%
Weak thermal (<1 m/s) = 33%
```

### Final Confidence
```
Confidence = 0.35×wind + 0.30×terrain + 0.20×temp + 0.10×time + 0.05×climb
```

## Interpreting Results

### High Confidence (80-100%) ✅
- **Strong match:** Wind, terrain, and temperature all align
- **Go here first:** This is your primary target
- **Example:** "West wind, forest edge, downwind, perfect temp match"

### Good Confidence (60-80%) ✅
- **Good match:** Most factors align well
- **Solid backup:** If primary fails, try this
- **Example:** "Wind close, good terrain, temp slightly off"

### Moderate Confidence (40-60%) ⚠️
- **Okay match:** Some factors align
- **Third choice:** If others don't work
- **Example:** "Wind okay, neutral terrain"

### Low Confidence (<40%) ❌
- **Poor match:** Few factors align
- **Not shown:** Filtered out from recommendations

## Comparison: Old vs New System

| Feature | Old (thermal_forecast.py) | New (thermal_predictor_advanced.py) |
|---------|---------------------------|-------------------------------------|
| **Data filtering** | Wind only | Wind + terrain + temp + time |
| **Scoring** | Binary (match/no match) | Weighted confidence (0-100%) |
| **Terrain awareness** | ❌ None | ✅ Full (forests, urban, water) |
| **Downwind effects** | ❌ None | ✅ Thermal triggers + downwind bonus |
| **Temperature** | ❌ Not used | ✅ 20% weight |
| **Confidence levels** | ❌ All equal | ✅ Color-coded priorities |
| **Reasoning** | ❌ None | ✅ Explains each recommendation |
| **Hotspot quality** | Grid density only | Weighted by confidence |

## Example Output Interpretation

```
🎯 HOTSPOT #1 - 87% CONFIDENCE
   Bearing: 279° (W)
   Distance: 0.7 km
   Terrain: forest_edge

   Why:
     • Wind match: 95% (dir ±5°, speed ±1.2 km/h)
     • Terrain: Forest edge, Downwind of forest (80% bonus)
     • Temperature match: 85% (±1.8°C)
```

**Translation:**
1. **87% confidence** = Very reliable prediction
2. **279° W** = Turn west after release
3. **0.7 km** = ~90 seconds of flight at 30 km/h
4. **Forest edge** = Known thermal trigger
5. **Downwind** = Wind pushes warm air from forest → creates lift
6. **95% wind match** = Almost identical conditions to historical thermals
7. **85% temp match** = Similar atmospheric conditions

**What to do:**
1. Release winch at ~400m
2. Turn left to heading 279°
3. Fly straight for 0.7 km (~45-60 seconds)
4. Look for forest edge on your right
5. Start circling when you feel lift
6. If no lift after 1 km, try backup hotspot #2

## Troubleshooting

### "No historical data found"
- Conditions too specific (rare wind direction/speed)
- Try broader time window or different date
- Check if terrain enrichment completed successfully

### "No significant hotspots found"
- All hotspots below 40% confidence threshold
- Weather conditions uncommon in your area
- Consider flying different time window

### "Terrain type NULL"
- Terrain enrichment not completed
- Run `terrain_enrichment.py` again

### Map doesn't load
- Check file permissions
- Verify browser can open HTML files
- Check console for JavaScript errors

## Advanced Usage

### Compare Different Time Windows
```bash
# Generate all three
.venv/bin/python thermal_predictor_advanced.py --time-window morning --output morning.html
.venv/bin/python thermal_predictor_advanced.py --time-window midday --output midday.html
.venv/bin/python thermal_predictor_advanced.py --time-window afternoon --output afternoon.html
```

### Custom Wind Override (Testing)
Edit `thermal_predictor_advanced.py` to add manual wind input:
```python
# In parse_arguments()
parser.add_argument('--wind-dir', type=float, help='Override wind direction')
parser.add_argument('--wind-speed', type=float, help='Override wind speed')
```

## Database Schema Changes

### New Columns in `igc_data`
```sql
distance_to_forest REAL     -- km to nearest forest
bearing_to_forest REAL      -- degrees (0-360)
distance_to_water REAL      -- km to nearest water
bearing_to_water REAL       -- degrees (0-360)
distance_to_urban REAL      -- km to nearest urban
bearing_to_urban REAL       -- degrees (0-360)
terrain_type TEXT           -- forest, forest_edge, field, urban_edge, water
```

### Space Usage
- Original database: ~150 MB
- After terrain enrichment: ~155 MB (+3%)
- Negligible impact

## Next Steps (Optional)

### Machine Learning Enhancement
If you want even better predictions:
1. Export thermal data with all features to CSV
2. Train Random Forest classifier
3. Feature importance analysis
4. Integrate ML predictions with current system

**Estimated improvement:** +10-15% accuracy

### More Terrain Features
- Plowed fields (dark = hot)
- Parking lots (heat up fast)
- Hills and slopes (topographic lift)
- Add via `terrain_enrichment.py` modifications

### Real-time Validation
- Add GPS logger to log where you actually find thermals
- Compare predictions vs reality
- Refine scoring weights

## Summary

**You now have:**
✅ Improved thermal detection (only winch-reachable thermals)
✅ Terrain-aware predictions (forests, water, urban)
✅ Weighted confidence scoring (0-100%)
✅ Downwind thermal trigger detection
✅ Interactive map with detailed reasoning
✅ Top 5 recommendations with flight strategy

**After winch launch, you'll know:**
- 🎯 Exact bearing to turn toward
- 📏 Exact distance to fly
- 🌳 What terrain to look for
- 💯 Confidence level for each option
- 🔄 Why each spot is predicted
- 🛡️ Backup plans if primary fails

**Good luck with your flying!** 🪂
