
#!/usr/bin/env python3
"""ENDURO - Richieste automatiche a tutte le reti OBS MeteoHub."""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://meteohub.agenziaitaliameteo.it"
BATCH_SIZE = 7
BATCHES_PER_DAY = 4


def api(path, token=None, payload=None):
    headers = {"Accept": "application/json"}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        BASE + path,
        data=body,
        headers=headers,
        method="POST" if body is not None else "GET",
    )

    with urllib.request.urlopen(req, timeout=90) as response:
        content = response.read()
        return json.loads(content) if content else {}


def main():
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

    print("Reti osservative disponibili:", len(networks))

    if not networks:
        raise RuntimeError("Nessun dataset OBS disponibile")

    now = datetime.now(timezone.utc)

    # Quattro gruppi quotidiani, massimo sette reti per gruppo.
    slot = now.hour // 6
    start_index = slot * BATCH_SIZE
    selected = networks[
        start_index:start_index + BATCH_SIZE
    ]

    # Recupera le ultime 26 ore per ogni rete.
    # La sovrapposizione facilita la gestione di ritardi.
    end = now - timedelta(minutes=30)
    start = end - timedelta(hours=26)

    stamp = now.strftime("%Y%m%d")
    existing_names = {
        str(item.get("request_name", ""))
        for item in requests
        if isinstance(item, dict)
    }

    created = 0
    skipped = 0
    failures = 0

    for network in selected:
        name = f"enduro-auto-{stamp}-{network}"

        if name in existing_names:
            print("Gia' richiesta:", network)
            skipped += 1
            continue

        payload = {
            "request_name": name,
            "dataset_names": [network],
            "reftime": {
                "from": start.isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z"),
                "to": end.isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z"),
            },
            "output_format": "json",
            "only_reliable": True,
        }

        try:
            response = api("/api/data", token, payload)
            print(
                "Richiesta inviata:",
                network,
                "risposta:",
                str(response)[:250],
            )
            created += 1
            existing_names.add(name)

        except urllib.error.HTTPError as exc:
            print(
                "ERRORE:",
                network,
                "HTTP",
                exc.code,
            )
            failures += 1

        except (urllib.error.URLError, TimeoutError) as exc:
            print(
                "ERRORE DI RETE:",
                network,
                type(exc).__name__,
            )
            failures += 1

    print("Reti selezionate:", len(selected))
    print("Nuove richieste:", created)
    print("Gia' presenti:", skipped)
    print("Errori:", failures)

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
