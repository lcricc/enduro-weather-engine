#!/usr/bin/env python3
"""Build a MeteoHub station catalog and a forecast list representing every region."""

import argparse
import csv
import gzip
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from prepare_meteohub_stations import FIELDS, OUTPUT
from validate_meteohub import PRECIPITATION_CODE, VARIABLES, load_approved, number, signature, timestamp

BOUNDARIES = Path(__file__).parent / "data/geography/istat-regioni-2026.geojson.gz"
ARCHIVE = Path(__file__).parent / "data/forecasts/national"
MAX_STATIONS = 450
PER_REGION = 12
RECENT = timedelta(hours=48)
CODES = (*VARIABLES, PRECIPITATION_CODE)
HOUR_FIELDS = ("temperature_hours", "wind_hours", "precipitation_hours")
CATALOG_FIELDS = [
    *FIELDS, "region", "last_observation_utc", *HOUR_FIELDS,
    "selected_for_forecasts",
]


def region_boundaries():
    with gzip.open(BOUNDARIES, "rt", encoding="utf-8") as source:
        regions = json.load(source)["features"]
    boundaries = {}
    for region in regions:
        name = region["properties"]["DEN_REG"].split("/")[0]
        geometry = region["geometry"]
        polygons = geometry["coordinates"]
        if geometry["type"] == "Polygon":
            polygons = [polygons]
        boundaries[name] = []
        for outer, *holes in polygons:
            lon, lat = zip(*outer)
            bounds = (min(lon), min(lat), max(lon), max(lat))
            boundaries[name].append((bounds, outer, holes))
    return boundaries


def inside_ring(lon, lat, ring):
    inside = False
    for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
        if (y0 > lat) != (y1 > lat) and lon < (x1 - x0) * (lat - y0) / (y1 - y0) + x0:
            inside = not inside
    return inside


def region_lookup(boundaries):
    @lru_cache(maxsize=None)
    def lookup(lon, lat):
        for name, polygons in boundaries.items():
            for (left, bottom, right, top), outer, holes in polygons:
                if (
                    left <= lon <= right and bottom <= lat <= top
                    and inside_ring(lon, lat, outer)
                    and not any(inside_ring(lon, lat, hole) for hole in holes)
                ):
                    return name
        return ""
    return lookup


def station_key(row):
    return row["network"], row["station_id"]


def read_inventory(path):
    if not path or not path.is_file():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as source:
        return {station_key(row): row for row in csv.DictReader(source)}


def update_catalog(files, catalog, approved, lookup, now):
    recent = defaultdict(lambda: defaultdict(set))
    @lru_cache(maxsize=4096)
    def approved_metadata(code, level, timerange):
        try:
            return signature(code, json.loads(level), json.loads(timerange)) in approved
        except (TypeError, ValueError):
            return False

    for path in files:
        with path.open(encoding="utf-8-sig", newline="") as source:
            for row in csv.DictReader(source):
                code = row["variable"]
                if code not in CODES:
                    continue
                value = number(row["value"])
                if value is None or (code == PRECIPITATION_CODE and value < 0):
                    continue
                if not approved_metadata(code, row["level"], row["timerange"]):
                    continue
                when = timestamp(row["time_utc"])
                lat, lon = number(row["latitude"]), number(row["longitude"])
                key = station_key(row)
                if not all(key) or when is None or when > now or lat is None or lon is None:
                    continue
                # Store real station coordinates even for sites on a coastline or across a border.
                if key not in catalog:
                    catalog[key] = {
                        **{field: row.get(field, "") for field in FIELDS},
                        "latitude": lat,
                        "longitude": lon,
                        "region": lookup(lon, lat),
                        "last_observation_utc": when.isoformat(),
                    }
                station = catalog[key]
                if abs(float(station["latitude"]) - lat) > 0.0001 or abs(float(station["longitude"]) - lon) > 0.0001:
                    raise ValueError(f"Conflicting coordinates in verified station {key}")
                if not station["station_name"]:
                    station["station_name"] = row["station_name"]
                station["last_observation_utc"] = max(station["last_observation_utc"], when.isoformat())
                if when >= now - RECENT:
                    recent[key][code].add(when.replace(minute=0, second=0, microsecond=0))

    for key, station in catalog.items():
        station.update({field: len(recent[key][code]) for field, code in zip(HOUR_FIELDS, CODES)})


def select_stations(catalog, legacy_keys, prior_keys, regions, now, max_stations=MAX_STATIONS):
    by_region = defaultdict(list)
    for key, station in catalog.items():
        if timestamp(station["last_observation_utc"]) >= now - RECENT:
            by_region[station["region"]].append(key)

    def rank(key):
        row = catalog[key]
        counts = [row[field] for field in HOUR_FIELDS]
        return (key not in prior_keys, key not in legacy_keys, -sum(n > 0 for n in counts), -min(counts), -sum(counts), key)

    selected = {}
    coordinates = set()

    def add(key):
        row = catalog[key]
        point = float(row["latitude"]), float(row["longitude"])
        if point in coordinates:
            return
        selected[key] = row
        coordinates.add(point)

    for name in regions:
        before = len(selected)
        for key in sorted(by_region[name], key=rank):
            add(key)
            if len(selected) - before == PER_REGION or len(selected) == max_stations:
                break
        if len(selected) == max_stations:
            return selected

    # Preserve active comparison points, including verified coastal sites present in the legacy list.
    for key in sorted((prior_keys | legacy_keys) & catalog.keys(), key=rank):
        if len(selected) == max_stations:
            break
        row = catalog[key]
        if timestamp(row["last_observation_utc"]) >= now - RECENT:
            add(key)

    # Fill the remaining budget evenly across regions with additional measurements.
    queues = {name: iter(sorted(by_region[name], key=rank)) for name in regions}
    while queues and len(selected) < max_stations:
        for name, queue in list(queues.items()):
            before = len(selected)
            for key in queue:
                add(key)
                if len(selected) > before:
                    break
            else:
                del queues[name]
            if len(selected) == max_stations:
                break
    return selected


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    parser.add_argument("--previous-catalog", type=Path)
    parser.add_argument("--additional-observations", type=Path)
    parser.add_argument("--max-stations", type=int, default=MAX_STATIONS)
    args = parser.parse_args()
    if args.max_stations < 1:
        parser.error("--max-stations must be positive")

    files = sorted(args.observations.rglob("observations_clean.csv"))
    if args.additional_observations and args.additional_observations.is_file():
        files.append(args.additional_observations)
    if not files:
        raise ValueError("No observation archives available")
    approved = load_approved()
    if not approved:
        raise ValueError("Approved observation metadata unavailable")
    catalog = read_inventory(args.previous_catalog)
    previous_keys = {key for key, row in catalog.items() if row["selected_for_forecasts"] == "1"}
    legacy_keys = set(read_inventory(OUTPUT))
    boundaries = region_boundaries()
    now = datetime.now(timezone.utc)
    update_catalog(files, catalog, approved, region_lookup(boundaries), now)
    selected = select_stations(catalog, legacy_keys, previous_keys, boundaries, now, args.max_stations)
    if not selected:
        raise ValueError("No stations with recently verified measurements")

    for key, row in catalog.items():
        row["selected_for_forecasts"] = int(key in selected)
    write_csv(OUTPUT, FIELDS, selected.values())
    write_csv(ARCHIVE / "meteohub_station_catalog.csv", CATALOG_FIELDS, (catalog[key] for key in sorted(catalog)))
    regional_counts = Counter(row["region"] for row in selected.values())
    report = {
        "generated_utc": now.isoformat(),
        "observation_archives": sorted({part for path in files for part in path.parts if re.fullmatch(r"meteohub-observations-\d+", part)}),
        "catalog_station_ids": len(catalog),
        "catalog_positions": len({(float(row["latitude"]), float(row["longitude"])) for row in catalog.values()}),
        "selected_positions": len(selected),
        "regions": {region: regional_counts[region] for region in boundaries},
        "other_selected_positions": regional_counts[""],
        "selected_with_previous_forecasts": len(set(selected) & (legacy_keys | previous_keys)),
        "observation_lookback_hours": int(RECENT.total_seconds() / 3600),
        "source": "MeteoHub observations with approved metadata; regional boundaries from Istat 2026",
    }
    (ARCHIVE / "meteohub_station_selection.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
