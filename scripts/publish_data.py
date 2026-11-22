#!/usr/bin/env python3
"""Publish bounded normalized files to the data branch; full raw is an artifact."""
import argparse
from datetime import date
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from collect import MANIFEST_SCHEMA


def git(*args, cwd=None, check=True, text=True):
    return subprocess.run(['git', *args], cwd=cwd, check=check,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text)


def fetch_data_head(repository=None):
    remote = git('ls-remote', '--heads', 'origin', 'refs/heads/data', cwd=repository)
    if not remote.stdout.strip():
        return None
    commit = remote.stdout.split()[0]
    git('fetch', '--no-tags', '--depth=1', 'origin', commit, cwd=repository)
    return commit


def read_manifest(blob):
    manifest = json.loads(blob)
    if not isinstance(manifest, dict) or manifest.get('schema') != MANIFEST_SCHEMA:
        raise ValueError('Invalid forecast manifest schema.')
    if not isinstance(manifest.get('files'), dict) or not isinstance(manifest.get('quality'), dict):
        raise ValueError('Invalid forecast manifest files or quality.')
    value = manifest.get('target_date_jst')
    try:
        report_date = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'Invalid manifest report date: {value!r}') from exc
    if value != report_date.isoformat():
        raise ValueError(f'Invalid manifest report date: {value!r}')
    return manifest, report_date


def data_manifest(commit, repository=None):
    try:
        blob = git('cat-file', 'blob', f'{commit}:latest/manifest.json', cwd=repository, text=False).stdout
        return read_manifest(blob)
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise ValueError(f'Cannot read the published manifest at data commit {commit}: {exc}') from exc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('output'))
    args = parser.parse_args()
    source = (args.output / 'latest').resolve()
    manifest, candidate_date = read_manifest((source / 'manifest.json').read_bytes())
    repository = Path(git('rev-parse', '--show-toplevel').stdout.strip())
    parent = fetch_data_head(repository)
    if parent is not None:
        _, published_date = data_manifest(parent, repository)
        if candidate_date < published_date:
            print(f'Skipping publication for {candidate_date}: latest is already dated {published_date}.')
            return
    with tempfile.TemporaryDirectory(prefix='weather-publish-') as temp:
        target = Path(temp) / 'tree'
        git('worktree', 'add', '--detach', str(target), parent or 'HEAD', cwd=repository)
        try:
            if parent is None:
                git('switch', '--orphan', 'weather-data-publish', cwd=target)
            # Replace the snapshot so failed products cannot leave an old chart.
            shutil.rmtree(target / 'latest', ignore_errors=True)
            shutil.copytree(source, target / 'latest', dirs_exist_ok=True)
            git('config', 'user.name', 'github-actions[bot]', cwd=target)
            git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com', cwd=target)
            git('add', 'latest', cwd=target)
            git('commit', '-m', f'Collect {manifest["target_date_jst"]} forecast data [skip ci]', cwd=target)
            # Normal fast-forward push. Never rewrite published data history.
            git('push', 'origin', 'HEAD:refs/heads/data', cwd=target)
        finally:
            git('worktree', 'remove', '--force', str(target), cwd=repository, check=False)
    print(json.dumps({'published_date_jst': manifest['target_date_jst'], 'quality': manifest['quality']}))


if __name__ == '__main__':
    main()
