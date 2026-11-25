import datetime as dt
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import wnp_watch_collect as w

UTC = dt.timezone.utc

class WatchCollectionTests(unittest.TestCase):
    def test_cycle_rollover(self):
        at = dt.datetime(2026, 12, 1, 4, 10, tzinfo=UTC)
        self.assertEqual([c.isoformat() for c in w.latest_cycles(at)], [
            '2026-12-01T00:00:00+00:00',
            '2026-11-30T12:00:00+00:00',
            '2026-11-30T00:00:00+00:00'])
    def test_regions_are_not_cleared_by_missing_evidence(self):
        now = dt.datetime(2026, 11, 25, 4, 10, tzinfo=UTC)
        with patch.object(w, 'jtwc_inventory', return_value={'status': 'inventory_unverified'}), \
             patch.object(w, 'cyclone_cycle', return_value={'status': 'PENDING'}), \
             patch.object(w, 'fetch_text', side_effect=lambda url, **kw: {'url': url, 'status': 'fetch_failed'}):
            result = w.collection(now)
        self.assertEqual(len(result['coverage_matrix']), 4)
        self.assertTrue(all(result['coverage_matrix'][r][p][h] == 'PENDING'
                            for r in w.REGIONS for p in w.PERIODS for h in w.HAZARDS))
        self.assertEqual(result['ecmwf']['cycle_order'][0], '2026-11-25T00:00:00Z')
    def test_wrong_heading_and_unlabelled_timestamps_cannot_verify_jtwc_inventory(self):
        now = dt.datetime(2026, 11, 25, 4, 10, tzinfo=UTC)
        for text in ('HTTP error reference 250400', 'ABIO10 PGTW 250400',
                     'UNEXPECTED ABPW10 PGTW 250400', 'ABPW10 PGTW 252490'):
            with self.subTest(text=text), patch.object(w, 'fetch_text', return_value={
                    'status': 'retrieved', 'http_status': 200, 'text': text}):
                result = w.jtwc_inventory(now)
                self.assertEqual(result['status'], 'inventory_unverified')
                self.assertNotIn('issued_at_utc', result)
        with patch.object(w, 'fetch_text', return_value={
                'status': 'retrieved', 'http_status': 200,
                'text': 'unrelated reference 250100\r\r\nABPW10 PGTW 250400 COR\r\r\nBulletin'}):
            result = w.jtwc_inventory(now)
        self.assertEqual(result['issued_at_utc'], '2026-11-25T04:00:00Z')
        self.assertEqual(result['status'], 'advisory_timestamp_checked')
        self.assertTrue(result['tcfa_inventory'].startswith('UNVERIFIED'))

    def test_fetch_failure_remains_distinct_from_unverified_jtwc_body(self):
        with patch.object(w, 'fetch_text', return_value={
                'status': 'fetch_failed', 'http_status': 503, 'reason': 'HTTP 503'}):
            result = w.jtwc_inventory(dt.datetime(2026, 11, 25, tzinfo=UTC))
        self.assertEqual((result['status'], result['http_status']), ('fetch_failed', 503))
        self.assertNotIn('issued_at_utc', result)

    def test_packed_index_is_requested_once_and_counts_only_rows_for_each_lead(self):
        cycle = dt.datetime(2026, 11, 25, tzinfo=UTC)
        def row(step, param, kind):
            return dict(date='20261125', time='0000', stream='enfo', type=kind,
                        param=param, step=str(step), _offset=0, _length=100,
                        **{'class': 'od'})
        lines = '\n'.join(map(json.dumps, (row(24, 't', 'em'), row(24, 'u', 'es'),
                                         row('0-24', 'tpg1', 'ep'), row(120, 'tp', 'ep'), row(144, 'v', 'em'))))
        with patch.object(w, 'fetch_text', return_value={
                'status': 'retrieved', 'http_status': 200, 'text': lines}) as fetch:
            sampled = w.probe_indices(cycle, 'ifs', 'enfo', 'ep')
        fetch.assert_called_once_with(w.grib_index_url(cycle, 'ifs', 'enfo', 'ep', 240),
                                     byte_limit=w.MAX_INDEX_BYTES)
        self.assertEqual(sampled['24']['field_names'], ['t', 'tpg1', 'u'])
        self.assertEqual(sampled['24']['index_record_types'], ['em', 'ep', 'es'])
        self.assertEqual(sampled['24']['index_step_ranges'], ['0-24'])
        self.assertEqual(sampled['120']['field_names'], ['tp'])
        self.assertEqual(sampled['240']['status'], 'sample_step_not_indexed')
        self.assertNotIn('text', sampled['24'])

    def test_bad_date_type_stream_or_span_does_not_verify_an_index(self):
        cycle = dt.datetime(2026, 11, 25, tzinfo=UTC)
        good = dict(date='20261125', time='0000', stream='enfo', type='pf', model='aifs-ens',
                    param='t', step='24', _offset=0, _length=100, **{'class': 'ai'})
        changes = ({'date': '20261124'}, {'time': '1200'}, {'stream': 'waef'}, {'type': 'ep'},
                   {'model': 'aifs-single'}, {'step': '25'}, {'step': '36-24'}, {'_offset': -1}, {'_length': 0},
                   {'_offset': True})
        for change in changes:
            with self.subTest(change=change), patch.object(w, 'fetch_text', return_value={
                    'status': 'retrieved', 'text': json.dumps(dict(good, **change))}):
                self.assertEqual(w.probe_indices(cycle, 'aifs-ens', 'enfo', 'pf')['24']['status'], 'index_unverified')

    def test_aifs_files_use_pf_and_validated_lead_rows(self):
        cycle = dt.datetime(2026, 11, 25, tzinfo=UTC)
        def fetch(url, byte_limit):
            step = next(x for x in w.INDEX_SAMPLE_STEPS if f'-{x}h-' in url)
            self.assertTrue(url.endswith(f'-{step}h-enfo-pf.index'))
            return {'url': url, 'status': 'retrieved', 'text': json.dumps({
                'model': 'aifs-ens', 'date': '20261125', 'time': '0000', 'class': 'ai',
                'stream': 'enfo', 'type': 'pf', 'step': str(step), 'param': 't', '_offset': 0, '_length': 100,
            })}
        self.assertIn(('aifs-ens', 'enfo', 'pf'), w.INDEX_STREAMS)
        with patch.object(w, 'fetch_text', side_effect=fetch) as requests:
            sampled = w.probe_indices(cycle, 'aifs-ens', 'enfo', 'pf')
        self.assertEqual(requests.call_count, 3)
        self.assertTrue(all(row['status']=='index_only' and row['indexed_field_count']==1
                            for row in sampled.values()))

if __name__ == '__main__':
    unittest.main()
