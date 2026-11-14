
#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - Richieste MeteoHub nazionali."""

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://meteohub.agenziaitaliameteo.it"
BATCH_SIZE = 7

LARGE_NETWORKS = {
    "dpcn-puglia",
    "dpcn-sardegna",
    "dpcn-sicilia",
    "dpcn-umbria",
}

SHORT_HOURS = 3
NORMAL_HOURS = 26


def api(path, token=None, payload=None):
    headers = {"Accept": "application/json"}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        BASE + path,
        data=body,
        headers=headers,
        method="POST" if body is not None else "GET",
    )

    with urllib.request.urlopen(
        request, timeout=90
    ) as response:
        content = response.read()
        return json.loads(content) if content else {}


def login():
    auth = api(
        "/auth/login",
        payload={
            "username": os.environ["METEOHUB_USERNAME"],
            "password": os.environ["METEOHUB_PASSWORD"],
        },
    )

    token = (
        auth if isinstance(auth, str)
        else auth.get("access_token") or auth.get("token")
    )

    if not token:
        raise RuntimeError("Token MeteoHub assente")

    return token


def utc_string(value):
    return value.isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def main():
    token = login()

    datasets = api("/api/datasets", token)
    requests = api("/api/requests", token)

    if not isinstance(datasets, list):
        raise RuntimeError("Elenco dataset non valido")

    if not isinstance(requests, list):
        raise RuntimeError("Elenco richieste non valido")

    networks = sorted({
        item["id"]
        for item in datasets
        if isinstance(item, dict)
        and item.get("category") == "OBS"
        and isinstance(item.get("id"), str)
    })

    if not networks:
        raise RuntimeError("Nessuna rete OBS disponibile")

    now = datetime.now(timezone.utc)
    slot = now.hour // 6

    selected = networks[
        slot * BATCH_SIZE:
        (slot + 1) * BATCH_SIZE
    ]

    end = now - timedelta(minutes=30)
    stamp = now.strftime("%Y%m%d")

    existing_names = {
        str(item.get("request_name", ""))
        for item in requests
        if isinstance(item, dict)
    }

    created = 0
    skipped = 0
    failures = 0

    print("Reti OBS disponibili:", len(networks))
    print("Reti selezionate:", len(selected))

    for network in selected:
        hours = (
            SHORT_HOURS
            if network in LARGE_NETWORKS
            else NORMAL_HOURS
        )

        start = end - timedelta(hours=hours)

        name = (
            f"enduro-auto-{stamp}-"
            f"s{slot}-{network}"
        )

        if name in existing_names:
            print("GIA PRESENTE:", network)
            skipped += 1
            continue

        payload = {
            "request_name": name,
            "dataset_names": [network],
            "reftime": {
                "from": utc_string(start),
                "to": utc_string(end),
            },
            "output_format": "json",
            "only_reliable": True,
        }

        success = False

        for attempt in range(2):
            try:
                response = api(
                    "/api/data",
                    token,
                    payload,
                )

                print(
                    "OK:",
                    network,
                    "ore:",
                    hours,
                    "risposta:",
                    str(response)[:200],
                )

                created += 1
                existing_names.add(name)
                success = True
                break

            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 0:
                    print(
                        "HTTP 401:",
                        network,
                        "- nuova autenticazione",
                    )

                    try:
                        token = login()
                    except (
                        urllib.error.HTTPError,
                        urllib.error.URLError,
                        TimeoutError,
                        ValueError,
                        RuntimeError,
                    ) as auth_error:
                        print(
                            "AUTENTICAZIONE FALLITA:",
                            network,
                            type(auth_error).__name__,
                        )
                        break

                    continue

                print(
                    "ERRORE HTTP:",
                    network,
                    exc.code,
                )
                break

            except (
                urllib.error.URLError,
                TimeoutError,
                ValueError,
            ) as exc:
                print(
                    "ERRORE:",
                    network,
                    type(exc).__name__,
                )
                break

        if not success:
            failures += 1

    print()
    print("===== RIEPILOGO =====")
    print("Nuove richieste:", created)
    print("Duplicate saltate:", skipped)
    print("Errori:", failures)

    if failures:
        print("ATTENZIONE: richieste non riuscite")

    print("Operazione terminata")


if __name__ == "__main__":
    main()
