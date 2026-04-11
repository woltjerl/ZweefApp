import json

import requests
import os
import time

class GlidingAPI:
    """
    A class to encapsulate various endpoints and interactions with the GlidingApp API.
    """

    def __init__(self, base_url: str, version: str = "4.1.2"):
        """
        Initialize the GlidingAPI with a base URL and a default API version.
        Set up a requests Session for reusability (handles cookies, headers, etc.).
        """
        self.base_url = base_url
        self.version = version

        # Create a session and set default headers
        self.session = requests.Session()
        self.session.headers.update({
            "Version": self.version,
            "Accept": "*/*",
            "Content-Type": "application/json",
        })

        # Common headers used across all requests
        self._common_headers = {
            "Host": "admin.gliding.app",
            "sec-ch-ua-platform": "\"macOS\"",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/133.0.0.0 Safari/537.36"
            ),
            "sec-ch-ua": "\"Not(A:Brand\";v=\"99\", \"Google Chrome\";v=\"133\", \"Chromium\";v=\"133\"",
            "sec-ch-ua-mobile": "?0",
            "Origin": "https://gozc.gliding.app",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": "https://gozc.gliding.app/",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8,nl;q=0.7"
        }

    def _build_headers(self, access_token: str = None) -> dict:
        """
        Build a combined headers dictionary from the common headers.
        Optionally add Authorization if an access token is provided.
        """
        headers = dict(self._common_headers)  # make a copy
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    def _make_request(
        self,
        method: str,
        endpoint: str,
        access_token: str = None,
        payload: dict = None
    ) -> dict:
        """
        Internal helper method to reduce code duplication.
        - Supports 'GET' or 'POST'.
        - Merges default headers, optionally adds an Authorization header.
        - Returns the JSON response (raises for 4xx/5xx).

        :param method: HTTP method ('GET' or 'POST')
        :param endpoint: API endpoint (e.g., '/club/gozc/internal_api/auth/login.json')
        :param access_token: If provided, 'Authorization: Bearer <access_token>' is added
        :param payload: For POST requests, this will be sent as JSON
        """
        url = f"{self.base_url}{endpoint}"
        headers = self._build_headers(access_token)

        if method.upper() == "GET":
            response = self.session.get(url, headers=headers)
        elif method.upper() == "POST":
            response = self.session.post(url, json=payload, headers=headers)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        response.raise_for_status()  # Raise HTTPError if the request failed
        return response.json()

    def login(
        self,
        email: str,
        password: str,
        client_secret: str,
        grant_type: str = "login"
    ) -> dict:
        """
        Logs in to the GlidingApp with the given credentials.
        """
        endpoint = "/club/gozc/internal_api/auth/login.json"
        payload = {
            "grant_type": grant_type,
            "client_secret": client_secret,
            "email": email,
            "password": password
        }
        return self._make_request("POST", endpoint, payload=payload)

    def get_days(self, access_token: str) -> dict:
        """
        Retrieves days data from the GlidingApp.
        """
        endpoint = "/club/gozc/internal_api/days.json"
        return self._make_request("GET", endpoint, access_token=access_token)

    def get_flights_for_day(self, day_id: int, access_token: str) -> dict:
        """
        Retrieves all flights for a given day.
        """
        endpoint = f"/club/gozc/internal_api/flights/{day_id}/flights.json"
        return self._make_request("GET", endpoint, access_token=access_token)

    def download_igc_file(self, igc_path: str) -> bytes:
        """
        Downloads the given IGC file from S3, using the cross-site headers from the example.

        :param igc_path: The portion after 'igc/', e.g. 'm8007sm7ogx5tk80lms/538XZAP1.igc'
        :return: The raw IGC file content as bytes
        """
        igc_url = f"https://zweefapp.s3.amazonaws.com/media/igc/{igc_path}"
        # Cross-site headers from your curl request
        headers = {
            "Host": "zweefapp.s3.amazonaws.com",
            "sec-ch-ua-platform": "\"macOS\"",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/133.0.0.0 Safari/537.36"
            ),
            "sec-ch-ua": "\"Not(A:Brand\";v=\"99\", \"Google Chrome\";v=\"133\", \"Chromium\";v=\"133\"",
            "sec-ch-ua-mobile": "?0",
            "Accept": "*/*",
            "Origin": "https://gozc.gliding.app",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": "https://gozc.gliding.app/",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8,nl;q=0.7"
        }
        response = requests.get(igc_url, headers=headers)
        response.raise_for_status()
        return response.content

    def download_all_igcs_for_day(self, day_id: int, access_token: str, output_dir: str = "data/icg_files") -> None:
        """
        1) Get all flights for day_id.
        2) Determine date from flights data to name a folder (e.g., "2025-03-08").
        3) Store flights JSON in that folder.
        4) Download each flight's IGC (if present), store it in the same folder.
        5) Pause 4 seconds after each download to prevent throttling.

        :param day_id: e.g., 1471
        :param access_token: Bearer token
        """
        flights_data = self.get_flights_for_day(day_id, access_token)
        all_flights = flights_data.get("allFlights", [])

        if not all_flights:
            print(f"No flights found for day_id={day_id}")
            return

        # We'll fetch the 'datum' from the first flight. Fallback to day_id if none present.
        date_str = all_flights[0].get("datum") or f"day_{day_id}"

        # 1) Create folder named after the date
        dir_name = date_str+'_'+str(day_id)
        full_path = os.path.join(output_dir, dir_name)
        os.makedirs(full_path, exist_ok=True)

        # 2) Save the entire flights JSON into date folder
        flights_json_path = os.path.join(full_path, f"flights_{date_str}.json")
        with open(flights_json_path, "w", encoding="utf-8") as f:
            json.dump(flights_data, f, indent=2)
        print(f"Saved flight data JSON -> {flights_json_path}")

        # 3) For each flight, download the IGC file (if present)
        skipped = 0
        for idx, flight in enumerate(all_flights, start=1):
            igc_field = flight.get("igc")
            flight_id = flight.get("id")
            if not igc_field:
                continue

            # Remove 'igc/' prefix if present
            igc_path = igc_field[4:] if igc_field.startswith("igc/") else igc_field
            filename = os.path.basename(igc_path)
            local_igc_path = os.path.join(full_path, filename)

            # Skip if already downloaded
            if os.path.exists(local_igc_path) and os.path.getsize(local_igc_path) > 0:
                skipped += 1
                continue

            print(f"  Downloading Flight {idx} (id={flight_id}) -> {filename}")
            try:
                igc_content = self.download_igc_file(igc_path)
                with open(local_igc_path, "wb") as f:
                    f.write(igc_content)
            except requests.exceptions.RequestException as err:
                print(f"  Error downloading IGC for flight {idx}: {err}")

            # Delay between downloads to prevent throttling
            time.sleep(4)

        if skipped:
            print(f"  Skipped {skipped} already downloaded IGC files")