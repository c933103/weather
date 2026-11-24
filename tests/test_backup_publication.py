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

    def write_bundle(self, day='2026-11-22', revision=1, collection='2026-11-21T20:01:36+00:00', omit=None):
        manifest = {'schema': MANIFEST_SCHEMA, 'target_date_jst': day, 'quality': {'ready': False},
                    'collection_utc': collection, 'collector_commit': 'collector',
                    'workflow_run_id': str(revision), 'window_start_utc': '2026-11-21T21:00:00+00:00',
                    'window_end_utc': '2026-11-22T21:00:00+00:00',
                    'chart_end_utc': '2026-11-27T21:00:00+00:00',
                    'long_end_utc': '2026-12-06T21:00:00+00:00', 'raw_artifact': {'sha256': 'raw'},
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
        if omit:
            del manifest[omit]
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

    def publish_product_snapshot(self, products):
        path = self.latest / 'manifest.json'
        manifest = json.loads(path.read_bytes())
        manifest['products'] = products
        path.write_text(json.dumps(manifest), encoding='utf-8')
        self.publish()

    @staticmethod
    def product_entry(name, blob):
        return {'path': f'latest/{name}', 'bytes': len(blob),
                'sha256': hashlib.sha256(blob).hexdigest()}

    def test_advertised_report_products_must_match_published_bytes(self):
        for key, name, blob in (
            ('chart', 'chart.png', b'chart-good'),
            ('chart_svg', 'chart.svg', b'<svg/>'),
            ('chart_aifs', 'chart_aifs.png', b'aifs-good'),
            ('table4', 'table4.md', b'valid outlook'),
            ('validation', 'validation.json', b'{"ready": false}'),
        ):
            with self.subTest(product=key):
                self.write_bundle()
                product = self.latest / name
                product.write_bytes(blob)
                products = {key: self.product_entry(name, blob), 'chart_aifs_ready': True}
                self.publish_product_snapshot(products)
                self.guard('false')
                product.write_bytes(b'x' * len(blob))
                self.publish_product_snapshot(products)
                self.guard('true')
                product.unlink()
                self.publish_product_snapshot(products)
                self.guard('true')

    def test_product_paths_and_validation_json_must_be_reader_compatible(self):
        self.write_bundle()
        (self.latest / 'alternate.png').write_bytes(b'wrong path')
        self.publish_product_snapshot({'chart': self.product_entry('alternate.png', b'wrong path')})
        self.guard('true')
        payload = b'[]'
        (self.latest / 'validation.json').write_bytes(payload)
        self.publish_product_snapshot({'validation': self.product_entry('validation.json', payload)})
        self.guard('true')

    def test_explicit_product_unavailability_does_not_discard_partial_report(self):
        self.write_bundle()
        self.publish_product_snapshot({'chart_ready': False, 'chart_aifs_ready': False,
                                       'tables_ready': False, 'outlook_ready': False})
        self.guard('false')

    def test_reader_required_envelope_fields_cannot_be_omitted(self):
        for key in ('chart_end_utc', 'long_end_utc'):
            with self.subTest(key=key):
                self.write_bundle(omit=key)
                self.publish()
                self.guard('true')

    def test_same_date_early_push_cannot_suppress_morning_backup(self):
        self.write_bundle(collection='2026-11-21T07:48:36+00:00')
        self.publish()
        self.guard('true')
        # A suitable primary remains suitable even when the backup starts
        # hours later. Test the exact inclusive reader-age boundary.
        self.write_bundle(collection='2026-11-21T18:00:00+00:00')
        self.publish()
        self.guard('false')
        self.write_bundle(collection='2026-11-21T17:59:59+00:00')
        self.publish()
        self.guard('true')

    def test_already_published_late_backup_does_not_repeat(self):
        self.write_bundle(collection='2026-11-22T00:15:00+00:00')
        self.publish()
        self.guard('false')

    def test_future_collection_timestamp_does_not_suppress_backup(self):
        self.write_bundle(collection='2099-01-01T00:00:00+00:00')
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
