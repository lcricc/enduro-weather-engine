
#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - Forecast validation v1."""

import csv
import gzip
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FORECAST_DIR = ROOT / "data/forecasts/national"
OBS_DIR = ROOT / "data/observations/noaa"
OUTPUT_DIR = ROOT / "data/validation"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

VARIABLES = {
    "temperature_2m_c": "temperature_2m_c",
    "wind_speed_10m_kmh": "wind_speed_10m_kmh",
}

def parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None

def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None

def observation_files():
    return sorted(
        OBS_DIR.glob("observations_*.csv")
    )

def forecast_files():
    return sorted(
        FORECAST_DIR.glob("forecasts_*.csv.gz")
    )

def load_observations():
    observations = {}

    for path in observation_files():
        with path.open(
            encoding="utf-8-sig", newline=""
        ) as file:
            for row in csv.DictReader(file):
                network = row.get("network", "")
                station = row.get("station_id", "")
                observed = parse_time(
                    row.get("observed_utc")
                )

                if not station or observed is None:
                    continue

                key = (
                    network,
                    station,
                    observed,
                )

                observations[key] = {
                    variable: number(row.get(variable))
                    for variable in VARIABLES.values()
                }

    return observations

def load_forecasts():
    for path in forecast_files():
        with gzip.open(
            path, "rt", encoding="utf-8"
        ) as file:
            yield from csv.DictReader(file)

def calculate_metrics(errors):
    n = len(errors)

    if n == 0:
        return None

    mae = sum(abs(e) for e in errors) / n
    mse = sum(e * e for e in errors) / n
    bias = sum(errors) / n

    return {
        "count": n,
        "mae": round(mae, 4),
        "rmse": round(math.sqrt(mse), 4),
        "bias": round(bias, 4),
    }

def main():
    observations = load_observations()

    if not observations:
        print(
            "No NOAA observations available. "
            "Validation postponed."
        )
        return

    errors = defaultdict(list)
    matched_rows = 0
    forecast_rows = 0

    for row in load_forecasts():
        forecast_rows += 1

        valid_time = parse_time(
            row.get("valid_time_utc")
        )

        if valid_time is None:
            continue

        key = (
            row.get("network", ""),
            row.get("station_id", ""),
            valid_time,
        )

        observation = observations.get(key)

        if observation is None:
            continue

        matched_rows += 1
        model = row.get("model", "")

        for variable in VARIABLES:
            predicted = number(row.get(variable))
            observed = observation.get(variable)

            if predicted is None or observed is None:
                continue

            errors[(model, variable)].append(
                predicted - observed
            )

    results = []

    for (model, variable), values in sorted(
        errors.items()
    ):
        metrics = calculate_metrics(values)

        if metrics is None:
            continue

        results.append({
            "model": model,
            "variable": variable,
            **metrics,
        })

    report = {
        "generated_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "forecast_rows_scanned": forecast_rows,
        "matched_forecast_rows": matched_rows,
        "observation_records": len(observations),
        "metrics": results,
    }

    path = OUTPUT_DIR / "validation_latest.json"

    path.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8",
    )

    print(json.dumps(
        report,
        indent=2,
        ensure_ascii=False
    ))

    if not results:
        print(
            "No valid forecast-observation pairs yet. "
            "More overlapping data is required."
        )

if __name__ == "__main__":
    main()
