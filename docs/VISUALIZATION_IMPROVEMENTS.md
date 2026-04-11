# Visualization Improvements

## Date: 2026-03-27

## Summary

Created `plot_data_improved.py` - an enhanced version of `plot_data.py` with better filtering, exclusions, and markers.

## Problems with Original `plot_data.py`

### ❌ Issues Found:

1. **Includes winch launch data** - altitude < 400m includes the climb from 0-400m
2. **No aerotow exclusion** - PH-GOZ (tow plane) included in data
3. **Only one marker** - just center point, no context
4. **Hardcoded filters** - wind dir/speed not configurable
5. **Morning filter bug** - checks `< 22` hours (actually shows all day)
6. **No statistics** - unclear what data is shown
7. **Limited visualization** - just basic heatmap

## Improvements in `plot_data_improved.py`

### ✅ Better Filtering:

**1. Excludes Winch Launch:**
```python
--min-alt 200  # Default: only data above 200m
```
- Winch launch is 0-200m (excluded)
- Only shows actual thermal circling

**2. Excludes Aerotow Plane:**
```python
f.registratie NOT IN ('PH-GOZ', 'PH GOZ')
```
- PH-GOZ (tow plane) completely excluded
- Only actual gliders shown

**3. Uses `first_thermal` Flag:**
```python
--use-first-thermal  # Default: ON
```
- Uses your improved thermal detection
- Shows only first thermal after winch release
- Much cleaner data

### ✅ Enhanced Visualization:

**Multiple Markers:**
1. 🟢 **Airport (EHHV)** - Green plane icon
2. 🟠 **Winch release zone** - Orange circle (~800m radius)
3. 🔴 **Top 5 hotspots** - Red markers with statistics
4. 🔵 **Search radius** - Blue dashed circle (10km)

**Legend with Statistics:**
- Total data points
- Unique flights
- Average altitude
- Average climb rate
- Average distance from airport
- Average wind conditions (if filtered)

**Improved Heatmap:**
- Weighted by climb rate (stronger thermals = more intense color)
- Better color gradient (blue → cyan → lime → yellow → red)
- Adjustable radius and blur

### ✅ Configurable Parameters:

```bash
# Basic usage (good defaults)
python plot_data_improved.py

# Custom altitude range
python plot_data_improved.py --min-alt 200 --max-alt 800

# Filter by wind
python plot_data_improved.py --wind-dir 300 --wind-tolerance 20

# Filter by wind speed
python plot_data_improved.py --wind-speed-min 10 --wind-speed-max 25

# Filter by time of day
python plot_data_improved.py --time-window midday

# Combine filters
python plot_data_improved.py --wind-dir 295 --wind-speed-min 10 --wind-speed-max 20 --time-window midday

# Custom output
python plot_data_improved.py --output output/my_heatmap.html
```

## Comparison: Old vs New

### Original `plot_data.py`:
```python
# Hardcoded filters
altitude < 400           # Includes winch launch (0-400m)
vertical_speed > 0       # Any positive climb
wind_dir BETWEEN 290-300 # Fixed range
wind_spd BETWEEN 10-20   # Fixed range
time < 22:00 CET         # Bug: shows all day

# No exclusions
# - Aerotow plane included
# - Winch launch included

# Output
# - Basic heatmap
# - One marker (center)
# - No statistics
```

**Result:**
- Cluttered data with winch launches and tow plane
- Can't change parameters without editing code

### Improved `plot_data_improved.py`:
```python
# Smart defaults
--min-alt 200            # Excludes winch launch
--max-alt 600            # Winch-reachable altitude
--min-vs 0.5             # Meaningful lift
--use-first-thermal      # Clean thermal data

# Automatic exclusions
PH-GOZ excluded          # No aerotow
first_thermal = 1        # Only post-release thermals

# Output
# - Weighted heatmap
# - 5 markers with context
# - Statistics legend
# - Hotspot markers
```

**Result:**
- Clean data showing actual thermal locations
- Configurable via command line
- Much more informative

## Generated Maps

### 1. `output/heatmap_improved.html`
**Default settings** - All first thermals, 200-600m altitude

**Statistics:**
- Data points: 69,137
- Unique flights: 2,163
- Avg altitude: 359m
- Avg climb rate: 2.1 m/s

**Best for:**
- General overview of thermal activity
- Identifying common thermal areas
- Planning flights

### 2. `output/heatmap_wind_filtered.html`
**Filtered** - Wind 295° ±5°, 10-20 km/h, 150-400m altitude

**Statistics:**
- Data points: 2,073
- Unique flights: 105
- Avg altitude: 273m
- Avg climb rate: 1.9 m/s

**Best for:**
- Specific weather conditions
- Tomorrow's wind forecast
- Tactical planning

## Usage Examples

### Show All Thermal Activity:
```bash
python src/visualization/plot_data_improved.py
```

### Filter by Tomorrow's Wind:
```bash
# If tomorrow: 300° at 15 km/h
python src/visualization/plot_data_improved.py \
  --wind-dir 300 \
  --wind-tolerance 20 \
  --wind-speed-min 10 \
  --wind-speed-max 20
```

### Morning Flight Planning:
```bash
python src/visualization/plot_data_improved.py \
  --time-window morning \
  --output output/morning_thermals.html
```

### Low Altitude Thermals Only:
```bash
python src/visualization/plot_data_improved.py \
  --min-alt 200 \
  --max-alt 400 \
  --output output/low_thermals.html
```

### Strong Thermals Only:
```bash
python src/visualization/plot_data_improved.py \
  --min-vs 2.0 \
  --output output/strong_thermals.html
```

## Map Features Explained

### 🟢 Airport Marker (Green Plane)
- Center of map
- Hilversum Airport (EHHV)
- Elevation: 1m MSL
- Your launch point

### 🟠 Winch Release Zone (Orange Circle)
- ~800m radius from airport
- Typical winch release altitude: 400m
- Shows where you'll be after launch
- DON'T expect thermals here (still climbing)

### 🔴 Hotspot Markers (Red Circles)
- Top 5 most active thermal areas
- Click for details:
  - Thermal count
  - Average climb rate
  - Distance from airport
  - Bearing to fly

### 🔵 Search Radius (Blue Dashed)
- 10km radius (default)
- Shows data boundary
- Typical cross-country range from winch launch

### Heatmap Colors:
- 🔵 **Blue:** Low thermal activity (few points)
- 🟢 **Green/Cyan:** Moderate activity
- 🟡 **Yellow:** High activity
- 🔴 **Red:** Very high activity (many strong thermals)

## Key Differences from thermal_predictor_advanced.py

### `plot_data_improved.py` (Historical View):
- Shows **ALL historical thermals**
- No weather forecast
- No confidence scoring
- Pure density map
- Best for: General patterns, long-term trends

### `thermal_predictor_advanced.py` (Forecast):
- Shows **tomorrow's predictions**
- Uses weather forecast
- Confidence scoring (0-100%)
- Terrain-aware
- Best for: Tomorrow's flight planning

## When to Use Each Tool

### Use `plot_data_improved.py` when:
- ✅ Learning the area (first time flying EHHV)
- ✅ Analyzing seasonal patterns
- ✅ Checking "where do thermals usually form?"
- ✅ Not flying tomorrow (just exploring data)
- ✅ Want to see ALL thermal activity (big picture)

### Use `thermal_predictor_advanced.py` when:
- ✅ Flying tomorrow (need specific forecast)
- ✅ Want confidence scores (80% vs 60% likelihood)
- ✅ Need terrain context (forest edge, downwind effects)
- ✅ Want top 5 recommendations with reasoning
- ✅ Need wind-specific predictions

## Migrating from Old Script

**Old workflow:**
```bash
# Edit plot_data.py to change parameters
# Run script
python src/visualization/plot_data.py
```

**New workflow:**
```bash
# Just pass parameters on command line
python src/visualization/plot_data_improved.py \
  --wind-dir 300 \
  --wind-speed-min 10 \
  --wind-speed-max 20
```

**Keep old script?**
- ✅ Keep `plot_data.py` as backup
- ✅ Use `plot_data_improved.py` going forward
- Script is backward compatible (same output format)

## Files

### Created:
- `src/visualization/plot_data_improved.py` (370 lines)
- `output/heatmap_improved.html` (default output)
- `output/heatmap_wind_filtered.html` (example with filters)
- `docs/VISUALIZATION_IMPROVEMENTS.md` (this file)

### Kept:
- `src/visualization/plot_data.py` (original, unmodified)
- `output/heatmap.html` (original output)

## Summary

✅ **Better data filtering** (excludes winch launch + aerotow)
✅ **More informative** (5 markers + statistics legend)
✅ **Configurable** (CLI parameters, no code editing)
✅ **Cleaner visualization** (weighted heatmap)
✅ **Backward compatible** (same data, same format)

**Result:** Much better tool for understanding thermal patterns at Hilversum! 🎯
