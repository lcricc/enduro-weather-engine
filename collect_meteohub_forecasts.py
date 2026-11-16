
#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - MeteoHub Forecast Collector V2.1.

Collects hourly forecasts from eight Open-Meteo models.
Checks missing weather values for every model.
Preserves the historical CSV structure.
"""

import csv
import gzip
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent

STATIONS = (
    ROOT
    / "data/observations/meteohub/stations_meteohub.csv"
)

OUTPUT = ROOT / "data/forecasts/national"
OUTPUT.mkdir(parents=True, exist_ok=True)

MODELS = [
    "ecmwf_ifs025",
    "ecmwf_aifs025_single",
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

BATCH_SIZE = 25
MAX_WEIGHTED_CALLS = 4500
WEIGHTED_CALLS_PER_SECOND = 8

NOW = datetime.now(timezone.utc)
STAMP = NOW.strftime("%Y%m%dT%H%M%SZ")


def load_stations():
    if not STATIONS.is_file():
        raise RuntimeError(
            "Inventario MeteoHub mancante"
        )

    stations = {}

    with STATIONS.open(
        encoding="utf-8-sig",
        newline=""
    ) as file:

        for row in csv.DictReader(file):
            network = (
                row.get("network") or ""
            ).strip()

            station_id = (
                row.get("station_id") or ""
            ).strip()

            try:
                lat = float(row["latitude"])
                lon = float(row["longitude"])
            except (
                ValueError,
                TypeError,
                KeyError
            ):
                continue

            if not (
                network
                and station_id
                and math.isfinite(lat)
                and math.isfinite(lon)
                and 35 <= lat <= 48
                and 6 <= lon <= 19
            ):
                continue

            stations[
                (network, station_id)
            ] = {
                "network": network,
                "station_id": station_id,
                "latitude": lat,
                "longitude": lon,
            }

    if not stations:
        raise RuntimeError(
            "Nessuna stazione MeteoHub valida"
        )

    return list(stations.values())


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class ForecastQuotaExceeded(RuntimeError):
    """Stop the whole run when Open-Meteo or the job reaches its budget."""


class ForecastBudget:
    def __init__(self):
        self.calls = 0
        self.next_request = 0.0

    def reserve(self, locations):
        if self.calls + locations > MAX_WEIGHTED_CALLS:
            raise ForecastQuotaExceeded("Run budget for Open-Meteo exhausted")
        delay = self.next_request - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.next_request = time.monotonic() + locations / WEIGHTED_CALLS_PER_SECOND
        self.calls += locations


def fetch(params, budget):
    url = (
        "https://api.open-meteo.com/v1/forecast?"
        + urllib.parse.urlencode(params)
    )

    for attempt in range(4):
        budget.reserve(params["latitude"].count(",") + 1)
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent":
                    "EnduroWeatherEngine/2.1"
                }
            )

            with urllib.request.urlopen(
                request,
                timeout=90
            ) as response:
                return json.load(response)

        except urllib.error.HTTPError as exc:
            if exc.code not in (
                429, 500, 502, 503, 504
            ):
                raise

            if attempt == 3:
                if exc.code == 429:
                    raise ForecastQuotaExceeded("Open-Meteo quota reached") from exc
                raise

            retry = exc.headers.get(
                "Retry-After"
            )

            try:
                delay = min(
                    120,
                    max(1, float(retry))
                )
            except (TypeError, ValueError):
                delay = 5 * (2 ** attempt)

            time.sleep(delay)

        except (
            urllib.error.URLError,
            TimeoutError
        ):
            if attempt == 3:
                raise

            time.sleep(
                5 * (2 ** attempt)
            )


def parse_utc(value):
    dt = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )

    if dt.tzinfo is None:
        dt = dt.replace(
            tzinfo=timezone.utc
        )

    return dt.astimezone(timezone.utc)


def valid_number(value):
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except (TypeError, ValueError):
        pass

    return None


def collect_batch(
    batch,
    model,
    writer,
    diagnostics,
    budget,
):
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

    payload = fetch(params, budget)

    responses = (
        payload
        if isinstance(payload, list)
        else [payload]
    )

    if len(responses) != len(batch):
        raise RuntimeError(
            "Numero localita restituito "
            "non corretto"
        )

    written = 0

    for station, response in zip(
        batch, responses
    ):
        if not isinstance(response, dict):
            raise RuntimeError(
                "Risposta API non valida"
            )

        if response.get("error"):
            raise RuntimeError(
                str(response.get("reason"))
            )

        hourly = response.get(
            "hourly", {}
        )

        times = hourly.get(
            "time", []
        )

        def field(name, index):
            values = hourly.get(name)

            if not isinstance(
                values, list
            ):
                return None

            if index >= len(values):
                return None

            return valid_number(
                values[index]
            )

        for i, raw_time in enumerate(times):
            valid = parse_utc(raw_time)

            if valid <= NOW:
                continue

            temperature = field(
                "temperature_2m", i
            )

            precipitation = field(
                "precipitation", i
            )

            wind = field(
                "wind_speed_10m", i
            )

            diagnostics["rows"] += 1

            for name, value in (
                ("temperature", temperature),
                ("precipitation", precipitation),
                ("wind", wind),
            ):
                key = (
                    f"{name}_valid"
                    if value is not None
                    else f"{name}_missing"
                )

                diagnostics[key] += 1

            writer.writerow({
                "acquired_utc":
                    NOW.isoformat(),
                "network":
                    station["network"],
                "station_id":
                    station["station_id"],
                "latitude":
                    station["latitude"],
                "longitude":
                    station["longitude"],
                "model":
                    model,
                "valid_time_utc":
                    valid.isoformat(),
                "lead_hours":
                    round(
                        (
                            valid - NOW
                        ).total_seconds() / 3600,
                        3
                    ),
                "temperature_2m_c":
                    temperature,
                "precipitation_mm":
                    precipitation,
                "wind_speed_10m_kmh":
                    wind,
            })

            written += 1

    return written


def close_forecast_cycle(staged, target, acquired):
    """Give all models a common acquisition time after the last download."""
    counts = Counter()
    with (
        gzip.open(staged, "rt", encoding="utf-8", newline="") as source,
        gzip.open(target, "wt", encoding="utf-8", newline="") as destination,
    ):
        writer = csv.DictWriter(destination, fieldnames=FIELDS)
        writer.writeheader()
        for row in csv.DictReader(source):
            ahead = (parse_utc(row["valid_time_utc"]) - acquired).total_seconds() / 3600
            if ahead <= 0:
                continue
            row["acquired_utc"] = acquired.isoformat()
            row["lead_hours"] = round(ahead, 3)
            writer.writerow(row)
            counts[row["model"]] += 1
    staged.unlink()
    return counts


def main():
    stations = load_stations()

    target = OUTPUT / (
        f"forecasts_meteohub_{STAMP}.csv.gz"
    )

    report_path = OUTPUT / (
        f"report_meteohub_{STAMP}.json"
    )

    counts = Counter()
    budget = ForecastBudget()
    staged = target.with_suffix(".partial")
    errors = []
    diagnostics = {}

    with gzip.open(
        staged,
        "wt",
        encoding="utf-8",
        newline=""
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=FIELDS
        )

        writer.writeheader()

        for model in MODELS:
            model_stats = Counter()
            diagnostics[model] = model_stats

            for index, batch in enumerate(
                chunks(
                    stations,
                    BATCH_SIZE
                ),
                1
            ):
                try:
                    count = collect_batch(
                        batch,
                        model,
                        writer,
                        model_stats,
                        budget,
                    )

                    counts[model] += count

                except ForecastQuotaExceeded:
                    raise
                except Exception as exc:
                    errors.append({
                        "model": model,
                        "batch": index,
                        "error": str(exc)[:250],
                    })

            print(
                f"{model}: "
                f"{counts[model]} righe, "
                f"{model_stats['temperature_valid']} "
                f"temperature valide, "
                f"{model_stats['wind_valid']} "
                f"valori vento validi",
                flush=True
            )

            # Detect empty model datasets.
            if (
                counts[model] > 0
                and model_stats[
                    "temperature_valid"
                ] == 0
                and model_stats[
                    "wind_valid"
                ] == 0
            ):
                errors.append({
                    "model": model,
                    "batch": "all",
                    "error":
                        "Nessun valore valido "
                        "di temperatura o vento",
                })

    acquired = datetime.now(timezone.utc)
    counts = close_forecast_cycle(staged, target, acquired)
    if any(counts[model] == 0 for model in MODELS):
        errors.append({"error": "At least one model has no archived forecast rows"})
    report = {
        "engine": "ENDURO WEATHER ENGINE",
        "version": "Forecast Collector V2.2",
        "collection_started_utc": NOW.isoformat(),
        "acquired_utc": acquired.isoformat(),
        "weighted_api_calls_estimate": budget.calls,
        "data_source": "Open-Meteo (https://open-meteo.com/)",
        "stations": len(stations),
        "models": MODELS,
        "forecast_rows": dict(counts),
        "total_rows": sum(
            counts.values()
        ),
        "model_diagnostics_scope": "downloaded forecasts before the common acquisition time cutoff",
        "model_diagnostics": {
            model: dict(stats)
            for model, stats
            in diagnostics.items()
        },
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
        encoding="utf-8"
    )

    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2
        )
    )

    if (
        not sum(counts.values())
        or errors
    ):
        raise RuntimeError(
            "Raccolta MeteoHub incompleta: "
            "consultare il report"
        )


if __name__ == "__main__":
    main()
