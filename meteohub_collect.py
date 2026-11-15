
#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - Raccolta robusta MeteoHub."""

import argparse
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from meteohub_clean import utc_time

BASE_URL = "https://meteohub.agenziaitaliameteo.it"
OUTPUT_DIR = Path("data/observations/meteohub")
MAX_BYTES = 50 * 1024 * 1024
FAILED_STATUSES = {"FAILURE", "FAILED", "REVOKED", "CANCELLED", "CANCELED", "EXPIRED"}


def api_json(path, token=None, payload=None):
    headers = {"Accept": "application/json"}

    if token:
        headers["Authorization"] = "Bearer " + token

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        BASE_URL + path,
        data=data,
        headers=headers,
        method="POST" if data is not None else "GET",
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def login():
    username = os.environ.get("METEOHUB_USERNAME")
    password = os.environ.get("METEOHUB_PASSWORD")

    if not username or not password:
        raise RuntimeError("Credenziali MeteoHub mancanti")

    result = api_json(
        "/auth/login",
        payload={"username": username, "password": password},
    )

    token = (
        result
        if isinstance(result, str)
        else (
            result.get("access_token") or result.get("token")
            if isinstance(result, dict)
            else None
        )
    )

    if not token:
        raise RuntimeError("Autenticazione MeteoHub fallita")

    return token


def safe_filename(fileoutput):
    if not isinstance(fileoutput, str) or not fileoutput.strip():
        return None

    parsed = urlparse(fileoutput.strip())

    if parsed.scheme or parsed.netloc:
        return None

    filename = os.path.basename(
        unquote(fileoutput.replace("\\", "/"))
    )

    if filename in ("", ".", ".."):
        return None

    return filename


def download_records(filename, token):
    url = BASE_URL + "/api/data/" + quote(filename, safe="")

    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(request, timeout=120) as response:
        content = response.read(MAX_BYTES + 1)

    if len(content) > MAX_BYTES:
        raise ValueError("File oltre il limite di 50 MB")

    if not content:
        raise ValueError("Risposta vuota")

    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Contenuto non UTF-8") from None

    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError:
        records = []

        for number, line in enumerate(decoded.splitlines(), 1):
            if not line.strip():
                continue

            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                raise ValueError(
                    f"JSON Lines non valido alla riga {number}"
                ) from None

        format_name = "JSON Lines"
    else:
        records = parsed if isinstance(parsed, list) else [parsed]
        format_name = "JSON"

    if not records:
        raise ValueError("Nessun record presente")

    if not all(isinstance(record, dict) for record in records):
        raise ValueError("Struttura dei record non valida")

    return records, format_name, len(content)


def save_records(request_id, records):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    identifier = re.sub(
        r"[^a-zA-Z0-9_-]", "_", str(request_id)
    )[:80]

    if not identifier:
        raise ValueError("ID richiesta non valido")

    target = OUTPUT_DIR / f"request_{identifier}.jsonl"

    fd, temporary = tempfile.mkstemp(
        prefix=".meteohub_", dir=OUTPUT_DIR
    )

    try:
        with os.fdopen(
            fd, "w", encoding="utf-8", newline="\n"
        ) as output:
            for record in records:
                output.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                output.write("\n")

        os.replace(temporary, target)

    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

    return target


def save_report(report):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    target = OUTPUT_DIR / "collection_report.json"
    temporary = target.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    os.replace(temporary, target)
    print("Rapporto salvato:", target)


def classify_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"

    if isinstance(exc, urllib.error.URLError):
        return "Errore di rete"

    if isinstance(exc, TimeoutError):
        return "Timeout"

    if isinstance(exc, ValueError):
        return str(exc)

    if isinstance(exc, OSError):
        return "Errore di lettura o scrittura"

    return type(exc).__name__


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--request-ids",
        help="ID di richieste esistenti, separati da virgola o spazio",
    )
    parser.add_argument(
        "--wait-seconds", type=int, default=0,
        help="Secondi di attesa massima per le richieste selezionate",
    )
    args = parser.parse_args()
    ids = ()
    if args.request_ids is not None:
        ids = tuple(dict.fromkeys(re.split(r"[,\s]+", args.request_ids.strip())))
        if any(not re.fullmatch(r"[0-9]+", request_id) for request_id in ids):
            parser.error("--request-ids richiede uno o più ID numerici")
    if not 0 <= args.wait_seconds <= 600 or (args.wait_seconds and not ids):
        parser.error("--wait-seconds deve essere tra 0 e 600 ed è riservato alla raccolta per ID")
    return ids, args.wait_seconds


def select_requests(token, request_ids, wait_seconds):
    deadline = time.monotonic() + wait_seconds
    selected = {}
    statuses = {}

    while True:
        requests = api_json("/api/requests", token=token)
        if not isinstance(requests, list):
            raise RuntimeError("Elenco richieste MeteoHub non valido")
        requests_by_id = {
            str(item["id"]): item for item in requests
            if isinstance(item, dict) and item.get("id") is not None
        }

        if not request_ids:
            return len(requests), requests, {}

        waiting = []
        for request_id in request_ids:
            if request_id in selected:
                continue
            item = requests_by_id.get(request_id)
            status = str(item.get("status") or "UNKNOWN").upper() if item else "NOT_LISTED"
            if status == "SUCCESS" and safe_filename(item.get("fileoutput")):
                selected[request_id] = item
            elif status in FAILED_STATUSES:
                selected[request_id] = item
            else:
                if status == "SUCCESS":
                    status = "SUCCESS_WITHOUT_FILE"
                waiting.append(request_id)
            if statuses.get(request_id) != status:
                statuses[request_id] = status
                print(f"Stato richiesta {request_id}: {status}", flush=True)

        remaining = deadline - time.monotonic()
        if not waiting or remaining <= 0:
            return len(requests), list(selected.values()), statuses
        time.sleep(min(15, remaining))


def main():
    print("ENDURO - RACCOLTA METEOHUB ROBUSTA")

    # Gli errori generali restano bloccanti.
    request_ids, wait_seconds = parse_args()
    token = login()
    requests_total, requests, statuses = select_requests(token, request_ids, wait_seconds)

    completed = [
        item
        for item in requests
        if isinstance(item, dict)
        and str(item.get("status", "")).upper() == "SUCCESS"
    ]

    report = {
        "generated_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "requests_total": requests_total,
        "requests_completed": len(completed),
        "files_saved": 0,
        "records_downloaded": 0,
        "requests_skipped": 0,
        "errors": [],
        "saved_requests": [],
    }

    if request_ids:
        report["requested_statuses"] = statuses
        report["request_results"] = {}

    print("Richieste totali:", requests_total)
    print("Richieste completate:", len(completed))

    for item in completed:
        request_id = item.get("id")
        filename = safe_filename(item.get("fileoutput"))

        if request_id is None or not filename:
            report["requests_skipped"] += 1
            report["errors"].append({
                "request_id": request_id,
                "reason": "ID o nome file mancante",
            })
            print("SKIP: ID o file mancante")
            continue

        try:
            records, format_name, size = download_records(
                filename, token
            )

            destination = save_records(
                request_id, records
            )

        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            ValueError,
            OSError,
        ) as exc:
            reason = classify_error(exc)

            report["requests_skipped"] += 1
            report["errors"].append({
                "request_id": request_id,
                "reason": reason,
            })

            print(
                f"SKIP richiesta {request_id}: {reason}"
            )
            continue

        report["files_saved"] += 1
        report["records_downloaded"] += len(records)
        report["saved_requests"].append(request_id)
        if request_ids:
            latest = max(
                filter(None, (utc_time(record.get("date")) for record in records)),
                default=None,
            )
            report["request_results"][str(request_id)] = {
                "records": len(records),
                "latest_record_utc": latest,
            }
            print(f"Ultimo record richiesta {request_id}: {latest}")

        print(
            f"OK richiesta {request_id}: "
            f"{len(records)} record, "
            f"{format_name}, {size} byte"
        )
        print("Salvato:", destination)

    save_report(report)

    print()
    print("===== RIEPILOGO =====")
    print("File salvati:", report["files_saved"])
    print("Record scaricati:", report["records_downloaded"])
    print("Richieste saltate:", report["requests_skipped"])

    if report["errors"]:
        print("ATTENZIONE: alcune estrazioni sono state saltate")

    if request_ids:
        missing = [request_id for request_id in request_ids if request_id not in report["request_results"]]
        if missing:
            print("Richieste selezionate senza file raccolto:", ", ".join(missing))

    if report["files_saved"] == 0:
        raise RuntimeError(
            "Nessuna estrazione valida scaricata"
        )

    print("Raccolta completata: dati pronti per la pulizia")


if __name__ == "__main__":
    main()
