"""Collect national observations, verify the original files on GitHub, then free MeteoHub storage."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import meteohub_rain_recovery
from meteohub_collect import BASE_URL, safe_filename
from meteohub_request_all import api, login, utc_string
from validate_meteohub import load_approved, signature

OBSERVATIONS = Path("data/observations/meteohub")
VARIABLES = ("B13011", "B12101", "B11002")
PREFIX = "enduro-nazionale-"
GROUPS = (
    ("nord", ("dpcn-piemonte", "dpcn-bolzanoboze", "dpcn-lombardia", "dpcn-veneto", "dpcn-liguria"), None),
    ("centro", ("dpcn-lazio", "dpcn-marche", "dpcn-umbria", "dpcn-molise"), None),
    ("sud-isole", ("dpcn-basilicata", "dpcn-calabria", "dpcn-campania", "dpcn-puglia", "dpcn-sardegna", "dpcn-sicilia"), None),
    ("emilia-romagna", ("locali",), None),
    ("friuli-venezia-giulia", ("arpafvg",), None),
    ("toscana", ("sir-toscana",), None),
    ("trento", ("open-trentino",), None),
    ("abruzzo", ("mnw",), {"latmin": 41.65, "latmax": 42.90, "lonmin": 13.01, "lonmax": 14.80}),
    ("valle-aosta", ("mnw",), {"latmin": 45.43, "latmax": 46.02, "lonmin": 6.78, "lonmax": 7.97}),
)
READY = {"SUCCESS", "FAILURE", "REVOKED"}


def request_list(token, archived=False):
    query = {"archived": str(archived).lower(), "page": 1, "size": 100}
    return api("/api/requests?" + urlencode(query), token)


def error_text(error):
    try:
        value = json.loads(error.read(1200))
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)[:240]
    except ValueError:
        pass
    return ""


def submit(token, end, hours):
    active = request_list(token)
    archived = request_list(token, archived=True)
    available = {
        row["id"] for row in api("/api/datasets", token) if row.get("category") == "OBS"
    }
    quota = api("/api/hourly", token)["remaining"]
    start = end - timedelta(hours=hours)
    print(f"Observation interval UTC: {start.isoformat()} – {end.isoformat()}. Provider slots: {quota}.", flush=True)
    for label, networks, bbox in GROUPS:
        present = [network for network in networks if network in available]
        missing = sorted(set(networks) - available)
        if missing:
            print(f"{label}: datasets unavailable {missing}", flush=True)
        stem = f"{PREFIX}{end:%Y%m%dT%H%MZ}-{hours}h-{label}"
        if not present:
            continue
        matches = [
            row for row in (*active, *archived)
            if row.get("name") == stem or (row.get("name") or "").startswith(stem + "--retry-")
        ]
        if any(row.get("status") not in {"FAILURE", "REVOKED"} for row in matches):
            continue
        name = f"{stem}--retry-{len(matches)}" if matches else stem
        if quota <= 0:
            print(f"{label}: delayed by MeteoHub hourly quota", flush=True)
            continue
        filters = {"product": [{"code": code} for code in VARIABLES]}
        if bbox:
            filters["bounding_box"] = bbox
        payload = {
            "request_name": name,
            "dataset_names": present,
            "reftime": {
                "from": utc_string(start + timedelta(seconds=1)),
                "to": utc_string(end + timedelta(seconds=1)),
            },
            "filters": filters,
            "output_format": "json",
            "only_reliable": True,
        }
        try:
            response = api("/api/data", token, payload)
            identifier = response.get("request_id") or response.get("id")
            if not identifier:
                raise RuntimeError(f"{label}: missing MeteoHub request ID")
            quota -= 1
            print(f"Submitted {label} {identifier}: {', '.join(present)}", flush=True)
        except HTTPError as error:
            print(f"{label}: HTTP {error.code} {error_text(error)}", flush=True)
            if error.code == 429:
                break


def select_results(token, ids, wait):
    stop = time.monotonic() + wait
    while True:
        rows = request_list(token)
        selected = []
        for row in rows:
            if ids:
                if str(row["id"]) in ids:
                    selected.append(row)
            else:
                name = row.get("name") or ""
                if name.startswith((PREFIX, "enduro-rain-gap-")):
                    selected.append(row)
        pending = [row for row in selected if row.get("status") not in READY]
        if not pending or time.monotonic() >= stop:
            print(f"Request results: {dict(Counter(row.get('status') for row in selected))}", flush=True)
            for row in selected:
                if row.get("status") != "SUCCESS":
                    print(f"Request {row['id']} {row.get('name')}: {row.get('status')} {row.get('error_message') or ''}", flush=True)
            return [row for row in selected if row.get("status") == "SUCCESS" and row.get("fileoutput")]
        time.sleep(10)


def normalize_record(record, approved):
    retained = []
    has_measurement = False
    for group in record["data"]:
        if not isinstance(group, dict) or not isinstance(group.get("vars"), dict):
            continue
        if "timerange" not in group:
            retained.append(group)
            continue
        variables = {
            code: entry for code, entry in group["vars"].items()
            if signature(code, group.get("level"), group["timerange"]) in approved
        }
        if variables:
            retained.append({**group, "vars": variables})
            has_measurement = True
    if has_measurement:
        return {**record, "data": retained}
    return None


def fetch_file(row, token, approved):
    identifier = str(row["id"])
    filename = safe_filename(row["fileoutput"])
    if not identifier.isdecimal() or not filename:
        raise ValueError("Invalid MeteoHub file reference")
    raw = OBSERVATIONS / "originals" / f"request_{identifier}.json"
    normalized = OBSERVATIONS / f"request_{identifier}.jsonl"
    digest = hashlib.sha256()
    length = 0
    request = Request(
        BASE_URL + "/api/data/" + quote(filename, safe=""),
        headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"},
    )
    with urlopen(request, timeout=90) as source, raw.open("wb") as target:
        while chunk := source.read(1024 * 1024):
            target.write(chunk)
            digest.update(chunk)
            length += len(chunk)
    if length != row["filesize"] or length <= 0:
        raise ValueError(f"MeteoHub byte count mismatch for request {identifier}")

    records = 0
    networks = set()
    try:
        with raw.open(encoding="utf-8-sig") as source, normalized.open("w", encoding="utf-8") as out:
            first = source.readline()
            source.seek(0)
            if first.lstrip().startswith("["):
                record_iter = iter(json.load(source))
            else:
                record_iter = (json.loads(line) for line in source if line.strip())
            for record in record_iter:
                if not isinstance(record, dict) or not isinstance(record.get("data"), list):
                    raise ValueError(f"Invalid observation in request {identifier}")
                networks.add(record["network"])
                accepted = normalize_record(record, approved)
                if accepted is not None:
                    out.write(json.dumps(accepted, separators=(",", ":"), ensure_ascii=False) + "\n")
                records += 1
        if not records:
            raise ValueError(f"No measurements in request {identifier}")
    except (ValueError, KeyError):
        normalized.unlink(missing_ok=True)
        raw.unlink(missing_ok=True)
        raise
    print(f"Download {identifier}: {records} records, {length} bytes, {', '.join(sorted(networks))}", flush=True)
    return {
        "id": identifier, "filename": row["fileoutput"], "bytes": length,
        "sha256": digest.hexdigest(), "path": raw.as_posix(),
        "request_name": row.get("name"), "networks": sorted(networks),
    }


def create_release(work, tag):
    archive = work / f"{tag.removeprefix('meteohub-')}.tar.gz"
    with tarfile.open(archive, "w:gz", compresslevel=1) as pack:
        pack.add(OBSERVATIONS, arcname=OBSERVATIONS.as_posix())
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    checksum = archive.with_suffix(".gz.sha256")
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    subprocess.run(
        ["gh", "release", "create", tag, str(archive), str(checksum),
         "--repo", os.environ["GITHUB_REPOSITORY"], "--target", os.environ["GITHUB_SHA"],
         "--title", f"ENDURO MeteoHub observations {tag.rsplit('-', 1)[-1]}",
         "--notes", "Original observations, checksums and normalized data for validation.", "--latest=false"],
        check=True,
    )


def verify_release(work, tag):
    verify = work / "verify"
    verify.mkdir(exist_ok=True)
    archive_name = f"{tag.removeprefix('meteohub-')}.tar.gz"
    subprocess.run(
        ["gh", "release", "download", tag, "--repo", os.environ["GITHUB_REPOSITORY"],
         "--pattern", archive_name, "--pattern", archive_name + ".sha256", "--dir", str(verify)], check=True,
    )
    subprocess.run(["sha256sum", "-c", archive_name + ".sha256"], cwd=verify, check=True)
    with tarfile.open(verify / archive_name) as pack:
        with pack.extractfile((OBSERVATIONS / "backup_manifest.json").as_posix()) as stream:
            files = json.load(stream)
        clean = pack.getmember((OBSERVATIONS / "clean" / "observations_clean.csv").as_posix())
        if clean.size <= 0 or not files:
            raise RuntimeError("Verified release must contain originals and cleaned observations")
        for item in sorted(files, key=lambda file: file["path"]):
            if item["path"] != (OBSERVATIONS / "originals" / f"request_{item['id']}.json").as_posix():
                raise ValueError("Unexpected archived original path")
            member = pack.getmember(item["path"])
            with pack.extractfile(member) as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            if member.size != item["bytes"] or digest != item["sha256"]:
                raise RuntimeError(f"GitHub copy is incomplete for request {item['id']}")
    print(f"Downloaded GitHub release and verified {len(files)} original files.", flush=True)
    return files


def free_verified_files(token, files):
    active = {str(row["id"]): row for row in request_list(token)}
    before = api("/api/usage", token)["used"]
    removed = []
    failed = []
    for file in files:
        identifier = file["id"]
        row = active.get(identifier)
        if row is None:
            continue
        if row.get("status") != "SUCCESS" or row.get("fileoutput") != file["filename"] or row.get("filesize") != file["bytes"]:
            failed.append(identifier)
            continue
        req = Request(
            BASE_URL + "/api/requests/" + identifier,
            method="PUT", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        try:
            with urlopen(req, timeout=30) as response:
                if response.status != 200:
                    raise RuntimeError(f"Unexpected archive status {response.status}")
            removed.append(identifier)
        except (URLError, TimeoutError) as error:
            failed.append(identifier)
            print(f"Cannot release MeteoHub file {identifier}: {type(error).__name__}", flush=True)
    after = api("/api/usage", token)
    live_ids = {str(row["id"]) for row in request_list(token) if row.get("fileoutput")}
    remaining = set(failed) | ({item["id"] for item in files} & live_ids)
    print(json.dumps({
        "archived_requests": [identifier for identifier in removed if identifier not in live_ids],
        "files_not_removed": sorted(remaining),
        "bytes_freed": before - after["used"], "meteohub_disk": after,
    }), flush=True)
    if remaining:
        raise RuntimeError("Some verified files still occupy MeteoHub storage")


def collect(args):
    token = login()
    work = Path(os.environ["RUNNER_TEMP"])
    tag = f"meteohub-observations-{os.environ['GITHUB_RUN_ID']}"
    repo = os.environ["GITHUB_REPOSITORY"]
    existing = subprocess.run(["gh", "release", "view", tag, "--repo", repo], capture_output=True)
    if existing.returncode == 0:
        free_verified_files(token, verify_release(work, tag))
        return

    ids = set(filter(None, re.split(r"[,\s]+", args.request_ids)))
    if ids and not all(value.isdecimal() and value.isascii() for value in ids):
        raise ValueError("MeteoHub request IDs must be numeric")
    if not ids:
        now = datetime.now(timezone.utc) - timedelta(minutes=45)
        end = datetime.fromisoformat(args.end) if args.end else now.replace(hour=now.hour // 3 * 3, minute=0, second=0, microsecond=0)
        if end.utcoffset() != timedelta(0) or end > now:
            raise ValueError("Observation window must end in UTC after the publication delay")
        submit(token, end, args.hours)
        plan = work / "rain-recovery" / "rain_recovery_plan.json"
        if plan.is_file():
            try:
                meteohub_rain_recovery.submit(json.loads(plan.read_text()), datetime.now(timezone.utc))
            except (OSError, ValueError, RuntimeError, KeyError) as error:
                print(f"Rain-gap requests deferred: {type(error).__name__}", flush=True)
    ready = select_results(token, ids, 240)
    if not ready:
        raise RuntimeError("No completed MeteoHub result available to archive")
    (OBSERVATIONS / "originals").mkdir(parents=True, exist_ok=True)
    files = []
    approved = load_approved()
    for row in ready:
        try:
            files.append(fetch_file(row, token, approved))
        except (OSError, ValueError) as error:
            print(f"File {row['id']} retained in MeteoHub: {error}", flush=True)
            (OBSERVATIONS / "originals" / f"request_{row['id']}.json").unlink(missing_ok=True)
            (OBSERVATIONS / f"request_{row['id']}.jsonl").unlink(missing_ok=True)
    if not files:
        raise RuntimeError("No source file was copied successfully")
    subprocess.run(["python", "meteohub_clean.py"], check=True)
    clean = OBSERVATIONS / "clean" / "observations_clean.csv"
    with clean.open("rb") as observations:
        if not observations.readline() or not observations.readline():
            raise RuntimeError("No cleaned measurements are available for validation")
    for path in OBSERVATIONS.glob("request_*.jsonl"):
        path.unlink()
    (OBSERVATIONS / "backup_manifest.json").write_text(json.dumps(files, indent=2), encoding="utf-8")
    create_release(work, tag)
    free_verified_files(token, verify_release(work, tag))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-ids", default="")
    parser.add_argument("--end", help="End of observation window in ISO 8601 UTC")
    parser.add_argument("--hours", type=int, choices=(1, 3), default=3)
    collect(parser.parse_args())
