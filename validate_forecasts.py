
#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - Forecast validation and diagnostics."""

import csv
import gzip
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OBS = ROOT / "data/observations/noaa"
FC = ROOT / "data/forecasts/national"
OUT = ROOT / "data/validation"

VARIABLES = ("temperature_2m_c", "wind_speed_10m_kmh")
MAX_EXAMPLES = 5


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return None


def hour_key(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def observations():
    sums = defaultdict(lambda: [0.0, 0, 0.0, 0])
    files = sorted(OBS.glob("observations_*.csv"))
    stats = Counter()
    examples = defaultdict(list)

    for path in files:
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)

            for row in reader:
                stats["rows_scanned"] += 1
                dt = timestamp(row.get("observed_utc"))
                station = (row.get("station_id") or "").strip()

                if dt is None:
                    stats["invalid_timestamp"] += 1
                    continue

                if not station:
                    stats["missing_station_id"] += 1
                    continue

                key = (
                    (row.get("network") or "").strip(),
                    station,
                    hour_key(dt),
                )
                a = sums[key]
                stats["valid_rows"] += 1

                for j, variable in enumerate(VARIABLES):
                    val = number(row.get(variable))
                    if val is not None:
                        a[2 * j] += val
                        a[2 * j + 1] += 1

    data = {
        k: tuple(
            a[2 * j] / a[2 * j + 1]
            if a[2 * j + 1] else None
            for j in range(len(VARIABLES))
        )
        for k, a in sums.items()
    }

    return data, len(files), dict(stats)


def forecasts():
    files = sorted(FC.glob("forecasts_*.csv.gz"))

    for path in files:
        with gzip.open(
            path, "rt", encoding="utf-8", newline=""
        ) as f:
            yield from csv.DictReader(f)


def lead_bucket(hours):
    if hours is None or hours < 0:
        return None
    if hours <= 24:
        return "0-24h"
    if hours <= 48:
        return "24-48h"
    if hours <= 72:
        return "48-72h"
    if hours <= 96:
        return "72-96h"
    return None


def add_example(examples, reason, row, valid):
    if len(examples[reason]) >= MAX_EXAMPLES:
        return

    examples[reason].append({
        "model": row.get("model"),
        "network": row.get("network"),
        "station_id": row.get("station_id"),
        "valid_time_utc": (
            valid.isoformat() if valid else
            row.get("valid_time_utc")
        ),
        "acquired_utc": row.get("acquired_utc"),
    })


def diagnose(key, valid, stations, networks, hours, all_hours):
    network, station, target_hour = key

    if station not in stations:
        return "station_not_in_observations"

    if (network, station) not in networks:
        return "network_mismatch"

    station_hours = hours[(network, station)]

    if target_hour in station_hours:
        return "unexpected_key_mismatch"

    if not station_hours:
        return "station_without_valid_hours"

    nearest = min(
        station_hours,
        key=lambda h: abs((h - target_hour).total_seconds())
    )

    difference = abs(
        (nearest - target_hour).total_seconds()
    ) / 3600

    if difference <= 1:
        return "hour_mismatch_within_1h"

    if difference <= 3:
        return "hour_mismatch_within_3h"

    if target_hour in all_hours:
        return "station_time_not_available"

    first = min(station_hours)
    last = max(station_hours)

    if target_hour < first:
        return "forecast_before_station_observations"

    if target_hour > last:
        return "forecast_after_station_observations"

    return "observation_time_gap"


def main():
    obs, obs_files, obs_stats = observations()

    metrics = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    diagnostics = Counter()
    examples = defaultdict(list)

    stations = set()
    networks = set()
    hours = defaultdict(set)
    all_hours = set()

    for network, station, hour in obs:
        stations.add(station)
        networks.add((network, station))
        hours[(network, station)].add(hour)
        all_hours.add(hour)

    scanned = 0
    matched = 0

    for row in forecasts():
        scanned += 1

        valid = timestamp(row.get("valid_time_utc"))
        issued = timestamp(row.get("acquired_utc"))

        if valid is None:
            reason = "invalid_forecast_valid_time"
            diagnostics[reason] += 1
            add_example(examples, reason, row, valid)
            continue

        if issued is None:
            reason = "invalid_forecast_acquired_time"
            diagnostics[reason] += 1
            add_example(examples, reason, row, valid)
            continue

        if valid <= issued:
            reason = "non_positive_forecast_lead"
            diagnostics[reason] += 1
            add_example(examples, reason, row, valid)
            continue

        lead = (valid - issued).total_seconds() / 3600
        bucket = lead_bucket(lead)

        if bucket is None:
            reason = "forecast_lead_over_96h"
            diagnostics[reason] += 1
            add_example(examples, reason, row, valid)
            continue

        key = (
            (row.get("network") or "").strip(),
            (row.get("station_id") or "").strip(),
            hour_key(valid),
        )

        observed = obs.get(key)

        if observed is None:
            reason = diagnose(
                key, valid, stations, networks, hours, all_hours
            )
            diagnostics[reason] += 1
            add_example(examples, reason, row, valid)
            continue

        matched += 1
        diagnostics["matched_rows"] += 1

        valid_variables = 0

        for idx, variable in enumerate(VARIABLES):
            pred = number(row.get(variable))
            actual = observed[idx]

            if pred is None:
                diagnostics["missing_forecast_" + variable] += 1
                continue

            if actual is None:
                diagnostics["missing_observation_" + variable] += 1
                continue

            error = pred - actual
            a = metrics[(row.get("model", ""), variable, bucket)]

            a[0] += 1
            a[1] += abs(error)
            a[2] += error * error
            a[3] += error
            valid_variables += 1

        if valid_variables == 0:
            diagnostics["matched_without_usable_values"] += 1

    results = []

    for (model, variable, bucket), values in sorted(metrics.items()):
        n, absolute, squared, signed = values

        results.append({
            "model": model,
            "variable": variable,
            "lead_bucket": bucket,
            "count": n,
            "mae": round(absolute / n, 3),
            "rmse": round(math.sqrt(squared / n), 3),
            "bias": round(signed / n, 3),
        })

    forecast_files = len(
        list(FC.glob("forecasts_*.csv.gz"))
    )

    unmatched = scanned - matched
    excluded_before_matching = sum(
        diagnostics[k] for k in (
            "invalid_forecast_valid_time",
            "invalid_forecast_acquired_time",
            "non_positive_forecast_lead",
            "forecast_lead_over_96h",
        )
    )

    missing_pairs = unmatched - excluded_before_matching

    if obs_files == 0:
        primary_cause = "NO_OBSERVATION_FILES"
    elif forecast_files == 0:
        primary_cause = "NO_FORECAST_FILES"
    elif not obs:
        primary_cause = "NO_VALID_OBSERVATIONS"
    elif scanned == 0:
        primary_cause = "NO_FORECAST_ROWS"
    elif matched == 0:
        causes = {
            k: v for k, v in diagnostics.items()
            if k != "matched_rows"
        }
        primary_cause = (
            max(causes, key=causes.get)
            if causes else "UNKNOWN"
        )
    else:
        primary_cause = "MATCHES_FOUND"

    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "observation_files": obs_files,
        "observation_station_hours": len(obs),
        "forecast_rows_scanned": scanned,
        "matched_forecast_rows": matched,
        "metrics": results,
        "diagnostics": {
            "forecast_files": forecast_files,
            "observation_quality": obs_stats,
            "unmatched_forecast_rows": unmatched,
            "excluded_before_matching": excluded_before_matching,
            "missing_station_hour_pairs": missing_pairs,
            "match_rate_percent": (
                round(100 * matched / scanned, 2)
                if scanned else 0.0
            ),
            "primary_cause": primary_cause,
            "reason_counts": dict(sorted(diagnostics.items())),
            "examples": dict(examples),
        },
        "notes": [
            "Observation samples aggregated to UTC clock hours",
            "Forecasts compared only with matching station IDs and valid UTC hours",
            "Metrics are descriptive, not yet a fair common-sample model ranking",
            "Diagnostics do not alter matching or forecast scores",
            "Timestamp offsets are normalized to UTC",
            "Naive timestamps are assumed UTC, as in the original validator",
            "Time differences are diagnostic only; no fuzzy matching is performed",
        ],
    }

    OUT.mkdir(parents=True, exist_ok=True)

    output = OUT / "validation_latest.json"
    output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("\n" + "=" * 65)
    print("ENDURO WEATHER ENGINE - VALIDATION DIAGNOSTICS")
    print("=" * 65)

    print("Observation files:", obs_files)
    print("Forecast files:", forecast_files)
    print("Observation station-hours:", len(obs))
    print("Forecast rows:", scanned)
    print("Matched rows:", matched)
    print("Unmatched rows:", unmatched)
    print("Match rate:", report["diagnostics"]["match_rate_percent"], "%")

    print("\nPRIMARY DIAGNOSTIC:", primary_cause)

    print("\nREASON COUNTS:")
    for reason, count in sorted(
        diagnostics.items(),
        key=lambda item: (-item[1], item[0])
    ):
        print(f"  {reason}: {count}")

    print("\nOBSERVATION QUALITY:")
    for reason, count in sorted(obs_stats.items()):
        print(f"  {reason}: {count}")

    print("\nEXAMPLES:")
    for reason, samples in examples.items():
        print(f"\n  {reason}")
        for sample in samples:
            print("   ", json.dumps(sample, ensure_ascii=False))

    print("\nMetric groups:", len(results))
    print("Report:", output)

    if not results:
        print(
            "\nNo usable forecast/observation pairs. "
            "Review diagnostics before assuming more data is needed."
        )

    print("=" * 65)


if __name__ == "__main__":
    main()
