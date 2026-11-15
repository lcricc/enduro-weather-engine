"""Plan and request missing precipitation intervals for comparable station-hours."""

import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

from meteohub_request_all import api, login, utc_string

HOUR = timedelta(hours=1)
PUBLICATION_DELAY = timedelta(minutes=45)
LOOKBACK = timedelta(hours=24)
REQUESTS_PER_COLLECTION = 3
BOUND_MARGIN = 0.0002


def missing_ends(hour_end, intervals):
    """Infer missing endpoints only from at least two aligned, equal intervals."""
    if len(intervals) < 2 or any(value is None for value in intervals.values()):
        return ()
    durations = {end - start for start, end in intervals}
    if len(durations) != 1:
        return ()
    duration = next(iter(durations))
    if duration <= timedelta(0) or HOUR % duration:
        return ()

    count = HOUR // duration
    expected = {
        (hour_end - (count - index + 1) * duration,
         hour_end - (count - index) * duration)
        for index in range(1, count + 1)
    }
    if not intervals.keys() <= expected:
        return ()
    return tuple(sorted(end for _, end in expected - intervals.keys()))


def recovery_plan(gaps, forecast_counts, coordinates, verified_stations, models):
    eligible = {
        key for (_, key), counts in forecast_counts.items()
        if counts.keys() == models and all(count == 1 for count in counts.values())
    }
    requests = defaultdict(list)
    hours = []
    for key in sorted(eligible):
        network, station, valid, _ = key
        if (network, station) not in verified_stations or key not in coordinates:
            continue
        ends = gaps[key]
        hours.append({
            "network": network,
            "station_id": station,
            "hour_end_utc": valid.isoformat(),
            "missing_measurement_ends_utc": [end.isoformat() for end in ends],
        })
        for end in ends:
            requests[(network, end)].append((key, len(ends)))

    targets = []
    for (network, end), items in requests.items():
        points = [coordinates[key] for key, _ in items]
        latitudes, longitudes = zip(*points)
        bbox = {
            "latmin": max(-90, min(latitudes) - BOUND_MARGIN),
            "latmax": min(90, max(latitudes) + BOUND_MARGIN),
            "lonmin": max(-180, min(longitudes) - BOUND_MARGIN),
            "lonmax": min(180, max(longitudes) + BOUND_MARGIN),
        }
        targets.append({
            "network": network,
            "measurement_end_utc": end.isoformat(),
            "bounding_box": bbox,
            "station_count": len(items),
            "single_gap_hours": sum(count == 1 for _, count in items),
        })
    targets.sort(key=lambda target: (
        -target["single_gap_hours"],
        -target["station_count"],
        -datetime.fromisoformat(target["measurement_end_utc"]).timestamp(),
        target["network"],
    ))
    return {"station_hours": hours, "requests": targets}


def submit(plan, now):
    targets = [
        target for target in plan["requests"]
        if now - LOOKBACK <= datetime.fromisoformat(target["measurement_end_utc"]) <= now
    ]
    print(f"Recupero pioggia: {len(targets)} finestre disponibili.")
    if not targets:
        return
    token = login()
    quota = api("/api/hourly", token)
    budget = min(REQUESTS_PER_COLLECTION, quota["remaining"])
    if budget <= 0:
        print("Recupero pioggia rinviato: quota oraria esaurita.")
        return
    usage = api("/api/usage", token)
    if usage["used"] >= usage["quota"]:
        print("Recupero pioggia rinviato: quota disco esaurita.")
        return
    previous = api("/api/requests", token)
    generated = datetime.fromisoformat(plan["generated_utc"])
    slot = f"{now:%Y%m%d}-{now.hour // 6}"

    attempts = 0
    for target in targets:
        if attempts >= budget:
            break
        network = target["network"]
        end = datetime.fromisoformat(target["measurement_end_utc"])
        prefix = f"enduro-rain-gap-{network}-{end:%Y%m%dT%H%M%SZ}-"
        name = prefix + slot
        if any(already_requested(item, prefix, name, generated) for item in previous):
            continue
        attempts += 1
        try:
            response = api("/api/data", token, {
                "request_name": name,
                "dataset_names": [network],
                "reftime": {
                    "from": utc_string(end - timedelta(seconds=1)),
                    "to": utc_string(end + timedelta(seconds=1)),
                },
                "filters": {"bounding_box": target["bounding_box"]},
                "output_format": "json",
                "only_reliable": True,
            })
        except HTTPError as error:
            print(f"Recupero pioggia {network} {end.isoformat()}: HTTP {error.code}")
            if error.code in {401, 429}:
                break
            continue
        print(f"Recupero pioggia {network} {end.isoformat()}: ID {response.get('request_id')}")


def already_requested(item, prefix, name, generated):
    existing = item.get("name") or item.get("request_name") or ""
    if existing == name:
        return True
    if not existing.startswith(prefix):
        return False
    status = item.get("status")
    if status in {"PENDING", "STARTED", "RETRY"}:
        return True
    if status != "SUCCESS" or not item.get("end_date"):
        return False
    try:
        completed = datetime.fromisoformat(item["end_date"])
    except (ValueError, TypeError):
        return False
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone.utc)
    return completed >= generated


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    args = parser.parse_args()
    submit(json.loads(args.plan.read_text(encoding="utf-8")), datetime.now(timezone.utc))
