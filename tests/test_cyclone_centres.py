"""Pure pressure-grid and model-track checks for JTWC-guided IFS centres."""
import math
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from cyclone_centres import (pressure_minimum_candidate, cyclonic_wind_support,
                             distance_km)
from compute import _track_assessment
from render_tables import _ecmwf_analysis, _ecmwf_values
from validate import build_validation


class PressureCentreTests(unittest.TestCase):
    def test_screened_closed_low_with_cyclonic_rotation(self):
        lats, lons = np.meshgrid(np.arange(14, 27, 0.25),
                                np.arange(134, 147, 0.25), indexing='ij')
        # A symmetric low at 20 N 140 E, with a 2-degree ring of
        # substantially higher MSLP; use Pa as in the GRIB.
        radius2 = (lats - 20.0) ** 2 + ((lons - 140.0) * math.cos(math.radians(20))) ** 2
        pressure_pa = 99000.0 + 140.0 * radius2
        c = pressure_minimum_candidate(lats, lons, pressure_pa, 20.8, 140.5)
        self.assertEqual(c['status'], 'closed_pressure_low')
        self.assertEqual((c['latitude'], c['longitude']), (20.0, 140.0))
        self.assertEqual(c['minimum_pressure_hpa'], 990.0)
        self.assertEqual(c['ring_supported_sectors'], 8)
        self.assertLess(c['distance_to_jtwc_km'], 150)
        winds = cyclonic_wind_support(north_u=-6, east_v=6, south_u=4, west_v=-5)
        self.assertTrue(winds['supported'])
        self.assertEqual(winds['cyclonic_components'], 4)

    def test_flat_field_not_a_cyclone_centre(self):
        lat, lon = np.meshgrid(np.arange(17, 24, 0.25),
                               np.arange(136, 144, 0.25), indexing='ij')
        c = pressure_minimum_candidate(lat, lon, np.full(lat.shape, 101000.0),
                                        20, 140)
        self.assertNotEqual(c['status'], 'closed_pressure_low')

    def test_open_trough_not_a_cyclone_centre(self):
        lat, lon = np.meshgrid(np.arange(12, 29, 0.25),
                               np.arange(130, 151, 0.25), indexing='ij')
        p = 101000.0 + 90.0 * (lon - 140.0)
        c = pressure_minimum_candidate(lat, lon, p, 20.0, 140.0)
        self.assertNotEqual(c['status'], 'closed_pressure_low')

    def test_wind_rotates_wrong_way(self):
        opposite = cyclonic_wind_support(north_u=5, east_v=-5, south_u=-5, west_v=5)
        self.assertFalse(opposite['supported'])
        self.assertEqual(opposite['cyclonic_components'], 0)

    def test_dateline_distance_is_short(self):
        self.assertLess(distance_km(20, 179.7, 20, -179.7), 70)


class TrackCentreAssessmentTests(unittest.TestCase):
    def _samples(self):
        return [
            {'tau_h': 24, 'valid_utc': '2026-11-16T12:00:00+00:00',
             'ecmwf_pressure_msl_hpa': 993.0,
             'model_centre_candidate': {
                 'status': 'circulation_supported', 'centre_identified': True,
                 'latitude': 21.0, 'longitude': 142.0,
                 'minimum_pressure_hpa': 986.0, 'distance_to_jtwc_km': 83.0,
                 'ring_supported_sectors': 8}},
            {'tau_h': 48, 'valid_utc': '2026-11-17T12:00:00+00:00',
             'ecmwf_pressure_msl_hpa': 999.0,
             'model_centre_candidate': {
                 'status': 'circulation_supported', 'centre_identified': True,
                 'latitude': 24.0, 'longitude': 145.0,
                 'minimum_pressure_hpa': 994.0, 'distance_to_jtwc_km': 122.0,
                 'ring_supported_sectors': 7}},
        ]

    def test_centres_and_displacement_are_kept_but_not_called_basin_wide(self):
        a = _track_assessment(self._samples(), 'ecmwf_pressure_msl_hpa')
        self.assertTrue(a['centre_identified'])
        self.assertEqual(a['centre_count'], 2)
        self.assertEqual(a['position_difference_km'], 102.5)
        self.assertEqual(a['agreement'], 'centre_candidates_compared')
        self.assertEqual(a['track_temporal_agreement'], 'two_or_more_guided_candidates')
        self.assertFalse(a['ensemble_spread_verified'])
        self.assertIn('JTWC-guided', a['search_independence'])

    def test_unconfirmed_pressure_low_is_not_centre_identification(self):
        xs = [{'ecmwf_pressure_msl_hpa': 997.0,
               'model_centre_candidate': {'status': 'pressure_minimum_only',
                                          'centre_identified': False}}]
        a = _track_assessment(xs, 'ecmwf_pressure_msl_hpa')
        self.assertFalse(a['centre_identified'])
        self.assertIsNone(a['position_difference_km'])

    def test_generated_outlook_marks_guided_scope_and_ensemble_gap(self):
        check = _track_assessment(self._samples(), 'ecmwf_pressure_msl_hpa')
        cw = {'track_checks': {
            'ecmwf_opendata/cyclone_check_31w': {
                'ok': True, 'assessment': check}},
            'ecmwf_cyclone_product': {'ok': False, 'http_status': 404,
                                     'attempts': [{'base_time': 'test'}]}}
        self.assertIn('EC46アンサンブル進路', _ecmwf_analysis(cw))
        values = _ecmwf_values(cw)
        self.assertIn('IFS局所中心候補2点', values)
        self.assertIn('103km', values)
        self.assertIn('EC46進路未検証', values)

    def test_validation_caution_despite_guided_ifs_centres(self):
        assessment = _track_assessment(self._samples(), 'ecmwf_pressure_msl_hpa')
        calculated = {'target_date_jst': '2026-11-15', 'cities': {},
                      'cyclone_watch': {
                          'systems': [{'id': '31W'}],
                          'track_checks': {'ecmwf_opendata/cyclone_check_31w': {
                              'assessment': assessment}}},
                      'space_weather': {'sources_ok': {}, 'geomagnetic_forecast_status': 'unverified'}}
        raw = {'sources': {}, 'cities': {}, 'quality': {'state': 'partial', 'reasons': []}}
        v = build_validation(raw, calculated)
        self.assertEqual(v['hazards']['cyclone_verification']['status'], 'caution')
        self.assertIn('JTWC-guided', v['hazards']['cyclone_verification']['detail'])


if __name__ == '__main__':
    unittest.main()
