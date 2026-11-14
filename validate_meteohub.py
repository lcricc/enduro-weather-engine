
#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - MeteoHub validation V2.

Conservative validation of hourly temperature and wind forecasts.
Only manually approved BUFR metadata signatures are accepted.
"""

import csv
import gzip
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

OBS = Path(
    "data/observations/meteohub/clean/observations_clean.csv"
)
FC_DIR = Path("data/forecasts/national")
OUT = Path("data/validation/meteohub")
APPROVED = Path("meteohub_approved_metadata.json")

VARIABLES = {
    "B12101": "temperature_2m_c",
    "B11002": "wind_speed_10m_kmh",
}


def number(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError):
        return None


def timestamp(value):
    try:
        d = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if d.tzinfo is None:
            return None
        return d.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def signature(code, level, timerange):
    return json.dumps(
        {
            "code": code,
            "level": level,
            "timerange": timerange,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def load_approved():
    if not APPROVED.exists():
        return {}

    raw = json.loads(APPROVED.read_text(encoding="utf-8"))

    if not isinstance(raw, list):
        raise ValueError("Approved metadata must be a list")

    result = {}

    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Invalid metadata entry")

        code = item.get("code")
        unit = item.get("unit")

        if code not in VARIABLES:
            raise ValueError(f"Unsupported BUFR code: {code}")

        if unit not in ("K", "degC", "m/s", "km/h"):
            raise ValueError(
                f"Explicit unit required for {code}"
            )

        if code == "B12101" and unit not in ("K", "degC"):
            raise ValueError("Invalid temperature unit")

        if code == "B11002" and unit not in ("m/s", "km/h"):
            raise ValueError("Invalid wind unit")

        if not isinstance(item.get("level"), list):
            raise ValueError("BUFR level must be an array")

        if not isinstance(item.get("timerange"), list):
            raise ValueError("BUFR timerange must be an array")

        key = signature(
            code, item["level"], item["timerange"]
        )

        if key in result:
            raise ValueError("Duplicate approved signature")

        result[key] = unit

    return result


def convert(value, unit):
    if unit == "K":
        return value - 273.15
    if unit == "m/s":
        return value * 3.6
    return value


def load_observations(approved):
    observations = defaultdict(list)
    stats = Counter()

    with OBS.open(
        encoding="utf-8-sig", newline=""
    ) as file:
        for row in csv.DictReader(file):
            stats["rows"] += 1

            code = (row.get("variable") or "").strip()

            if code not in VARIABLES:
                continue

            try:
                level = json.loads(row.get("level") or "null")
                timerange = json.loads(
                    row.get("timerange") or "null"
                )
            except (ValueError, TypeError):
                stats["invalid_metadata"] += 1
                continue

            key = signature(code, level, timerange)

            if key not in approved:
                stats["unapproved_metadata"] += 1
                continue

            unit = approved[key]
            observed_unit = (row.get("unit") or "").strip()

            if observed_unit and observed_unit != unit:
                stats["unit_conflicts"] += 1
                continue

            dt = timestamp(row.get("time_utc"))
            value = number(row.get("value"))

            if dt is None or value is None:
                stats["invalid_values"] += 1
                continue

            if dt.minute or dt.second or dt.microsecond:
                stats["non_hourly"] += 1
                continue

            network = (row.get("network") or "").strip()
            station = (row.get("station_id") or "").strip()

            if not network or not station:
                stats["missing_station"] += 1
                continue

            obs_key = (
                network,
                station,
                dt,
                VARIABLES[code],
            )

            observations[obs_key].append(
                convert(value, unit)
            )

    clean = {}

    for key, values in observations.items():
        if len(values) == 1:
            clean[key] = values[0]
        else:
            stats["ambiguous_observations"] += 1

    stats["usable_station_hours"] = len(clean)
    return clean, stats


def lead_bucket(hours):
    if 0 < hours <= 24:
        return "0-24h"
    if 24 < hours <= 48:
        return "24-48h"
    if 48 < hours <= 72:
        return "48-72h"
    if 72 < hours <= 96:
        return "72-96h"
    return None


def validate():
    OUT.mkdir(parents=True, exist_ok=True)

    approved = load_approved()

    if not OBS.is_file():
        raise FileNotFoundError("MeteoHub observations missing")

    forecast_files = sorted(
        FC_DIR.glob("forecasts_meteohub_*.csv.gz")
    )

    if not forecast_files:
        raise FileNotFoundError("MeteoHub forecasts missing")

    observations, obs_stats = load_observations(approved)

    diagnostics = Counter()
    candidates = defaultdict(list)
    all_models = set()

    for path in forecast_files:
        with gzip.open(
            path, "rt", encoding="utf-8", newline=""
        ) as file:
            for row in csv.DictReader(file):
                diagnostics["forecast_rows"] += 1

                model = (row.get("model") or "").strip()
                if model:
                    all_models.add(model)

                issue = timestamp(row.get("acquired_utc"))
                valid = timestamp(row.get("valid_time_utc"))

                if not issue or not valid or valid <= issue:
                    diagnostics["invalid_forecast_time"] += 1
                    continue

                if valid.minute or valid.second:
                    diagnostics["non_hourly_forecast"] += 1
                    continue

                hours = (valid - issue).total_seconds() / 3600
                bucket = lead_bucket(hours)

                if bucket is None:
                    continue

                network = (row.get("network") or "").strip()
                station = (row.get("station_id") or "").strip()

                for variable in VARIABLES.values():
                    predicted = number(row.get(variable))

                    if predicted is None:
                        continue

                    obs_key = (
                        network, station, valid, variable
                    )

                    if obs_key not in observations:
                        diagnostics["unmatched"] += 1
                        continue

                    event_key = (
                        issue, obs_key, bucket, model
                    )

                    candidates[event_key].append(
                        (predicted, observations[obs_key])
                    )

    events = defaultdict(dict)

    for (issue, obs_key, bucket, model), values in (
        candidates.items()
    ):
        if len(values) != 1:
            diagnostics["duplicate_forecasts"] += 1
            continue

        events[(issue, obs_key, bucket)][model] = values[0]

    errors = defaultdict(list)

    for (_, obs_key, bucket), models in events.items():
        if set(models) != all_models:
            diagnostics["incomplete_model_comparison"] += 1
            continue

        variable = obs_key[3]

        for model, (predicted, observed) in models.items():
            errors[(model, variable, bucket)].append(
                predicted - observed
            )

    metrics = []

    for (model, variable, bucket), values in sorted(
        errors.items()
    ):
        n = len(values)

        metrics.append({
            "model": model,
            "variable": variable,
            "lead_bucket": bucket,
            "n": n,
            "mae": round(
                sum(abs(x) for x in values) / n, 4
            ),
            "rmse": round(
                math.sqrt(
                    sum(x * x for x in values) / n
                ), 4
            ),
            "bias": round(sum(values) / n, 4),
        })

    report = {
        "engine": "ENDURO WEATHER ENGINE",
        "version": "MeteoHub Validation V2",
        "generated_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "forecast_files": len(forecast_files),
        "forecast_models": sorted(all_models),
        "approved_signatures": len(approved),
        "observation_diagnostics": dict(obs_stats),
        "forecast_diagnostics": dict(diagnostics),
        "metrics": metrics,
        "status": (
            "VALIDATED"
            if metrics
            else "AWAITING_VERIFIABLE_PAIRS"
        ),
        "limitations": [
            "Only manually approved metadata accepted",
            "Exact hourly UTC matching required",
            "Forecast acquisition is a proxy for issuance",
            "Only common-model samples compared",
            "Precipitation excluded pending BUFR review",
            "Zero metrics does not establish accuracy",
        ],
    }

    (OUT / "validation_latest.json").write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )

    with (OUT / "metrics.csv").open(
        "w", newline="", encoding="utf-8"
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "model", "variable", "lead_bucket",
                "n", "mae", "rmse", "bias",
            ],
        )
        writer.writeheader()
        writer.writerows(metrics)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    validate()
