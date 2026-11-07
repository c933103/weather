#!/usr/bin/env python3
"""Read one dated, fresh, checksum-verified data-branch snapshot for 06:00."""
import argparse
import concurrent.futures as futures
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import urllib.request

from collect import CITIES, UTC, iso_now, parse_time


def get_bytes(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={
        'User-Agent': 'EastAsiaWeatherDaily/3.0', 'Accept': 'application/vnd.github+json'}), timeout=30) as response:
        return response.read(12_000_000)


def validate_manifest(manifest, day, now, max_age_hours):
    if manifest.get('schema') != 'east_asia_weather_raw_v3':
        raise ValueError('UNSUPPORTED_SCHEMA')
    if manifest.get('target_date_jst') != day:
        raise ValueError('WRONG_REPORT_DATE')
    age = (now - parse_time(manifest['collection_utc'])).total_seconds()/3600
    if age < -0.1 or age > max_age_hours:
        raise ValueError(f'STALE_OR_FUTURE_COLLECTION: {age:.2f} hours')
    if not all(key in manifest.get('files', {}) for key in [*CITIES, 'official']):
        raise ValueError('INCOMPLETE_FILE_MANIFEST')


def verify_bytes(blob, entry):
    if len(blob) != entry['bytes'] or hashlib.sha256(blob).hexdigest() != entry['sha256']:
        raise ValueError('DATASET_CHECKSUM_MISMATCH')


def validate_calculated(record, manifest):
    if record.get('schema') != 'east_asia_weather_calculated_v1':
        raise ValueError('UNSUPPORTED_CALCULATION_SCHEMA')
    for key in ('target_date_jst', 'collection_utc', 'window_start_utc', 'window_end_utc',
                'collector_commit', 'workflow_run_id'):
        if record.get(key) != manifest.get(key):
            raise ValueError(f'CALCULATION_IDENTITY_MISMATCH: {key}')
    if record.get('source_raw_sha256') != manifest['raw_artifact']['sha256']:
        raise ValueError('CALCULATION_INPUT_MISMATCH')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', default='c933103/weather')
    parser.add_argument('--target-date-jst', required=True)
    parser.add_argument('--max-age-hours', type=float, default=3)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repository):
        raise ValueError('INVALID_REPOSITORY')
    # Pin all reads to a single commit to avoid mixing adjacent collection runs.
    ref = json.loads(get_bytes(f'https://api.github.com/repos/{args.repository}/git/ref/heads/data'))
    commit = ref['object']['sha']
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('INVALID_DATA_COMMIT')
    base = f'https://raw.githubusercontent.com/{args.repository}/{commit}/'
    manifest = json.loads(get_bytes(base+'latest/manifest.json'))
    validate_manifest(manifest, args.target_date_jst, dt.datetime.now(UTC), args.max_age_hours)
    bundle = {k: manifest[k] for k in ('schema', 'target_date_jst', 'collection_utc', 'window_start_utc',
              'window_end_utc', 'long_end_utc', 'chart_end_utc', 'collector_commit', 'workflow_run_id', 'quality')}
    bundle.update(data_commit=commit, loaded_at_utc=iso_now(), cities={}, sources={})
    def read_entry(key, entry):
        if not re.fullmatch(r'latest/[a-z_]+\.json', entry['path']):
            raise ValueError('UNEXPECTED_DATASET_PATH')
        blob = get_bytes(base+entry['path'])
        verify_bytes(blob, entry)
        return key, json.loads(blob)
    with futures.ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda kv: read_entry(*kv), manifest['files'].items()))
    for key, record in records:
        if key == 'official':
            bundle['sources'] = record
        elif key == 'regional':
            bundle['regional_forecasts'] = record
        elif key == 'calculated':
            validate_calculated(record, manifest)
            bundle['calculated'] = record
        else:
            if record.get('key') != key:
                raise ValueError('CITY_IDENTITY_MISMATCH')
            bundle['cities'][CITIES[key][0]] = record
    bundle['products'] = manifest.get('products', {})
    chart = bundle['products'].get('chart')
    if chart:
        if chart['path'] != 'latest/chart.png':
            raise ValueError('UNEXPECTED_CHART_PATH')
        blob = get_bytes(base+chart['path'])
        verify_bytes(blob, chart)
        chart_path = args.out.with_suffix('.png')
        chart_path.parent.mkdir(parents=True, exist_ok=True)
        chart_path.write_bytes(blob)
        bundle['chart_local_path'] = str(chart_path)
        bundle['chart_url'] = base+chart['path']
    chart_svg = bundle['products'].get('chart_svg')
    if chart_svg:
        if chart_svg['path'] != 'latest/chart.svg':
            raise ValueError('UNEXPECTED_CHART_SVG_PATH')
        blob = get_bytes(base+chart_svg['path'])
        verify_bytes(blob, chart_svg)
        svg_path = args.out.with_suffix('.svg')
        svg_path.parent.mkdir(parents=True, exist_ok=True)
        svg_path.write_bytes(blob)
        bundle['chart_svg_local_path'] = str(svg_path)
        bundle['chart_svg_url'] = base+chart_svg['path']
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(bundle, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(json.dumps({'date': args.target_date_jst, 'data_commit': commit,
                      'quality': bundle['quality'], 'out': str(args.out)}))


if __name__ == '__main__':
    main()
