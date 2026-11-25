#!/usr/bin/env python3
"""Pack the WNP harvest for reliable read-back without dropping member points."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path


def package(source, destination):
    data = source.read_bytes()
    snapshot = json.loads(data)
    if snapshot['schema'] != 'wnp-watch-source-handoff/v1':
        raise ValueError('Unknown source evidence schema')
    destination.mkdir(parents=True, exist_ok=True)
    payload = 'latest.json.gz'
    compressed = gzip.compress(data, mtime=0)
    (destination / payload).write_bytes(compressed)

    jtwc = snapshot['jtwc']
    result = {
        'schema': 'wnp-watch-source-index/v1',
        'collected_at_utc': snapshot['collected_at_utc'],
        'assessment_status': snapshot['assessment_status'],
        'regions': snapshot['regions'],
        'payload': {'relative_path': payload, 'encoding': 'gzip',
                    'sha256': hashlib.sha256(data).hexdigest(),
                    'uncompressed_bytes': len(data),
                    'compressed_sha256': hashlib.sha256(compressed).hexdigest(),
                    'compressed_bytes': len(compressed)},
        'jtwc': {key: jtwc[key] for key in ('status', 'issued_at_utc', 'url', 'sha256')
                 if key in jtwc},
        'ecmwf_ifs_track_inventory': {
            cycle: {stream: {key: record[key] for key in ('status', 'cycle_utc')
                             if key in record}
                    for stream, record in streams.items()}
            for cycle, streams in snapshot['ecmwf']['ifs_cyclone'].items()},
    }
    if 'aifs_ens_cyclone' in snapshot['ecmwf']:
        result['ecmwf_aifs_ens_track_inventory'] = {
            cycle: {k: record[k] for k in ('status', 'cycle_utc') if k in record}
            for cycle, record in snapshot['ecmwf']['aifs_ens_cyclone'].items()}
    (destination / 'latest.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    package(args.source, args.output)
