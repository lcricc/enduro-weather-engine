
#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - Pulizia osservazioni MeteoHub.

Legge tutti i file request_*.jsonl.
Produce osservazioni deduplicate e rapporto qualità.
Non modifica mai i file originali.
"""

import csv
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("data/observations/meteohub")
OUTPUT = ROOT / "clean"

METADATA = {
    "B01019", "B01194",
    "B04001", "B04002", "B04003",
    "B04004", "B04005", "B04006",
    "B05001", "B06001",
    "B07030", "B07031",
}

FIELDS = [
    "network",
    "station_id",
    "station_name",
    "latitude",
    "longitude",
    "elevation_m",
    "time_utc",
    "variable",
    "timerange",
    "level",
    "value",
    "unit",
    "quality",
]


def number(value):
    try:
        if value is None or isinstance(value, bool):
            return None

        result = float(value)

        if not math.isfinite(result):
            return None

        return result

    except (TypeError, ValueError):
        return None


def utc_time(value):
    if not isinstance(value, str):
        return None

    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            return None

        return parsed.astimezone(
            timezone.utc
        ).isoformat()

    except ValueError:
        return None


def extract_metadata(record):
    result = {}

    groups = record.get("data", [])

    if not isinstance(groups, list):
        return result

    for group in groups:
        if not isinstance(group, dict):
            continue

        variables = group.get("vars", {})

        if not isinstance(variables, dict):
            continue

        for code, entry in variables.items():
            if (
                code in METADATA
                and isinstance(entry, dict)
            ):
                result.setdefault(
                    code,
                    entry.get("v")
                )

    return result


def station_info(record, meta):
    lat = number(meta.get("B05001"))
    lon = number(meta.get("B06001"))

    if lat is None or lon is None:
        raw_lat = number(record.get("lat"))
        raw_lon = number(record.get("lon"))

        if (
            raw_lat is not None
            and raw_lon is not None
        ):
            lat = raw_lat / 100000
            lon = raw_lon / 100000

    if lat is None or lon is None:
        return None

    if not (
        35 <= lat <= 48
        and 6 <= lon <= 19
    ):
        return None

    network = str(
        record.get("network") or ""
    ).strip()

    if not network:
        return None

    name = str(
        meta.get("B01019") or ""
    ).strip()

    identifier = record.get("ident")

    if (
        identifier is None
        or str(identifier).strip() == ""
    ):
        identifier = (
            f"{lat:.5f}_{lon:.5f}"
        )
    else:
        identifier = str(identifier).strip()

    return {
        "network": network,
        "station_id": identifier,
        "station_name": name,
        "latitude": round(lat, 6),
        "longitude": round(lon, 6),
        "elevation_m": number(
            meta.get("B07030")
        ),
    }


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def main():
    OUTPUT.mkdir(
        parents=True,
        exist_ok=True
    )

    files = sorted(
        ROOT.glob("request_*.jsonl")
    )

    if not files:
        raise RuntimeError(
            "Nessun file MeteoHub trovato"
        )

    observations = {}
    counters = Counter()

    networks = set()
    stations = set()
    variables_found = set()

    for path in files:
        print("Lettura:", path.name)

        with path.open(
            encoding="utf-8-sig"
        ) as source:

            for line in source:
                if not line.strip():
                    continue

                counters["records_read"] += 1

                try:
                    record = json.loads(line)

                except json.JSONDecodeError:
                    counters["invalid_json"] += 1
                    continue

                if not isinstance(record, dict):
                    counters["invalid_structure"] += 1
                    continue

                meta = extract_metadata(record)

                station = station_info(
                    record,
                    meta
                )

                timestamp = utc_time(
                    record.get("date")
                )

                if (
                    station is None
                    or timestamp is None
                ):
                    counters[
                        "invalid_station_or_time"
                    ] += 1
                    continue

                networks.add(
                    station["network"]
                )

                stations.add((
                    station["network"],
                    station["station_id"],
                ))

                groups = record.get(
                    "data", []
                )

                if not isinstance(groups, list):
                    counters[
                        "invalid_structure"
                    ] += 1
                    continue

                for group in groups:
                    if not isinstance(group, dict):
                        continue

                    vars_dict = group.get(
                        "vars", {}
                    )

                    if not isinstance(
                        vars_dict, dict
                    ):
                        continue

                    timerange = canonical(
                        group.get("timerange")
                    )

                    level = canonical(
                        group.get("level")
                    )

                    for code, entry in vars_dict.items():
                        if code in METADATA:
                            continue

                        counters[
                            "measurements_read"
                        ] += 1

                        if not isinstance(entry, dict):
                            counters[
                                "invalid_value"
                            ] += 1
                            continue

                        value = number(
                            entry.get("v")
                        )

                        if value is None:
                            counters[
                                "invalid_value"
                            ] += 1
                            continue

                        unit = entry.get("unit")
                        quality = entry.get("q")

                        key = (
                            station["network"],
                            station["station_id"],
                            timestamp,
                            code,
                            timerange,
                            level,
                        )

                        row = {
                            **station,
                            "time_utc": timestamp,
                            "variable": code,
                            "timerange": timerange,
                            "level": level,
                            "value": value,
                            "unit": (
                                str(unit)
                                if unit is not None
                                else ""
                            ),
                            "quality": (
                                canonical(quality)
                                if quality is not None
                                else ""
                            ),
                        }

                        if key in observations:
                            previous = observations[key]

                            if previous is None:
                                # Conflitto già individuato.
                                counters[
                                    "conflicts"
                                ] += 1

                            elif previous["value"] == value:
                                counters[
                                    "duplicates"
                                ] += 1

                            else:
                                counters[
                                    "conflicts"
                                ] += 1

                                # Esclude la misura discordante.
                                observations[key] = None

                        else:
                            observations[key] = row

                        variables_found.add(code)

    clean = [
        row
        for row in observations.values()
        if row is not None
    ]

    clean.sort(
        key=lambda row: (
            row["network"],
            row["station_id"],
            row["time_utc"],
            row["variable"],
            row["timerange"],
            row["level"],
        )
    )

    csv_path = (
        OUTPUT / "observations_clean.csv"
    )

    with csv_path.open(
        "w",
        newline="",
        encoding="utf-8"
    ) as destination:

        writer = csv.DictWriter(
            destination,
            fieldnames=FIELDS
        )

        writer.writeheader()
        writer.writerows(clean)

    report = {
        "generated_utc": datetime.now(
            timezone.utc
        ).isoformat(),

        "source_files": len(files),

        "records_read": counters[
            "records_read"
        ],

        "measurements_read": counters[
            "measurements_read"
        ],

        "measurements_clean": len(clean),

        "duplicates_removed": counters[
            "duplicates"
        ],

        "conflicts_detected": counters[
            "conflicts"
        ],

        "invalid_values": counters[
            "invalid_value"
        ],

        "invalid_json": counters[
            "invalid_json"
        ],

        "invalid_structure": counters[
            "invalid_structure"
        ],

        "invalid_station_or_time": counters[
            "invalid_station_or_time"
        ],

        "networks": sorted(networks),

        "stations_found": len(stations),

        "variables": sorted(
            variables_found
        ),

        "validation_ready": (
            counters["conflicts"] == 0
        ),
    }

    report_path = (
        OUTPUT / "quality_report.json"
    )

    report_path.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    print(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False
        )
    )

    if not clean:
        raise RuntimeError(
            "Nessuna osservazione valida prodotta"
        )

    print(
        "METEOHUB CLEAN: COMPLETATO"
    )


if __name__ == "__main__":
    main()
