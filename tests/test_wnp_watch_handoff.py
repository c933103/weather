import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from wnp_watch_handoff import package


class WnpHandoffTest(unittest.TestCase):
    def test_small_index_points_to_byte_exact_complete_evidence(self):
        evidence = {
            'schema': 'wnp-watch-source-handoff/v1',
            'collected_at_utc': '2026-11-25T16:15:23Z',
            'assessment_status': 'SOURCE_COLLECTION_ONLY; NO WEATHER CLEARANCE',
            'regions': ['prd', 'luzon_strait', 'taiwan', 'tokaido'],
            'jtwc': {'status': 'advisory_timestamp_checked',
                     'issued_at_utc': '2026-11-25T06:00:00Z', 'raw_advisory': 'text'},
            'ecmwf': {'ifs_cyclone': {'2026-11-25T12:00:00Z': {
                'enfo': {'status': 'PENDING', 'cycle_utc': '2026-11-25T12:00:00Z'},
                'oper': {'status': 'available', 'cycle_utc': '2026-11-25T12:00:00Z',
                         'evidence': {'tracks': [{'member_number': 31}] * 4000}},
            }}, 'aifs_ens_cyclone': {'2026-11-25T06:00:00Z': {
                'status': 'available', 'cycle_utc': '2026-11-25T06:00:00Z',
                'evidence': {'tracks': [{'member_number': 14}]}},
            }},
        }
        content = json.dumps(evidence, ensure_ascii=False, indent=4).encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'evidence.json'
            source.write_bytes(content)
            dest = root / 'data'
            package(source, dest)
            index = json.loads((dest / 'latest.json').read_bytes())
            self.assertEqual(index['schema'], 'wnp-watch-source-index/v1')
            self.assertEqual(index['assessment_status'], evidence['assessment_status'])
            self.assertEqual(index['regions'], evidence['regions'])
            self.assertNotIn('raw_advisory', index['jtwc'])
            self.assertEqual(index['ecmwf_ifs_track_inventory']['2026-11-25T12:00:00Z']['enfo']['status'],
                             'PENDING')
            self.assertEqual(index['ecmwf_aifs_ens_track_inventory']['2026-11-25T06:00:00Z'],
                             {'status': 'available', 'cycle_utc': '2026-11-25T06:00:00Z'})
            payload = index['payload']
            path = dest / payload['relative_path']
            compressed = path.read_bytes()
            self.assertEqual(gzip.decompress(compressed), content)
            self.assertEqual(payload['uncompressed_bytes'], len(content))
            self.assertEqual(payload['compressed_bytes'], len(compressed))
            self.assertEqual(payload['sha256'], hashlib.sha256(content).hexdigest())
            self.assertEqual(payload['compressed_sha256'], hashlib.sha256(compressed).hexdigest())
            self.assertLess((dest / 'latest.json').stat().st_size, 4096)
            package(source, dest)
            self.assertEqual(path.read_bytes(), compressed)

    def test_unrecognized_input_does_not_create_a_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'evidence.json'
            source.write_text('{"schema":"other"}')
            with self.assertRaises(ValueError):
                package(source, root / 'data')
            self.assertFalse((root / 'data' / 'latest.json').exists())
