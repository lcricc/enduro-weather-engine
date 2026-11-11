import csv, json, os, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'data'
OUT.mkdir(exist_ok=True)
NOW = datetime.now(timezone.utc)
STAMP = NOW.strftime('%Y%m%dT%H%M%SZ')
# Tor Vergata approximate coordinates; adjust after station coordinates verified.
LOCATIONS = {'tor_vergata': (41.85, 12.60)}
MODELS = ['ecmwf_ifs025', 'icon_seamless', 'gfs_seamless', 'meteofrance_seamless', 'ukmo_seamless', 'gem_seamless']
FIELDS = ['acquired_utc','location','latitude','longitude','model','valid_time_utc','precipitation_mm','temperature_2m_c','wind_speed_10m_kmh']
rows = []
errors = []
for name, (lat, lon) in LOCATIONS.items():
    for model in MODELS:
        params = dict(latitude=lat, longitude=lon, hourly='precipitation,temperature_2m,wind_speed_10m', models=model, forecast_days=3, timezone='GMT')
        url = 'https://api.open-meteo.com/v1/forecast?' + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=35) as response:
                payload = json.load(response)
            hourly = payload['hourly']
            times = hourly['time']
            for i, valid in enumerate(times):
                rows.append([NOW.isoformat(), name, lat, lon, model, valid + 'Z', hourly['precipitation'][i], hourly['temperature_2m'][i], hourly['wind_speed_10m'][i]])
            print(f'{name}: {model}: {len(times)} ore')
        except Exception as exc:
            errors.append({'location':name, 'model':model, 'error':str(exc)})
            print(f'{name}: {model}: ERRORE {exc}')
if not rows:
    raise RuntimeError('Nessuna previsione acquisita; nessun archivio creato.')
path = OUT / f'forecasts_{STAMP}.csv'
with path.open('w', newline='', encoding='utf-8') as f:
    writer = csv.writer(f)
    writer.writerow(FIELDS)
    writer.writerows(rows)
if errors:
    (OUT / f'errors_{STAMP}.json').write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding='utf-8')
print(f'Salvate {len(rows)} righe in {path}')
