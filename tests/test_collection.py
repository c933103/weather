import datetime as dt
import math
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from collect import (UTC, audit_hourly, audit_long, combined_temperature,
                     hko_tides, numeric, redact_secrets, report_date, source_freshness)
from read_bundle import validate_manifest, verify_bytes


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.start = dt.datetime(2026, 11, 7, 21, tzinfo=UTC)
        self.hourly = {'time': [(self.start + dt.timedelta(hours=i)).isoformat() for i in range(24)],
                       'temperature_2m': [20.0] * 24}

    def test_complete_exact_report_window(self):
        self.assertTrue(audit_hourly(self.hourly, ['temperature_2m'], self.start)['ready'])

    def test_null_duplicate_missing_field_and_wrong_day_fail(self):
        self.hourly['temperature_2m'][5] = None
        self.assertFalse(audit_hourly(self.hourly, ['temperature_2m'], self.start)['ready'])
        self.hourly['temperature_2m'][5] = 20
        self.hourly['time'][6] = self.hourly['time'][5]
        self.assertFalse(audit_hourly(self.hourly, ['temperature_2m'], self.start)['ready'])
        self.assertFalse(audit_hourly(self.hourly, ['pressure_msl'], self.start)['ready'])
        self.assertFalse(audit_hourly(self.hourly, ['temperature_2m'], self.start + dt.timedelta(days=1))['ready'])

    def test_non_finite_and_booleans_are_not_forecast_values(self):
        for bad in (math.nan, math.inf, True, '20', None):
            self.assertFalse(numeric(bad))

    def test_28_days_needs_both_boundaries(self):
        stop = self.start + dt.timedelta(days=28)
        hr = {'time': [(self.start + dt.timedelta(hours=i)).isoformat() for i in range(0, 673, 6)],
              'temperature_2m': [20] * 113}
        self.assertTrue(audit_long(hr, self.start, stop)['ready'])
        hr['time'].pop(); hr['temperature_2m'].pop()
        self.assertFalse(audit_long(hr, self.start, stop)['ready'])

    def test_ec46_tail_never_replaces_ifs_or_fills_hourly_values(self):
        a = {'time': ['2026-11-07T00:00', '2026-11-07T01:00'], 'temperature_2m': [21, 22]}
        b = {'time': ['2026-11-07T00:00', '2026-11-07T06:00'], 'temperature_2m': [19, 23]}
        merged = combined_temperature(a, b)
        self.assertEqual(merged['temperature_2m'], [21, 22, 23])
        self.assertEqual(len(merged['time']), 3)
        self.assertIn('ec46', merged['sample_model'][-1])

    def test_target_date_before_and_after_delivery(self):
        self.assertEqual(str(report_date(dt.datetime(2026, 11, 7, 20, tzinfo=UTC))), '2026-11-08')
        self.assertEqual(str(report_date(dt.datetime(2026, 11, 7, 22, tzinfo=UTC))), '2026-11-09')

    def test_stale_warning_is_not_current(self):
        source = {'response': {'reportDatetime': '2026-05-28T10:16:00+09:00'}}
        self.assertEqual(source_freshness(source, self.start)['freshness']['status'], 'stale')

    def test_hko_hour_24_rolls_into_following_local_day(self):
        source = {'ok': True, 'response': {'data': [['11', '08'] + ['1.2'] * 24]}}
        tide = hko_tides(source, 2026, dt.datetime(2026, 11, 8, 15, tzinfo=UTC),
                         dt.datetime(2026, 11, 8, 17, tzinfo=UTC))
        self.assertEqual(tide['time'], ['2026-11-08T15:00:00+00:00', '2026-11-08T16:00:00+00:00'])

    def test_api_secrets_redacted_from_urls_errors_and_nested_values(self):
        with patch.dict(os.environ, {'CWA_API_KEY': 'private-token'}):
            value = redact_secrets({'url': 'https://example.test?Authorization=private-token',
                                    'attempts': [{'error': 'private-token'}]})
            self.assertNotIn('private-token', str(value))

    def test_report_reader_rejects_wrong_date_and_stale_bundle(self):
        m = {'schema': 'east_asia_weather_raw_v3', 'target_date_jst': '2026-11-08',
             'collection_utc': self.start.isoformat(),
             'files': {k: {} for k in ('hong_kong', 'taipei', 'tokyo_minato', 'takamatsu', 'official')}}
        validate_manifest(m, '2026-11-08', self.start+dt.timedelta(hours=1), 3)
        with self.assertRaisesRegex(ValueError, 'WRONG_REPORT_DATE'):
            validate_manifest(m, '2026-11-09', self.start, 3)
        with self.assertRaisesRegex(ValueError, 'STALE_OR_FUTURE_COLLECTION'):
            validate_manifest(m, '2026-11-08', self.start+dt.timedelta(hours=4), 3)

    def test_report_reader_rejects_corrupt_data(self):
        with self.assertRaisesRegex(ValueError, 'DATASET_CHECKSUM_MISMATCH'):
            verify_bytes(b'changed', {'bytes': 7, 'sha256': '0'*64})


if __name__ == '__main__':
    unittest.main()
