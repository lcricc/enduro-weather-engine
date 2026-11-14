
#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - MeteoHub metadata analyzer.

Audits BUFR metadata without automatically approving observations.
Standard-library only; compatible with Python 3.11.
"""

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

SOURCE = Path(
    "data/observations/meteohub/clean/observations_clean.csv"
)
OUTPUT = Path("data/validation/meteohub/metadata")

TARGET_CODES = {
    "B12101": "air_temperature",
    "B11002": "wind_speed",
}

MAX_EXAMPLES = 3


def parse_metadata(value):
    if not value or not value.strip():
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return {"_unparsed": value}


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def analyze():
    if not SOURCE.is_file():
        raise FileNotFoundError(
            f"Observation file not found: {SOURCE}"
        )

    OUTPUT.mkdir(parents=True, exist_ok=True)

    combinations = defaultdict(
        lambda: {
            "count": 0,
            "networks": Counter(),
            "stations": set(),
            "examples": [],
            "hourly_exact": 0,
            "non_hourly": 0,
        }
    )

    codes = Counter()
    units = defaultdict(Counter)
    networks = Counter()
    total = 0
    target_rows = 0

    with SOURCE.open(
        "r", encoding="utf-8-sig", newline=""
    ) as file:
        reader = csv.DictReader(file)

        for row in reader:
            total += 1
            code = (row.get("variable") or "").strip()
            codes[code] += 1

            if code not in TARGET_CODES:
                continue

            target_rows += 1

            unit = (row.get("unit") or "").strip()
            network = (row.get("network") or "").strip()
            station = (row.get("station_id") or "").strip()

            level = parse_metadata(row.get("level"))
            timerange = parse_metadata(row.get("timerange"))

            units[code][unit] += 1
            networks[network] += 1

            signature = {
                "code": code,
                "level": level,
                "timerange": timerange,
            }

            key = canonical(signature)
            item = combinations[key]

            item["count"] += 1
            item["networks"][network] += 1
            item["stations"].add((network, station))

            timestamp = (row.get("time_utc") or "").strip()

            try:
                from datetime import datetime
                dt = datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")
                )
                exact = (
                    dt.tzinfo is not None
                    and dt.minute == 0
                    and dt.second == 0
                    and dt.microsecond == 0
                )
            except ValueError:
                exact = False

            if exact:
                item["hourly_exact"] += 1
            else:
                item["non_hourly"] += 1

            if len(item["examples"]) < MAX_EXAMPLES:
                item["examples"].append({
                    "network": network,
                    "station_id": station,
                    "time_utc": timestamp,
                    "value": row.get("value"),
                    "unit": unit,
                    "quality": row.get("quality"),
                })

    ranked = sorted(
        combinations.items(),
        key=lambda pair: pair[1]["count"],
        reverse=True,
    )

    results = []

    for key, item in ranked:
        signature = json.loads(key)

        results.append({
            "signature": signature,
            "variable_name": TARGET_CODES[
                signature["code"]
            ],
            "measurements": item["count"],
            "station_count": len(item["stations"]),
            "hourly_exact": item["hourly_exact"],
            "non_hourly": item["non_hourly"],
            "networks": dict(item["networks"]),
            "examples": item["examples"],
            "review_status": "REQUIRES_MANUAL_REVIEW",
        })

    report = {
        "engine": "ENDURO WEATHER ENGINE",
        "analysis": "MeteoHub BUFR metadata audit",
        "total_observations": total,
        "target_observations": target_rows,
        "target_codes": TARGET_CODES,
        "all_variable_counts": dict(codes),
        "target_units": {
            code: dict(counter)
            for code, counter in units.items()
        },
        "target_networks": dict(networks),
        "metadata_combinations": results,
        "scientifically_approved": 0,
        "warning": (
            "This report does not approve BUFR metadata. "
            "Sensor heights, timeranges, units and quality "
            "must be checked before validation."
        ),
    }

    report_path = OUTPUT / "metadata_report.json"
    report_path.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    csv_path = OUTPUT / "metadata_summary.csv"

    with csv_path.open(
        "w", encoding="utf-8", newline=""
    ) as file:
        writer = csv.writer(file)
        writer.writerow([
            "code",
            "variable",
            "measurements",
            "stations",
            "hourly_exact",
            "non_hourly",
            "level_json",
            "timerange_json",
            "review_status",
        ])

        for item in results:
            sig = item["signature"]

            writer.writerow([
                sig["code"],
                item["variable_name"],
                item["measurements"],
                item["station_count"],
                item["hourly_exact"],
                item["non_hourly"],
                canonical(sig["level"]),
                canonical(sig["timerange"]),
                item["review_status"],
            ])

    print("ENDURO MeteoHub metadata analysis completed")
    print(f"Observations examined: {total}")
    print(f"Target observations: {target_rows}")
    print(f"Metadata combinations: {len(results)}")
    print("Scientifically approved: 0")

    for item in results[:10]:
        sig = item["signature"]
        print(
            f"{sig['code']} | "
            f"count={item['measurements']} | "
            f"stations={item['station_count']} | "
            f"hourly={item['hourly_exact']} | "
            f"level={canonical(sig['level'])} | "
            f"timerange={canonical(sig['timerange'])}"
        )

    print(f"Report: {report_path}")
    print(f"Summary: {csv_path}")


if __name__ == "__main__":
    analyze()
