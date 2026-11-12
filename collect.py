
import csv
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data"
OUT.mkdir(exist_ok=True)

NOW = datetime.now(timezone.utc)
STAMP = NOW.strftime("%Y%m%dT%H%M%SZ")

# Coordinate della stazione AEGIS Tor Vergata
LOCATIONS = {
    "tor_vergata": (41.851111, 12.631111)
}

# Otto modelli da monitorare
MODELS = [
    "ecmwf_ifs025",
    "ecmwf_aifs025",
    "icon_seamless",
    "gfs_seamless",
    "meteofrance_seamless",
    "ukmo_seamless",
    "gem_seamless",
    "arpege_world"
]

FIELDS = [
    "acquired_utc",
    "location",
    "latitude",
    "longitude",
    "model",
    "valid_time_utc",
    "precipitation_mm",
    "temperature_2m_c",
    "wind_speed_10m_kmh"
]

rows = []
errors = []

for name, (lat, lon) in LOCATIONS.items():
    for model in MODELS:

        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "precipitation,temperature_2m,wind_speed_10m",
            "models": model,
            "forecast_days": 3,
            "timezone": "GMT"
        }

        url = (
            "https://api.open-meteo.com/v1/forecast?"
            + urllib.parse.urlencode(params)
        )

        try:
            with urllib.request.urlopen(url, timeout=35) as response:
                payload = json.load(response)

            hourly = payload["hourly"]
            times = hourly["time"]
            saved = 0

            for i, valid in enumerate(times):
                valid_dt = datetime.fromisoformat(
                    valid.replace("Z", "+00:00")
                ).replace(tzinfo=timezone.utc)

                # Non archiviare ore precedenti all'acquisizione
                if valid_dt <= NOW:
                    continue

                rows.append([
                    NOW.isoformat(),
                    name,
                    lat,
                    lon,
                    model,
                    valid_dt.isoformat(),
                    hourly["precipitation"][i],
                    hourly["temperature_2m"][i],
                    hourly["wind_speed_10m"][i]
                ])
                saved += 1

            print(f"{name}: {model}: {saved} ore future")

        except Exception as exc:
            errors.append({
                "location": name,
                "model": model,
                "error": str(exc)
            })
            print(f"{name}: {model}: ERRORE {exc}")

if not rows:
    raise RuntimeError(
        "Nessuna previsione acquisita; nessun archivio creato."
    )

path = OUT / f"forecasts_{STAMP}.csv"

with path.open("w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(FIELDS)
    writer.writerows(rows)

if errors:
    error_path = OUT / f"errors_{STAMP}.json"
    error_path.write_text(
        json.dumps(errors, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

print(f"Salvate {len(rows)} righe in {path}")
print(f"Modelli riusciti: {len(MODELS) - len(errors)}/{len(MODELS)}")
