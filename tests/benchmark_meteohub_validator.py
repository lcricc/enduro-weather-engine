"""Benchmark the validator on a fixed input set in a fresh Python process (Linux)."""
import argparse
import contextlib
import io
import json
import resource
import sys
import tempfile
import threading
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import validate_meteohub as validator
from test_meteohub_validator import NOW, load_baseline, make_fixture


def measure_temp(directory, stop, peak):
    while not stop.is_set():
        total = 0
        for path in directory.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except FileNotFoundError:
                continue
        peak[0] = max(peak[0], total)
        stop.wait(0.05)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validator", type=Path, help="Alternate validator module for comparison")
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--forecasts", type=Path)
    parser.add_argument("--historical-dir", type=Path, default=Path.cwd(),
                        help="Working directory containing historical/forecasts, if present")
    parser.add_argument("--now", default=NOW.isoformat())
    parser.add_argument("--output", type=Path, required=True, help="New directory for the 7 report files")
    args = parser.parse_args()
    if bool(args.observations) != bool(args.forecasts):
        parser.error("Use --observations and --forecasts together, or omit both for the synthetic fixture")
    if args.output.exists():
        parser.error("--output must not exist")
    module = load_baseline(args.validator.resolve()) if args.validator else validator
    now = validator.timestamp(args.now)
    if now is None:
        parser.error("--now must be an ISO 8601 timestamp with a time zone")
    with tempfile.TemporaryDirectory(prefix="meteohub-benchmark-") as directory:
        root = Path(directory)
        scratch = root / "spool"
        scratch.mkdir()
        if args.observations:
            observations = args.observations.resolve()
            forecasts = args.forecasts.resolve()
            work = args.historical_dir.resolve()
        else:
            make_fixture(root)
            observations = root / "observations.csv"
            forecasts = root / "forecasts"
            work = root
        output = args.output.resolve()
        stop = threading.Event()
        peak = [0]
        thread = threading.Thread(target=measure_temp, args=(scratch, stop, peak))
        thread.start()
        start = perf_counter()
        try:
            with patch.multiple(
                module, OBS=observations, FC_DIR=forecasts, OUT=output,
                APPROVED=Path(__file__).resolve().parents[1] / "meteohub_approved_metadata.json",
            ), patch.object(tempfile, "tempdir", str(scratch)), contextlib.chdir(work), contextlib.redirect_stdout(io.StringIO()) as log:
                module.validate(now=now)
        finally:
            stop.set()
            thread.join()
        elapsed = perf_counter() - start
        report = json.loads((output / "validation_latest.json").read_text())
        print(json.dumps({
            "observation_rows": report["observation_diagnostics"]["rows"],
            "forecast_rows": report["forecast_diagnostics"]["forecast_rows"],
            "forecast_files": report["forecast_files"],
            "elapsed_seconds": round(elapsed, 3),
            "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2),
            "sampled_peak_temp_bytes": peak[0],
            "partition_logs": [line for line in log.getvalue().splitlines() if line.startswith("[temporanei]")],
        }, indent=2))


if __name__ == "__main__":
    main()
