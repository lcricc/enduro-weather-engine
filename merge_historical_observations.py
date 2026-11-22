"""Merge historical CSVs in source order, keeping the first copy of each row."""
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path


def merge(root, output):
    files = sorted(root.rglob("observations_clean.csv"))
    if not files:
        raise ValueError("No historical observations")
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        tmp = Path(tmp)
        db = sqlite3.connect(tmp / "keys.sqlite")
        try:
            db.execute("PRAGMA journal_mode=OFF")
            db.execute("PRAGMA cache_size=-32768")
            db.execute("CREATE TABLE seen (k BLOB PRIMARY KEY) WITHOUT ROWID")
            fingerprints = {}
            total = unique = 0
            fields = None
            with (tmp / "merged.csv").open("w", encoding="utf-8", newline="") as dest:
                for path in files:
                    with path.open("rb") as raw:
                        fingerprint = hashlib.file_digest(raw, "sha256").digest()
                    if fingerprint in fingerprints:
                        total += fingerprints[fingerprint]
                        continue
                    rows = 0
                    with path.open(encoding="utf-8-sig", newline="") as source:
                        reader = csv.DictReader(source)
                        if not reader.fieldnames or (fields is not None and reader.fieldnames != fields):
                            raise ValueError(f"Missing or incompatible CSV header: {path}")
                        if fields is None:
                            fields = reader.fieldnames
                            writer = csv.DictWriter(dest, fieldnames=fields)
                            writer.writeheader()
                        for row in reader:
                            rows += 1
                            key = json.dumps([row[f] for f in fields], ensure_ascii=False,
                                             separators=(",", ":")).encode("utf-8")
                            if db.execute("INSERT OR IGNORE INTO seen VALUES (?)", (key,)).rowcount:
                                writer.writerow(row)
                                unique += 1
                    fingerprints[fingerprint] = rows
                    total += rows
                    print(f"{path}: {rows} rows, {unique} unique so far", flush=True)
            os.replace(tmp / "merged.csv", output)
        finally:
            db.close()
    print(f"{len(files)} files, {total} rows, {unique} unique; "
          f"{shutil.disk_usage(output.parent).free // 2**20} MiB free", flush=True)


if __name__ == "__main__":
    merge(Path("historical/observations"), Path("data/observations/meteohub/clean/observations_clean.csv"))
