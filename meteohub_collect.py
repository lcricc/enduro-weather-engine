
import os
import json
import urllib.request
import urllib.error

BASE_URL = "https://meteohub.agenziaitaliameteo.it"


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
            "Token di autenticazione non trovato"
        )

    return token


def inspect_requests(token):
    result = api_request("/api/requests", token)

    print("METEOHUB - DIAGNOSTICA RICHIESTE")
    print("Connessione riuscita")

    if isinstance(result, list):
        print("Risposta: elenco")
        print("Numero elementi:", len(result))

        if result and isinstance(result[0], dict):
            print(
                "Campi disponibili:",
                sorted(result[0].keys())
            )

    elif isinstance(result, dict):
        print("Risposta: oggetto")
        print(
            "Campi principali:",
            sorted(result.keys())
        )

        for key, value in result.items():
            if isinstance(value, list):
                print(
                    f"Campo elenco '{key}':",
                    len(value),
                    "elementi"
                )
                if value and isinstance(value[0], dict):
                    print(
                        "Campi degli elementi:",
                        sorted(value[0].keys())
                    )

    else:
        print(
            "Tipo risposta:",
            type(result).__name__
        )

    print("Diagnostica completata")


def main():
    token = login()
    inspect_requests(token)


if __name__ == "__main__":
    main()
