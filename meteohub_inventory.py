
#!/usr/bin/env python3
"""ENDURO - Inventario dataset MeteoHub."""

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://meteohub.agenziaitaliameteo.it"
OUT = Path("data/observations/meteohub")
OUT.mkdir(parents=True, exist_ok=True)


def request(path, token=None, payload=None):
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
        return json.load(response)


def main():
    username = os.environ["METEOHUB_USERNAME"]
    password = os.environ["METEOHUB_PASSWORD"]

    auth = request(
        "/auth/login",
        payload={
            "username": username,
            "password": password,
        },
    )

    token = (
        auth if isinstance(auth, str)
        else auth.get("access_token") or auth.get("token")
    )

    if not token:
        raise RuntimeError("Autenticazione fallita")

    datasets = request("/api/datasets", token)
    existing = request("/api/requests", token)

    if isinstance(datasets, dict):
        for key in ("datasets", "items", "results"):
            if isinstance(datasets.get(key), list):
                datasets = datasets[key]
                break

    if not isinstance(datasets, list):
        raise RuntimeError("Formato dataset inatteso")

    if not isinstance(existing, list):
        raise RuntimeError("Formato richieste inatteso")

    observations = []

    for dataset in datasets:
        if not isinstance(dataset, dict):
            continue

        searchable = " ".join(
            str(dataset.get(key, ""))
            for key in (
                "category",
                "type",
                "name",
                "dataset_name",
                "description",
            )
        ).lower()

        if any(
            word in searchable
            for word in (
                "observ",
                "osserv",
                "station",
                "stazion",
            )
        ):
            observations.append(dataset)

    report = {
        "generated_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "datasets_total": len(datasets),
        "observation_candidates": len(observations),
        "existing_requests": len(existing),
        "datasets": datasets,
        "observation_datasets": observations,
        "requests_metadata": [
            {
                key: item.get(key)
                for key in (
                    "id",
                    "request_name",
                    "status",
                    "dataset_names",
                    "reftime",
                )
                if key in item
            }
            for item in existing
            if isinstance(item, dict)
        ],
    }

    target = OUT / "national_inventory.json"
    target.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print("Dataset totali:", len(datasets))
    print("Dataset osservativi candidati:", len(observations))
    print("Richieste esistenti:", len(existing))
    print("Inventario:", target)


if __name__ == "__main__":
    main()
