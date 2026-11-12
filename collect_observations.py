
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone

import requests

BASE = Path("data/observations")
BASE.mkdir(parents=True, exist_ok=True)

REGIONS = [
    "Abruzzo", "Basilicata", "Calabria", "Campania",
    "Emilia-Romagna", "Friuli-Venezia Giulia",
    "Lazio", "Liguria", "Lombardia", "Marche",
    "Molise", "Piemonte", "Puglia", "Sardegna",
    "Sicilia", "Toscana", "Trentino-Alto Adige",
    "Umbria", "Valle d'Aosta", "Veneto"
]

SOURCES = {
    "Umbria": {
        "type": "ckan",
        "api": "https://dati.regione.umbria.it/api/3/action/package_show",
        "dataset": "sir_precipitazioni_anno_corrente",
        "resource_keyword": "giornal"
    }
}

def collect_umbria(source):
    response = requests.get(
        source["api"],
        params={"id": source["dataset"]},
        timeout=45
    )
    response.raise_for_status()

    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError("Risposta del catalogo non valida")

    resources = [
        item for item in payload["result"].get("resources", [])
        if item.get("format", "").upper() == "CSV"
        and source["resource_keyword"] in (
            item.get("name", "") + " " +
            item.get("description", "")
        ).lower()
    ]

    if not resources:
        raise RuntimeError("Nessun CSV giornaliero disponibile")

    destination = BASE / "umbria"
    destination.mkdir(parents=True, exist_ok=True)

    downloaded = 0

    for resource in resources:
        url = resource.get("url")
        if not url:
            continue

        result = requests.get(url, timeout=120)
        result.raise_for_status()

        content = result.content
        if not content:
            raise RuntimeError("File vuoto")

        checksum = hashlib.sha256(content).hexdigest()
        latest = destination / "precipitazioni_giornaliere.csv"

        if latest.exists():
            old_checksum = hashlib.sha256(
                latest.read_bytes()
            ).hexdigest()
            if checksum == old_checksum:
                print("Umbria: dati invariati")
                downloaded += 1
                continue

        timestamp = datetime.now(
            timezone.utc
        ).strftime("%Y%m%dT%H%M%SZ")

        archive = destination / f"precipitazioni_{timestamp}.csv"
        archive.write_bytes(content)
        latest.write_bytes(content)

        print("Umbria: acquisizione completata")
        downloaded += 1

    if downloaded == 0:
        raise RuntimeError("Nessuna risorsa scaricata")

def main():
    status = {}

    for region in REGIONS:
        if region not in SOURCES:
            status[region] = "pending_connector"
            continue

        try:
            if region == "Umbria":
                collect_umbria(SOURCES[region])
            status[region] = "success"
        except Exception as error:
            status[region] = "error"
            print(region, "ERRORE:", error)

    report = {
        "checked_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "regions": status
    }

    (BASE / "national_status.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    print("\nREGIONI:", len(REGIONS))
    print("OPERATIVE:",
          sum(v == "success" for v in status.values()))
    print("DA INTEGRARE:",
          sum(v == "pending_connector" for v in status.values()))

if __name__ == "__main__":
    main()
