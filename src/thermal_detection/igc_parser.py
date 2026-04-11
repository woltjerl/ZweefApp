import re
from typing import Dict, Any, Optional


class IGCParser:
    """
    A class to parse IGC file content into structured data.

    Usage:
        parser = IGCParser(century_threshold=80)
        result = parser.parse(igc_text)
        # result -> dictionary with keys like:
        #   - "headers" : {...}
        #   - "date" : "09-03-2025"
        #   - "pilot" : "GoZC"
        #   - "glider_id" : "PH-1431"
        #   - "glider_type" : "DG-1000"
        #   - "fixes" : [ { 'time': '13:01:03', 'latitude': 52.191466, 'longitude': 5.139516, 'altitude': 0 }, ... ]
    """

    def __init__(self, century_threshold: int = 80):
        """
        :param century_threshold: The pivot for interpreting 2-digit years.
               If year < century_threshold -> 2000 + year
               else -> 1900 + year
               Example: If year is '25' => 2025
        """
        self.century_threshold = century_threshold

        # Regex patterns for header lines.
        self.date_regex = re.compile(r"^HFDTE(\d{2})(\d{2})(\d{2})")
        self.pilot_regex = re.compile(r"^HFPLT.*:(.*)")
        self.glider_type_regex = re.compile(r"^HFGTY.*:(.*)")
        self.glider_id_regex = re.compile(r"^HFGID.*:(.*)")

        # Regex pattern for a fix (B-record) line.
        # Matches: BHHMMSS ddmmmmm [N/S] dddmmmmm [E/W] and the rest.
        self.fix_regex = re.compile(r"^B(\d{6})(\d{7})([NS])(\d{8})([EW])(.+)$")

    def parse(self, igc_content: str) -> Dict[str, Any]:
        """
        Parses the provided IGC text and returns a structured dictionary.
        """
        lines = igc_content.splitlines()

        result = {
            "headers": {},
            "date": None,  # e.g. "09-03-2025"
            "pilot": None,
            "glider_id": None,
            "glider_type": None,
            "fixes": []  # A list of fix dictionaries
        }

        for line in lines:
            # 1) Check for date
            match = self.date_regex.match(line)
            if match:
                result["date"] = self._parse_date(*match.groups())
                continue

            # 2) Check for pilot
            match = self.pilot_regex.match(line)
            if match:
                result["pilot"] = match.group(1).strip()
                continue

            # 3) Check for glider type
            match = self.glider_type_regex.match(line)
            if match:
                result["glider_type"] = match.group(1).strip()
                continue

            # 4) Check for glider ID
            match = self.glider_id_regex.match(line)
            if match:
                result["glider_id"] = match.group(1).strip()
                continue

            # 5) Parse fix lines (track points)
            match = self.fix_regex.match(line)
            if match:
                fix_data = self._parse_fix(match)
                if fix_data:
                    result["fixes"].append(fix_data)
                continue

            # 6) Store other 'H...' lines in headers for reference.
            if line.startswith("H"):
                prefix = line[0:5]  # e.g. "HFDTE", "HFFXA"
                result["headers"][prefix] = line

        return result

    def _parse_date(self, day: str, month: str, year: str) -> str:
        """
        Converts e.g. day='09', month='03', year='25' -> '09-03-2025'
        """
        year_val = int(year)
        if year_val < self.century_threshold:
            year_val += 2000
        else:
            year_val += 1900
        return f"{day}-{month}-{year_val}"

    def _parse_fix(self, match: re.Match) -> Optional[Dict[str, Any]]:
        """
        Given a match from self.fix_regex, parse out the time, latitude, longitude, and altitude.
        """
        time_utc = match.group(1)  # "HHMMSS"
        lat_str = match.group(2)  # "ddmmmmm", e.g. "5211488" => 52 deg, 11.488 min
        lat_ns = match.group(3)  # "N" or "S"
        lon_str = match.group(4)  # "dddmmmmm", e.g. "00508371" => 5 deg, 08.371 min
        lon_ew = match.group(5)  # "E" or "W"
        remainder = match.group(6)  # Contains altitude info (and possibly other data)

        # Convert latitude
        lat_deg = int(lat_str[0:2])
        lat_min_raw = float(lat_str[2:])  # e.g. "11488" -> interpreted as minutes fraction (divide by 1000 then 60)
        lat_in_degs = lat_deg + (lat_min_raw / 1000.0) / 60.0
        if lat_ns == "S":
            lat_in_degs *= -1.0

        # Convert longitude
        lon_deg = int(lon_str[0:3])
        lon_min_raw = float(lon_str[3:])
        lon_in_degs = lon_deg + (lon_min_raw / 1000.0) / 60.0
        if lon_ew == "W":
            lon_in_degs *= -1.0

        # Format time as HH:MM:SS
        hh = time_utc[0:2]
        mm = time_utc[2:4]
        ss = time_utc[4:6]
        time_str = f"{hh}:{mm}:{ss}"

        # Extract altitude using fixed-width slicing.
        altitude = self._parse_altitude_from_remainder(remainder)

        return {
            "time": time_str,
            "latitude": lat_in_degs,
            "longitude": lon_in_degs,
            "altitude": altitude
        }

    def _parse_altitude_from_remainder(self, remainder: str) -> int:
        """
        Extracts the GPS altitude from the remainder of the fix line.

        Assumes the remainder follows a common IGC format:
            - The first character is a validity flag (e.g., 'A').
            - The next 5 characters (positions 1-5) are the pressure altitude.
            - The following 5 characters (positions 6-10) are the GPS altitude.

        This function extracts the GPS altitude (characters 6-10).
        """
        if len(remainder) >= 11:
            try:
                gps_altitude = int(remainder[6:11])
                return gps_altitude
            except ValueError:
                return 0
        return 0


if __name__ == "__main__":
    # For testing: change the filename to one of your IGC files.
    from igc_parser import IGCParser

    filename = "icg_files/2025-03-12_1542/53CXZAP18.igc"
    with open(filename, "r", encoding="utf-8") as f:
        igc_text = f.read()

    parser = IGCParser(century_threshold=80)
    result = parser.parse(igc_text)

    print("Date:", result["date"])
    print("Pilot:", result["pilot"])
    print("Glider:", result["glider_id"], "-", result["glider_type"])
    print("Number of fixes:", len(result["fixes"]))
    for fix in result["fixes"]:
        print(fix)
