#!/usr/bin/env python3
"""Evidence-only source handoff for WNP Regional Impact Watch.

This collector is not an alert classifier. Unretrieved fields stay PENDING.
Only JTWC and actual ECMWF products are collected.
"""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import urllib.error
import urllib.request

from ifs_cyclone import ORIGINS, _read_product, product_path

UTC = dt.timezone.utc
REGIONS = ('prd', 'luzon_strait', 'taiwan', 'tokaido')
PERIODS = ('T0_120', 'T144', 'T168', 'T192', 'T216', 'T240')
HAZARDS = ('genesis', 'wind', 'gust', 'rain', 'marine', 'transport', 'transition')
STEPS = (0, 6, 12, 24, 48, 72, 96, 120, 144, 168, 192, 216, 240)
MAX_SOURCE_BYTES = 2000000
USER_AGENT = 'WNPSourceHandoff/1.0 (github.com/c933103/weather)'
# Fields are marked as retrieved indices only. No ENS member or regional
# assessment is made without decoding the underlying corresponding GRIB data.
INDEX_STREAMS = (
    ('ifs', 'enfo', 'ef'),
    ('ifs', 'enfo', 'ep'),
    ('ifs', 'waef', 'ef'),
    ('aifs-ens', 'enfo', 'ef'),
)
SOURCE_URL = 'https://www.metoc.navy.mil/jtwc/products/abpwweb.txt'


def iso(time):
    return time.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def fetch_text(url):
    result = {'url': url, 'retrieved_at_utc': iso(dt.datetime.now(UTC)),
              'http_status': None, 'status': 'unverified'}
    try:
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(request, timeout=12) as response:
            data = response.read(MAX_SOURCE_BYTES + 1)
            result['http_status'] = response.status
            if len(data) > MAX_SOURCE_BYTES:
                raise ValueError('response exceeds byte limit')
            text = data.decode('utf-8-sig', errors='replace')
            result.update(status='retrieved', sha256=hashlib.sha256(data).hexdigest(),
                          text=text)
    except urllib.error.HTTPError as exc:
        result.update(http_status=exc.code, status='fetch_failed', reason='HTTP ' + str(exc.code))
        exc.close()
    except (OSError, ValueError) as exc:
        result.update(status='fetch_failed', reason=str(exc)[:250])
    return result


def jtwc_inventory(now):
    primary = fetch_text(SOURCE_URL)
    primary['status'] = 'inventory_unverified'
    data = primary.pop('text', None)
    if not data:
        return primary
    primary['raw_advisory'] = data
    # The issuing DTG is a mandatory identity check. It does not prove TCFA completeness.
    match = re.search(r'\b(?:ABPW10\s+PGTW\s+)?(\d{2})(\d{4})\b', data[:1200])
    if match:
        day, hhmm = int(match.group(1)), match.group(2)
        dates = []
        for month_shift in (-1, 0, 1):
            year = now.year + (now.month - 1 + month_shift) // 12
            month = (now.month - 1 + month_shift) % 12 + 1
            try:
                dates.append(dt.datetime(year, month, day, int(hhmm[:2]),
                                         int(hhmm[2:]), tzinfo=UTC))
            except ValueError:
                pass
        if dates:
            issue = min(dates, key=lambda when: abs(when - now))
            primary['issued_at_utc'] = iso(issue)
            primary['age_hours_at_collection'] = round((now - issue).total_seconds()/3600, 2)
            if dt.timedelta(hours=-1) <= now - issue <= dt.timedelta(hours=30):
                primary['status'] = 'advisory_timestamp_checked'
    primary['tcfa_inventory'] = 'UNVERIFIED: bulletin alone does not verify independent TCFAs'
    primary['named_warning_products'] = 'PENDING: obtain same-cycle text, image and reasoning for systems identified by analysis'
    return primary


def latest_cycles(now):
    base = now.replace(hour=12 if now.hour >= 12 else 0,
                       minute=0, second=0, microsecond=0)
    return [base, base-dt.timedelta(hours=12), base-dt.timedelta(hours=24)]


def grib_index_url(cycle, model, stream, kind, step):
    stamp = cycle.strftime('%Y%m%d%H%M%S')
    filename = f'{stamp}-{step}h-{stream}-{kind}.index'
    return f'{ORIGINS[0]}/{cycle:%Y%m%d/%Hz}/{model}/0p25/{stream}/{filename}'


def probe_index(cycle, model, stream, kind, step):
    product = fetch_text(grib_index_url(cycle, model, stream, kind, step))
    if product['status'] != 'retrieved':
        return product
    text = product.pop('text', '')
    try:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        if not rows or not all(isinstance(row, dict) and
                               isinstance(row.get('_offset'), int) and
                               isinstance(row.get('_length'), int) for row in rows):
            raise ValueError('invalid or empty index')
        fields = sorted(set(str(row.get('param', '')) for row in rows))
        product.update(status='index_only', field_names=fields,
                       indexed_field_count=len(rows))
    except (ValueError, TypeError) as exc:
        product.update(status='index_unverified', reason=str(exc)[:160])
    return product


def cyclone_cycle(cycle, stream):
    attempts = []
    for origin in ORIGINS:
        status, payload = _read_product(f'{origin}/{product_path(cycle, stream)}',
                                        cycle, stream)
        attempts.append(status)
        if payload is not None:
            return {'status': status['status'], 'cycle_utc': iso(cycle),
                    'source': status, 'attempts': attempts, 'evidence': payload}
        if status['status'] == 'decoder_unavailable':
            break
    return {'status': 'PENDING', 'cycle_utc': iso(cycle), 'attempts': attempts,
            'evidence': None}


def collection(now):
    cycles = latest_cycles(now)
    result = {
        'schema': 'wnp-watch-source-handoff/v1',
        'collected_at_utc': iso(now),
        'source_policy': 'Only JTWC and ECMWF; index metadata is NOT forecast evidence.',
        'regions': list(REGIONS), 'period_order': list(PERIODS),
        'hazard_order': list(HAZARDS),
        'coverage_matrix': {r: {p: {h: 'PENDING' for h in HAZARDS}
                                 for p in PERIODS} for r in REGIONS},
        'assessment_status': 'SOURCE_COLLECTION_ONLY; NO WEATHER CLEARANCE',
        'jtwc': jtwc_inventory(now),
        'ecmwf': {'cycle_order': [iso(c) for c in cycles],
                  'ifs_cyclone': {}, 'model_indices': {}, 'classification': 'PENDING'},
        'unresolved': [
            'TCFA independent inventory and complete same-cycle JTWC warning triads',
            'IFS/AIFS ENS regional member clouds, genesis/strike probabilities',
            'Decoded regional wind, gust, rainfall, swell and wave-period envelopes',
            'Frontal/ET/indirect and transport impacts; equal-valid comparisons',
            'No field becomes assessed from a source filename, index or older run',
        ],
    }
    # Current plus immediate fallback and predecessor are all attempted in
    # this run; previous success never blocks newer cycles.
    for cycle in cycles:
        key = iso(cycle)
        result['ecmwf']['ifs_cyclone'][key] = {
            stream: cyclone_cycle(cycle, stream) for stream in ('enfo', 'oper')}
    # Bound Open Data probes: sampled lead-times for each forecast system,
    # never misdescribe a retrieved index as decoded model fields.
    for cycle in cycles[:2]:
        for model, stream, kind in INDEX_STREAMS:
            key = f'{iso(cycle)}/{model}/{stream}/{kind}'
            result['ecmwf']['model_indices'][key] = {
                str(step): probe_index(cycle, model, stream, kind, step)
                for step in (24, 120, 240)
            }
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    result = collection(dt.datetime.now(UTC))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({'path': str(args.out), 'jtwc_status': result['jtwc']['status'],
                      'ecmwf_cycles': result['ecmwf']['cycle_order']}))


if __name__ == '__main__':
    main()
