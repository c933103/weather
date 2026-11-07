import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from prepare_report import prepare
from read_bundle import validate_calculated, verify_bytes


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.raw = {'target_date_jst': '2026-11-08', 'collection_utc': '2026-11-07T20:00:00+00:00',
                    'window_start_utc': '2026-11-07T21:00:00+00:00',
                    'window_end_utc': '2026-11-08T21:00:00+00:00',
                    'collector_commit': 'a'*40, 'workflow_run_id': '123',
                    'quality': {'ready': False, 'reasons': ['Unvalidated flood reach']}, 'cities': {}}

    def test_failed_calculation_and_chart_publish_honest_diagnostics(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            latest = output / 'latest'
            latest.mkdir()
            blob = json.dumps(self.raw).encode()
            (output / 'raw.json').write_bytes(blob)
            manifest = {k: v for k, v in self.raw.items() if k != 'cities'}
            manifest.update(files={}, raw_artifact={'filename': 'raw.json', 'bytes': len(blob),
                            'sha256': hashlib.sha256(blob).hexdigest()})
            (latest / 'manifest.json').write_text(json.dumps(manifest))
            (latest / 'chart.png').write_bytes(b'old chart must be removed')
            with patch('render_chart.render', side_effect=ValueError('no real temperatures')):
                status = prepare(output)
            self.assertFalse(status['calculation_ready'])
            self.assertFalse(status['chart_ready'])
            self.assertFalse((latest / 'chart.png').exists())
            saved = json.loads((latest / 'manifest.json').read_text())
            self.assertEqual(saved['quality'], self.raw['quality'])
            calc_blob = (latest / 'calculated.json').read_bytes()
            verify_bytes(calc_blob, saved['files']['calculated'])
            calculated = json.loads(calc_blob)
            validate_calculated(calculated, saved)
            altered = copy.deepcopy(calculated)
            altered['workflow_run_id'] = 'another run'
            with self.assertRaisesRegex(ValueError, 'CALCULATION_IDENTITY_MISMATCH'):
                validate_calculated(altered, saved)
            altered = copy.deepcopy(calculated)
            altered['source_raw_sha256'] = 'b'*64
            with self.assertRaisesRegex(ValueError, 'CALCULATION_INPUT_MISMATCH'):
                validate_calculated(altered, saved)

    def test_tampered_raw_cannot_produce_calculated_products(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            (output / 'latest').mkdir()
            (output / 'raw.json').write_text(json.dumps(self.raw))
            (output / 'latest/manifest.json').write_text(json.dumps({
                'raw_artifact': {'filename': 'raw.json', 'bytes': 1, 'sha256': '0'*64}}))
            with self.assertRaisesRegex(ValueError, 'DATASET_CHECKSUM_MISMATCH'):
                prepare(output)
            self.assertFalse((output / 'latest/calculated.json').exists())


if __name__ == '__main__':
    unittest.main()
