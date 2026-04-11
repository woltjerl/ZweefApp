import sqlite3
import pandas as pd
import numpy as np
import folium
import branca.colormap as cm

# ----------------------------
# 1. Configuration and Data Loading
# ----------------------------
db_path = "data/flights.db"  # Database name
grid_size_m = 80  # Grid cell size in meters (change this to configure the grid size)

# Connect to the database and load the igc_data table
conn = sqlite3.connect(db_path)
df = pd.read_sql_query("SELECT * FROM igc_data", conn)
conn.close()

# Convert relevant columns to numeric
df['vertical_speed'] = pd.to_numeric(df['vertical_speed'], errors='coerce')
df['wind_dir'] = pd.to_numeric(df['wind_dir'], errors='coerce')
df['altitude'] = pd.to_numeric(df['altitude'], errors='coerce')

# -----------------------------------------------------------
# 2. Filter points within 5 km of EHHV (approximate bounding box)
# -----------------------------------------------------------
center_lat = 52.1932  # EHHV latitude
center_lon = 5.1528  # EHHV longitude

# Approximate conversion: 1 degree latitude ~111 km; adjust longitude for latitude
lat_offset = 5000 / 111000  # ~0.04505 degrees latitude
lon_offset = 5000 / (111000 * np.cos(np.deg2rad(center_lat)))  # ~0.0732 degrees longitude

lat_min = center_lat - lat_offset
lat_max = center_lat + lat_offset
lon_min = center_lon - lon_offset
lon_max = center_lon + lon_offset

df_filtered = df[(df['latitude'] >= lat_min) & (df['latitude'] <= lat_max) &
                 (df['longitude'] >= lon_min) & (df['longitude'] <= lon_max)].copy()

# -----------------------------------------------------------
# 3. Additional filtering: wind_dir between 10 and 80, and altitude < 400
# -----------------------------------------------------------
df_filtered = df_filtered[(df_filtered['wind_dir'] >= 80) &
                          (df_filtered['wind_dir'] <= 100) &
                          (df_filtered['altitude'] < 400)]

# -----------------------------------------------------------
# 4. Create Configurable Grid Cells (grid_size_m x grid_size_m) in degrees
# -----------------------------------------------------------
cell_size_lat = grid_size_m / 111000  # degrees latitude
cell_size_lon = grid_size_m / (111000 * np.cos(np.deg2rad(center_lat)))  # degrees longitude

# Assign each point a grid index based on its position
df_filtered['grid_x'] = ((df_filtered['longitude'] - lon_min) // cell_size_lon).astype(int)
df_filtered['grid_y'] = ((df_filtered['latitude'] - lat_min) // cell_size_lat).astype(int)

# -----------------------------------------------------------
# 5. Calculate Statistics for Each Grid Cell
# -----------------------------------------------------------
grouped = df_filtered.groupby(['grid_x', 'grid_y'])
cell_stats = grouped.apply(lambda group: pd.Series({
    'total': len(group),
    'above_threshold': (group['vertical_speed'] > -0.5).sum(),
    'percentage': (group['vertical_speed'] > -0.5).sum() / len(group) * 100
})).reset_index()

# Only consider cells with a significant amount of data (>30 points)
cell_stats = cell_stats[cell_stats['total'] > 30]

# -----------------------------------------------------------
# 6. Draw All Grid Cells on a Folium Map with Colors Indicating the Percentage
# -----------------------------------------------------------
# Create a folium map centered on EHHV
m = folium.Map(location=[center_lat, center_lon], zoom_start=14)

# Create a linear colormap for percentage values:
# green = low percentage, red = high percentage.
colormap = cm.LinearColormap(colors=['green', 'yellow', 'red'], vmin=0, vmax=100)
colormap.caption = 'Percentage of points with vertical_speed > -0.5'
colormap.add_to(m)

# Draw every grid cell that has more than 30 data points
for idx, row in cell_stats.iterrows():
    grid_x = row['grid_x']
    grid_y = row['grid_y']

    # Determine cell boundaries in degrees
    cell_lon_min = lon_min + grid_x * cell_size_lon
    cell_lon_max = cell_lon_min + cell_size_lon
    cell_lat_min = lat_min + grid_y * cell_size_lat
    cell_lat_max = cell_lat_min + cell_size_lat

    # Get the color based on the percentage of points meeting the vertical speed condition
    color = colormap(row['percentage'])

    # Add a rectangle representing this grid cell to the folium map
    folium.Rectangle(
        bounds=[[cell_lat_min, cell_lon_min], [cell_lat_max, cell_lon_max]],
        color=color,
        fill=True,
        fill_opacity=0.7,
        popup=(f"Percentage: {row['percentage']:.2f}%<br>Total points: {row['total']}")
    ).add_to(m)

# Save the map to an HTML file (optional) and display it
m.save("heatmap2.html")
m  # In a Jupyter Notebook, this will display the map inline.
