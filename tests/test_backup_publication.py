from datetime import date
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from backup_guard import BACKUP_CRON, scheduled_report_date
from collect import CITIES, MANIFEST_SCHEMA


class BackupDateTests(unittest.TestCase):
    def test_observed_late_runs_keep_the_scheduled_jst_date(self):
        for created_at, day in (
            ('2026-11-20T20:05:00Z', date(2026, 11, 21)),
            ('2026-11-21T01:23:51Z', date(2026, 11, 21)),
            ('2026-11-17T02:52:41Z', date(2026, 11, 17)),
            ('2026-11-22T00:16:00Z', date(2026, 11, 22)),
            ('2026-11-22T08:05:00Z', date(2026, 11, 22)),
        ):
            with self.subTest(created_at=created_at):
                self.assertEqual(scheduled_report_date(BACKUP_CRON, created_at), day)

    def test_unbound_run_is_rejected(self):
        for created_at in ('2026-11-22T08:05:01Z', '2026-11-22T20:04:59Z'):
            with self.subTest(created_at=created_at), self.assertRaisesRegex(ValueError, '12-hour delay'):
                scheduled_report_date(BACKUP_CRON, created_at)
        with self.assertRaisesRegex(ValueError, 'Unknown backup schedule'):
            scheduled_report_date('0 20 * * *', '2026-11-22T00:16:00Z')


class DataBranchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.repo = root / 'collector'
        self.remote = root / 'remote.git'
        self.output = root / 'output'
        self.latest = self.output / 'latest'
        self.latest.mkdir(parents=True)
        self.outputs = root / 'guard-outputs'
        self.repo.mkdir()
        self.git('init', '--bare', str(self.remote))
        self.git('init')
        self.git('config', 'user.name', 'Weather test')
        self.git('config', 'user.email', 'weather@example.test')
        self.git('commit', '--allow-empty', '-m', 'Initial collector')
        self.git('remote', 'add', 'origin', str(self.remote))

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo, text=True, stderr=subprocess.PIPE).strip()

    def run_script(self, script, *args, check=True):
        return subprocess.run([sys.executable, str(SCRIPTS / script), *args], cwd=self.repo,
                              env={**os.environ, 'GITHUB_OUTPUT': str(self.outputs)},
                              capture_output=True, text=True, check=check)

    def publish(self, check=True):
        return self.run_script('publish_data.py', '--output', str(self.output), check=check)

    def data_head(self):
        return self.git('--git-dir', str(self.remote), 'rev-parse', 'refs/heads/data')

    def guard(self, expected_proceed):
        self.outputs.unlink(missing_ok=True)
        self.run_script('backup_guard.py', '--schedule', BACKUP_CRON, '--created-at', '2026-11-22T00:16:00Z')
        self.assertEqual(self.outputs.read_text().splitlines(),
                         ['target_date_jst=2026-11-22', f'proceed={expected_proceed}'])

    def write_bundle(self, day='2026-11-22', revision=1):
        manifest = {'schema': MANIFEST_SCHEMA, 'target_date_jst': day, 'quality': {'ready': False},
                    'collection_utc': '2026-11-21T20:01:36+00:00', 'collector_commit': 'collector',
                    'workflow_run_id': str(revision), 'window_start_utc': '2026-11-21T21:00:00+00:00',
                    'window_end_utc': '2026-11-22T21:00:00+00:00', 'raw_artifact': {'sha256': 'raw'},
                    'files': {}}
        records = {key: {'key': key} for key in CITIES}
        records.update(official={}, regional={}, calculated={
            **{key: manifest[key] for key in ('target_date_jst', 'collection_utc', 'window_start_utc',
                                             'window_end_utc', 'collector_commit', 'workflow_run_id')},
            'schema': 'east_asia_weather_calculated_v1', 'source_raw_sha256': 'raw'})
        for key, record in records.items():
            blob = json.dumps(record).encode()
            (self.latest / f'{key}.json').write_bytes(blob)
            manifest['files'][key] = {'path': f'latest/{key}.json', 'bytes': len(blob),
                                      'sha256': hashlib.sha256(blob).hexdigest()}
        (self.latest / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')

    def test_only_publication_for_the_intended_date_suppresses_backup(self):
        self.guard('true')
        self.write_bundle('2026-11-21')
        self.publish()
        self.guard('true')
        self.write_bundle()
        self.publish()
        self.guard('false')

    def test_missing_or_corrupt_json_does_not_count_as_publication(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                self.write_bundle()
                record = self.latest / 'calculated.json'
                if missing:
                    record.unlink()
                else:
                    record.write_text('{}', encoding='utf-8')
                self.publish()
                self.guard('true')

    def test_older_candidate_cannot_overwrite_newer_data(self):
        self.write_bundle('2026-11-23')
        self.publish()
        head = self.data_head()
        self.guard('false')
        self.write_bundle()
        result = self.publish()
        self.assertIn('latest is already dated 2026-11-23', result.stdout)
        self.assertEqual(self.data_head(), head)

    def test_same_date_correction_and_later_report_can_publish(self):
        self.write_bundle()
        self.publish()
        head = self.data_head()
        self.write_bundle(revision=2)
        self.publish()
        corrected_head = self.data_head()
        self.assertNotEqual(corrected_head, head)
        self.write_bundle('2026-11-23')
        self.publish()
        self.assertNotEqual(self.data_head(), corrected_head)

    def test_malformed_published_manifest_blocks_replacement(self):
        self.write_bundle()
        self.publish()
        self.git('fetch', 'origin', 'data')
        self.git('switch', '--detach', 'FETCH_HEAD')
        manifest = self.repo / 'latest/manifest.json'
        bad = json.loads(manifest.read_bytes())
        bad['target_date_jst'] = '2026-02-30'
        manifest.write_text(json.dumps(bad), encoding='utf-8')
        self.git('commit', '-am', 'Break the published manifest')
        self.git('push', 'origin', 'HEAD:refs/heads/data')
        head = self.data_head()
        self.write_bundle('2026-11-23')
        result = self.publish(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Cannot read the published manifest', result.stderr)
        self.assertEqual(self.data_head(), head)
