"""Measure the Linux validation process and render its GitHub Actions summary."""
import argparse
import json
import os
import re
import resource
import subprocess
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter, time

SPOOL = re.compile(r"^\[temporanei\] totale=(\d+) byte .* limite in RAM=(\d+) byte .* dimensioni=(\[.*\])$")


def temporary_bytes(path):
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except FileNotFoundError:
            continue
    return total


def sample_disk(root, stop, peak):
    while not stop.is_set():
        peak[0] = max(peak[0], temporary_bytes(root))
        stop.wait(0.1)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def measure(profile_path, command):
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    peak = [0]
    stop = threading.Event()
    phase = "Avvio"
    ram_blocks = sqlite_blocks = 0
    spool_peak = 0
    process = None
    started = perf_counter()
    with TemporaryDirectory(prefix="enduro-validation-") as root:
        root = Path(root)
        monitor = threading.Thread(target=sample_disk, args=(root, stop, peak))
        monitor.start()
        try:
            env = dict(os.environ, TMPDIR=str(root), ENDURO_VALIDATION_PHASES="1")
            with subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, errors="replace") as process:
                for line in process.stdout:
                    print(line, end="", flush=True)
                    if line.startswith("[fase] "):
                        phase = line.removeprefix("[fase] ").strip()
                    spool = SPOOL.match(line.strip())
                    if spool:
                        spool_peak = max(spool_peak, int(spool[1]))
                        sizes = json.loads(spool[3])
                        ram_blocks = sum(size <= int(spool[2]) for size in sizes)
                        sqlite_blocks = len(sizes) - ram_blocks
                    if line.startswith("[temporanei] blocco ") and "SQLite=" in line:
                        spool_peak = max(spool_peak, temporary_bytes(root))
        finally:
            status = process.returncode if process else None
            peak_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
            stop.set()
            monitor.join()
            report = read_json(profile_path.parent / "validation_latest.json")
            prospective = read_json(profile_path.parent / "enduro_rain_v01.json")
            obs = report.get("observation_diagnostics", {})
            forecasts = report.get("forecast_diagnostics", {})
            profile_path.write_text(json.dumps({
                "exit_code": status,
                "validation_seconds": round(perf_counter() - started, 2),
                "peak_rss_kib": peak_rss,
                "peak_temporary_bytes": max(peak[0], spool_peak),
                "temporary_sampling_seconds": 0.1,
                "last_phase": phase,
                "partition_strategy": "station modulo partition count",
                "ram_partitions_planned": ram_blocks,
                "sqlite_partitions_planned": sqlite_blocks,
                "observation_rows": obs.get("rows"),
                "forecast_rows": forecasts.get("forecast_rows"),
                "forecast_files": report.get("forecast_files"),
                "complete_model_events": forecasts.get("complete_model_events"),
                "complete_precipitation_events": forecasts.get("complete_precipitation_events"),
                "prospective_station_hours": prospective.get("overall", {}).get("station_hours"),
                "scientific_status": report.get("status"),
            }, indent=2) + "\n", encoding="utf-8")
    return status if status is not None and status >= 0 else 128 + abs(status or 0)


def summary(path, step_outcome):
    profile = read_json(path)
    values = {"Esito fase di validazione": step_outcome}
    if profile:
        values.update({
            "Ultima fase": profile["last_phase"],
            "Tempo validatore (s)": profile["validation_seconds"],
            "Picco RAM (MiB)": round(profile["peak_rss_kib"] / 1024, 2),
            "Picco temporanei del validatore (MiB, campionato)": round(profile["peak_temporary_bytes"] / 1024**2, 2),
            "Partizioni previste per stazione · RAM / SQLite":
                f'{profile["ram_partitions_planned"]} / {profile["sqlite_partitions_planned"]}',
            "Righe osservazioni": profile["observation_rows"],
            "Righe previsioni": profile["forecast_rows"],
            "File previsionali": profile["forecast_files"],
            "Eventi comuni a otto modelli": profile["complete_model_events"],
            "Eventi pioggia comuni": profile["complete_precipitation_events"],
            "Ore-stazione pioggia prospettica": profile["prospective_station_hours"],
            "Esito scientifico": profile["scientific_status"],
        })
    else:
        values["Profilo"] = "Non disponibile (fase non avviata o runner interrotto)"
    job_start = os.environ.get("ENDURO_VALIDATION_JOB_STARTED")
    if job_start:
        elapsed = round(time() - float(job_start), 2)
        values["Tempo job prima della Summary (s)"] = elapsed
        if profile:
            profile["job_seconds_to_summary"] = elapsed
            path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    print("### Validazione scientifica MeteoHub\n\n| Indicatore | Valore |\n|---|---|")
    for label, value in values.items():
        label = label.replace("|", "\\|")
        value = str(value).replace("|", "\\|") if value is not None else "n/d"
        print(f"| {label} | {value} |")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--summary", metavar="STEP_OUTCOME")
    parser.add_argument("command", nargs="*")
    args = parser.parse_args()
    if args.summary is not None:
        summary(args.profile, args.summary)
        return 0
    if not args.command:
        parser.error("Provide a validator command after --")
    return measure(args.profile, args.command)


if __name__ == "__main__":
    raise SystemExit(main())
