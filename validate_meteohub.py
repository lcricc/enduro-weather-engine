
#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - MeteoHub Scientific Validation V3.

Official metrics use common samples from all available models.
Preliminary metrics use valid pairs for each individual model.
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

FIELDS = [
    "model", "variable", "lead_bucket",
    "n", "mae", "rmse", "bias",
]


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

    raw = json.loads(
        APPROVED.read_text(encoding="utf-8")
    )

    if not isinstance(raw, list):
        raise ValueError(
            "Approved metadata must be a list"
        )

    result = {}

    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Invalid metadata entry")

        code = item.get("code")
        unit = item.get("unit")

        if code not in VARIABLES:
            raise ValueError(
                f"Unsupported BUFR code: {code}"
            )

        if code == "B12101":
            if unit not in ("K", "degC"):
                raise ValueError(
                    "Invalid temperature unit"
                )

        if code == "B11002":
            if unit not in ("m/s", "km/h"):
                raise ValueError(
                    "Invalid wind unit"
                )

        level = item.get("level")
        timerange = item.get("timerange")

        if not isinstance(level, list):
            raise ValueError(
                "BUFR level must be an array"
            )

        if not isinstance(timerange, list):
            raise ValueError(
                "BUFR timerange must be an array"
            )

        key = signature(
            code, level, timerange
        )

        if key in result:
            raise ValueError(
                "Duplicate approved signature"
            )

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
        encoding="utf-8-sig",
        newline=""
    ) as file:

        for row in csv.DictReader(file):
            stats["rows"] += 1

            code = (
                row.get("variable") or ""
            ).strip()

            if code not in VARIABLES:
                continue

            try:
                level = json.loads(
                    row.get("level") or "null"
                )
                timerange = json.loads(
                    row.get("timerange") or "null"
                )
            except (ValueError, TypeError):
                stats["invalid_metadata"] += 1
                continue

            key = signature(
                code, level, timerange
            )

            if key not in approved:
                stats["unapproved_metadata"] += 1
                continue

            unit = approved[key]

            observed_unit = (
                row.get("unit") or ""
            ).strip()

            if (
                observed_unit
                and observed_unit != unit
            ):
                stats["unit_conflicts"] += 1
                continue

            dt = timestamp(
                row.get("time_utc")
            )
            value = number(
                row.get("value")
            )

            if dt is None or value is None:
                stats["invalid_values"] += 1
                continue

            if (
                dt.minute
                or dt.second
                or dt.microsecond
            ):
                stats["non_hourly"] += 1
                continue

            network = (
                row.get("network") or ""
            ).strip()

            station = (
                row.get("station_id") or ""
            ).strip()

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


def calculate_metrics(errors):
    metrics = []

    for (model, variable, bucket), values in sorted(
        errors.items()
    ):
        n = len(values)

        if n == 0:
            continue

        metrics.append({
            "model": model,
            "variable": variable,
            "lead_bucket": bucket,
            "n": n,
            "mae": round(
                sum(abs(x) for x in values) / n,
                4
            ),
            "rmse": round(
                math.sqrt(
                    sum(x * x for x in values) / n
                ),
                4
            ),
            "bias": round(
                sum(values) / n,
                4
            ),
        })

    return metrics


def save_csv(path, metrics):
    with path.open(
        "w",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=FIELDS
        )

        writer.writeheader()
        writer.writerows(metrics)


def validate():
    OUT.mkdir(
        parents=True,
        exist_ok=True
    )

    approved = load_approved()

    if not OBS.is_file():
        raise FileNotFoundError(
            "MeteoHub observations missing"
        )

    forecast_files = sorted(
        FC_DIR.glob(
            "forecasts_meteohub_*.csv.gz"
        )
    )

    if not forecast_files:
        raise FileNotFoundError(
            "MeteoHub forecasts missing"
        )

    observations, obs_stats = (
        load_observations(approved)
    )

    now = datetime.now(timezone.utc)

    latest_observation = max(
        (key[2] for key in observations),
        default=None
    )

    diagnostics = Counter()
    candidates = defaultdict(list)
    all_models = set()

    for path in forecast_files:

        with gzip.open(
            path,
            "rt",
            encoding="utf-8",
            newline=""
        ) as file:

            for row in csv.DictReader(file):
                diagnostics["forecast_rows"] += 1

                model = (
                    row.get("model") or ""
                ).strip()

                if model:
                    all_models.add(model)

                issue = timestamp(
                    row.get("acquired_utc")
                )

                valid = timestamp(
                    row.get("valid_time_utc")
                )

                if (
                    not issue
                    or not valid
                    or valid <= issue
                ):
                    diagnostics[
                        "invalid_forecast_time"
                    ] += 1
                    continue

                if valid.minute or valid.second:
                    diagnostics[
                        "non_hourly_forecast"
                    ] += 1
                    continue

                hours = (
                    valid - issue
                ).total_seconds() / 3600

                bucket = lead_bucket(hours)

                if bucket is None:
                    diagnostics[
                        "outside_lead_window"
                    ] += 1
                    continue

                network = (
                    row.get("network") or ""
                ).strip()

                station = (
                    row.get("station_id") or ""
                ).strip()

                if not network or not station:
                    diagnostics[
                        "missing_forecast_station"
                    ] += 1
                    continue

                for variable in VARIABLES.values():

                    predicted = number(
                        row.get(variable)
                    )

                    if predicted is None:
                        diagnostics[
                            "missing_prediction_value"
                        ] += 1
                        continue

                    obs_key = (
                        network,
                        station,
                        valid,
                        variable
                    )

                    if obs_key not in observations:

                        diagnostics["unmatched"] += 1

                        if valid > now:
                            diagnostics[
                                "forecast_valid_time_in_future"
                            ] += 1

                        elif (
                            latest_observation is not None
                            and valid > latest_observation
                        ):
                            diagnostics[
                                "forecast_after_latest_observation"
                            ] += 1

                        else:
                            diagnostics[
                                "missing_observation_at_valid_time"
                            ] += 1

                        continue

                    event_key = (
                        issue,
                        obs_key,
                        bucket,
                        model
                    )

                    candidates[event_key].append(
                        (
                            predicted,
                            observations[obs_key]
                        )
                    )

    events = defaultdict(dict)

    for (
        issue,
        obs_key,
        bucket,
        model
    ), values in candidates.items():

        if len(values) != 1:
            diagnostics[
                "duplicate_forecasts"
            ] += 1
            continue

        events[
            (issue, obs_key, bucket)
        ][model] = values[0]

    official_errors = defaultdict(list)
    preliminary_errors = defaultdict(list)

    for (
        issue,
        obs_key,
        bucket
    ), models in events.items():

        variable = obs_key[3]

        diagnostics[
            "events_with_at_least_one_model"
        ] += 1

        for model, (
            predicted,
            observed
        ) in models.items():

            preliminary_errors[
                (model, variable, bucket)
            ].append(
                predicted - observed
            )

        if set(models) != all_models:
            diagnostics[
                "incomplete_model_comparison"
            ] += 1

            diagnostics[
                "partial_events"
            ] += 1

            continue

        diagnostics[
            "complete_model_events"
        ] += 1

        for model, (
            predicted,
            observed
        ) in models.items():

            official_errors[
                (model, variable, bucket)
            ].append(
                predicted - observed
            )

    metrics = calculate_metrics(
        official_errors
    )

    preliminary_metrics = calculate_metrics(
        preliminary_errors
    )

    report = {
        "engine": "ENDURO WEATHER ENGINE",
        "version": "MeteoHub Validation V3",
        "generated_utc": now.isoformat(),
        "forecast_files": len(forecast_files),
        "forecast_models": sorted(all_models),
        "approved_signatures": len(approved),
        "latest_observation_utc": (
            latest_observation.isoformat()
            if latest_observation
            else None
        ),
        "observation_diagnostics": dict(
            obs_stats
        ),
        "forecast_diagnostics": dict(
            diagnostics
        ),
        "metrics": metrics,
        "preliminary_metrics": preliminary_metrics,
        "status": (
            "VALIDATED"
            if metrics
            else "AWAITING_VERIFIABLE_PAIRS"
        ),
        "preliminary_status": (
            "PRELIMINARY_AVAILABLE"
            if preliminary_metrics
            else "AWAITING_PAIRS"
        ),
        "limitations": [
            "Only manually approved metadata accepted",
            "Exact hourly UTC matching required",
            "Forecast acquisition is a proxy for issuance",
            "Official metrics require common-model samples",
            "Preliminary metrics may use different samples per model",
            "Preliminary metrics are not official model rankings",
            "Precipitation excluded pending BUFR review",
            "Zero metrics does not establish accuracy",
        ],
    }

    (
        OUT / "validation_latest.json"
    ).write_text(
        json.dumps(
            report,
            indent=2
        ),
        encoding="utf-8"
    )

    save_csv(
        OUT / "metrics.csv",
        metrics
    )

    save_csv(
        OUT / "preliminary_metrics.csv",
        preliminary_metrics
    )

    print(
        json.dumps(
            report,
            indent=2
        )
    )


if __name__ == "__main__":
    validate()
