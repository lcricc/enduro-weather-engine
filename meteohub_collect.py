import os
import json
import urllib.request
import urllib.error
from urllib.parse import quote, urlparse, unquote
from pathlib import Path

BASE_URL = "https://meteohub.agenziaitaliameteo.it"
OUTPUT_DIR = Path("data/meteohub")


def api_request(path, token=None, payload=None):
    headers = {"Accept": "application/json"}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    data = None
    method = "GET"

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"

    request = urllib.request.Request(
        BASE_URL + path,
        data=data,
        headers=headers,
        method=method
    )

    with urllib.request.urlopen(
        request, timeout=60
    ) as response:
        return json.load(response)


def login():
    username = os.environ.get("METEOHUB_USERNAME")
    password = os.environ.get("METEOHUB_PASSWORD")

    if not username or not password:
        raise RuntimeError(
            "Credenziali MeteoHub mancanti"
        )

    result = api_request(
        "/auth/login",
        payload={
            "username": username,
            "password": password
        }
    )

    if isinstance(result, str):
        token = result
    elif isinstance(result, dict):
        token = (
            result.get("access_token")
            or result.get("token")
        )
    else:
        token = None

    if not token:
        raise RuntimeError(
            "Token MeteoHub non trovato"
        )

    return token


def get_filename(fileoutput):
    if not isinstance(fileoutput, str):
        return None

    value = fileoutput.strip()

    if not value:
        return None

    parsed = urlparse(value)

    if parsed.scheme or parsed.netloc:
        return None

    filename = os.path.basename(
        unquote(value.replace("\\", "/"))
    )

    if filename in ("", ".", ".."):
        return None

    return filename


def download_data(filename, token):
    encoded_name = quote(filename, safe="")

    url = (
        BASE_URL
        + "/api/data/"
        + encoded_name
    )

    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        },
        method="GET"
    )

    with urllib.request.urlopen(
        request, timeout=120
    ) as response:
        content = response.read()

    if not content:
        raise RuntimeError(
            "Il file scaricato è vuoto"
        )

    try:
        observations = json.loads(
            content.decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError(
            "Il file ricevuto non è un JSON valido"
        ) from None

    return observations


def collect():
    print("METEOHUB - DOWNLOAD AUTOMATICO")

    token = login()
    requests = api_request(
        "/api/requests",
        token
    )

    if not isinstance(requests, list):
        raise RuntimeError(
            "Formato elenco richieste inatteso"
        )

    print("Richieste trovate:", len(requests))

    downloaded = 0

    for item in requests:
        if not isinstance(item, dict):
            continue

        status = str(
            item.get("status", "")
        ).upper()

        if status != "SUCCESS":
            continue

        filename = get_filename(
            item.get("fileoutput")
        )

        if not filename:
            print(
                "Richiesta completata senza "
                "nome file utilizzabile"
            )
            continue

        print("Download richiesta completata")

        try:
            observations = download_data(
                filename,
                token
            )

        except urllib.error.HTTPError as error:
            print(
                "Errore download HTTP:",
                error.code
            )
            continue

        OUTPUT_DIR.mkdir(
            parents=True,
            exist_ok=True
        )

        request_id = str(
            item.get("id", "unknown")
        )

        safe_id = "".join(
            c for c in request_id
            if c.isalnum() or c in ("-", "_")
        ) or "unknown"

        destination = (
            OUTPUT_DIR
            / f"meteohub_{safe_id}.json"
        )

        with open(
            destination,
            "w",
            encoding="utf-8"
        ) as output:
            json.dump(
                observations,
                output,
                ensure_ascii=False
            )

        downloaded += 1

        print("File salvato:", destination)

        if isinstance(observations, list):
            print(
                "Osservazioni:",
                len(observations)
            )

    print("Download completati:", downloaded)

    if downloaded == 0:
        raise RuntimeError(
            "Nessun file scaricato. "
            "Controllare il registro GitHub."
        )


if __name__ == "__main__":
    collect()
