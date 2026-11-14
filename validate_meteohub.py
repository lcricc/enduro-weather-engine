#!/usr/bin/env python3
"""Conservative, reproducible validation of MeteoHub temperature/wind forecasts.

Precipitation is deliberately excluded until BUFR timerange/accumulation semantics
are verified. Do not infer meteorological units from numerical magnitude.
"""
import csv
import gzip
import json
import math
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path

OBS = Path('data/observations/meteohub/clean/observations_clean.csv')
FC_DIR = Path('data/forecasts/national')
OUT = Path('data/validation/meteohub')
# BUFR 0-12-101 air temperature; 0-11-002 wind speed.
CODES = {'B12101': 'temperature_2m_c', 'B11002': 'wind_speed_10m_kmh'}
EXPECTED = {'temperature_2m_c': 'c', 'wind_speed_10m_kmh': 'km/h'}


def num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def utc(x):
    try:
        d = datetime.fromisoformat(str(x).replace('Z', '+00:00'))
        return d.astimezone(timezone.utc) if d.tzinfo else None
    except (ValueError, TypeError):
        return None


def hour(d):
    return d.replace(minute=0, second=0, microsecond=0)


def convert(code, value, unit):
    """Only explicit, recognized units are accepted."""
    u = unit.strip().lower().replace('°', '').replace(' ', '')
    if code == 'B12101':
        if u in ('k', 'kelvin'):
            return value - 273.15
        if u in ('c', 'celsius', 'degc'):
            return value
    if code == 'B11002':
        if u in ('m/s', 'ms-1', 'm.s-1', 'm*s-1'):
            return value * 3.6
        if u in ('km/h', 'kmh', 'kmh-1'):
            return value
    return None


def load_obs(path):
    """Hourly exact-time observation; conflicting or duplicate measurements excluded."""
    vals = defaultdict(list)
    stats = Counter()
    with path.open(encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            stats['rows'] += 1
            code = (r.get('variable') or '').strip()
            if code not in CODES:
                continue
            d = utc(r.get('time_utc'))
            v = num(r.get('value'))
            network = (r.get('network') or '').strip()
            station = (r.get('station_id') or '').strip()
            if not (d and v is not None and network and station):
                stats['invalid'] += 1
                continue
            # Do not silently align a 10-minute observation to an hourly forecast.
            if d.minute or d.second or d.microsecond:
                stats['non_hourly'] += 1
                continue
            # BUFR level and timerange must be decoded before a 2m/10m comparison
            # can be scientifically justified. Preserve only groups with metadata
            # matching an explicitly supplied allowlist (see below).
            try:
                level = json.loads(r.get('level') or 'null')
                timerange = json.loads(r.get('timerange') or 'null')
            except json.JSONDecodeError:
                stats['invalid_metadata'] += 1
                continue
            if not (isinstance(level, dict) and isinstance(timerange, dict)):
                stats['unverified_level_timerange'] += 1
                continue
            # No assumptions about MeteoHub's BUFR metadata schema: by default
            # require exact approved signatures in a sidecar file.
            sig = json.dumps({'code': code, 'level': level, 'timerange': timerange}, sort_keys=True, separators=(',', ':'))
            if sig not in ALLOWED:
                stats['unapproved_metadata'] += 1
                continue
            converted = convert(code, v, r.get('unit') or '')
            if converted is None:
                stats['unknown_unit'] += 1
                continue
            vals[(network, station, d, CODES[code])].append(converted)
    result = {}
    for key, samples in vals.items():
        if len(samples) == 1:
            result[key] = samples[0]
        else:
            stats['ambiguous_station_hours'] += 1
    stats['usable_station_hours'] = len(result)
    return result, stats


def bucket(h):
    if 0 < h <= 24: return '0-24h'
    if h <= 48 and h > 24: return '24-48h'
    if h <= 72 and h > 48: return '48-72h'
    if h <= 96 and h > 72: return '72-96h'
    return None


def load_allowlist(path):
    if not path.exists():
        return set()
    raw = json.loads(path.read_text(encoding='utf-8'))
    return {json.dumps(x, sort_keys=True, separators=(',', ':')) for x in raw}


def run():
    global ALLOWED
    OUT.mkdir(parents=True, exist_ok=True)
    ALLOWED = load_allowlist(Path('meteohub_approved_metadata.json'))
    if not OBS.exists():
        raise RuntimeError('Missing MeteoHub observations_clean.csv')
    files = sorted(FC_DIR.glob('forecasts_meteohub_*.csv.gz'))
    if not files:
        raise RuntimeError('Missing MeteoHub forecast archive')
    observations, obs_stats = load_obs(OBS)
    diagnostics = Counter()
    # Each forecast issuance must be treated separately, not averaged into another.
    # Duplicates in same issuance are excluded, not silently counted twice.
    candidates = defaultdict(list)
    for path in files:
        with gzip.open(path, 'rt', encoding='utf-8', newline='') as f:
            for r in csv.DictReader(f):
                diagnostics['forecast_rows'] += 1
                issue, valid = utc(r.get('acquired_utc')), utc(r.get('valid_time_utc'))
                if not (issue and valid and valid > issue):
                    diagnostics['invalid_forecast_time'] += 1
                    continue
                if valid.minute or valid.second or valid.microsecond:
                    diagnostics['non_hourly_forecast'] += 1
                    continue
                b = bucket((valid - issue).total_seconds() / 3600)
                if not b:
                    continue
                for variable in CODES.values():
                    pred = num(r.get(variable))
                    if pred is None: continue
                    key = ((r.get('network') or '').strip(), (r.get('station_id') or '').strip(), valid, variable)
                    if key not in observations:
                        diagnostics['unmatched_variable_rows'] += 1
                        continue
                    model = (r.get('model') or '').strip()
                    if not model: continue
                    candidates[(issue, key, model, b)].append((pred, observations[key]))
    # Compare models only on identical issue, station, time, variable and lead bucket.
    by_event = defaultdict(dict)
    for (issue, key, model, b), entries in candidates.items():
        if len(entries) != 1:
            diagnostics['duplicate_forecasts'] += 1
            continue
        by_event[(issue, key, b)][model] = entries[0]
    models = sorted({m for group in by_event.values() for m in group})
    accum = defaultdict(list)
    for (issue, key, b), group in by_event.items():
        if set(group) != set(models):
            diagnostics['incomplete_model_comparison'] += 1
            continue
        variable = key[3]
        for model, (pred, obs) in group.items():
            accum[(model, variable, b)].append(pred - obs)
    metrics = []
    for (model, variable, b), errors in sorted(accum.items()):
        n = len(errors)
        metrics.append({'model': model, 'variable': variable, 'lead_bucket': b,
                        'n': n, 'mae': round(sum(map(abs, errors))/n, 4),
                        'rmse': round(math.sqrt(sum(e*e for e in errors)/n), 4),
                        'bias': round(sum(errors)/n, 4)})
    report = {'generated_utc': datetime.now(timezone.utc).isoformat(),
              'forecast_files': len(files), 'models_seen': models,
              'approved_metadata_signatures': len(ALLOWED),
              'observation_diagnostics': dict(obs_stats),
              'forecast_diagnostics': dict(diagnostics), 'metrics': metrics,
              'status': 'VALIDATED' if metrics else 'AWAITING_VERIFIABLE_PAIRS',
              'notes': ['Only explicitly approved BUFR level/timerange signatures are used',
                        'Only exact UTC hourly observations are matched',
                        'Ranking uses common model samples per issuance',
                        'Precipitation intentionally excluded pending accumulation semantics',
                        'Zero metrics is not a successful scientific validation']}
    (OUT/'validation_latest.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    with (OUT/'metrics.csv').open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['model','variable','lead_bucket','n','mae','rmse','bias'])
        w.writeheader(); w.writerows(metrics)
    print(json.dumps(report, indent=2))


ALLOWED = set()
if __name__ == '__main__':
    run()
