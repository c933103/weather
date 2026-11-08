#!/usr/bin/env python3
"""East Asia weather chart — entry point kept for prepare_report and tests.

The chart's data derivation lives in chart_data.py (unit-tested there);
the drawing itself is done by scripts/render_chart.R (ggplot2), invoked
here as a subprocess. This module re-exports the derivation functions so
existing imports (`from render_chart import daily_icon, extrema_by_date,
...`) keep working, and render() keeps its old contract: it returns the
per-city coverage report with the emoji count the R renderer confirms it
actually drew.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

from chart_data import (CASCADE_LABELS, SITES, build_chart_data, cascade_note,
                        cascade_segments, daily_icon, ec46_probability_icon,
                        enough_coverage, extrema_by_date, extrema_runs,
                        get_hourly, member_probability_icon, parse_utc, wmo_emoji)

__all__ = ['render', 'build_chart_data', 'cascade_note', 'cascade_segments',
           'daily_icon', 'ec46_probability_icon', 'enough_coverage',
           'extrema_by_date', 'extrema_runs', 'get_hourly',
           'member_probability_icon', 'parse_utc', 'wmo_emoji',
           'CASCADE_LABELS', 'SITES']

_HERE = Path(__file__).resolve().parent


def render(raw, target_day, out_path):
    data, reports = build_chart_data(raw, target_day)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    rscript = os.environ.get('WEATHER_RSCRIPT', 'Rscript')
    with tempfile.TemporaryDirectory(prefix='chart-data-') as tmp:
        data_path = Path(tmp) / 'chart_data.json'
        data_path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        proc = subprocess.run(
            [rscript, str(_HERE / 'render_chart.R'),
             '--input', str(data_path), '--output', str(out),
             '--assets', str(_HERE / 'assets' / 'emoji')],
            capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or '').strip().splitlines()[-5:]
        raise RuntimeError('R chart renderer failed: ' + ' | '.join(tail))
    svg = out.with_suffix('.svg')
    if not out.exists() or out.stat().st_size < 5000 or not svg.exists():
        raise RuntimeError('R chart renderer did not produce a complete chart pair.')
    drawn = json.loads(proc.stdout.strip().splitlines()[-1])
    reports['emoji_composited'] = int(drawn['emoji_composited'])
    from PIL import Image
    with Image.open(out) as im:
        im.verify()
    return reports


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--target-date-jst', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    raw = json.loads(Path(args.input).read_text(encoding='utf-8'))
    print(json.dumps(render(raw, args.target_date_jst, args.output),
                     ensure_ascii=False, indent=2))
