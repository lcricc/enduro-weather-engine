
#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - MeteoHub Scientific Validation V3.2.

Official metrics require identical observation events for all 8 models.
Preliminary metrics use valid model-observation pairs.
Only manually approved BUFR metadata signatures are accepted.

V3.2:
- Explicit list of eight official models
- Excludes obsolete ECMWF AIFS identifier from comparisons
- Preserves historical forecast archives
- Retains V3.1 per-model diagnostics
"""

import csv
import gzip
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from meteohub_rain_recovery import LOOKBACK, PUBLICATION_DELAY, missing_ends, recovery_plan

OBS = Path(
    "data/observations/meteohub/clean/observations_clean.csv"
)
FC_DIR = Path("data/forecasts/national")
OUT = Path("data/validation/meteohub")
APPROVED = Path("meteohub_approved_metadata.json")
OBSERVATION_TOLERANCE = timedelta(minutes=30)
EXACT_TIME = timedelta(0)
HOUR = timedelta(hours=1)
PRECIPITATION_CODE = "B13011"
PRECIPITATION_VARIABLE = "precipitation_mm"
PRECIPITATION_UNITS = ("kg/m2", "kg/m**2", "kg/m^2", "kg/m²", "mm")
PRECIPITATION_THRESHOLD_MM = 0.1
PRECIPITATION_OUTCOMES = {
    (True, True): "hits",
    (False, True): "misses",
    (True, False): "false_alarms",
    (False, False): "correct_dry",
}

VARIABLES = {
    "B12101": "temperature_2m_c",
    "B11002": "wind_speed_10m_kmh",
}
FORECAST_VARIABLES = (*VARIABLES.values(), PRECIPITATION_VARIABLE)

OFFICIAL_MODELS = {
    "ecmwf_ifs025",
    "ecmwf_aifs025_single",
    "icon_seamless",
    "gfs_seamless",
    "meteofrance_seamless",
    "ukmo_seamless",
    "gem_seamless",
    "arpege_world",
}

LEGACY_MODELS = {
    "ecmwf_aifs025",
}

FIELDS = [
    "model",
    "variable",
    "lead_bucket",
    "n",
    "mae",
    "rmse",
    "bias",
]

PRECIPITATION_FIELDS = [
    "model",
    "lead_bucket",
    "threshold_mm",
    "n",
    "observed_wet",
    *PRECIPITATION_OUTCOMES.values(),
    "probability_of_detection",
    "false_alarm_ratio",
    "critical_success_index",
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
            raise ValueError(
                "Invalid metadata entry"
            )

        code = item.get("code")
        unit = item.get("unit")

        if code not in VARIABLES and code != PRECIPITATION_CODE:
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

        if code == PRECIPITATION_CODE and (
            unit not in PRECIPITATION_UNITS
            or level != [1, None, None, None]
            or len(timerange) != 3
            or timerange[:2] != [1, 0]
            or type(timerange[2]) is not int
            or not 0 < timerange[2] <= HOUR.total_seconds()
        ):
            raise ValueError("Invalid BUFR surface precipitation accumulation")

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


def closest_hour(measured_at):
    """Assign observations at the half-hour boundary to the following hour."""
    hour = measured_at.replace(minute=0, second=0, microsecond=0)
    if measured_at.minute >= 30:
        hour += timedelta(hours=1)
    return hour


def add_precipitation_interval(
    intervals_by_hour, network, station, end, seconds, value, stats
):
    if value < 0:
        stats["negative_precipitation_observations"] += 1
        return

    hour_end = end.replace(minute=0, second=0, microsecond=0)
    if end > hour_end:
        hour_end += HOUR
    start = end - timedelta(seconds=seconds)
    if start < hour_end - HOUR:
        stats["precipitation_intervals_crossing_hour"] += 1
        return

    key = (network, station, hour_end, PRECIPITATION_VARIABLE)
    intervals = intervals_by_hour[key]
    bounds = (start, end)
    if bounds not in intervals:
        intervals[bounds] = value
    elif intervals[bounds] is not None and intervals[bounds] != value:
        intervals[bounds] = None
        stats["conflicting_precipitation_intervals"] += 1


def hourly_precipitation(intervals_by_hour, stats):
    observations = {}
    for key, intervals in intervals_by_hour.items():
        end = key[2]
        start = end - HOUR
        measured_hour = intervals.get((start, end))
        if measured_hour is not None:
            observations[key] = measured_hour
            stats["direct_precipitation_hours"] += 1
            continue

        # A complete path uses adjoining intervals from start to end.
        # Track the two extremes to detect disagreement between complete paths.
        totals = {start: (0.0, 0.0)}
        for (segment_start, segment_end), value in sorted(intervals.items()):
            if value is None or segment_start not in totals:
                continue
            lower, upper = totals[segment_start]
            candidate_min, candidate_max = lower + value, upper + value
            old_min, old_max = totals.get(
                segment_end, (candidate_min, candidate_max)
            )
            totals[segment_end] = (
                min(old_min, candidate_min), max(old_max, candidate_max)
            )

        if end not in totals:
            stats["incomplete_precipitation_hours"] += 1
            continue
        lower, upper = totals[end]
        if not math.isfinite(upper) or not math.isclose(
            lower, upper, rel_tol=0, abs_tol=1e-8
        ):
            stats["ambiguous_precipitation_hours"] += 1
            continue
        observations[key] = lower
        stats["reconstructed_precipitation_hours"] += 1

    stats["usable_precipitation_station_hours"] = len(observations)
    return observations


def load_observations(
    approved, *, time_tolerance=EXACT_TIME, include_precipitation=False,
    recovery_gaps=None, now=None
):
    observations = defaultdict(list)
    precipitation_intervals = defaultdict(dict)
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

            is_precipitation = include_precipitation and code == PRECIPITATION_CODE
            if code not in VARIABLES and not is_precipitation:
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

            if observed_unit and observed_unit != unit:
                if (
                    not is_precipitation
                    or observed_unit.lower() not in PRECIPITATION_UNITS
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

            if not is_precipitation and (
                dt.minute
                or dt.second
                or dt.microsecond
            ):
                stats["non_hourly"] += 1
                if (
                    not time_tolerance
                    or abs(dt - closest_hour(dt)) > time_tolerance
                ):
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

            if is_precipitation:
                add_precipitation_interval(
                    precipitation_intervals, network, station, dt,
                    timerange[2], value, stats
                )
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
            measured_at = key[2]
            if measured_at == closest_hour(measured_at):
                stats["usable_station_hours"] += 1
            else:
                stats["usable_non_hourly_observations"] += 1
        else:
            stats["ambiguous_observations"] += 1

    stats.setdefault("usable_station_hours", 0)
    if include_precipitation:
        rain = hourly_precipitation(precipitation_intervals, stats)
        clean.update(rain)
        if recovery_gaps is not None:
            for key, intervals in precipitation_intervals.items():
                if key in rain or not now - LOOKBACK <= key[2] <= now - PUBLICATION_DELAY:
                    continue
                ends = missing_ends(key[2], intervals)
                if ends:
                    recovery_gaps[key] = ends

    return clean, stats


def index_nearby_observations(observations):
    by_hour = defaultdict(list)
    for (network, station, measured_at, variable), value in observations.items():
        hour = closest_hour(measured_at)
        offset = measured_at - hour
        if offset:
            by_hour[(network, station, hour, variable)].append((offset, value))
    for candidates in by_hour.values():
        candidates.sort(key=lambda candidate: (abs(candidate[0]), candidate[0]))
    return by_hour


def match_observation(
    obs_key, issue, now, observations, nearby_observations
):
    valid = obs_key[2]
    if valid > now:
        return None

    exact = observations.get(obs_key)
    if exact is not None:
        return exact, EXACT_TIME

    for offset, value in nearby_observations.get(obs_key, ()):
        if issue < valid + offset <= now:
            return value, offset
    return None


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


def precipitation_metrics(counts):
    rows = []
    for (model, bucket), outcomes in sorted(counts.items()):
        hits = outcomes["hits"]
        misses = outcomes["misses"]
        false_alarms = outcomes["false_alarms"]
        observed_wet = hits + misses
        forecast_wet = hits + false_alarms
        rain_cases = forecast_wet + misses
        rows.append({
            "model": model,
            "lead_bucket": bucket,
            "threshold_mm": PRECIPITATION_THRESHOLD_MM,
            "n": sum(outcomes.values()),
            "observed_wet": observed_wet,
            **{name: outcomes[name] for name in PRECIPITATION_OUTCOMES.values()},
            "probability_of_detection": round(hits / observed_wet, 4) if observed_wet else None,
            "false_alarm_ratio": round(false_alarms / forecast_wet, 4) if forecast_wet else None,
            "critical_success_index": round(hits / rain_cases, 4) if rain_cases else None,
        })
    return rows


def write_precipitation_summary(rows):
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return

    threshold = str(PRECIPITATION_THRESHOLD_MM).replace(".", ",")
    lines = [
        "## Confronto sulla precipitazione oraria",
        "",
        f"Almeno {threshold} mm in un’ora. Stesso campione per gli otto modelli in ogni fascia.",
        "",
    ]
    if rows:
        lines.extend([
            "| Modello | Preavviso | Confronti | Ore di pioggia | Piogge previste | Piogge mancate | Falsi allarmi | Ore asciutte previste |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ])
        for row in rows:
            lines.append(
                "| {model} | {lead_bucket} | {n} | {observed_wet} | {hits} | {misses} | {false_alarms} | {correct_dry} |".format(**row)
            )
    else:
        lines.append("In attesa di confronti completi sulla precipitazione per gli otto modelli.")

    with Path(summary).open("a", encoding="utf-8") as file:
        file.write("\n".join(lines) + "\n")


def save_csv(path, metrics, fields=FIELDS):
    with path.open(
        "w",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fields
        )

        writer.writeheader()
        writer.writerows(metrics)


def validate(*, time_tolerance=OBSERVATION_TOLERANCE, now=None):
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

    now = now or datetime.now(timezone.utc)
    gaps = {}
    observations, obs_stats = load_observations(
        approved, time_tolerance=time_tolerance, include_precipitation=True,
        recovery_gaps=gaps, now=now
    )
    nearby_observations = index_nearby_observations(observations)

    latest_observation = max(
        (key[2] for key in observations),
        default=None
    )

    latest_exact_observation = max(
        (key[2] for key in observations if key[2] == closest_hour(key[2])),
        default=None,
    )

    diagnostics = Counter(
        complete_precipitation_events=0, complete_wet_precipitation_events=0
    )
    candidates = defaultdict(list)
    gap_forecasts = defaultdict(Counter)
    gap_coordinates = {}
    verified_rain_stations = set()

    # Exactly eight models are required.
    all_models = set(OFFICIAL_MODELS)

    model_diagnostics = defaultdict(Counter)

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

                # Preserve historical files but do not
                # use the obsolete AIFS identifier.
                if model in LEGACY_MODELS:
                    diagnostics[
                        "legacy_rows_excluded"
                    ] += 1
                    continue

                if model not in OFFICIAL_MODELS:
                    diagnostics[
                        "unknown_model_rows"
                    ] += 1
                    continue

                model_diagnostics[model][
                    "forecast_rows"
                ] += 1

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

                    model_diagnostics[model][
                        "invalid_forecast_time"
                    ] += 1
                    continue

                if (
                    valid.minute
                    or valid.second
                    or valid.microsecond
                ):
                    diagnostics[
                        "non_hourly_forecast"
                    ] += 1

                    model_diagnostics[model][
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

                    model_diagnostics[model][
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

                    model_diagnostics[model][
                        "missing_forecast_station"
                    ] += 1
                    continue

                for variable in FORECAST_VARIABLES:

                    predicted = number(
                        row.get(variable)
                    )

                    if predicted is None or (
                        variable == PRECIPITATION_VARIABLE and predicted < 0
                    ):
                        diagnostics[
                            "missing_prediction_value"
                        ] += 1

                        model_diagnostics[model][
                            f"{variable}_missing"
                        ] += 1
                        continue

                    if (
                        variable == PRECIPITATION_VARIABLE
                        and issue > valid - HOUR
                    ):
                        diagnostics["precipitation_already_started"] += 1
                        model_diagnostics[model]["precipitation_already_started"] += 1
                        continue

                    model_diagnostics[model][
                        f"{variable}_valid"
                    ] += 1

                    obs_key = (
                        network,
                        station,
                        valid,
                        variable
                    )

                    if obs_key in gaps:
                        gap_forecasts[(issue, obs_key)][model] += 1
                        latitude = number(row.get("latitude"))
                        longitude = number(row.get("longitude"))
                        if (
                            latitude is not None and -90 <= latitude <= 90
                            and longitude is not None and -180 <= longitude <= 180
                        ):
                            gap_coordinates.setdefault(obs_key, (latitude, longitude))

                    match = match_observation(
                        obs_key, issue, now, observations, nearby_observations
                    )
                    if match is None:

                        diagnostics["unmatched"] += 1

                        model_diagnostics[model][
                            f"{variable}_unmatched"
                        ] += 1

                        if valid > now:
                            diagnostics[
                                "forecast_valid_time_in_future"
                            ] += 1

                            model_diagnostics[model][
                                "future"
                            ] += 1

                        elif (
                            latest_observation is not None
                            and valid > latest_observation
                        ):
                            diagnostics[
                                "forecast_after_latest_observation"
                            ] += 1

                            model_diagnostics[model][
                                "after_latest_observation"
                            ] += 1

                        else:
                            diagnostics[
                                "missing_observation_at_valid_time"
                            ] += 1

                            model_diagnostics[model][
                                "missing_observation"
                            ] += 1

                        continue

                    event_key = (
                        issue,
                        obs_key,
                        bucket,
                        model
                    )

                    observed, offset = match
                    candidates[event_key].append((predicted, observed, offset))

                    model_diagnostics[model][
                        f"{variable}_matched"
                    ] += 1

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

            model_diagnostics[model][
                "duplicate_forecasts"
            ] += 1
            continue

        events[
            (issue, obs_key, bucket)
        ][model] = values[0]

        model_diagnostics[model][
            "unique_pairs"
        ] += 1

    official_errors = defaultdict(list)
    exact_time_errors = defaultdict(list)
    preliminary_errors = defaultdict(list)
    precipitation_counts = defaultdict(Counter)
    event_offsets = Counter()
    complete_event_offsets = Counter()

    for (
        issue,
        obs_key,
        bucket
    ), models in events.items():

        variable = obs_key[3]
        # All models in the same forecast cycle use the same observation.
        _, measured, offset = next(iter(models.values()))
        offset_seconds = offset.total_seconds()
        event_offsets[offset_seconds] += 1

        diagnostics[
            "events_with_at_least_one_model"
        ] += 1

        for model, (
            predicted,
            observed,
            _
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
        complete_event_offsets[offset_seconds] += 1
        if variable == PRECIPITATION_VARIABLE:
            diagnostics["complete_precipitation_events"] += 1
            verified_rain_stations.add(obs_key[:2])
            if measured > 0:
                diagnostics["complete_wet_precipitation_events"] += 1

        for model, (
            predicted,
            observed,
            offset
        ) in models.items():
            error = predicted - observed
            key = (model, variable, bucket)
            official_errors[key].append(error)
            if not offset:
                exact_time_errors[key].append(error)
            if variable == PRECIPITATION_VARIABLE:
                outcome = PRECIPITATION_OUTCOMES[
                    (predicted >= PRECIPITATION_THRESHOLD_MM, observed >= PRECIPITATION_THRESHOLD_MM)
                ]
                precipitation_counts[(model, bucket)][outcome] += 1

    metrics = calculate_metrics(
        official_errors
    )
    rain_metrics = precipitation_metrics(precipitation_counts)

    plan = recovery_plan(
        gaps, gap_forecasts, gap_coordinates, verified_rain_stations, all_models
    )
    plan["generated_utc"] = now.isoformat()
    previous_plan = OUT.parent / "previous_rain_recovery" / "rain_recovery_plan.json"
    previous_hours = []
    if previous_plan.is_file():
        previous_hours = json.loads(previous_plan.read_text(encoding="utf-8"))["station_hours"]
    completed_hours = sum(
        (item["network"], item["station_id"], timestamp(item["hour_end_utc"]), PRECIPITATION_VARIABLE)
        in observations
        for item in previous_hours
    )
    (OUT / "rain_recovery_plan.json").write_text(
        json.dumps(plan, indent=2), encoding="utf-8"
    )

    preliminary_metrics = calculate_metrics(
        preliminary_errors
    )

    model_report = {
        model: dict(model_diagnostics[model])
        for model in sorted(all_models)
    }

    print("\n=== DIAGNOSTICA MODELLI V3.2 ===")

    for model in sorted(all_models):
        stats = model_diagnostics[model]

        print(f"\nMODELLO: {model}")
        print(
            "Righe raccolte:",
            stats["forecast_rows"]
        )
        print(
            "Temperatura valida:",
            stats["temperature_2m_c_valid"]
        )
        print(
            "Temperatura abbinata:",
            stats["temperature_2m_c_matched"]
        )
        print(
            "Vento valido:",
            stats["wind_speed_10m_kmh_valid"]
        )
        print(
            "Vento abbinato:",
            stats["wind_speed_10m_kmh_matched"]
        )
        print(
            "Coppie univoche:",
            stats["unique_pairs"]
        )
        print(
            "Valori mancanti temperatura:",
            stats["temperature_2m_c_missing"]
        )
        print(
            "Valori mancanti vento:",
            stats["wind_speed_10m_kmh_missing"]
        )
        print(
            "Previsioni future:",
            stats["future"]
        )
        print(
            "Osservazioni mancanti:",
            stats["missing_observation"]
        )

    print("\n=== FINE DIAGNOSTICA ===\n")

    report = {
        "engine": "ENDURO WEATHER ENGINE",
        "version": "MeteoHub Validation V3.2",
        "generated_utc": now.isoformat(),
        "forecast_files": len(forecast_files),
        "forecast_models": sorted(all_models),
        "approved_signatures": len(approved),
        "rain_recovery": {
            "pending_station_hours": len(plan["station_hours"]),
            "target_windows": len(plan["requests"]),
            "previous_plan_hours_now_complete": completed_hours
        },
        "latest_observation_utc": (
            latest_exact_observation.isoformat()
            if latest_exact_observation
            else None
        ),
        "observation_matching": {
            "time_tolerance_seconds": time_tolerance.total_seconds(),
            "precipitation_accumulation_seconds": int(HOUR.total_seconds()),
            "precipitation_time_tolerance_seconds": 0,
            "half_hour_assignment": "following_hour",
            "latest_eligible_observation_utc": (
                latest_observation.isoformat() if latest_observation else None
            ),
            "exact_events": event_offsets[0],
            "events_with_tolerance": sum(event_offsets.values()) - event_offsets[0],
            "complete_exact_events": complete_event_offsets[0],
            "complete_events_with_tolerance": (
                sum(complete_event_offsets.values()) - complete_event_offsets[0]
            ),
            "event_offsets_seconds": [
                {
                    "observation_minus_forecast_seconds": offset,
                    "events": count,
                    "complete_events": complete_event_offsets[offset],
                }
                for offset, count in sorted(event_offsets.items())
            ],
        },
        "observation_diagnostics": dict(
            obs_stats
        ),
        "forecast_diagnostics": dict(
            diagnostics
        ),
        "model_diagnostics": model_report,
        "metrics": metrics,
        "precipitation_occurrence": {
            "threshold_mm": PRECIPITATION_THRESHOLD_MM,
            "metrics": rain_metrics,
        },
        "exact_time_metrics": calculate_metrics(exact_time_errors),
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
            "Hourly temperature and wind forecasts match the nearest approved "
            "station observation "
            "within the reported tolerance",
            "Exact observations have priority",
            "A half-hour observation belongs to the following hour",
            "Equidistant observations for the same forecast hour prefer the earlier measurement",
            "Observation time must follow forecast acquisition",
            "Forecast acquisition is a proxy for issuance",
            "Official metrics require common-eight-model samples",
            "Preliminary metrics may use different samples per model",
            "Preliminary metrics are not official model rankings",
            "Legacy ECMWF AIFS rows excluded from calculations",
            "Precipitation uses exact hourly totals in millimetres of water equivalent",
            "Precipitation forecasts must be acquired by the start of the accumulation hour",
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
    from enduro_rain_v01 import validate_issues

    prospective = validate_issues(Path("historical/forecasts"), observations, PRECIPITATION_VARIABLE, now)
    (OUT / "enduro_rain_v01.json").write_text(json.dumps(prospective, indent=2), encoding="utf-8")
    print(f"ENDURO rain v0.1: {prospective['overall']['station_hours']} prospective station-hours")

    save_csv(OUT / "precipitation_occurrence.csv", rain_metrics, PRECIPITATION_FIELDS)
    write_precipitation_summary(rain_metrics)

    (
        OUT / "model_diagnostics.json"
    ).write_text(
        json.dumps(
            model_report,
            indent=2
        ),
        encoding="utf-8"
    )

    print(
        json.dumps(
            report,
            indent=2
        )
    )


if __name__ == "__main__":
    validate()
