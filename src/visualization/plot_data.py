import sqlite3
import math
import pandas as pd
import folium
from folium.plugins import HeatMap
import datetime

# Connect to the SQLite database.
conn = sqlite3.connect("data/flights.db")

# Define center coordinates for EHHV and the 5 km radius.
center_lat = 52.1932
center_lon = 5.1528
radius_km = 5

# Use an approximate bounding box to reduce the number of records from the DB.
lat_delta = radius_km / 111  # approx. km per degree latitude
lon_delta = radius_km / (111 * math.cos(math.radians(center_lat)))  # approx. for longitude

min_lat = center_lat - lat_delta
max_lat = center_lat + lat_delta
min_lon = center_lon - lon_delta
max_lon = center_lon + lon_delta

# Query for fixes with altitude below 500m, positive vertical speed, within the bounding box,
# and with wind direction and wind speed filters.
query = """
SELECT latitude, longitude, altitude, vertical_speed, wind_dir, wind_spd, time
FROM igc_data
WHERE altitude < 400
  AND vertical_speed > 0
  AND latitude BETWEEN ? AND ?
  AND longitude BETWEEN ? AND ?
  AND wind_dir BETWEEN 290 AND 300
  AND wind_spd BETWEEN 10 AND 20
"""
df = pd.read_sql_query(query, conn, params=(min_lat, max_lat, min_lon, max_lon))

# Function to compute Haversine distance (in km) between two points.
def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371  # Earth radius in kilometers.
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

# Precisely filter points to only those within a 5km radius of the center.
def within_radius(row):
    dist = haversine_distance(center_lat, center_lon, row['latitude'], row['longitude'])
    return dist <= radius_km

df['within'] = df.apply(within_radius, axis=1)
df_filtered = df[df['within']].copy()

# Filter for morning data: Only include fixes that occur before 13:00 CET.
# Since our times are in UTC and CET is UTC+1, we add one hour.
def is_morning(time_str):
    # Parse the time string assuming "HH:MM:SS" format.
    t = datetime.datetime.strptime(time_str, "%H:%M:%S")
    # Convert to CET (UTC + 1 hour)
    cet_hour = (t.hour + 1) % 24
    return cet_hour < 22

df_filtered = df_filtered[df_filtered['time'].apply(is_morning)]

# Prepare a list of [latitude, longitude] pairs for the heatmap.
heat_data = df_filtered[['latitude', 'longitude']].values.tolist()

# Create a folium map centered at the specified coordinates.
m = folium.Map(location=[center_lat, center_lon], zoom_start=13)

# Add a heatmap layer.
HeatMap(heat_data, radius=5, blur=3, min_opacity=0.4).add_to(m)

# Optionally, add a marker for the center.
folium.Marker([center_lat, center_lon], tooltip="EHHV Center").add_to(m)

# Save the map to an HTML file.
m.save("heatmap.html")
print("Heatmap saved to heatmap.html")

conn.close()
