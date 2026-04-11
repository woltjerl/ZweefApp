# Project Cleanup Plan

## Current Issues
- Backup files scattered (.backup, .pre-improvements)
- Multiple HTML output files (8+ heatmaps)
- No clear structure
- Mix of source code, outputs, docs, and backups

## New Structure

```
ZweefApp/
├── README.md                          # Main project documentation
├── requirements.txt                   # Python dependencies
├── .env / .env.example               # Environment configuration
├── .gitignore                        # Git ignore rules
│
├── src/                              # Source code
│   ├── data_collection/
│   │   ├── fetch_igc_files.py       # Download IGC files from API
│   │   ├── store_icg_data.py        # Process and store in database
│   │   └── gliding_api.py           # API client
│   │
│   ├── thermal_detection/
│   │   ├── FindThermals_improved.py # Thermal detection with change detection
│   │   └── igc_parser.py            # IGC file parser
│   │
│   ├── weather/
│   │   └── HilversumWeatherFetcher.py # Weather data fetcher
│   │
│   ├── prediction/
│   │   ├── thermal_forecast.py      # Basic thermal prediction
│   │   ├── thermal_predictor_advanced.py # Advanced terrain-aware
│   │   └── terrain_enrichment.py    # OpenStreetMap terrain data
│   │
│   └── visualization/
│       ├── plot_data.py             # Basic plotting
│       ├── plot_data2.py            # Advanced heatmaps
│       └── plot_data3.py            # Experimental plots
│
├── data/                             # Data files
│   ├── flights.db                   # Main database
│   └── icg_files/                   # Downloaded IGC files
│
├── output/                           # Generated files
│   └── .gitignore                   # Ignore all outputs
│
├── logs/                             # Log files
│   └── .gitignore                   # Ignore all logs
│
├── docs/                             # Documentation
│   ├── QUICK_START.md
│   ├── THERMAL_PREDICTION_SETUP.md
│   ├── COMPREHENSIVE_IMPROVEMENTS_SUMMARY.md
│   ├── IMPLEMENTATION_SUMMARY.md
│   ├── README_FETCH_IGC.md
│   └── STORE_ICG_FIX_SUMMARY.md
│
├── scripts/                          # Helper scripts
│   └── setup_thermal_prediction.sh
│
└── backups/                          # Old backup files
    └── .gitignore                   # Ignore all backups
```

## Files to Delete
- ✓ *.backup (old backups)
- ✓ *.pre-improvements (old backups)
- ✓ Old heatmap HTML files (keep only latest)
- ✓ test.py (if not needed)
- ✓ icg_converter.py (if obsolete)
- ✓ pdf_counter.py (if not used)
- ✓ .DS_Store (macOS metadata)
- ✓ flights.db-journal (temporary SQLite file)

## Files to Archive in /backups
- HilversumWeatherFetcher.py.backup
- HilversumWeatherFetcher.py.pre-improvements
- fetch_igc_files.py.backup
- store_icg_data.py.pre-improvements

## Files to Move to /output
- heatmap*.html (all 8+ files)
- thermal_forecast.html
- thermal_prediction_advanced.html (when generated)

## Files to Move to /logs
- fetch_igc_files.log
- store_icg_data.log

## Files to Move to /docs
- COMPREHENSIVE_IMPROVEMENTS_SUMMARY.md
- IMPLEMENTATION_SUMMARY.md
- QUICK_START.md
- README_FETCH_IGC.md
- STORE_ICG_FIX_SUMMARY.md
- THERMAL_PREDICTION_SETUP.md

## Files to Keep in Root
- .env, .env.example
- .gitignore
- README.md (create main one)
- requirements.txt (if exists)
- setup_thermal_prediction.sh (or move to /scripts)
