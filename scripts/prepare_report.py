#!/usr/bin/env python3
"""Calculate and render this collection, publishing checksum-bound products."""
import argparse
import hashlib
import json
import os
from pathlib import Path

from collect import iso_now
from compute import compute_bundle
from read_bundle import verify_bytes


def file_entry(path):
    blob = path.read_bytes()
    return {'path': f'latest/{path.name}', 'bytes': len(blob),
            'sha256': hashlib.sha256(blob).hexdigest()}


def prepare(output):
    latest = output / 'latest'
    manifest_path = latest / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    raw_path = output / manifest['raw_artifact']['filename']
    raw_bytes = raw_path.read_bytes()
    verify_bytes(raw_bytes, manifest['raw_artifact'])
    raw = json.loads(raw_bytes)
    for key in ('target_date_jst', 'collection_utc', 'window_start_utc', 'window_end_utc',
                'collector_commit', 'workflow_run_id'):
        if raw.get(key) != manifest.get(key):
            raise ValueError(f'RAW_MANIFEST_IDENTITY_MISMATCH: {key}')
    calculated = compute_bundle(raw)
    calculated['source_raw_sha256'] = manifest['raw_artifact']['sha256']
    calc_path = latest / 'calculated.json'
    calc_path.write_text(json.dumps(calculated, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    manifest['files']['calculated'] = file_entry(calc_path)
    products = {'calculation_ready': calculated['calculation_quality']['ready'],
                'chart_ready': False, 'prepared_at_utc': iso_now(),
                'execution': 'GitHub Actions' if os.getenv('GITHUB_ACTIONS') == 'true' else 'local'}
    # Remove a prior product before rendering so a failed retry cannot publish it.
    chart_path = latest / 'chart.png'
    chart_path.unlink(missing_ok=True)
    chart_path.with_suffix('.svg').unlink(missing_ok=True)
    try:
        from render_chart import render
        products['chart_coverage'] = render(raw, raw['target_date_jst'], chart_path)
        products['chart'] = file_entry(chart_path)
        svg_path = chart_path.with_suffix('.svg')
        if svg_path.exists():
            products['chart_svg'] = file_entry(svg_path)
        products['chart_ready'] = True
    except Exception as exc:
        products['chart_error'] = f'{type(exc).__name__}: {exc}'
        chart_path.unlink(missing_ok=True)
        chart_path.with_suffix('.svg').unlink(missing_ok=True)
    manifest['products'] = products
    # Never replace the original collection quality with calculation/chart success.
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return products


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('output'))
    products = prepare(parser.parse_args().output)
    print(json.dumps(products, ensure_ascii=False))
    return 0 if products['calculation_ready'] and products['chart_ready'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
