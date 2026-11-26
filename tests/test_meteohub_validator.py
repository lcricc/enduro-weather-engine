import contextlib
import csv
import gzip
import importlib.util
import io
import json
import math
import os
import tempfile
import unittest
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import validate_meteohub as validator

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
HOUR = NOW - timedelta(hours=1)
MINUTE = timedelta(minutes=1)


def write_csv(file, rows):
    writer = csv.DictWriter(file, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)


def load_baseline(path):
    spec = importlib.util.spec_from_file_location("baseline_validator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_validator(module, root, name):
    output = root / name
    with patch.multiple(
        module,
        OBS=root / "observations.csv",
        APPROVED=ROOT / "meteohub_approved_metadata.json",
        FC_DIR=root / "forecasts",
        OUT=output,
    ), contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
        module.validate(now=NOW)
    return {
        file.name: json.loads(file.read_text()) if file.suffix == ".json" else file.read_text()
        for file in output.iterdir()
    }


def make_fixture(root):
    observations = []

    def sample(station, when, value, code="B12101", duration=0):
        levels = {"B12101": [103, 2000, None, None], "B11002": [103, 10000, None, None],
                  "B13011": [1, None, None, None]}
        observations.append(dict(
            network="network", station_id=station, time_utc=when.isoformat(), variable=code,
            level=json.dumps(levels[code]), timerange=json.dumps([1, 0, duration] if duration else [254, 0, 0]),
            value=value, unit={"B12101": "K", "B11002": "m/s", "B13011": "kg/m2"}[code],
        ))

    sample("exact", HOUR, 283.15)
    sample("exact", HOUR - MINUTE, 303.15)
    sample("tied", HOUR - 10 * MINUTE, 284.15)
    sample("tied", HOUR + 10 * MINUTE, 291.15)
    sample("after_issue", HOUR - 10 * MINUTE, 300.15)
    sample("after_issue", HOUR + 15 * MINUTE, 286.15)
    sample("boundary", HOUR - 30 * MINUTE, 285.15)
    sample("duplicate_obs", HOUR, 290.15)
    sample("duplicate_obs", HOUR, 290.15)
    sample("partial", HOUR, 287.15)
    sample("duplicate_fc", HOUR, 288.15)
    sample("separate_issues", HOUR, 289.15)
    sample("exact", HOUR, 5, "B11002")
    sample("exact", HOUR, 0.2, "B13011", 3600)
    sample("rain_segments", HOUR - 30 * MINUTE, 0.05, "B13011", 1800)
    sample("rain_segments", HOUR, 0.05, "B13011", 1800)
    sample("rain_incomplete", HOUR, 0.05, "B13011", 1800)
    sample("off_hour_last", HOUR + 21 * MINUTE, 294.15)
    with (root / "observations.csv").open("w", newline="") as file:
        write_csv(file, observations)

    def forecast(station, model, issue=HOUR - timedelta(hours=2), valid=HOUR, **values):
        return dict(network="network", station_id=station, model=model,
                    acquired_utc=issue.isoformat(), valid_time_utc=valid.isoformat(),
                    temperature_2m_c=values.get("temperature", 16),
                    wind_speed_10m_kmh=values.get("wind", 20), precipitation_mm=values.get("rain", 0.1))

    models = sorted(validator.OFFICIAL_MODELS)
    rows = [forecast(station, model) for station in (
        "exact", "tied", "boundary", "duplicate_obs", "duplicate_fc", "missing",
        "rain_segments", "rain_incomplete",
    ) for model in models]
    rows += [forecast("after_issue", model, HOUR - 5 * MINUTE) for model in models]
    rows += [forecast("partial", model) for model in models[:-1]]
    rows += [forecast("separate_issues", model) for model in models[:4]]
    rows += [forecast("separate_issues", model, HOUR - 25 * validator.HOUR)
             for model in models[4:]]
    rows += [forecast("missing", models[0], valid=NOW + timedelta(hours=1))]
    rows += [forecast("exact", "ecmwf_aifs025")]
    forecasts = root / "forecasts"
    forecasts.mkdir()
    with gzip.open(forecasts / "forecasts_meteohub_a.csv.gz", "wt", newline="") as file:
        write_csv(file, rows)
    with gzip.open(forecasts / "forecasts_meteohub_b.csv.gz", "wt", newline="") as file:
        write_csv(file, [forecast("duplicate_fc", models[0])])

    release = root / "historical/forecasts/meteohub-forecasts-test"
    release.mkdir(parents=True)
    issue = HOUR - 2 * validator.HOUR
    (release / "published_at.txt").write_text(issue.isoformat())
    with gzip.open(release / "enduro_rain_v01_test.json.gz", "wt") as file:
        json.dump({
            "version": json.loads((ROOT / "rain-v01.json").read_text())["version"],
            "acquired_utc": issue.isoformat(), "recorded_utc": issue.isoformat(),
            "predictions": [["network", station, int((HOUR - validator.HOUR).timestamp()), 5, 0.6, True, False]
                            for station in ("exact", "rain_segments", "rain_incomplete")],
        }, file)


class ValidatorRegression(unittest.TestCase):
    def test_matching_samples_and_disk_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_fixture(root)
            ram = run_validator(validator, root, "ram")
            with patch.object(validator, "MAX_IN_MEMORY_PARTITION_BYTES", 0):
                disk = run_validator(validator, root, "disk")
            self.assertEqual(ram, disk)

            baseline = os.environ.get("METEOHUB_VALIDATOR_BASELINE")
            if baseline:
                self.assertEqual(ram, run_validator(load_baseline(baseline), root, "before"))

        report = ram["validation_latest.json"]
        self.assertEqual(report["observation_diagnostics"]["ambiguous_observations"], 1)
        self.assertEqual(report["forecast_diagnostics"]["duplicate_forecasts"], 1)
        self.assertGreater(report["forecast_diagnostics"]["missing_observation_at_valid_time"], 0)
        self.assertEqual(report["forecast_diagnostics"]["forecast_valid_time_in_future"], 3)
        self.assertEqual(report["forecast_diagnostics"]["precipitation_already_started"], 8)
        self.assertEqual(report["forecast_diagnostics"]["partial_events"], 4)
        self.assertEqual(report["forecast_diagnostics"]["complete_model_events"], 7)
        self.assertEqual(report["observation_matching"]["latest_eligible_observation_utc"],
                         (HOUR + 21 * MINUTE).isoformat())
        self.assertEqual(report["latest_observation_utc"], HOUR.isoformat())
        offsets = {x["observation_minus_forecast_seconds"]: x["complete_events"]
                   for x in report["observation_matching"]["event_offsets_seconds"]}
        self.assertEqual(offsets, {-1800: 1, -600: 1, 0: 4, 900: 1})
        for metric in report["metrics"]:
            if metric["variable"] == "temperature_2m_c":
                self.assertEqual((metric["n"], metric["mae"], metric["bias"]), (4, 4.5, 4.5))
                self.assertEqual(metric["rmse"], round(math.sqrt(86 / 4), 4))
        self.assertEqual(ram["enduro_rain_v01.json"]["overall"]["station_hours"], 2)

    def test_compensated_signed_sum(self):
        totals = validator.ErrorTotals()
        for error in (1e16, 1, -1e16):
            totals.add(error)
        metric, = validator.calculate_metrics({("model", "test", "0-24h"): totals})
        self.assertEqual(metric["n"], 3)
        self.assertEqual(metric["bias"], 0.3333)

    def test_loader_keeps_original_default_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_fixture(root)
            with patch.multiple(validator, OBS=root / "observations.csv",
                                APPROVED=ROOT / "meteohub_approved_metadata.json"):
                approved = validator.load_approved()
                all_observations, before = validator.load_observations(
                    approved, time_tolerance=validator.OBSERVATION_TOLERANCE, include_precipitation=True)
                nearby = defaultdict(list)
                exact, after = validator.load_observations(
                    approved, time_tolerance=validator.OBSERVATION_TOLERANCE,
                    include_precipitation=True, nearby_observations=nearby)
        self.assertEqual(before, after)
        self.assertEqual(nearby, validator.index_nearby_observations(all_observations))
        self.assertEqual(exact, {key: value for key, value in all_observations.items()
                                 if key[2] == validator.closest_hour(key[2])})
