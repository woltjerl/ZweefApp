"""
Runway and Aerotow Line Constants for Hilversum Airport (EHHV)

Calculated from actual GPS tracks of all winch launches and aerotow flights.
- Start positions: median ground-level GPS coordinates per runway
- End positions: median winch release point (peak altitude before first descent)
- Aerotow: takeoff roll only (ground to 50m altitude)

Analysis based on 5900+ winch launches and 1200+ aerotow flights.
"""

# Hilversum Airport center (median of all ground-level GPS points)
AIRPORT_LAT = 52.193650
AIRPORT_LON = 5.145217

# Winch launch runways (from actual GPS data: ground start → winch release point)
RUNWAY_LINES = {
    '07': {
        'bearing': 64.2,
        'start': (52.191675, 5.139661),
        'end': (52.194367, 5.148733),
        'color': '#FF6B6B',  # Red
        'name': 'Runway 07',
    },
    '12': {
        'bearing': 93.7,
        'start': (52.193842, 5.143855),
        'end': (52.193533, 5.151667),
        'color': '#4ECDC4',  # Teal
        'name': 'Runway 12',
    },
    '18': {
        'bearing': 182.8,
        'start': (52.194797, 5.145185),
        'end': (52.189542, 5.144767),
        'color': '#95E1D3',  # Light teal
        'name': 'Runway 18',
    },
    '25': {
        'bearing': 249.6,
        'start': (52.194316, 5.149731),
        'end': (52.192317, 5.140983),
        'color': '#F38181',  # Pink
        'name': 'Runway 25',
    },
    '30': {
        'bearing': 273.2,
        'start': (52.193511, 5.151617),
        'end': (52.193783, 5.143567),
        'color': '#AA96DA',  # Purple
        'name': 'Runway 30',
    },
    '36': {
        'bearing': 357.9,
        'start': (52.189432, 5.145135),
        'end': (52.194733, 5.144817),
        'color': '#FCBAD3',  # Light pink
        'name': 'Runway 36',
    },
}

# Aerotow line (takeoff roll: ground to 50m altitude)
AEROTOW_LINE = {
    'bearing': 357.6,
    'start': (52.191730, 5.146387),
    'end': (52.193597, 5.146260),
    'color': '#FFD93D',  # Yellow
    'name': 'Aerotow',
}
