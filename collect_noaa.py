
#!/usr/bin/env python3
"""
ENDURO WEATHER ENGINE
NOAA ISD - Italian weather observations

Independent from MeteoHub and existing forecast collectors.
Python standard library only.
"""

import csv
import gzip
import io
import json
import os
import time
import urllib.error
import urllib.request

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "observations" / "noaa"
OUT.mkdir(parents=True, exist_ok=True)

NOW = datetime.now(timezone.utc)
YEAR = int(os.getenv("NOAA_YEAR", str(NOW.year)))

# Zero means all matching candidate stations.
MAX_STATIONS = max(0, int(os.getenv("NOAA_MAX_STATIONS", "0")))
WORKERS = max(1, min(5, int(os.getenv("NOAA_WORKERS", "4"))))
DAYS = max(1, int(os.getenv("NOAA_DAYS", "14")))

HISTORY_URL = (
    "https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv"
)

DATA_URL = (
    "https://www.ncei.noaa.gov/pub/data/noaa/"
    "{year}/{usaf}-{wban}-{year}.gz"
)

STATION_FIELDS = [
    "network",
    "station_id",
    "station_name",
    "latitude",
    "longitude",
    "elevation_m",
    "begin",
    "end",
]

OBS_FIELDS = [
    "network",
    "station_id",
    "observed_utc",
    "temperature_2m_c",
    "wind_speed_10m_kmh",
]


def fetch(url, timeout=90):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "EnduroWeatherEngine/1.1",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(
        request, timeout=timeout
    ) as response:
        return response.read()


def as_int(value, default=0):
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def inventory():
    print("Downloading NOAA station inventory...", flush=True)

    raw = fetch(HISTORY_URL, timeout=60)
    reader = csv.DictReader(
        io.StringIO(raw.decode("utf-8-sig"))
    )

    all_rows = list(reader)
    countries = {}
    italian = []

    for row in all_rows:
        country = (row.get("CTRY") or "").strip().upper()
        countries[country] = countries.get(country, 0) + 1

        if country != "IT":
            continue

        try:
            lat = float(row["LAT"])
            lon = float(row["LON"])
        except (ValueError, TypeError, KeyError):
            continue

        if not (35 <= lat <= 48 and 6 <= lon <= 19):
            continue

        usaf = (row.get("USAF") or "").strip()
        wban = (row.get("WBAN") or "").strip()

        if (
            len(usaf) != 6
            or len(wban) != 5
            or not (usaf + wban).isdigit()
        ):
            continue

        italian.append({
            "network": "noaa_isd",
            "station_id": usaf + wban,
            "station_name": (
                row.get("STATION NAME") or ""
            ).strip(),
            "latitude": lat,
            "longitude": lon,
            "elevation_m": (
                row.get("ELEVATION") or ""
            ).strip(),
            "begin": as_int(row.get("BEGIN")),
            "end": as_int(row.get("END")),
        })

    year_start = YEAR * 10000 + 101
    year_end = YEAR * 10000 + 1231

    # The inventory may lag behind the actual hourly files.
    # Accept stations with an end date within the previous year.
    earliest_end = (YEAR - 1) * 10000 + 101

    candidates = [
        s for s in italian
        if s["begin"] <= year_end
        and (
            s["end"] == 0
            or s["end"] >= earliest_end
        )
    ]

    candidates.sort(
        key=lambda s: (
            -s["end"],
            s["station_id"],
        )
    )

    if MAX_STATIONS:
        candidates = candidates[:MAX_STATIONS]

    print(
        f"Inventory entries: {len(all_rows)}",
        flush=True
    )
    print(
        f"Italian stations with coordinates: {len(italian)}",
        flush=True
    )
    print(
        f"Candidate stations: {len(candidates)}",
        flush=True
    )

    if not candidates:
        print(
            "Inventory country codes:",
            sorted(
                countries.items(),
                key=lambda x: -x[1]
            )[:15],
            flush=True,
        )

    return italian, candidates


def parse_file(data, station):
    try:
        content = gzip.decompress(data)
    except (OSError, EOFError) as exc:
        raise ValueError(
            f"Invalid NOAA gzip: {exc}"
        ) from exc

    threshold = max(
        datetime(YEAR, 1, 1, tzinfo=timezone.utc),
        NOW - timedelta(days=DAYS),
    )

    observations = {}

    for line in content.decode(
        "latin-1", errors="replace"
    ).splitlines():

        if len(line) < 93:
            continue

        try:
            observed = datetime.strptime(
                line[15:27],
                "%Y%m%d%H%M"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        if observed.year != YEAR:
            continue

        if observed < threshold or observed > NOW:
            continue

        # NOAA ISD air temperature: signed tenths C.
        temp = None

        try:
            raw_temp = line[87:92].strip()
            if raw_temp not in (
                "+9999", "-9999", "9999", ""
            ):
                value = int(raw_temp) / 10
                if -90 <= value <= 60:
                    temp = value
        except ValueError:
            pass

        # NOAA ISD wind speed: tenths of m/s.
        # Convert to km/h.
        wind = None

        try:
            raw_wind = line[65:69].strip()
            if raw_wind not in ("9999", ""):
                value = int(raw_wind) / 10
                if 0 <= value <= 100:
                    wind = round(value * 3.6, 2)
        except ValueError:
            pass

        if temp is None and wind is None:
            continue

        key = observed.isoformat()

        existing = observations.get(key)

        new_row = {
            "network": "noaa_isd",
            "station_id": station["station_id"],
            "observed_utc": key,
            "temperature_2m_c": temp,
            "wind_speed_10m_kmh": wind,
        }

        if existing is None:
            observations[key] = new_row
        else:
            for field in (
                "temperature_2m_c",
                "wind_speed_10m_kmh",
            ):
                if (
                    existing[field] is None
                    and new_row[field] is not None
                ):
                    existing[field] = new_row[field]

    return list(observations.values())


def collect_station(station):
    sid = station["station_id"]

    url = DATA_URL.format(
        year=YEAR,
        usaf=sid[:6],
        wban=sid[6:],
    )

    last_error = None

    for attempt in range(2):
        try:
            data = fetch(url)
            rows = parse_file(data, station)

            return station, rows, None

        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"

            if exc.code in (403, 404):
                break

        except Exception as exc:
            last_error = (
                f"{type(exc).__name__}: {exc}"
            )

        time.sleep(2 * (attempt + 1))

    return station, [], last_error


def save_csv(path, fields, rows):
    temporary = path.with_suffix(".tmp")

    with temporary.open(
        "w", newline="", encoding="utf-8"
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fields,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)

    temporary.replace(path)


def main():
    italian, candidates = inventory()

    stamp = NOW.strftime("%Y%m%dT%H%M%SZ")

    if not italian:
        raise RuntimeError(
            "NOAA inventory returned no Italian stations. "
            "Check the country code and inventory format."
        )

    if not candidates:
        raise RuntimeError(
            "No eligible stations. Check inventory dates "
            "and the requested year."
        )

    all_observations = []
    successful_stations = []
    errors = []

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:

        futures = [
            executor.submit(
                collect_station, station
            )
            for station in candidates
        ]

        for index, future in enumerate(
            as_completed(futures), 1
        ):
            station, rows, error = future.result()

            if rows:
                successful_stations.append(station)
                all_observations.extend(rows)
            else:
                errors.append({
                    "station_id": station["station_id"],
                    "station_name": station["station_name"],
                    "reason": error or "no recent valid data",
                })

            if index % 25 == 0:
                print(
                    f"Checked {index}/{len(candidates)} | "
                    f"stations: {len(successful_stations)} | "
                    f"observations: {len(all_observations)}",
                    flush=True,
                )

    successful_stations.sort(
        key=lambda x: x["station_id"]
    )

    all_observations.sort(
        key=lambda x: (
            x["station_id"],
            x["observed_utc"],
        )
    )

    save_csv(
        OUT / f"stations_{YEAR}.csv",
        STATION_FIELDS,
        successful_stations,
    )

    save_csv(
        OUT / f"observations_{YEAR}.csv",
        OBS_FIELDS,
        all_observations,
    )

    report = {
        "acquired_utc": NOW.isoformat(),
        "year": YEAR,
        "recent_days": DAYS,
        "italian_inventory_stations": len(italian),
        "candidate_stations": len(candidates),
        "stations_with_observations": len(
            successful_stations
        ),
        "observations": len(all_observations),
        "stations_without_recent_data": len(errors),
        "errors_sample": errors[:100],
    }

    (
        OUT / f"report_{YEAR}.json"
    ).write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print("\nNOAA COLLECTION REPORT", flush=True)
    print(
        json.dumps(
            {
                k: v for k, v in report.items()
                if k != "errors_sample"
            },
            indent=2,
        ),
        flush=True,
    )

    if not successful_stations:
        raise RuntimeError(
            "No recent NOAA observations collected. "
            "See report and workflow logs."
        )


if __name__ == "__main__":
    main()
