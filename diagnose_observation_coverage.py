
#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - Diagnostica copertura MeteoHub V2.

Legge gli archivi storici scaricati dal workflow.
Non modifica le osservazioni né le previsioni originali.
"""

import csv
import gzip
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OBS_ROOT = ROOT / "historical/observations"
FC_ROOT = ROOT / "historical/forecasts"
OUT = ROOT / "data/diagnostics"

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


def parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(
            str(value).strip().replace("Z", "+00:00")
        )
        if dt.tzinfo is None:
            return None
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def utc_string(value):
    return value.isoformat() if value else None


def load_observations():
    files = sorted(OBS_ROOT.rglob("observations_clean.csv"))
    if not files:
        raise RuntimeError(
            "Nessun archivio storico MeteoHub trovato"
        )

    latest = {}
    observed_hours = set()
    hourly_counts = Counter()
    diagnostics = Counter()

    for path in files:
        with path.open(
            newline="", encoding="utf-8-sig"
        ) as source:
            for row in csv.DictReader(source):
                diagnostics["rows_read"] += 1

                dt = parse_time(row.get("time_utc"))
                network = (row.get("network") or "").strip()
                station = (row.get("station_id") or "").strip()

                if not dt or not network or not station:
                    diagnostics["invalid_rows"] += 1
                    continue

                key = (network, station)
                if key not in latest or dt > latest[key]:
                    latest[key] = dt

                if dt.minute or dt.second or dt.microsecond:
                    diagnostics["non_hourly_rows"] += 1
                    continue

                hour_key = (network, station, dt)
                observed_hours.add(hour_key)
                hourly_counts[dt.isoformat()] += 1

    diagnostics["unique_station_hours"] = len(observed_hours)
    diagnostics["stations"] = len(latest)

    return (
        files, latest, observed_hours,
        hourly_counts, diagnostics
    )


def load_forecasts(now, latest, observed_hours):
    files = sorted(
        FC_ROOT.rglob("forecasts_meteohub_*.csv.gz")
    )
    if not files:
        raise RuntimeError(
            "Nessun archivio storico previsioni trovato"
        )

    counters = Counter()
    per_model = defaultdict(Counter)
    matched_hours = set()
    aifs_matured_hours = set()
    aifs_matched_hours = set()

    for path in files:
        with gzip.open(
            path, "rt", encoding="utf-8-sig", newline=""
        ) as source:
            for row in csv.DictReader(source):
                model = (row.get("model") or "").strip()

                if model not in OFFICIAL_MODELS:
                    counters["legacy_or_unknown_rows"] += 1
                    continue

                counters["official_forecast_rows"] += 1
                per_model[model]["rows"] += 1

                issued = parse_time(row.get("acquired_utc"))
                valid = parse_time(row.get("valid_time_utc"))

                if not issued or not valid or valid <= issued:
                    per_model[model]["invalid_time"] += 1
                    continue

                if valid.minute or valid.second or valid.microsecond:
                    per_model[model]["non_hourly"] += 1
                    continue

                if valid > now:
                    per_model[model]["future"] += 1
                    continue

                per_model[model]["matured"] += 1

                network = (row.get("network") or "").strip()
                station = (row.get("station_id") or "").strip()
                key = (network, station)
                event = (network, station, valid)

                if model == "ecmwf_aifs025_single":
                    aifs_matured_hours.add(event)

                if event in observed_hours:
                    per_model[model]["matched"] += 1
                    matched_hours.add(event)

                    if model == "ecmwf_aifs025_single":
                        aifs_matched_hours.add(event)

                elif key not in latest:
                    per_model[model]["station_not_observed"] += 1
                elif valid > latest[key]:
                    per_model[model]["observation_not_yet_available"] += 1
                else:
                    per_model[model]["observation_hour_missing"] += 1

    return {
        "files": len(files),
        "counters": dict(counters),
        "models": {
            model: dict(per_model[model])
            for model in sorted(OFFICIAL_MODELS)
        },
        "unique_matched_station_hours": len(matched_hours),
        "aifs_matured_station_hours": len(aifs_matured_hours),
        "aifs_matched_station_hours": len(aifs_matched_hours),
    }


def main():
    now = datetime.now(timezone.utc)

    (
        files, latest, observed_hours,
        hourly_counts, diagnostics
    ) = load_observations()

    latest_global = max(latest.values(), default=None)
    forecast = load_forecasts(now, latest, observed_hours)

    freshness = Counter()
    stations = []

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
            "latest_observation_utc": utc_string(dt),
            "age_hours": round(age, 2),
        })

    recent_hours = sorted(hourly_counts.keys())[-72:]

    report = {
        "version": "ENDURO MeteoHub Coverage V2",
        "generated_utc": utc_string(now),
        "observation_archives": len(files),
        "observation_diagnostics": dict(diagnostics),
        "latest_observation_utc": utc_string(latest_global),
        "hours_since_latest_observation": (
            round((now - latest_global).total_seconds() / 3600, 2)
            if latest_global else None
        ),
        "station_freshness": dict(freshness),
        "recent_hourly_coverage": [
            {
                "hour_utc": hour,
                "stations": hourly_counts[hour],
            }
            for hour in recent_hours
        ],
        "forecast_diagnostics": forecast,
        "stations": stations,
    }

    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / "observation_coverage_latest.json"

    target.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    summary = {
        key: value for key, value in report.items()
        if key not in ("stations", "recent_hourly_coverage")
    }

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print("Report completo:", target)


if __name__ == "__main__":
    main()
