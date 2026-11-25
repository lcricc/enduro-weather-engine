"""Freeze ENDURO rain decisions at acquisition and score only previously published forecasts."""
import csv
import gzip
import json
import math
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

CONFIG = json.loads(Path(__file__).with_name("rain-v01.json").read_text())
BASELINE = json.loads(Path(__file__).with_name("rain-baseline-v01.json").read_text())
UTC = timezone.utc
ROME = ZoneInfo("Europe/Rome")
HOUR = timedelta(hours=1)


def write_issue(source, acquired, destination):
    bits = {m: 1 << i for i, m in enumerate(CONFIG["models"])}
    groups = {}
    parse = lru_cache(maxsize=256)(datetime.fromisoformat)
    with gzip.open(source, "rt", newline="") as src:
        reader = csv.reader(src)
        header = next(reader)
        network, station, model, end, rain = map(header.index, (
            "network", "station_id", "model", "valid_time_utc", "precipitation_mm"))
        for row in reader:
            start = parse(row[end]) - HOUR
            if not (acquired < start and start + HOUR <= acquired + 24 * HOUR):
                continue
            bit = bits.get(row[model])
            if bit is None:
                continue
            key = row[network], row[station], start
            entry = groups.setdefault(key, [0, 0, True])
            if entry[0] & bit:
                entry[2] = False
            entry[0] |= bit
            try:
                value = float(row[rain])
            except ValueError:
                entry[2] = False
                continue
            if not math.isfinite(value) or value < 0:
                entry[2] = False
            elif value >= CONFIG["threshold_mm"]:
                entry[1] += 1
    recorded = datetime.now(UTC)
    predictions = []
    for (network, station, start), (mask, votes, valid) in groups.items():
        if mask == 255 and valid and recorded < start:
            p = (CONFIG["wet"][votes] + 1) / (CONFIG["total"][votes] + 2)
            predictions.append([network, station, int(start.timestamp()), votes, p, votes >= 5, votes >= 6])
    payload = {"version": CONFIG["version"], "calibration_id": CONFIG.get("calibration", {}).get("id"), "acquired_utc": acquired.isoformat(),
               "recorded_utc": recorded.isoformat(), "predictions": predictions}
    temp = destination.with_suffix(".tmp")
    with gzip.open(temp, "wt", encoding="utf-8") as out:
        json.dump(payload, out, separators=(",", ":"), allow_nan=False)
    temp.replace(destination)
    return len(predictions)


def scores(pairs):
    result = {"station_hours": len(pairs), "brier": None, "brier_reference_v01": None}
    if pairs:
        result["brier"] = sum((row[0]-row[3])**2 for row in pairs)/len(pairs)
        result["brier_reference_v01"] = sum((row[4]-row[3])**2 for row in pairs)/len(pairs)
    for index, name in ((1, "5_of_8"), (2, "6_of_8")):
        hits = sum(bool(row[index]) and row[3] for row in pairs)
        misses = sum(not row[index] and row[3] for row in pairs)
        false = sum(bool(row[index]) and not row[3] for row in pairs)
        denom = hits+misses+false
        result[name] = {"hits": hits, "misses": misses, "false_alarms": false,
                        "csi": hits/denom if denom else None}
    return result


def validate_issues(root, observations, variable, now):
    best = {}
    seen = 0
    for path in sorted(root.glob("meteohub-forecasts-*/**/enduro_rain_v01_*.json.gz")):
        release = next(p for p in path.parents if p.parent == root)
        published = datetime.fromisoformat((release/"published_at.txt").read_text().strip().replace("Z", "+00:00"))
        with gzip.open(path, "rt", encoding="utf-8") as src:
            issue = json.load(src)
        if issue["version"] != CONFIG["version"]:
            continue
        seen += 1
        acquired = datetime.fromisoformat(issue["acquired_utc"])
        available = max(acquired, published, datetime.fromisoformat(issue["recorded_utc"]))
        for network, station, epoch, votes, probability, signal5, signal6 in issue["predictions"]:
            start = datetime.fromtimestamp(epoch, UTC)
            if not available < start <= acquired + 24 * HOUR or start + HOUR > now:
                continue
            key = network, station, start + HOUR, variable
            previous = best.get(key)
            if previous is None or acquired > previous[0]:
                best[key] = (acquired, probability, signal5, signal6, votes, issue.get("calibration_id") or "v0.1-initial")

    daily = {}
    by_calibration = {}
    all_pairs = []
    for key, (_, p, signal5, signal6, votes, calibration_id) in best.items():
        if key not in observations:
            continue
        wet = observations[key] >= CONFIG["threshold_mm"]
        reference = (BASELINE["wet"][votes]+1)/(BASELINE["total"][votes]+2)
        pair = (p, signal5, signal6, wet, reference)
        day = (key[2]-HOUR).astimezone(ROME).date().isoformat()
        daily.setdefault(day, []).append(pair)
        by_calibration.setdefault(calibration_id, []).append(pair)
        all_pairs.append(pair)
    return {"version": CONFIG["version"], "archived_cycles": seen,
            "elapsed_station_hours": len(best), "awaiting_measurement": len(best)-len(all_pairs),
            "by_calibration": {key: scores(rows) for key, rows in by_calibration.items()},
            "overall": scores(all_pairs), "days": {day: scores(daily[day]) for day in sorted(daily)}}
