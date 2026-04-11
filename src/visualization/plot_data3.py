import sqlite3
import pandas as pd
import folium
from folium.plugins import HeatMap

# ----------------------------
# 1. Data Loading from the Database
# ----------------------------
db_path = "data/flights.db"  # Database file name

# Connect and load both tables
conn = sqlite3.connect(db_path)
df_igc = pd.read_sql_query("SELECT * FROM igc_data", conn)
df_flights = pd.read_sql_query("SELECT * FROM flights", conn)
conn.close()

# ----------------------------
# 2. Filter Flights by Start Method
# ----------------------------
# Use "id" as flight identifier from the flights table
valid_flight_ids = df_flights[df_flights['start_methode'] == 'lier']['id']
df_igc = df_igc[df_igc['flight_id'].isin(valid_flight_ids)]

# ----------------------------
# 3. Data Conversion and Time Window Filtering
# ----------------------------
# Convert 'time' to timedelta (assuming time is in HH:MM:SS format)
df_igc['time'] = pd.to_timedelta(df_igc['time'])

# Filter the data points to only include those between 09:00 and 14:00
df_igc = df_igc[(df_igc['time'] > pd.Timedelta("08:00:00")) &
                (df_igc['time'] < pd.Timedelta("20:00:00"))]

# Convert additional columns to numeric
df_igc['vertical_speed'] = pd.to_numeric(df_igc['vertical_speed'], errors='coerce')
df_igc['wind_dir'] = pd.to_numeric(df_igc['wind_dir'], errors='coerce')
df_igc['altitude'] = pd.to_numeric(df_igc['altitude'], errors='coerce')

# ----------------------------
# 4. Flight Duration Filtering (>8 min)
# ----------------------------
# Compute flight duration per flight_id using the min and max times
flight_durations = df_igc.groupby('flight_id')['time'].agg(['min', 'max'])
flight_durations['duration'] = flight_durations['max'] - flight_durations['min']

# Select flight_ids with duration greater than 8 minutes
valid_flights = flight_durations[flight_durations['duration'] > pd.Timedelta(minutes=8)].index
df_igc = df_igc[df_igc['flight_id'].isin(valid_flights)]

# ----------------------------
# 5. Keep Only the First 12 Minutes of Each Flight
# ----------------------------
# Compute the start time for each flight and calculate the relative time
df_igc['start_time'] = df_igc.groupby('flight_id')['time'].transform('min')
df_igc['relative_time'] = df_igc['time'] - df_igc['start_time']

# Keep only data points within the first 12 minutes of the flight
#df_igc = df_igc[df_igc['relative_time'] <= pd.Timedelta(minutes=12)]

# ----------------------------
# 6. Additional Filtering: Wind Direction and Altitude
# ----------------------------
df_igc = df_igc[((df_igc['wind_dir'] >= 80) &
                ((df_igc['wind_dir'] <= 100)) &
                (df_igc['altitude'] < 400)) &
                (df_igc['vertical_speed'] >= 0)]
print("Number of unique flight_id in final df_igc:", df_igc['flight_id'].nunique())
# ----------------------------
# 7. Create a Heatmap Using the Filtered Data Points
# ----------------------------
# Center the map on EHHV (approximate coordinates)
center_lat = 52.1932
center_lon = 5.1528
m = folium.Map(location=[center_lat, center_lon], zoom_start=14)

# Prepare heatmap data (list of [latitude, longitude] pairs)
heat_data = df_igc[['latitude', 'longitude']].values.tolist()

# Add a heatmap layer to the map
HeatMap(heat_data, radius=6, blur=7, min_opacity=0.5).add_to(m)

# Save the heatmap to an HTML file and display the map
m.save("heatmap3.html")

