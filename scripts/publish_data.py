#!/usr/bin/env python3
"""Publish bounded normalized files to the data branch; full raw is an artifact."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def git(*args, cwd=None, check=True):
    return subprocess.run(['git', *args], cwd=cwd, check=check,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('output'))
    args = parser.parse_args()
    source = (args.output / 'latest').resolve()
    manifest = json.loads((source / 'manifest.json').read_text())
    repository = Path(git('rev-parse', '--show-toplevel').stdout.strip())
    with tempfile.TemporaryDirectory(prefix='weather-publish-') as temp:
        target = Path(temp) / 'tree'
        remote = git('ls-remote', '--heads', 'origin', 'data', cwd=repository)
        if remote.stdout.strip():
            git('fetch', '--depth=1', 'origin', 'data', cwd=repository)
            git('worktree', 'add', '--detach', str(target), 'FETCH_HEAD', cwd=repository)
            git('switch', '-c', 'weather-data-publish', cwd=target)
        else:
            git('worktree', 'add', '--detach', str(target), 'HEAD', cwd=repository)
            git('switch', '--orphan', 'weather-data-publish', cwd=target)
        try:
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
