#!/usr/bin/env python3
"""Pack the WNP harvest for reliable read-back without dropping member points."""
import argparse
import gzip
import hashlib
import json
import os
import re
from pathlib import Path


def github_publication(env):
    """Build a receipt only for the explicit publishing workflow."""
    repo = env.get('GITHUB_REPOSITORY', '')
    event = env.get('GITHUB_EVENT_NAME', '')
    sha = env.get('GITHUB_SHA', '')
    run = env.get('GITHUB_RUN_ID', '')
    attempt = env.get('GITHUB_RUN_ATTEMPT', '')
    server = env.get('GITHUB_SERVER_URL', '')
    workflow_ref = env.get('GITHUB_WORKFLOW_REF', '')
    if (not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', repo)
            or server != 'https://github.com'
            or workflow_ref != f'{repo}/.github/workflows/wnp-watch-sources.yml@refs/heads/main'
            or event not in ('schedule', 'workflow_dispatch')
            or not re.fullmatch(r'[0-9a-f]{40}', sha)
            or not all(re.fullmatch(r'[1-9][0-9]{0,19}', value) for value in (run, attempt))):
        raise ValueError('Missing or invalid WNP GitHub Actions publication provenance')
    return {
        'kind': 'github_actions',
        'event': event,
        'repository': repo,
        'workflow': '.github/workflows/wnp-watch-sources.yml',
        'run_id': run,
        'run_attempt': attempt,
        'collector_commit': sha,
        'run_url': f'{server}/{repo}/actions/runs/{run}/attempts/{attempt}',
    }


def package(source, destination, *, publication=None):
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
    if publication is not None:
        result['publication'] = publication
    (destination / 'latest.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--github-actions', action='store_true',
                        help='Require and record the publishing workflow identity')
    args = parser.parse_args()
    receipt = github_publication(os.environ) if args.github_actions else None
    package(args.source, args.output, publication=receipt)
