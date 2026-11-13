
import os
import json
import urllib.request
import urllib.error
from urllib.parse import urlparse

BASE_URL = "https://meteohub.agenziaitaliameteo.it"
OUTPUT_DIR = "data/meteohub"


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
        request, timeout=45
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


def safe_download_url(value):
    if not isinstance(value, str):
        return None

    value = value.strip()

    if not value:
        return None

    parsed = urlparse(value)

    if parsed.scheme == "https":
        if parsed.hostname == "meteohub.agenziaitaliameteo.it":
            return value
        return None

    if value.startswith("/") and not value.startswith("//"):
        return BASE_URL + value

    return None


def download_file(url, token, destination):
    headers = {
        "Authorization": f"Bearer {token}"
    }

    request = urllib.request.Request(
        url,
        headers=headers,
        method="GET"
    )

    with urllib.request.urlopen(
        request, timeout=90
    ) as response:
        content = response.read()

    if not content:
        raise RuntimeError("File scaricato vuoto")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    with open(destination, "wb") as file:
        file.write(content)

    return len(content)


def inspect_requests(token):
    result = api_request("/api/requests", token)

    if not isinstance(result, list):
        raise RuntimeError(
            "Formato elenco richieste inatteso"
        )

    print("METEOHUB - CONTROLLO DOWNLOAD")
    print("Richieste trovate:", len(result))

    for index, item in enumerate(result, start=1):
        if not isinstance(item, dict):
            continue

        request_id = item.get("id")
        status = item.get("status")
        output = item.get("fileoutput")
        size = item.get("filesize")

        print(f"\nRichiesta {index}")
        print("ID presente:", request_id is not None)
        print("Stato:", status)
        print("Dimensione dichiarata:", size)
        print("Tipo fileoutput:", type(output).__name__)

        if isinstance(output, dict):
            print(
                "Campi fileoutput:",
                sorted(output.keys())
            )

        url = safe_download_url(output)

        if url is None:
            print(
                "Download non avviato: "
                "indirizzo diretto non disponibile"
            )
            continue

        if not isinstance(status, str) or status.lower() not in (
            "success",
            "completed",
            "complete",
            "finished",
            "done"
        ):
            print(
                "Download non avviato: "
                "stato non riconosciuto come completato"
            )
            continue

        destination = os.path.join(
            OUTPUT_DIR,
            f"meteohub_request_{index}.json"
        )

        try:
            downloaded = download_file(
                url,
                token,
                destination
            )

            print("Download riuscito")
            print("Byte scaricati:", downloaded)
            print("File salvato:", destination)

        except (urllib.error.URLError, OSError) as error:
            print(
                "Download non riuscito:",
                type(error).__name__
            )

    print("\nControllo completato")


def main():
    token = login()
    inspect_requests(token)


if __name__ == "__main__":
    main()
