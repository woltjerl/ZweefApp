#!/usr/bin/env python3
"""
igc2gpx_strava_fixed.py

Convert a specific IGC flight log to a Strava‑compatible GPX 1.1 track
with hard‑coded input/output paths.
"""
import re
import xml.etree.ElementTree as ET
from datetime import datetime

# Register the xsi namespace for schemaLocation
ET.register_namespace('xsi', "http://www.w3.org/2001/XMLSchema-instance")

def parse_igc(filename):
    fixes = []
    flight_date = None

    with open(filename, 'r') as f:
        for line in f:
            if line.startswith('HFDTE'):
                # Works for: "HFDTE060925" and "HFDTEDATE:060925,01" (and similar)
                m = re.search(r'HFDTE.*?(\d{2})(\d{2})(\d{2})', line)
                if m:
                    dd, mm, yy = map(int, m.groups())
                    # Century pivot (00–79 -> 2000s, 80–99 -> 1900s). Adjust if you need older flights.
                    year = 2000 + yy if yy < 80 else 1900 + yy
                    flight_date = datetime(year, mm, dd).date()
                # else: silently ignore if malformed; flight_date stays None

            elif line.startswith('B') and len(line) >= 35:
                # Time
                hh, mm, ss = map(int, (line[1:3], line[3:5], line[5:7]))

                # Latitude
                lat_deg  = int(line[7:9])
                lat_min  = int(line[9:11])
                lat_frac = int(line[11:14]) / 1000.0
                lat = lat_deg + (lat_min + lat_frac) / 60.0
                if line[14] == 'S':
                    lat = -lat

                # Longitude
                lon_deg  = int(line[15:18])
                lon_min  = int(line[18:20])
                lon_frac = int(line[20:23]) / 1000.0
                lon = lon_deg + (lon_min + lon_frac) / 60.0
                if line[23] == 'W':
                    lon = -lon

                # GPS altitude
                ele = int(line[30:35])

                # Build full timestamp (UTC)
                if flight_date:
                    timestamp = datetime(
                        flight_date.year,
                        flight_date.month,
                        flight_date.day,
                        hh, mm, ss
                    )
                else:
                    timestamp = datetime.utcnow().replace(
                        hour=hh, minute=mm, second=ss, microsecond=0
                    )

                fixes.append({
                    'time': timestamp,
                    'lat':  round(lat, 6),
                    'lon':  round(lon, 6),
                    'ele':  ele
                })

    fixes.sort(key=lambda f: f['time'])
    return fixes

def build_strava_gpx(fixes, name):
    # Create <gpx> root with Strava‑friendly namespaces
    gpx_attrs = {
        'version': "1.1",
        'creator': "IGC2GPX Strava Converter",
        'xmlns': "http://www.topografix.com/GPX/1/1",
        'xmlns:xsi': "http://www.w3.org/2001/XMLSchema-instance",
        'xsi:schemaLocation':
            "http://www.topografix.com/GPX/1/1 "
            "http://www.topografix.com/GPX/1/1/gpx.xsd"
    }
    gpx = ET.Element('gpx', gpx_attrs)

    # Metadata block with the time of the first fix
    meta = ET.SubElement(gpx, 'metadata')
    ET.SubElement(meta, 'name').text = name
    if fixes:
        start_time = fixes[0]['time'].strftime("%Y-%m-%dT%H:%M:%SZ")
        ET.SubElement(meta, 'time').text = start_time

    # Track
    trk = ET.SubElement(gpx, 'trk')
    ET.SubElement(trk, 'name').text = name
    trkseg = ET.SubElement(trk, 'trkseg')

    for fix in fixes:
        pt = ET.SubElement(trkseg, 'trkpt', {
            'lat': str(fix['lat']),
            'lon': str(fix['lon'])
        })
        ET.SubElement(pt, 'ele').text  = str(fix['ele'])
        ET.SubElement(pt, 'time').text = fix['time'].strftime("%Y-%m-%dT%H:%M:%SZ")

    # Serialize to bytes with XML declaration
    return ET.tostring(gpx, encoding='utf-8', xml_declaration=True)

if __name__ == '__main__':
    # Hard‑coded paths:
    IN_PATH  = "/Users/woltjerl/Downloads/2025-09-06-XNA-5ED620345C53.igc"
    OUT_PATH = "/Users/woltjerl/Downloads/2025-09-06-XNA-5ED620345C53_strava.gpx"

    fixes = parse_igc(IN_PATH)
    if not fixes:
        print("Error: no valid B‑records found in the IGC file.")
        exit(1)

    track_name = IN_PATH.rsplit('/', 1)[-1].rsplit('.', 1)[0]
    gpx_data = build_strava_gpx(fixes, track_name)

    with open(OUT_PATH, 'wb') as f:
        f.write(gpx_data)

    print(f"✔ Converted {len(fixes)} fixes to Strava‑compatible GPX → {OUT_PATH}")
