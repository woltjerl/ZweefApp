#!/bin/bash

# Daily Thermal Forecast Generator
# Fetches tomorrow's weather and generates all thermal prediction maps

set -e  # Exit on error

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "============================================================"
echo "DAILY THERMAL FORECAST GENERATOR"
echo "============================================================"
echo ""

# Get tomorrow's date
TOMORROW=$(date -v+1d +%Y-%m-%d 2>/dev/null || date -d "+1 day" +%Y-%m-%d 2>/dev/null || date +%Y-%m-%d)
echo "Generating forecasts for: $TOMORROW"
echo ""

# Get time window (default: midday)
TIME_WINDOW="${1:-midday}"

if [[ "$TIME_WINDOW" == "morning" ]]; then
    echo "Time window: MORNING (09:00-12:00)"
elif [[ "$TIME_WINDOW" == "afternoon" ]]; then
    echo "Time window: AFTERNOON (15:00-18:00)"
else
    echo "Time window: MIDDAY (12:00-15:00)"
    TIME_WINDOW="midday"
fi

echo ""
echo "============================================================"
echo "STEP 1/2: ADVANCED TERRAIN-AWARE PREDICTION"
echo "============================================================"
echo ""

# Run advanced prediction
.venv/bin/python src/prediction/thermal_predictor_advanced.py \
    --date "$TOMORROW" \
    --time-window "$TIME_WINDOW" \
    --output "output/thermal_prediction_advanced_${TIME_WINDOW}.html"

echo ""
echo "============================================================"
echo "STEP 2/2: HISTORICAL HEATMAP"
echo "============================================================"
echo ""

# Run historical heatmap
.venv/bin/python src/visualization/plot_data_improved.py \
    --time-window "$TIME_WINDOW" \
    --output "output/heatmap_historical_${TIME_WINDOW}.html"

echo ""
echo "============================================================"
echo "COMPLETE!"
echo "============================================================"
echo ""
echo -e "${GREEN}✓${NC} Generated 2 maps for $TOMORROW ($TIME_WINDOW):"
echo ""
echo -e "${BLUE}1. Advanced Prediction (Terrain-Aware):${NC}"
echo "   output/thermal_prediction_advanced_${TIME_WINDOW}.html"
echo "   • Uses tomorrow's weather forecast"
echo "   • Confidence scoring (0-100%)"
echo "   • Terrain physics (downwind effects)"
echo "   • Top 5 recommendations with reasoning"
echo ""
echo -e "${BLUE}2. Historical Heatmap (Statistical):${NC}"
echo "   output/heatmap_historical_${TIME_WINDOW}.html"
echo "   • All historical thermals"
echo "   • Density-based visualization"
echo "   • Top 5 hotspots from history"
echo "   • Statistics legend"
echo ""
echo -e "${YELLOW}💡 Pro Tip:${NC}"
echo "   Open BOTH maps and look for overlapping red areas!"
echo "   Those have the highest probability of lift."
echo ""
echo "To view maps:"
echo "   open output/thermal_prediction_advanced_${TIME_WINDOW}.html"
echo "   open output/heatmap_historical_${TIME_WINDOW}.html"
echo ""
echo "Good luck with your flying tomorrow! 🪂"
echo ""
