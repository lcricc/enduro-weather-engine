
#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - NOAA ISD collector."""

import csv
import gzip
import io
import json
import os
import urllib.error
import urllib.request

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "observations" / "noaa"
OUT.mkdir(parents=True, exist_ok=True)

CURRENT_YEAR = datetime.now(timezone.utc).year
MAX_STATIONS = max(0, int(os.getenv("NOAA_MAX_STATIONS", "200")))
WORKERS = max(1, min(4, int(os.getenv("NOAA_WORKERS", "4"))))

INVENTORY_URL = (
    "https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv"
)

DATA_URL = (
    "https://www.ncei.noaa.gov/pub/data/noaa/"
    "{year}/{usaf}-{wban}-{year}.gz"
)

STATION_COLUMNS = [
    "network", "station_id", "station_name",
    "latitude", "longitude", "elevation_m",
]

OBS_COLUMNS = [
    "network", "station_id", "observed_utc",
    "temperature_2m_c", "wind_speed_10m_kmh",
]


def fetch(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "EnduroWeatherEngine/1.2"},
    )
    with urllib.request.urlopen(
        request, timeout=75
    ) as response:
        return response.read()


def read_inventory():
    raw = fetch(INVENTORY_URL)
    reader = csv.DictReader(
        io.StringIO(raw.decode("utf-8-sig"))
    )

    stations = []

    for row in reader:
        if (row.get("CTRY") or "").strip() != "IT":
            continue

        try:
            lat = float(row["LAT"])
            lon = float(row["LON"])
        except (ValueError, KeyError, TypeError):
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

        stations.append({
            "network": "noaa_isd",
            "station_id": usaf + wban,
            "station_name": (
                row.get("STATION NAME") or ""
            ).strip(),
            "latitude": lat,
            "longitude": lon,
            "elevation_m": row.get("ELEVATION") or "",
            "begin": row.get("BEGIN") or "",
            "end": row.get("END") or "",
        })

    stations.sort(key=lambda s: s["station_id"])
    return stations


def parse_isd(data, station, year):
    content = gzip.decompress(data)
    observations = {}

    lines = content.decode(
        "latin-1", errors="replace"
    ).splitlines()

    for line in lines:
        if len(line) < 93:
            continue

        try:
            observed = datetime.strptime(
                line[15:27], "%Y%m%d%H%M"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        if observed.year != year:
            continue

        temp = None
        wind = None

        try:
            code = line[87:92]
            if code not in ("+9999", "-9999"):
                value = int(code) / 10
                if -90 <= value <= 60:
                    temp = value
        except ValueError:
            pass

        try:
            code = line[65:69]
            if code != "9999":
                value = int(code) / 10
                if 0 <= value <= 100:
                    wind = round(value * 3.6, 2)
        except ValueError:
            pass

        if temp is None and wind is None:
            continue

        key = observed.isoformat()
        previous = observations.get(key)

        if previous is None:
            observations[key] = {
                "network": "noaa_isd",
                "station_id": station["station_id"],
                "observed_utc": key,
                "temperature_2m_c": temp,
                "wind_speed_10m_kmh": wind,
            }
        else:
            if previous["temperature_2m_c"] is None:
                previous["temperature_2m_c"] = temp
            if previous["wind_speed_10m_kmh"] is None:
                previous["wind_speed_10m_kmh"] = wind

    return list(observations.values()), len(lines)


def collect_station(station, year):
    sid = station["station_id"]

    url = DATA_URL.format(
        year=year,
        usaf=sid[:6],
        wban=sid[6:],
    )

    try:
        data = fetch(url)
        rows, line_count = parse_isd(
            data, station, year
        )

        return {
            "station": station,
            "rows": rows,
            "lines": line_count,
            "error": None,
        }

    except urllib.error.HTTPError as exc:
        error = f"HTTP {exc.code}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    return {
        "station": station,
        "rows": [],
        "lines": 0,
        "error": error,
    }


def write_csv(path, columns, rows):
    with path.open(
        "w", newline="", encoding="utf-8"
    ) as file:
        writer = csv.DictWriter(
            file, fieldnames=columns,
            extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)


def run_year(stations, year):
    print(f"\nChecking NOAA year {year}", flush=True)

    year_start = year * 10000 + 101
    year_end = year * 10000 + 1231

    eligible = []

    for station in stations:
        try:
            begin = int(station["begin"])
            end = int(station["end"])
        except ValueError:
            continue

        if begin <= year_end and (
            end >= year_start or year == CURRENT_YEAR
        ):
            eligible.append(station)

    if MAX_STATIONS:
        eligible = eligible[:MAX_STATIONS]

    print(
        f"Eligible stations: {len(eligible)}",
        flush=True
    )

    successful = []
    observations = []
    failures = []
    downloaded_files = 0

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:
        futures = [
            executor.submit(
                collect_station, station, year
            )
            for station in eligible
        ]

        for index, future in enumerate(
            as_completed(futures), 1
        ):
            result = future.result()
            station = result["station"]
            rows = result["rows"]

            if result["error"] is None:
                downloaded_files += 1

            if rows:
                successful.append(station)
                observations.extend(rows)
            else:
                failures.append({
                    "station_id": station["station_id"],
                    "station_name": station["station_name"],
                    "file_lines": result["lines"],
                    "error": result["error"] or "no valid rows",
                })

            if index % 25 == 0:
                print(
                    f"{year}: {index}/{len(eligible)} "
                    f"checked; {len(successful)} stations; "
                    f"{len(observations)} observations",
                    flush=True
                )

    successful.sort(key=lambda x: x["station_id"])
    observations.sort(
        key=lambda x: (
            x["station_id"], x["observed_utc"]
        )
    )

    write_csv(
        OUT / f"stations_{year}.csv",
        STATION_COLUMNS, successful
    )

    write_csv(
        OUT / f"observations_{year}.csv",
        OBS_COLUMNS, observations
    )

    report = {
        "year": year,
        "eligible_stations": len(eligible),
        "downloaded_files": downloaded_files,
        "stations_with_observations": len(successful),
        "observations": len(observations),
        "failures": failures,
    }

    (OUT / f"report_{year}.json").write_text(
        json.dumps(
            report, indent=2, ensure_ascii=False
        ),
        encoding="utf-8"
    )

    print(
        json.dumps(
            {k: v for k, v in report.items()
             if k != "failures"},
            indent=2
        ),
        flush=True
    )

    if failures:
        print(
            "FIRST 10 FAILURES:",
            json.dumps(
                failures[:10],
                ensure_ascii=False,
                indent=2
            ),
            flush=True
        )

    return len(successful)


def main():
    stations = read_inventory()

    print(
        f"Italian stations in inventory: "
        f"{len(stations)}",
        flush=True
    )

    if not stations:
        raise RuntimeError(
            "No Italian stations found in NOAA inventory"
        )

    count = run_year(stations, CURRENT_YEAR)

    if count == 0:
        print(
            "No observations in current year. "
            "Trying previous year...",
            flush=True
        )
        count = run_year(stations, CURRENT_YEAR - 1)

    if count == 0:
        print(
            "WARNING: No observations acquired. "
            "Inspect the failure details above.",
            flush=True
        )
    else:
        print(
            f"SUCCESS: {count} NOAA stations acquired.",
            flush=True
        )


if __name__ == "__main__":
    main()
