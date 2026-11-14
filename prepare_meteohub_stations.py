
#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - Inventario stazioni MeteoHub."""

import csv
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = (
    ROOT / "data/observations/meteohub"
    / "clean/observations_clean.csv"
)
OUTPUT = (
    ROOT / "data/observations/meteohub"
    / "stations_meteohub.csv"
)
REPORT = (
    ROOT / "data/observations/meteohub"
    / "stations_report.json"
)

FIELDS = [
    "network",
    "station_id",
    "station_name",
    "latitude",
    "longitude",
    "elevation_m",
]


def main():
    if not SOURCE.exists():
        raise FileNotFoundError(
            "Osservazioni MeteoHub non disponibili: "
            + str(SOURCE)
        )

    stations = {}
    variables = Counter()
    networks = Counter()
    conflicts = 0
    invalid = 0

    with SOURCE.open(
        encoding="utf-8-sig",
        newline=""
    ) as file:
        for row in csv.DictReader(file):
            network = (row.get("network") or "").strip()
            station_id = (
                row.get("station_id") or ""
            ).strip()

            try:
                lat = float(row["latitude"])
                lon = float(row["longitude"])
            except (TypeError, ValueError, KeyError):
                invalid += 1
                continue

            if not (
                network and station_id
                and 35 <= lat <= 48
                and 6 <= lon <= 19
            ):
                invalid += 1
                continue

            key = (network, station_id)

            station = {
                "network": network,
                "station_id": station_id,
                "station_name": row.get(
                    "station_name", ""
                ),
                "latitude": lat,
                "longitude": lon,
                "elevation_m": row.get(
                    "elevation_m", ""
                ),
            }

            if key in stations:
                previous = stations[key]

                if (
                    abs(previous["latitude"] - lat) > 0.001
                    or abs(previous["longitude"] - lon) > 0.001
                ):
                    conflicts += 1
                    continue
            else:
                stations[key] = station
                networks[network] += 1

            variable = row.get("variable", "")
            if variable:
                variables[variable] += 1

    OUTPUT.parent.mkdir(
        parents=True, exist_ok=True
    )

    with OUTPUT.open(
        "w", encoding="utf-8", newline=""
    ) as file:
        writer = csv.DictWriter(
            file, fieldnames=FIELDS
        )
        writer.writeheader()
        writer.writerows(
            stations[key] for key in sorted(stations)
        )

    report = {
        "stations": len(stations),
        "networks": dict(sorted(networks.items())),
        "variables": dict(sorted(variables.items())),
        "invalid_rows": invalid,
        "coordinate_conflicts": conflicts,
        "station_file": str(
            OUTPUT.relative_to(ROOT)
        ),
    }

    REPORT.write_text(
        json.dumps(
            report, indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8",
    )

    print(json.dumps(
        report, indent=2,
        ensure_ascii=False
    ))

    if not stations:
        raise RuntimeError(
            "Nessuna stazione valida trovata"
        )


if __name__ == "__main__":
    main()
