#!/usr/bin/env python3
"""Normalize MeteoHub JSON Lines observations into a consistent CSV and QC report."""
import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

COLUMNS = ['network','station_id','station_name','latitude','longitude','elevation_m','datetime_utc','variable','value','unit','accumulation_seconds','quality_flag','source_file']


def value(vars_, key):
    item = vars_.get(key)
    return item.get('v') if isinstance(item, dict) else None


def iso_date(raw):
    if not isinstance(raw, str):
        raise ValueError('missing datetime')
    dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('datetime without timezone')
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def number(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def normalize(obj, source):
    blocks = obj.get('data', [])
    if not isinstance(blocks, list):
        return [], 'invalid_data'
    meta = {}
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get('vars'), dict):
            meta.update({k: v for k, v in block['vars'].items() if k.startswith(('B01','B05','B06','B07'))})
    station = value(meta, 'B01019')
    network = obj.get('network') or value(meta, 'B01194') or 'unknown'
    lat = number(value(meta, 'B05001'))
    lon = number(value(meta, 'B06001'))
    if lat is None:
        raw = number(obj.get('lat'))
        lat = raw / 100000 if raw is not None and abs(raw) > 90 else raw
    if lon is None:
        raw = number(obj.get('lon'))
        lon = raw / 100000 if raw is not None and abs(raw) > 180 else raw
    elevation = number(value(meta, 'B07030'))
    try:
        date = iso_date(obj.get('date'))
    except (ValueError, TypeError):
        return [], 'invalid_datetime'
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return [], 'invalid_coordinates'
    identifier = obj.get('ident')
    if identifier is None or str(identifier).strip() == '':
        identifier = hashlib.sha256(f'{network}|{station or ""}|{lat:.5f}|{lon:.5f}'.encode()).hexdigest()[:16]
    rows = []
    for block in blocks:
        if not isinstance(block, dict) or 'timerange' not in block:
            continue
        vars_ = block.get('vars', {})
        if not isinstance(vars_, dict):
            continue
        timerange = block.get('timerange')
        seconds = None
        if isinstance(timerange, list) and len(timerange) >= 3:
            seconds = number(timerange[2])
        # B13011 is WMO/BUFR total precipitation in kg/m2, numerically mm of liquid water.
        for code, variable, unit in [('B13011', 'precipitation', 'mm')]:
            if code not in vars_:
                continue
            measurement = vars_[code]
            reading = number(value(vars_, code))
            qc = 'ok'
            if reading is None:
                qc = 'missing_value'
            elif reading < 0:
                qc = 'negative_precipitation'
            elif seconds is None or seconds <= 0:
                qc = 'unknown_accumulation'
            rows.append({
                'network': str(network), 'station_id': str(identifier),
                'station_name': str(station or ''), 'latitude': lat,
                'longitude': lon, 'elevation_m': elevation if elevation is not None else '',
                'datetime_utc': date, 'variable': variable,
                'value': reading if reading is not None else '', 'unit': unit,
                'accumulation_seconds': int(seconds) if seconds is not None and seconds.is_integer() else (seconds or ''),
                'quality_flag': qc, 'source_file': source,
            })
    return rows, None


def run(inputs, output):
    output.mkdir(parents=True, exist_ok=True)
    files = sorted({p for root in inputs for p in ([root] if root.is_file() else root.rglob('*.jsonl')) if p.suffix.lower() == '.jsonl'})
    if not files:
        raise RuntimeError('No .jsonl files found. Supply a file or directory.')
    stats = Counter()
    all_rows = []
    for path in files:
        with path.open('r', encoding='utf-8-sig') as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                stats['source_lines'] += 1
                try:
                    obj = json.loads(line)
                    if not isinstance(obj, dict):
                        raise ValueError('not an object')
                    rows, error = normalize(obj, path.name)
                    if error:
                        stats[error] += 1
                    elif not rows:
                        stats['unsupported_variable'] += 1
                    else:
                        all_rows.extend(rows)
                except (json.JSONDecodeError, ValueError, TypeError, KeyError):
                    stats['invalid_json_lines'] += 1
    unique = {}
    for row in all_rows:
        key = (row['network'], row['station_id'], row['datetime_utc'], row['variable'], row['accumulation_seconds'])
        if key in unique:
            stats['duplicate_rows'] += 1
        else:
            unique[key] = row
    result = sorted(unique.values(), key=lambda r: (r['network'], r['station_id'], r['datetime_utc']))
    with (output / 'observations.csv').open('w', newline='', encoding='utf-8') as out:
        writer = csv.DictWriter(out, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(result)
    stations = {}
    for r in result:
        stations[(r['network'], r['station_id'])] = {k: r[k] for k in ['network','station_id','station_name','latitude','longitude','elevation_m']}
    with (output / 'stations.csv').open('w', newline='', encoding='utf-8') as out:
        writer = csv.DictWriter(out, fieldnames=['network','station_id','station_name','latitude','longitude','elevation_m'])
        writer.writeheader()
        writer.writerows(sorted(stations.values(), key=lambda r: (r['network'], r['station_name'])))
    flags = Counter(r['quality_flag'] for r in result)
    report = {
        'source_files': [str(p) for p in files], 'source_lines': stats['source_lines'],
        'normalized_rows': len(result), 'stations': len(stations),
        'networks': sorted({r['network'] for r in result}),
        'quality_flags': dict(flags), 'skipped_or_duplicate': dict(stats),
        'first_datetime_utc': min((r['datetime_utc'] for r in result), default=None),
        'last_datetime_utc': max((r['datetime_utc'] for r in result), default=None),
        'note': 'Only B13011 precipitation is supported. No region is inferred from coordinates; QC is structural, not scientific validation.'
    }
    (output / 'quality_report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not result:
        raise RuntimeError('No supported observations extracted')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('inputs', nargs='+', type=Path)
    parser.add_argument('--output', type=Path, default=Path('normalized'))
    args = parser.parse_args()
    run(args.inputs, args.output)
