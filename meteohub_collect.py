"""Scarica le estrazioni MeteoHub completate e le salva come JSON Lines.

Credenziali: variabili d'ambiente METEOHUB_USERNAME e METEOHUB_PASSWORD.
Output locale: data/observations/meteohub/ (da pubblicare con GitHub Actions).
"""
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
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
    request = urllib.request.Request(BASE_URL + path, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def login():
    username = os.environ.get("METEOHUB_USERNAME")
    password = os.environ.get("METEOHUB_PASSWORD")
    if not username or not password:
        raise RuntimeError("Credenziali MeteoHub mancanti")
    result = api_json("/auth/login", payload={"username": username, "password": password})
    token = result if isinstance(result, str) else (
        result.get("access_token") or result.get("token") if isinstance(result, dict) else None
    )
    if not token:
        raise RuntimeError("Token MeteoHub non trovato")
    return token


def safe_filename(fileoutput):
    if not isinstance(fileoutput, str) or not fileoutput.strip():
        return None
    parsed = urlparse(fileoutput.strip())
    if parsed.scheme or parsed.netloc:
        return None
    filename = os.path.basename(unquote(fileoutput.replace("\\", "/")))
    return filename if filename not in ("", ".", "..") else None


def download_records(filename, token):
    url = BASE_URL + "/api/data/" + quote(filename, safe="")
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "Accept": "application/json"
    })
    with urllib.request.urlopen(request, timeout=120) as response:
        content = response.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError("File oltre il limite di sicurezza di 50 MB")
    if not content:
        raise ValueError("Risposta vuota")
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Il contenuto non e' UTF-8") from exc

    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError:
        records = []
        for number, line in enumerate(decoded.splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSON Lines non valido alla riga {number}: {exc.msg}") from exc
        if not records:
            raise ValueError("Nessun record JSON trovato")
        format_name = "JSON Lines"
    else:
        records = parsed if isinstance(parsed, list) else [parsed]
        format_name = "JSON"
    return records, format_name, len(content)


def save_records(request_id, records):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    identifier = re.sub(r"[^a-zA-Z0-9_-]", "_", str(request_id))[:80]
    if not identifier:
        raise ValueError("Identificativo richiesta non valido")
    target = OUTPUT_DIR / f"request_{identifier}.jsonl"
    fd, temporary = tempfile.mkstemp(prefix=".meteohub_", dir=OUTPUT_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            for record in records:
                output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                output.write("\n")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def main():
    print("METEOHUB - RACCOLTA JSON LINES")
    token = login()
    requests = api_json("/api/requests", token=token)
    if not isinstance(requests, list):
        raise RuntimeError("Formato elenco richieste inatteso")
    print("Richieste trovate:", len(requests))
    completed = [item for item in requests if isinstance(item, dict)
                 and str(item.get("status", "")).upper() == "SUCCESS"]
    print("Richieste completate:", len(completed))
    if not completed:
        print("Nessuna richiesta completata da scaricare")
        return

    saved = 0
    errors = []
    for item in completed:
        request_id = item.get("id")
        filename = safe_filename(item.get("fileoutput"))
        if request_id is None or not filename:
            errors.append("Richiesta completata senza ID o nome file")
            continue
        try:
            records, format_name, size = download_records(filename, token)
            destination = save_records(request_id, records)
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError, OSError) as exc:
            # Non registrare URL, nomi file o contenuto restituito dal server.
            errors.append(f"Richiesta {request_id}: {type(exc).__name__}")
            continue
        print(f"Richiesta {request_id}: {len(records)} record, {format_name}, {size} byte")
        print("Salvato:", destination)
        saved += 1

    print("File salvati:", saved)
    if errors:
        for error in errors:
            print("ERRORE:", error)
        raise RuntimeError(f"Raccolta incompleta: {len(errors)} richieste non salvate")
    if saved == 0:
        raise RuntimeError("Nessun file scaricato")


if __name__ == "__main__":
    main()
