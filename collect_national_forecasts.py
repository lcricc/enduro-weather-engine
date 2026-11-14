
#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - National multimodel forecast archive."""

import csv
import gzip
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUTPUT = DATA / "forecasts" / "national"
OUTPUT.mkdir(parents=True, exist_ok=True)

MODELS = [
    "ecmwf_ifs025",
    "ecmwf_aifs025",
    "icon_seamless",
    "gfs_seamless",
    "meteofrance_seamless",
    "ukmo_seamless",
    "gem_seamless",
    "arpege_world",
]

VARIABLES = [
    "temperature_2m",
    "precipitation",
    "wind_speed_10m",
]

COLUMNS = [
    "acquired_utc",
    "network",
    "station_id",
    "latitude",
    "longitude",
    "model",
    "valid_time_utc",
    "lead_hours",
    "temperature_2m_c",
    "precipitation_mm",
    "wind_speed_10m_kmh",
]

BATCH_SIZE = 20
MAX_RETRIES = 5
NOW = datetime.now(timezone.utc)
STAMP = NOW.strftime("%Y%m%dT%H%M%SZ")


def load_stations():
    """
    Use only NOAA GHCNh stations with observations.
    This guarantees consistent station identifiers
    between new forecasts and validation data.
    """
    noaa = DATA / "observations" / "noaa"
    sources = sorted(
        noaa.glob("stations_*.csv"),
        reverse=True
    )

    stations = {}

    for source in sources:
        with source.open(
            encoding="utf-8-sig",
            newline=""
        ) as f:
            for row in csv.DictReader(f):
                network = (
                    row.get("network") or ""
                ).strip()

                station_id = (
                    row.get("station_id") or ""
                ).strip()

                if network != "noaa_ghcnh":
                    continue

                if not station_id:
                    continue

                try:
                    lat = float(row["latitude"])
                    lon = float(row["longitude"])
                except (ValueError, KeyError, TypeError):
                    continue

                if not (
                    35 <= lat <= 48
                    and 6 <= lon <= 19
                ):
                    continue

                key = (network, station_id)

                stations[key] = {
                    "network": network,
                    "station_id": station_id,
                    "latitude": lat,
                    "longitude": lon,
                }

    if not stations:
        raise RuntimeError(
            "No NOAA GHCNh stations available. "
            "Run collect_noaa.py first."
        )

    return sorted(
        stations.values(),
        key=lambda s: s["station_id"]
    )


def request_json(params):
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        + urllib.parse.urlencode(params)
    )

    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "EnduroWeatherEngine/2.0"
                }
            )

            with urllib.request.urlopen(
                req,
                timeout=90
            ) as response:
                return json.load(response)

        except urllib.error.HTTPError as exc:
            if exc.code not in (
                429, 500, 502, 503, 504
            ):
                raise

            if attempt == MAX_RETRIES - 1:
                raise

            retry_after = exc.headers.get(
                "Retry-After"
            )

            try:
                delay = min(
                    120,
                    float(retry_after)
                )
            except (TypeError, ValueError):
                delay = min(
                    120,
                    5 * (2 ** attempt)
                )

            time.sleep(
                max(1, delay) + random.random()
            )

        except (
            urllib.error.URLError,
            TimeoutError
        ):
            if attempt == MAX_RETRIES - 1:
                raise

            time.sleep(
                min(60, 3 * (2 ** attempt))
            )

    raise RuntimeError(
        "Request retries exhausted"
    )


def chunks(items, size):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def parse_time(value):
    dt = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )

    if dt.tzinfo is None:
        dt = dt.replace(
            tzinfo=timezone.utc
        )

    return dt.astimezone(timezone.utc)


def collect_batch(batch, model, writer):
    params = {
        "latitude": ",".join(
            str(s["latitude"])
            for s in batch
        ),
        "longitude": ",".join(
            str(s["longitude"])
            for s in batch
        ),
        "hourly": ",".join(VARIABLES),
        "models": model,
        "forecast_days": 4,
        "timezone": "GMT",
    }

    result = request_json(params)

    responses = (
        result
        if isinstance(result, list)
        else [result]
    )

    if len(responses) != len(batch):
        raise RuntimeError(
            f"Expected {len(batch)} locations, "
            f"got {len(responses)}"
        )

    count = 0

    for station, payload in zip(
        batch,
        responses
    ):
        hourly = payload.get("hourly", {})
        times = hourly.get("time", [])

        for i, raw_time in enumerate(times):
            valid = parse_time(raw_time)

            if valid <= NOW:
                continue

            lead = round(
                (
                    valid - NOW
                ).total_seconds() / 3600,
                3
            )

            def field(name):
                values = hourly.get(name, [])
                return (
                    values[i]
                    if i < len(values)
                    else None
                )

            writer.writerow({
                "acquired_utc": NOW.isoformat(),
                "network": station["network"],
                "station_id": station["station_id"],
                "latitude": station["latitude"],
                "longitude": station["longitude"],
                "model": model,
                "valid_time_utc": valid.isoformat(),
                "lead_hours": lead,
                "temperature_2m_c": field(
                    "temperature_2m"
                ),
                "precipitation_mm": field(
                    "precipitation"
                ),
                "wind_speed_10m_kmh": field(
                    "wind_speed_10m"
                ),
            })

            count += 1

    return count


def main():
    stations = load_stations()

    # Keep every station identifier.
    # Do not discard stations sharing coordinates.
    locations = stations

    print(
        "NOAA GHCNh stations:",
        len(stations)
    )
    print("Models:", len(MODELS))

    target = (
        OUTPUT /
        f"forecasts_{STAMP}.csv.gz"
    )

    report_path = (
        OUTPUT /
        f"report_{STAMP}.json"
    )

    errors = []
    total_rows = 0
    successful_batches = 0

    with gzip.open(
        target,
        "wt",
        encoding="utf-8",
        newline=""
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=COLUMNS
        )

        writer.writeheader()

        for model in MODELS:
            model_rows = 0

            for batch_index, batch in enumerate(
                chunks(locations, BATCH_SIZE),
                1
            ):
                try:
                    count = collect_batch(
                        batch,
                        model,
                        writer
                    )

                    model_rows += count
                    total_rows += count
                    successful_batches += 1

                except Exception as exc:
                    errors.append({
                        "model": model,
                        "batch": batch_index,
                        "station_ids": [
                            s["station_id"]
                            for s in batch
                        ],
                        "error_type": (
                            type(exc).__name__
                        ),
                        "error": str(exc)[:250],
                    })

                time.sleep(1)

            print(
                f"{model}: {model_rows} rows",
                flush=True
            )

    report = {
        "acquired_utc": NOW.isoformat(),
        "station_identifiers": len(stations),
        "unique_locations": len({
            (
                round(s["latitude"], 5),
                round(s["longitude"], 5)
            )
            for s in stations
        }),
        "models_requested": MODELS,
        "successful_batches": successful_batches,
        "forecast_rows": total_rows,
        "failed_batches": len(errors),
        "errors": errors,
        "archive": str(
            target.relative_to(ROOT)
        ),
    }

    report_path.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8",
    )

    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2
        )
    )

    if total_rows == 0:
        raise RuntimeError(
            "No forecasts acquired"
        )

    if errors:
        raise RuntimeError(
            "Incomplete collection: "
            f"{len(errors)} batches failed"
        )


if __name__ == "__main__":
    main()
