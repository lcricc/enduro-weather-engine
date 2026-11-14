#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE - NOAA GHCNh Italian observations collector."""
import csv
import io
import json
import math
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'data' / 'observations' / 'noaa'
OUT.mkdir(parents=True, exist_ok=True)
YEAR = datetime.now(timezone.utc).year
LIMIT = max(0, int(os.getenv('NOAA_MAX_STATIONS', '200')))
WORKERS = max(1, min(4, int(os.getenv('NOAA_WORKERS', '4'))))
BASE = 'https://www.ncei.noaa.gov/oa/global-historical-climatology-network/hourly'
INVENTORY = BASE + '/doc/ghcnh-station-list.csv'
DATA = BASE + '/access/by-year/{year}/psv/GHCNh_{station}_{year}.psv'
STATION_COLUMNS = ['network', 'station_id', 'station_name', 'latitude', 'longitude', 'elevation_m']
OBS_COLUMNS = ['network', 'station_id', 'observed_utc', 'temperature_2m_c', 'wind_speed_10m_kmh']


def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'EnduroWeatherEngine/2.0 (GHCNh)'})
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read().decode('utf-8-sig', errors='replace')


def number(raw):
    try:
        value = float(raw)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def stations():
    rows = csv.DictReader(io.StringIO(fetch(INVENTORY)))
    found = []
    for row in rows:
        sid = (row.get('GHCN_ID') or row.get('ID') or '').strip()
        if not sid.startswith('IT'):
            continue
        lat = number(row.get('LATITUDE'))
        lon = number(row.get('LONGITUDE'))
        if lat is None or lon is None or not (35 <= lat <= 48 and 6 <= lon <= 19):
            continue
        elevation = number(row.get('ELEVATION'))
        found.append({
            'network': 'noaa_ghcnh', 'station_id': sid,
            'station_name': (row.get('NAME') or '').strip(),
            'latitude': lat, 'longitude': lon,
            'elevation_m': elevation if elevation is not None and elevation > -900 else '',
        })
    if not found:
        raise RuntimeError('No Italian GHCNh stations found; inspect NOAA inventory schema')
    found.sort(key=lambda r: r['station_id'])
    return found[:LIMIT] if LIMIT else found


def utc_time(raw):
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def get_time(row):
    for name in ('DATE', 'Date', 'date', 'DATE_TIME', 'datetime', 'Datetime', 'DATE_TIME_UTC'):
        if row.get(name):
            return utc_time(row[name])
    return None


def qc_ok(row, variable):
    flag = (row.get(variable + '_Quality_Code') or '').strip()
    # NOAA GHCNh flags: numeric 2/3/6/7 and alphabetic QC flags are suspect/failed.
    return flag not in {'2', '3', '6', '7'} and not (len(flag) == 1 and flag.islower())


def collect(station):
    sid = station['station_id']
    url = DATA.format(year=YEAR, station=sid)
    try:
        content = fetch(url)
        reader = csv.DictReader(io.StringIO(content), delimiter='|')
        if not reader.fieldnames or 'temperature' not in reader.fieldnames:
            return sid, [], 'Unexpected GHCNh columns: ' + str((reader.fieldnames or [])[:15])
        result = {}
        for row in reader:
            dt = get_time(row)
            if dt is None or dt.year != YEAR:
                continue
            temperature = number(row.get('temperature')) if qc_ok(row, 'temperature') else None
            wind = number(row.get('wind_speed')) if qc_ok(row, 'wind_speed') else None
            if temperature is not None and not (-90 <= temperature <= 60):
                temperature = None
            if wind is not None and not (0 <= wind <= 100):
                wind = None
            if temperature is None and wind is None:
                continue
            key = dt.isoformat()
            previous = result.setdefault(key, {
                'network': 'noaa_ghcnh', 'station_id': sid,
                'observed_utc': key, 'temperature_2m_c': '',
                'wind_speed_10m_kmh': '',
            })
            if temperature is not None and previous['temperature_2m_c'] == '':
                previous['temperature_2m_c'] = temperature
            if wind is not None and previous['wind_speed_10m_kmh'] == '':
                previous['wind_speed_10m_kmh'] = round(wind * 3.6, 2)
        return sid, list(result.values()), None
    except urllib.error.HTTPError as exc:
        return sid, [], 'HTTP ' + str(exc.code)
    except Exception as exc:
        return sid, [], type(exc).__name__ + ': ' + str(exc)


def save_csv(path, columns, rows):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def main():
    selected = stations()
    print(f'GHCNh Italian stations selected: {len(selected)}', flush=True)
    observations = []
    successful = set()
    failures = []
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(collect, s) for s in selected]
        for future in as_completed(futures):
            sid, rows, error = future.result()
            if rows:
                successful.add(sid)
                observations.extend(rows)
            else:
                failures.append({'station_id': sid, 'error': error or 'No valid 2026 data'})
    observations.sort(key=lambda r: (r['station_id'], r['observed_utc']))
    save_csv(OUT / f'stations_{YEAR}.csv', STATION_COLUMNS,
             [s for s in selected if s['station_id'] in successful])
    save_csv(OUT / f'observations_{YEAR}.csv', OBS_COLUMNS, observations)
    report = {
        'source': 'NOAA GHCNh', 'year': YEAR,
        'eligible_stations': len(selected),
        'downloaded_files': len(successful),
        'stations_with_observations': len(successful),
        'observations': len(observations), 'failures': failures,
        'note': 'GHCNh station IDs differ from legacy ISD; validation requires matching identifiers.'
    }
    (OUT / f'report_{YEAR}.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'failures'}, indent=2), flush=True)
    print('First failures:', failures[:5], flush=True)
    if not observations:
        raise RuntimeError('No GHCNh observations acquired. Check station URLs and PSV schema in logs.')


if __name__ == '__main__':
    main()
