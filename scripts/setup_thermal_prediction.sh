#!/bin/bash

# Advanced Thermal Prediction Setup Script
# Runs all setup steps in the correct order

set -e  # Exit on error

echo "============================================================"
echo "ADVANCED THERMAL PREDICTION SYSTEM - SETUP"
echo "============================================================"
echo ""
echo "This will set up your terrain-aware thermal prediction system:"
echo "  1. Improve thermal detection (mark only winch-reachable thermals)"
echo "  2. Fetch terrain features from OpenStreetMap"
echo "  3. Enrich database with terrain data"
echo ""
echo "Estimated time: 25-30 minutes"
echo "============================================================"
echo ""

read -p "Continue? (yes/no): " response
if [[ ! "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
    echo "Setup cancelled."
    exit 0
fi

echo ""
echo "============================================================"
echo "STEP 1/2: IMPROVING THERMAL DETECTION"
echo "============================================================"
echo ""
echo "Running FindThermals_improved.py..."
echo "This will process ~12,000 flights and take ~20 minutes."
echo ""

.venv/bin/python src/thermal_detection/FindThermals_improved.py --yes

echo ""
echo "✓ Thermal detection complete!"
echo ""

echo "============================================================"
echo "STEP 2/2: TERRAIN ENRICHMENT"
echo "============================================================"
echo ""
echo "Running terrain_enrichment.py..."
echo "This will fetch data from OpenStreetMap and take ~5 minutes."
echo ""

.venv/bin/python src/prediction/terrain_enrichment.py --yes

echo ""
echo "============================================================"
echo "SETUP COMPLETE!"
echo "============================================================"
echo ""
echo "Your thermal prediction system is ready to use!"
echo ""
echo "To get tomorrow's thermal prediction, run:"
echo "  .venv/bin/python src/prediction/thermal_predictor_advanced.py"
echo ""
echo "For more options, see docs/THERMAL_PREDICTION_SETUP.md"
echo ""
echo "Good luck with your flying! 🪂"
echo ""
