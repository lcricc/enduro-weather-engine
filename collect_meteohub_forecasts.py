
#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - Previsioni su stazioni MeteoHub."""

import csv
import gzip
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIONS = ROOT / "data/observations/meteohub/stations_meteohub.csv"
OUTPUT = ROOT / "data/forecasts/national"
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

FIELDS = [
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

BATCH_SIZE = 10
NOW = datetime.now(timezone.utc)
STAMP = NOW.strftime("%Y%m%dT%H%M%SZ")


def load_stations():
    if not STATIONS.is_file():
        raise RuntimeError("Inventario MeteoHub mancante")

    stations = {}
    with STATIONS.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            network = (row.get("network") or "").strip()
            station_id = (row.get("station_id") or "").strip()

            try:
                lat = float(row["latitude"])
                lon = float(row["longitude"])
            except (ValueError, TypeError, KeyError):
                continue

            if not (
                network and station_id
                and math.isfinite(lat)
                and math.isfinite(lon)
                and 35 <= lat <= 48
                and 6 <= lon <= 19
            ):
                continue

            stations[(network, station_id)] = {
                "network": network,
                "station_id": station_id,
                "latitude": lat,
                "longitude": lon,
            }

    if not stations:
        raise RuntimeError("Nessuna stazione MeteoHub valida")

    return list(stations.values())


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def fetch(params):
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        + urllib.parse.urlencode(params)
    )

    for attempt in range(4):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "EnduroWeatherEngine/2.0"},
            )
            with urllib.request.urlopen(req, timeout=90) as response:
                return json.load(response)

        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504):
                raise
            if attempt == 3:
                raise
            retry = exc.headers.get("Retry-After")
            try:
                delay = min(120, max(1, float(retry)))
            except (TypeError, ValueError):
                delay = 5 * (2 ** attempt)
            time.sleep(delay)

        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise
            time.sleep(5 * (2 ** attempt))


def parse_utc(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def collect_batch(batch, model, writer):
    params = {
        "latitude": ",".join(str(s["latitude"]) for s in batch),
        "longitude": ",".join(str(s["longitude"]) for s in batch),
        "hourly": ",".join(VARIABLES),
        "models": model,
        "forecast_days": 4,
        "timezone": "GMT",
    }

    payload = fetch(params)
    responses = payload if isinstance(payload, list) else [payload]

    if len(responses) != len(batch):
        raise RuntimeError("Numero località restituito non corretto")

    written = 0

    for station, response in zip(batch, responses):
        hourly = response.get("hourly", {})
        times = hourly.get("time", [])

        for i, raw_time in enumerate(times):
            valid = parse_utc(raw_time)
            if valid <= NOW:
                continue

            def field(name):
                values = hourly.get(name) or []
                return values[i] if i < len(values) else None

            writer.writerow({
                "acquired_utc": NOW.isoformat(),
                "network": station["network"],
                "station_id": station["station_id"],
                "latitude": station["latitude"],
                "longitude": station["longitude"],
                "model": model,
                "valid_time_utc": valid.isoformat(),
                "lead_hours": round(
                    (valid - NOW).total_seconds() / 3600, 3
                ),
                "temperature_2m_c": field("temperature_2m"),
                "precipitation_mm": field("precipitation"),
                "wind_speed_10m_kmh": field("wind_speed_10m"),
            })
            written += 1

    return written


def main():
    stations = load_stations()
    target = OUTPUT / f"forecasts_meteohub_{STAMP}.csv.gz"
    report_path = OUTPUT / f"report_meteohub_{STAMP}.json"

    counts = Counter()
    errors = []

    with gzip.open(
        target, "wt", encoding="utf-8", newline=""
    ) as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()

        for model in MODELS:
            for index, batch in enumerate(
                chunks(stations, BATCH_SIZE), 1
            ):
                try:
                    count = collect_batch(batch, model, writer)
                    counts[model] += count
                except Exception as exc:
                    errors.append({
                        "model": model,
                        "batch": index,
                        "error": str(exc)[:250],
                    })

                time.sleep(1)

            print(model, counts[model], flush=True)

    report = {
        "acquired_utc": NOW.isoformat(),
        "stations": len(stations),
        "models": MODELS,
        "forecast_rows": dict(counts),
        "total_rows": sum(counts.values()),
        "failed_batches": len(errors),
        "errors": errors,
        "archive": str(target.relative_to(ROOT)),
    }

    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(report, ensure_ascii=False, indent=2))

    if not sum(counts.values()) or errors:
        raise RuntimeError(
            "Raccolta MeteoHub incompleta: consultare il report"
        )


if __name__ == "__main__":
    main()
