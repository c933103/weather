import datetime as dt
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from collect import UTC, source_freshness, audit_hourly
from compute import select, snow_probability, cosine_zenith, wbgt
from specialists import extract_snow, extract_pollen


class SpecialistTests(unittest.TestCase):
    def setUp(self):
        self.start = dt.datetime(2026, 11, 7, 21, tzinfo=UTC)
        self.end = self.start+dt.timedelta(days=1)
        self.times = [(self.start+dt.timedelta(hours=h)).isoformat() for h in range(25)]

    def test_preceding_hour_totals_use_right_window_boundary(self):
        h = {'time': self.times, 'rain': [100]+[0]*23+[7]}
        actual = select(h, 'rain', self.start, self.end, accumulated=True)
        self.assertEqual(sum(v for _, v in actual), 7)
        self.assertEqual(actual[-1][0], self.end)
        instant = select(h, 'rain', self.start, self.end)
        self.assertEqual(sum(v for _, v in instant), 100)

    def test_jma_r8_snapshot_uses_latest_product_issue(self):
        source = {'response': [{'dataTypeCode': 'VPWW55', 'reportDatetime': '2026-10-26T15:44:00+09:00'},
                               {'dataTypeCode': 'VPWW58', 'reportDatetime': '2026-11-07T16:12:00+09:00'}]}
        result = source_freshness(source, dt.datetime(2026, 11, 7, 10, tzinfo=UTC))
        self.assertEqual(result['freshness']['status'], 'current')
        self.assertEqual(len(result['per_product_issue_times']), 2)

    def test_snow_probability_needs_all_51_members_and_24_hours(self):
        h = {'time': self.times, 'snowfall': [0]*25}
        h.update({f'snowfall_member{i:02d}': [0]*25 for i in range(1, 51)})
        h['snowfall_member01'][-1] = 1
        x = {'hourly': h, 'model': 'ecmwf_ifs025_ensemble'}
        result = snow_probability({'snow_probability_inputs': x}, self.start, self.end)
        self.assertTrue(result['ready'])
        self.assertAlmostEqual(result['percent'], 100/51, places=2)
        h['snowfall_member02'][-1] = None
        self.assertFalse(snow_probability({'snow_probability_inputs': x}, self.start, self.end)['ready'])

    def test_pollen_publication_closure_is_not_turned_into_zero(self):
        values = {(self.start+dt.timedelta(hours=h)).astimezone(dt.timezone(dt.timedelta(hours=9))).strftime('%Y%m%d_%H'): 2 for h in range(24)}
        result = extract_pollen({'ok': True, 'response': {'srf_1h': values}}, self.start, audit_hourly,
                                {'response': 'CLOSE_MODE:!0,TARGET_YEAR:2026'})
        self.assertTrue(result['numeric_coverage']['ready'])
        self.assertFalse(result['applicability_verified'])
        self.assertEqual(result['status'], 'retrieved_but_public_page_closed')

    def test_solar_position_has_correct_day_night_and_longitude(self):
        noon = dt.datetime(2026, 3, 20, 12, tzinfo=UTC)
        self.assertGreater(cosine_zenith(noon, 0, 0), .99)
        self.assertLess(cosine_zenith(noon, 0, 180), -.99)

    def test_wbgt_offline_without_thermofeel_installation(self):
        # Reporting may have NumPy but no internet access for package installation.
        h = {'time': self.times[:-1]}
        for field, value in [('temperature_2m', 25), ('relative_humidity_2m', 60),
                              ('surface_pressure', 1013), ('wind_speed_10m', 2),
                              ('shortwave_radiation_instant', 0), ('direct_radiation_instant', 0)]:
            h[field] = [value]*24
        with patch.dict(sys.modules, {'thermofeel': None}):
            result = wbgt({'hourly': h, 'latitude': 35.658, 'longitude': 139.745}, self.start, self.end)
        self.assertTrue(result['ready'])
        self.assertEqual(len(result['hourly']), 24)
        self.assertGreater(result['minimum'], 15)
        self.assertLess(result['maximum'], 26)


if __name__ == '__main__':
    unittest.main()
