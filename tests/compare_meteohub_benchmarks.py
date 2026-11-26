"""Prepare and compare a fixed historical MeteoHub benchmark."""
import argparse
import csv
import hashlib
import json
import math
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/lcricc/enduro-weather-engine"
HISTORICAL_NOW = "2026-11-26T00:00:00+00:00"
PUBLISHED_AT = "2026-11-25T07:00:20Z"
OPTIMIZED_REF = "11bc4e602dc6e322d283674691681f1dc92d0a0d"
RAM_LIMIT_PERCENT = 12
METRIC_TOLERANCE = 0.0001
ERROR_METRICS = {"mae", "rmse", "bias"}
OBS_TAG = "meteohub-observations-45629364734"
FC_TAG = "meteohub-forecasts-45552748830"
OBS_FILE = "observations-45629364734.tar.gz"
FC_FILE = "forecasts-45552748830.tar.gz"
FC_MEMBER_DIR = "data/enduro-meteohub-forecasts-45552748830"
FC_NAME = "forecasts_meteohub_20261125T052055Z.csv.gz"
PROSPECTIVE_NAME = "enduro_rain_v01_20261125T052055Z.json.gz"
ASSETS = {
    OBS_FILE: (OBS_TAG, "bc012c2406ecdfa43967a74c49cdd9826ed89a7254cb566a0ee7f41d6957f6ba"),
    FC_FILE: (FC_TAG, "9458f09772c0d38b0779f5d3dc9810584b8a7f2120c6cecbcac593d95b7bcd4b"),
}


def get_asset(cache, name, download=True):
    tag, expected = ASSETS[name]
    path = cache / name
    if not path.exists():
        if not download:
            raise FileNotFoundError(f"Missing pinned release asset: {tag}/{name}")
        partial = path.with_suffix(".partial")
        try:
            url = f"{REPOSITORY}/releases/download/{tag}/{name}"
            with urllib.request.urlopen(url, timeout=60) as source, partial.open("wb") as dest:
                shutil.copyfileobj(source, dest)
            partial.replace(path)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Cannot retrieve pinned release asset {tag}/{name}") from exc
        finally:
            partial.unlink(missing_ok=True)
    with path.open("rb") as file:
        actual = hashlib.file_digest(file, "sha256").hexdigest()
    if actual != expected:
        raise ValueError(f"Checksum mismatch for {path.name}: {actual} != {expected}")
    return path


def extract_member(archive, member, destination):
    entry = archive.getmember(member)
    if not entry.isfile():
        raise ValueError(f"Pinned archive member is not a regular file: {member}")
    with archive.extractfile(entry) as source, destination.open("wb") as dest:
        shutil.copyfileobj(source, dest)


def prepare_fixture(work, cache, download):
    observations = get_asset(cache, OBS_FILE, download)
    forecasts = get_asset(cache, FC_FILE, download)
    fixture = work / "fixture"
    release = fixture / "historical" / "forecasts" / FC_TAG
    release.mkdir(parents=True)
    forecast_dir = fixture / "forecasts"
    forecast_dir.mkdir()
    with tarfile.open(observations) as archive:
        extract_member(
            archive, "data/observations/meteohub/clean/observations_clean.csv",
            fixture / "observations.csv",
        )
    with tarfile.open(forecasts) as archive:
        extract_member(archive, f"{FC_MEMBER_DIR}/{FC_NAME}", forecast_dir / FC_NAME)
        extract_member(archive, f"{FC_MEMBER_DIR}/{PROSPECTIVE_NAME}", release / PROSPECTIVE_NAME)
    (release / "published_at.txt").write_text(PUBLISHED_AT + "\n", encoding="utf-8")
    return fixture


def compare_values(expected, actual, differences, path, field=None):
    if field in ERROR_METRICS and expected not in ("", None) and actual not in ("", None):
        if not math.isclose(float(expected), float(actual), rel_tol=0, abs_tol=METRIC_TOLERANCE + 1e-12):
            differences.append(f"{path}: {expected!r} != {actual!r}")
        return
    if type(expected) is not type(actual):
        differences.append(f"{path}: types differ")
        return
    if isinstance(expected, dict):
        if expected.keys() != actual.keys():
            differences.append(f"{path}: fields differ")
        for key in expected.keys() & actual.keys():
            compare_values(expected[key], actual[key], differences, f"{path}.{key}", key)
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            differences.append(f"{path}: length {len(expected)} != {len(actual)}")
        for index, (left, right) in enumerate(zip(expected, actual)):
            compare_values(left, right, differences, f"{path}[{index}]")
    elif actual != expected:
        differences.append(f"{path}: {expected!r} != {actual!r}")


def read_report(path):
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    with path.open(newline="", encoding="utf-8") as file:
        rows = csv.DictReader(file)
        return {"columns": rows.fieldnames, "rows": list(rows)}


def compare_outputs(before, after):
    before_names = {file.name for file in before.iterdir()}
    after_names = {file.name for file in after.iterdir()}
    differences = []
    if not before_names or before_names != after_names:
        differences.append(f"Report files differ: {sorted(before_names)} vs {sorted(after_names)}")
    for name in sorted(before_names & after_names):
        compare_values(read_report(before / name), read_report(after / name), differences, name)
    return differences


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def run_benchmark(fixture, work, name, module=None, partition_mode="auto"):
    command = [
        sys.executable, str(ROOT / "tests/benchmark_meteohub_validator.py"),
        "--observations", str(fixture / "observations.csv"),
        "--forecasts", str(fixture / "forecasts"), "--historical-dir", str(fixture),
        "--now", HISTORICAL_NOW, "--output", str(work / name), "--partition-mode", partition_mode,
    ]
    if module:
        command.extend(["--validator", str(module)])
    print(f"Running historical benchmark: {name}", flush=True)
    result = subprocess.check_output(command, cwd=ROOT, text=True)
    profile = json.loads(result)
    (work / f"{name}.json").write_text(result, encoding="utf-8")
    return profile


def ram_regressed(before, after):
    return after * 100 > before * (100 + RAM_LIMIT_PERCENT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New output directory")
    parser.add_argument("--cache", type=Path, required=True, help="Directory for the two pinned downloads")
    parser.add_argument("--baseline", default=OPTIMIZED_REF, help="Git commit available locally")
    parser.add_argument("--offline", action="store_true", help="Require the two releases in the cache")
    args = parser.parse_args()
    work = args.output.resolve()
    work.mkdir(parents=True)
    cache = args.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    fixture = prepare_fixture(work, cache, not args.offline)
    baseline = git("rev-parse", "--verify", f"{args.baseline}^{{commit}}")
    baseline_code = work / "baseline_code"
    baseline_code.mkdir()
    for name in ("validate_meteohub.py", "meteohub_rain_recovery.py", "meteohub_request_all.py", "enduro_rain_v01.py",
                 "meteohub_approved_metadata.json", "rain-v01.json", "rain-baseline-v01.json"):
        (baseline_code / name).write_text(git("show", f"{baseline}:{name}") + "\n", encoding="utf-8")
    module = baseline_code / "validate_meteohub.py"
    profiles = {
        "baseline": run_benchmark(fixture, work, "baseline", module),
        "candidate": run_benchmark(fixture, work, "candidate"),
        "candidate_sqlite": run_benchmark(fixture, work, "candidate_sqlite", partition_mode="sqlite"),
    }
    before, after = profiles["baseline"], profiles["candidate"]
    differences = compare_outputs(work / "baseline", work / "candidate")
    sqlite_diffs = compare_outputs(work / "candidate", work / "candidate_sqlite")
    differences.extend(f"SQLite parity: {item}" for item in sqlite_diffs)
    if not any("SQLite=" in line for line in profiles["candidate_sqlite"]["partition_logs"]):
        differences.append("Historical SQLite run did not execute the disk path")
    for field in ("observation_rows", "forecast_rows", "forecast_files"):
        if before[field] != after[field]:
            differences.append(f"{field}: {before[field]} != {after[field]}")
    if not all(after[field] for field in ("observation_rows", "forecast_rows", "complete_model_events",
                                         "complete_precipitation_events", "prospective_station_hours")):
        differences.append("Historical sample did not produce usable complete validation cases")
    ram_delta = 100 * (after["peak_rss_kib"] / before["peak_rss_kib"] - 1)
    report = {
        "baseline_commit": baseline,
        "candidate_commit": git("rev-parse", "HEAD"),
        "python": sys.version,
        "as_of_utc": HISTORICAL_NOW,
        "release_assets": [{"name": name, "tag": tag, "sha256": digest}
                           for name, (tag, digest) in ASSETS.items()],
        "forecast_release_published_at": PUBLISHED_AT,
        "ram_limit_percent": RAM_LIMIT_PERCENT,
        "ram_change_percent": round(ram_delta, 3),
        "metric_abs_tolerance": METRIC_TOLERANCE,
        "scientific_differences": differences,
        "passed": not differences and not ram_regressed(before["peak_rss_kib"], after["peak_rss_kib"]),
        **profiles,
    }
    encoded = json.dumps(report, indent=2) + "\n"
    (work / "comparison.json").write_text(encoded, encoding="utf-8")
    print(encoded)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
