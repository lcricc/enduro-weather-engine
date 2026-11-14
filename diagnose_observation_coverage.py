
#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - Observation coverage diagnostics."""

import csv
import gzip
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OBS = ROOT / "data/observations/noaa"
FC = ROOT / "data/forecasts/national"
OUT = ROOT / "data/diagnostics"


def parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(
            value.strip().replace("Z", "+00:00")
        )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def hour(dt):
    return dt.replace(
        minute=0, second=0, microsecond=0
    )


def main():
    now = datetime.now(timezone.utc)
    latest = {}
    observed_hours = set()
    observation_rows = 0
    invalid_observations = 0

    for path in sorted(OBS.glob("observations_*.csv")):
        with path.open(
            newline="", encoding="utf-8-sig"
        ) as f:
            for row in csv.DictReader(f):
                dt = parse_time(row.get("observed_utc"))
                network = (row.get("network") or "").strip()
                station = (row.get("station_id") or "").strip()

                if not dt or not network or not station:
                    invalid_observations += 1
                    continue

                key = (network, station)
                observation_rows += 1
                observed_hours.add((network, station, hour(dt)))

                if key not in latest or dt > latest[key]:
                    latest[key] = dt

    forecast_rows = 0
    matured = 0
    matched = 0
    future = 0
    missing = Counter()
    forecast_files = 0

    for path in sorted(FC.glob("forecasts_*.csv.gz")):
        forecast_files += 1

        with gzip.open(
            path, "rt", encoding="utf-8"
        ) as f:
            for row in csv.DictReader(f):
                forecast_rows += 1

                valid = parse_time(row.get("valid_time_utc"))
                issued = parse_time(row.get("acquired_utc"))

                if not valid or not issued or valid <= issued:
                    missing["invalid_forecast_time"] += 1
                    continue

                if valid > now:
                    future += 1
                    continue

                matured += 1
                network = (row.get("network") or "").strip()
                station = (row.get("station_id") or "").strip()
                key = (network, station)

                if (network, station, hour(valid)) in observed_hours:
                    matched += 1
                elif key not in latest:
                    missing["station_not_observed"] += 1
                elif hour(valid) > hour(latest[key]):
                    missing["observations_not_yet_available"] += 1
                else:
                    missing["observation_hour_missing"] += 1

    stations = []
    freshness = Counter()

    for (network, station), dt in sorted(latest.items()):
        age = (now - dt).total_seconds() / 3600

        if age <= 24:
            freshness["within_24h"] += 1
        elif age <= 72:
            freshness["24_to_72h"] += 1
        elif age <= 168:
            freshness["72h_to_7d"] += 1
        else:
            freshness["older_than_7d"] += 1

        stations.append({
            "network": network,
            "station_id": station,
            "latest_observation_utc": dt.isoformat(),
            "age_hours": round(age, 1),
        })

    report = {
        "generated_utc": now.isoformat(),
        "observation_rows": observation_rows,
        "invalid_observation_rows": invalid_observations,
        "stations_with_observations": len(latest),
        "station_freshness": dict(freshness),
        "forecast_files": forecast_files,
        "forecast_rows": forecast_rows,
        "forecast_hours_reached": matured,
        "future_forecast_rows": future,
        "matched_station_hours": matched,
        "unmatched_matured_reasons": dict(missing),
        "stations": stations,
    }

    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / "observation_coverage_latest.json"
    target.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(json.dumps({
        k: v for k, v in report.items()
        if k != "stations"
    }, indent=2))
    print("Report:", target)


if __name__ == "__main__":
    main()
