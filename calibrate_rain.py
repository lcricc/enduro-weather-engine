"""Calibrate the 0–24 h rain vote from archived, pre-event eight-model forecasts."""
import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from prepare_forecast_stations import region_boundaries, region_lookup
from validate_meteohub import PRECIPITATION_UNITS, hourly_precipitation, load_approved

ROME = ZoneInfo("Europe/Rome")
HOUR = timedelta(hours=1)
CONFIG = Path(__file__).with_name("rain-v01.json")


def project(db, source, member, target, kind, config):
    """Project one CSV into a compact cache, keeping enough detail to merge late copies."""
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / ("rows.csv.gz" if member.endswith((".gz", ".csv.partial")) else "rows.csv")
        with path.open("wb") as dest:
            shutil.copyfileobj(source, dest)
        db.read_csv(str(path), header=True, all_varchar=True, sample_size=1000).create_view("raw", replace=True)
        if kind == "observations":
            metadata = [json.loads(key) for key in load_approved()]
            ranges = [json.dumps(entry["timerange"], separators=(",", ":"))
                      for entry in metadata if entry["code"] == "B13011"]
            rows = db.sql("""
                WITH rain AS (
                    SELECT trim(network) net, trim(station_id) station,
                           try_cast(time_utc AS TIMESTAMPTZ) t,
                           try_cast(json_extract(timerange,'$[2]') AS INTEGER) AS seconds,
                           try_cast(value AS DOUBLE) mm
                    FROM raw WHERE variable='B13011' AND json_valid(level) AND json_valid(timerange)
                      AND json(level)='[1,null,null,null]'
                      AND CAST(json(timerange) AS VARCHAR) IN (SELECT unnest($ranges))
                      AND coalesce(lower(trim(unit)), '') IN (SELECT unnest($units))
                )
                SELECT net, station, t, seconds, min(mm) AS lo, max(mm) AS hi FROM rain
                WHERE t IS NOT NULL AND net<>'' AND station<>''
                  AND isfinite(mm) AND mm>=0 GROUP BY ALL
            """, params={"units": ["", *PRECIPITATION_UNITS], "ranges": ranges})
        else:
            rows = db.sql("""
                WITH fc AS (
                    SELECT trim(network) net, trim(station_id) station, model,
                           try_cast(latitude AS DOUBLE) lat, try_cast(longitude AS DOUBLE) lon,
                           try_cast(acquired_utc AS TIMESTAMPTZ) issue,
                           try_cast(valid_time_utc AS TIMESTAMPTZ) t, try_cast(precipitation_mm AS DOUBLE) mm
                    FROM raw WHERE model IN (SELECT unnest($models))
                )
                SELECT net, station, min(lat) lat, min(lon) lon, issue, t,
                       count(*) FILTER (WHERE mm >= $threshold)::INTEGER votes FROM fc
                WHERE net<>'' AND station<>'' AND issue < t-INTERVAL '1 hour'
                  AND t<=issue+INTERVAL '24 hours' AND date_trunc('hour',t)=t
                GROUP BY net,station,issue,t
                HAVING count(*)=8 AND count(DISTINCT model)=8 AND count(mm)=8 AND min(mm)>=0
                   AND bool_and(isfinite(mm)) AND count(lat)=8 AND count(lon)=8
                   AND min(lat)=max(lat) AND min(lon)=max(lon)
            """, params={"models": config["models"], "threshold": config["threshold_mm"]})
        rows.write_parquet(str(target), compression="zstd")


def project_archive(db, archive, target, kind, config):
    pattern = "observations_clean.csv" if kind == "observations" else "forecasts_meteohub_"
    def wanted(name):
        base = Path(name).name
        return base == pattern if kind == "observations" else base.startswith(pattern) and base.endswith((".csv.gz", ".csv.partial"))
    with tempfile.TemporaryDirectory(dir=target.parent) as work:
        pieces = []
        def consume(name, stream):
            piece = Path(work) / f"{len(pieces)}.parquet"
            project(db, stream, name, piece, kind, config)
            pieces.append(str(piece))
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as src:
                for name in src.namelist():
                    if wanted(name):
                        with src.open(name) as stream:
                            consume(name, stream)
        else:
            with tarfile.open(archive, "r|gz") as src:
                for entry in src:
                    if entry.isfile() and wanted(entry.name):
                        consume(entry.name, src.extractfile(entry))
        if not pieces:
            return False
        temp = Path(work) / "projection"
        db.read_parquet(pieces).write_parquet(str(temp), compression="zstd")
        temp.replace(target)
    return True


def fit(db, observations, forecasts, cutoff, config):
    # The time the source became available is the original artifact or release timestamp.
    db.read_parquet([str(p) for p, _ in forecasts], filename=True).create_view("forecasts")
    db.execute("CREATE TABLE availability(filename VARCHAR, published TIMESTAMPTZ)")
    db.executemany("INSERT INTO availability VALUES (?,?)", [(str(p), ts) for p, ts in forecasts])
    lookup = region_lookup(region_boundaries())
    coords = db.sql("SELECT DISTINCT lon, lat FROM forecasts").fetchall()
    italian = [(x,y) for x,y in coords if x is not None and y is not None and lookup(x,y)]
    db.execute("CREATE TABLE italy(lon DOUBLE, lat DOUBLE)")
    db.executemany("INSERT INTO italy VALUES (?,?)", italian)
    db.execute("""
        CREATE TEMP TABLE selected AS
        WITH cycles AS (
            SELECT net, station, t, issue, min(votes) votes FROM forecasts
            JOIN availability USING(filename) JOIN italy USING(lon,lat)
            WHERE published < t-INTERVAL '1 hour' AND t<=?
            GROUP BY net,station,t,issue HAVING min(votes)=max(votes)
        )
        SELECT net,station,t,arg_max(votes,issue) votes FROM cycles GROUP BY ALL
    """, [cutoff])
    db.read_parquet([str(path) for path in observations]).create_view("obs")
    # Consolidate before reconstruction so adjacent intervals may come from different copies.
    query = """
        WITH intervals AS (
            SELECT net,station,t,seconds,min(lo) lo,max(hi) hi,
                   CASE WHEN t=date_trunc('hour',t) THEN t ELSE date_trunc('hour',t)+INTERVAL '1 hour' END hour_end
            FROM obs WHERE t>? AND t<=? GROUP BY net,station,t,seconds
        )
        SELECT s.net,s.station,s.t,s.votes,i.t,i.seconds,i.lo,i.hi FROM intervals i
        JOIN selected s ON (i.net,i.station,i.hour_end)=(s.net,s.station,s.t)
        WHERE i.t-i.seconds*INTERVAL '1 second'>=s.t-INTERVAL '1 hour'
        ORDER BY s.t,s.net,s.station
    """
    daily = {}
    current = None
    segments = {}
    stats = Counter()
    def finish():
        if current is None:
            return
        hour_end, votes = current[2:]
        # Use the same complete-hour reconstruction as the scientific validator.
        key = (*current[:3], "precipitation_mm")
        mm = hourly_precipitation({key:segments}, stats).get(key)
        if mm is None:
            return
        day = (hour_end-HOUR).astimezone(ROME).date().isoformat()
        counts = daily.setdefault(day, {"total":[0]*9, "wet":[0]*9})
        counts["total"][votes] += 1
        counts["wet"][votes] += int(mm >= config["threshold_mm"])
    # Partition by UTC date to bound temporary disk and memory as the archive grows.
    dates = db.sql("SELECT DISTINCT CAST(t - INTERVAL '1 microsecond' AS DATE) FROM selected ORDER BY 1").fetchall()
    for (day,) in dates:
        start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        stream = db.execute(query,[start, start + timedelta(days=1)])
        while batch := stream.fetchmany(16384):
            for net, station, end, votes, t, seconds, lo, hi in batch:
                key = (net, station, end, votes)
                if key != current:
                    finish()
                    current, segments = key, {}
                segments[t-timedelta(seconds=seconds),t] = lo if lo==hi else None
    finish()
    if not daily:
        raise ValueError("No complete forecast / observation pairs")
    return daily


def update(config, daily):
    new = {key:config[key] for key in ("version","models","threshold_mm")}
    for field in ("total","wet"):
        new[field] = [sum(day[field][v] for day in daily.values()) for v in range(9)]
    new["calibration"] = {"from":min(daily), "through":max(daily), "days":len(daily),
                          "station_hours":sum(new["total"]), "daily":dict(sorted(daily.items()))}
    new["calibration"]["id"] = hashlib.sha256(json.dumps(daily,sort_keys=True).encode()).hexdigest()[:16]
    return new


def load_archive(entry, source):
    import subprocess

    with source.open("wb") as out:
        subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream", entry["url"]],
                       stdout=out, check=True)
    with source.open("rb") as saved:
        if hashlib.file_digest(saved, "sha256").hexdigest() != entry["key"]:
            raise ValueError(f"Archive checksum mismatch: {source}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=CONFIG)
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    config = json.loads(CONFIG.read_text())
    db = duckdb.connect()
    db.execute("SET TimeZone='UTC'")
    db.execute("SET threads=3")
    db.execute("SET memory_limit='1GB'")
    sources = defaultdict(list)
    for entry in json.loads(args.manifest.read_text()):
        kind = entry["kind"]
        target = args.cache / (entry["key"]+".parquet")
        if not target.exists():
            source = Path(entry["path"])
            download = "url" in entry
            if download:
                load_archive(entry, source)
            try:
                found = project_archive(db, source, target, kind, config)
            finally:
                if download:
                    source.unlink(missing_ok=True)
            if not found:
                if entry.get("optional"):
                    continue
                raise ValueError(f"Missing {kind} CSV: {source}")
        sources[kind].append((target,entry["published"]))
    cutoff = datetime.now(ROME).replace(hour=0,minute=0,second=0,microsecond=0)
    result = update(config,fit(db,[p for p,_ in sources["observations"]],sources["forecasts"],cutoff,config))
    args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n")
    print({k:v for k,v in result["calibration"].items() if k!="daily"},flush=True)

if __name__ == "__main__":
    main()
