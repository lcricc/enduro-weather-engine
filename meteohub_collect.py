
#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - Raccolta robusta MeteoHub."""

import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

BASE_URL = "https://meteohub.agenziaitaliameteo.it"
OUTPUT_DIR = Path("data/observations/meteohub")
MAX_BYTES = 50 * 1024 * 1024


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


def main():
    print("ENDURO - RACCOLTA METEOHUB ROBUSTA")

    # Gli errori generali restano bloccanti.
    token = login()
    requests = api_json("/api/requests", token=token)

    if not isinstance(requests, list):
        raise RuntimeError("Elenco richieste MeteoHub non valido")

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
        "requests_total": len(requests),
        "requests_completed": len(completed),
        "files_saved": 0,
        "records_downloaded": 0,
        "requests_skipped": 0,
        "errors": [],
        "saved_requests": [],
    }

    print("Richieste totali:", len(requests))
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

    if report["files_saved"] == 0:
        raise RuntimeError(
            "Nessuna estrazione valida scaricata"
        )

    print("Raccolta completata: dati pronti per la pulizia")


if __name__ == "__main__":
    main()
