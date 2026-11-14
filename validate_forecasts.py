#!/usr/bin/env python3
"""ENDURO WEATHER ENGINE: reproducible station-based forecast verification."""
import csv
import gzip
import json
import math
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OBS = ROOT / 'data/observations/noaa'
FC = ROOT / 'data/forecasts/national'
OUT = ROOT / 'data/validation'
VARIABLES = ('temperature_2m_c', 'wind_speed_10m_kmh')


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (AttributeError, ValueError):
        return None


def hour_key(dt):
    # GHCNh may contain multiple samples per hour. Aggregate by UTC hour.
    return dt.replace(minute=0, second=0, microsecond=0)


def observations():
    sums = defaultdict(lambda: [0.0, 0, 0.0, 0])
    files = sorted(OBS.glob('observations_*.csv'))
    for path in files:
        with path.open(newline='', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                dt = timestamp(row.get('observed_utc'))
                if dt is None or not row.get('station_id'):
                    continue
                key = (row.get('network', ''), row['station_id'], hour_key(dt))
                a = sums[key]
                for j, variable in enumerate(VARIABLES):
                    val = number(row.get(variable))
                    if val is not None:
                        a[2*j] += val
                        a[2*j+1] += 1
    return {k: tuple(a[2*j]/a[2*j+1] if a[2*j+1] else None for j in range(2)) for k, a in sums.items()}, len(files)


def forecasts():
    files = sorted(FC.glob('forecasts_*.csv.gz'))
    for path in files:
        with gzip.open(path, 'rt', encoding='utf-8', newline='') as f:
            yield from csv.DictReader(f)


def lead_bucket(hours):
    if hours is None or hours < 0:
        return None
    if hours <= 24:
        return '0-24h'
    if hours <= 48:
        return '24-48h'
    if hours <= 72:
        return '48-72h'
    if hours <= 96:
        return '72-96h'
    return None


def main():
    obs, obs_files = observations()
    metrics = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    scanned = matched = 0
    # Each archived forecast run is evaluated separately, without mixing leads.
    for row in forecasts():
        scanned += 1
        valid = timestamp(row.get('valid_time_utc'))
        issued = timestamp(row.get('acquired_utc'))
        if not valid or not issued or valid <= issued:
            continue
        bucket = lead_bucket((valid-issued).total_seconds()/3600)
        if bucket is None:
            continue
        key = (row.get('network',''), row.get('station_id',''), hour_key(valid))
        observed = obs.get(key)
        if observed is None:
            continue
        matched += 1
        for idx, variable in enumerate(VARIABLES):
            pred = number(row.get(variable))
            actual = observed[idx]
            if pred is None or actual is None:
                continue
            error = pred-actual
            a = metrics[(row.get('model',''), variable, bucket)]
            a[0] += 1
            a[1] += abs(error)
            a[2] += error*error
            a[3] += error
    results = []
    for (model, variable, bucket), (n, absolute, squared, signed) in sorted(metrics.items()):
        results.append({'model':model,'variable':variable,'lead_bucket':bucket,'count':n,
                        'mae':round(absolute/n,3),'rmse':round(math.sqrt(squared/n),3),
                        'bias':round(signed/n,3)})
    report = {'generated_utc':datetime.now(timezone.utc).isoformat(),
              'observation_files':obs_files,'observation_station_hours':len(obs),
              'forecast_rows_scanned':scanned,'matched_forecast_rows':matched,
              'metrics':results,
              'notes':['Observation samples aggregated to UTC clock hours',
                       'Forecasts compared only with matching station IDs and valid UTC hours',
                       'Metrics are descriptive, not yet a fair common-sample model ranking']}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT/'validation_latest.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='metrics'},indent=2))
    print('Metric groups:',len(results))
    if not results:
        print('No overlapping forecast/observation pairs yet; await new forecast runs.')


if __name__ == '__main__':
    main()
