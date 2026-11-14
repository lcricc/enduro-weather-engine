
#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - Richieste MeteoHub nazionali."""

import json
import os
import sys
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


def utc_string(value):
    return value.isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def main():
    auth = api(
        "/auth/login",
        payload={
            "username": os.environ["METEOHUB_USERNAME"],
            "password": os.environ["METEOHUB_PASSWORD"],
        },
    )

    token = (
        auth
        if isinstance(auth, str)
        else auth.get("access_token") or auth.get("token")
    )

    if not token:
        raise RuntimeError("Token MeteoHub assente")

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

        if network in LARGE_NETWORKS:
            hours = SHORT_HOURS
        else:
            hours = NORMAL_HOURS

        start = end - timedelta(hours=hours)

        # Identificativo distinto per rete, giorno e slot.
        # Evita richieste duplicate nello stesso intervallo.
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

        except urllib.error.HTTPError as exc:
            print(
                "ERRORE HTTP:",
                network,
                exc.code,
            )
            failures += 1

        except (
            urllib.error.URLError,
            TimeoutError,
        ) as exc:
            print(
                "ERRORE RETE:",
                network,
                type(exc).__name__,
            )
            failures += 1

    print()
    print("===== RIEPILOGO =====")
    print("Nuove richieste:", created)
    print("Duplicate saltate:", skipped)
    print("Errori:", failures)

    if failures:
        print(
            "ATTENZIONE: alcune reti non "
            "hanno accettato la richiesta"
        )

    print("Operazione terminata")


if __name__ == "__main__":
    main()
