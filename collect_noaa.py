
#!/usr/bin/env python3
"""Collect Italian NOAA ISD observations in an isolated archive.

Inventory: https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv
Hourly fixed-width gzip: https://www.ncei.noaa.gov/pub/data/noaa/YEAR/USAF-WBAN-YEAR.gz
"""
import csv
import io
import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import gzip

BASE = Path(__file__).resolve().parent / "data" / "observations" / "noaa"
BASE.mkdir(parents=True, exist_ok=True)

YEAR = int(os.getenv("NOAA_YEAR", str(datetime.now(timezone.utc).year)))
MAX_STATIONS = max(1, int(os.getenv("NOAA_MAX_STATIONS", "200")))
WORKERS = min(6, max(1, int(os.getenv("NOAA_WORKERS", "4"))))
MIN_RECORDS = max(1, int(os.getenv("NOAA_MIN_RECORDS", "12")))

HISTORY = "https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv"
HOURLY = "https://www.ncei.noaa.gov/pub/data/noaa/{year}/{usaf}-{wban}-{year}.gz"


def download(url, timeout=60):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "EnduroWeatherEngine/1.0 (research data collector)"
        }
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def parse_history():
    raw = download(HISTORY, 45).decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(raw)))
    result = []

    for r in rows:
        if (r.get("CTRY") or "").strip() != "IT":
            continue

        try:
            lat = float(r["LAT"])
            lon = float(r["LON"])
            begin = int(r.get("BEGIN") or 0)
            end = int(r.get("END") or 0)
        except (ValueError, TypeError):
            continue

        if not (35 <= lat <= 48 and 6 <= lon <= 19):
            continue

        if end < YEAR * 10000 + 101:
            continue

        if begin > YEAR * 10000 + 1231:
            continue

        usaf = (r.get("USAF") or "").strip()
        wban = (r.get("WBAN") or "").strip()

        if len(usaf) != 6 or len(wban) != 5:
            continue

        if not (usaf + wban).isdigit():
            continue

        result.append({
            "network": "noaa_isd",
            "station_id": usaf + wban,
            "station_name": r.get("STATION NAME", "").strip(),
            "latitude": lat,
            "longitude": lon,
            "elevation_m": r.get("ELEVATION", ""),
            "begin": begin,
            "end": end
        })

    return sorted(
        result,
        key=lambda r: (-r["end"], r["station_id"])
    )[:MAX_STATIONS]


def parse_isd(raw, station):
    result = []

    for line in gzip.decompress(raw).decode(
        "latin-1", errors="replace"
    ).splitlines():

        if len(line) < 93:
            continue

        stamp = line[15:27]

        try:
            dt = datetime.strptime(
                stamp, "%Y%m%d%H%M"
            ).replace(tzinfo=timezone.utc)

            if dt.year != YEAR:
                continue

            temp_code = line[87:92].strip()

            if temp_code in ("+9999", "-9999", "9999", ""):
                continue

            temp = int(temp_code) / 10

            if not (-90 <= temp <= 60):
                continue

            result.append({
                "network": "noaa_isd",
                "station_id": station["station_id"],
                "observed_utc": dt.isoformat(),
                "temperature_2m_c": temp
            })

        except (ValueError, OverflowError):
            continue

    unique = {
        row["observed_utc"]: row
        for row in result
    }

    return list(unique.values())


def collect(station):
    sid = station["station_id"]

    url = HOURLY.format(
        year=YEAR,
        usaf=sid[:6],
        wban=sid[6:]
    )

    for attempt in range(3):
        try:
            rows = parse_isd(
                download(url, 90),
                station
            )
            return station, rows, None

        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return station, [], f"HTTP {e.code}"
            err = f"HTTP {e.code}"

        except Exception as e:
            err = f"{type(e).__name__}: {e}"

        time.sleep(2 * (attempt + 1))

    return station, [], err


def main():
    stations = parse_history()

    if not stations:
        raise RuntimeError(
            "Nessuna stazione italiana trovata per l'anno selezionato"
        )

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )

    all_obs = []
    good = []
    errors = []

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        jobs = {
            pool.submit(collect, station): station
            for station in stations
        }

        for n, future in enumerate(
            as_completed(jobs), 1
        ):
            station, rows, error = future.result()

            if len(rows) >= MIN_RECORDS:
                all_obs.extend(rows)
                good.append(station)
            else:
                errors.append({
                    "station_id": station["station_id"],
                    "records": len(rows),
                    "error": error or "too few valid observations"
                })

            if n % 25 == 0:
                print(
                    f"Processed {n}/{len(stations)} stations; "
                    f"valid {len(good)}; "
                    f"observations {len(all_obs)}",
                    flush=True
                )

    def write_csv(path, fields, rows):
        tmp = path.with_suffix(".tmp")

        with tmp.open(
            "w", newline="", encoding="utf-8"
        ) as f:
            writer = csv.DictWriter(
                f,
                fieldnames=fields,
                extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(rows)

        tmp.replace(path)

    write_csv(
        BASE / f"stations_{YEAR}.csv",
        [
            "network", "station_id", "station_name",
            "latitude", "longitude", "elevation_m",
            "begin", "end"
        ],
        sorted(good, key=lambda x: x["station_id"])
    )

    write_csv(
        BASE / f"temperature_{YEAR}.csv",
        [
            "network", "station_id",
            "observed_utc", "temperature_2m_c"
        ],
        sorted(
            all_obs,
            key=lambda x: (
                x["station_id"],
                x["observed_utc"]
            )
        )
    )

    report = {
        "acquired_utc": stamp,
        "year": YEAR,
        "candidate_stations": len(stations),
        "stations_with_observations": len(good),
        "temperature_observations": len(all_obs),
        "stations_failed_or_insufficient": len(errors),
        "errors": errors[:100]
    }

    (
        BASE / f"report_{YEAR}.json"
    ).write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k != "errors"
            },
            indent=2
        )
    )

    if not good:
        raise RuntimeError(
            "Nessuna stazione acquisita: "
            "controllare URL NOAA e formato ISD nei log"
        )


if __name__ == "__main__":
    main()
