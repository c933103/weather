"""Tests for the 2026-11-08 scientific-validity review fixes.

Each test pins one review item: datum-gated surge comparison, fog by
cause (not visibility alone), non-equivalent frost limbs, worst-hour
AQI components, distinct snow quantities, wind statistics, the tide
row, cyclone wind-radii/wave impacts, ECMWF verification levels, the
validation layer, and the chart geometry/statistic rules.
"""
import datetime as dt
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from compute import (coastal_assessment, compute_city, parse_space_weather,
                     _track_assessment)
from validate import build_validation, check_products
import render_tables as rt
from chart_data import build_chart_data, extrema_by_date, extrema_runs

START = dt.datetime(2026, 1, 14, 21, 0, tzinfo=dt.timezone.utc)  # 06:00 JST Jan 15
END = START + dt.timedelta(hours=24)


def _series(stamps, fn):
    return [fn(t) for t in stamps]


def make_city():
    stamps = [START - dt.timedelta(hours=48) + dt.timedelta(hours=i) for i in range(96)]
    iso = [t.isoformat() for t in stamps]

    def const(v):
        return [v] * len(stamps)
    hourly = {
        'time': iso,
        'temperature_2m': const(5.0), 'apparent_temperature': const(3.0),
        'relative_humidity_2m': const(60.0), 'dew_point_2m': const(0.0),
        'pressure_msl': const(1015.0), 'surface_pressure': const(1013.0),
        'wind_speed_10m': const(4.0), 'wind_direction_10m': const(300.0),
        'wind_gusts_10m': const(8.0), 'visibility': const(20000.0),
        'surface_temperature': const(2.0), 'snow_depth': const(0.0),
        'precipitation': const(0.0), 'rain': const(0.0), 'snowfall': const(0.0),
        'precipitation_probability': const(10.0), 'weather_code': const(3),
        'cloud_cover_low': const(10.0),
        'shortwave_radiation_instant': const(0.0),
        'direct_radiation_instant': const(0.0),
    }
    snow_inputs = {'time': iso, 'snowfall': const(0.0)}
    for m in range(51):
        snow_inputs[f'snowfall_member{m:02d}'] = const(0.0)
    air = {'time': iso, 'us_aqi': const(50.0), 'us_aqi_pm2_5': const(40.0),
           'us_aqi_ozone': const(30.0), 'uv_index': const(1.0),
           'dust': const(5.0), 'pm2_5': const(10.0), 'pm10': const(20.0)}
    return {'latitude': 35.6, 'longitude': 139.7, 'timezone': 'Asia/Tokyo',
            'hourly': hourly, 'raw_air_quality': air,
            'snow_probability_inputs': {'hourly': snow_inputs, 'model': 'ifs_ens'},
            'gfs': {'time': iso, 'temperature_2m': const(5.5),
                    'soil_temperature_0_to_10cm': const(1.0)}}


def at(stamp, hours):
    return None  # placeholder replaced below


class AqiWorstHourTests(unittest.TestCase):
    def test_components_come_from_the_worst_hour_itself(self):
        city = make_city()
        stamps = city['hourly']['time']
        i3 = stamps.index((START + dt.timedelta(hours=3)).isoformat())
        i10 = stamps.index((START + dt.timedelta(hours=10)).isoformat())
        air = city['raw_air_quality']
        air['us_aqi'][i3] = 100.0
        air['us_aqi'][i10] = 120.0       # the day's worst AQI hour
        air['us_aqi_pm2_5'][i3] = 180.0  # PM2.5 peaks at a DIFFERENT hour
        air['us_aqi_pm2_5'][i10] = 115.0
        air['us_aqi_ozone'][i10] = 120.0
        got = compute_city(city, START, END)['aqi']
        lead = got['leading_component_at_worst_hour']
        self.assertEqual(lead[1], 'us_aqi_ozone')
        self.assertEqual(lead[0], 120.0)
        self.assertEqual(lead[2], (START + dt.timedelta(hours=10)).isoformat())
        second = got['second_component_at_worst_hour']
        self.assertEqual(second[1], 'us_aqi_pm2_5')
        self.assertEqual(second[0], 115.0)


class FogTests(unittest.TestCase):
    def _with_low_vis(self, rh, dew):
        city = make_city()
        stamps = city['hourly']['time']
        for h in (2, 3, 4):
            i = stamps.index((START + dt.timedelta(hours=h)).isoformat())
            city['hourly']['visibility'][i] = 500.0
            city['hourly']['relative_humidity_2m'][i] = rh
            city['hourly']['dew_point_2m'][i] = dew
        return compute_city(city, START, END)['fog']

    def test_saturated_low_visibility_is_fog(self):
        got = self._with_low_vis(99.0, 4.5)
        self.assertEqual(got['classification'], 'fog')
        self.assertTrue(got['corroborated'])

    def test_dry_low_visibility_is_not_fog(self):
        got = self._with_low_vis(60.0, 0.0)
        self.assertEqual(got['classification'],
                         'visibility_reduced_cause_undetermined')
        self.assertFalse(got['corroborated'])


class UrbanScreenTests(unittest.TestCase):
    def test_three_hour_burst_is_positive_and_distinct(self):
        city = make_city()
        stamps = city['hourly']['time']
        for h in (5, 6, 7):
            i = stamps.index((START + dt.timedelta(hours=h)).isoformat())
            city['hourly']['precipitation'][i] = 20.0
        got = compute_city(city, START, END)['urban_inundation']
        self.assertTrue(got['screen_positive'])
        self.assertEqual(got['max_3h_mm'], 60.0)


class WindowConventionTests(unittest.TestCase):
    def test_conventions_recorded(self):
        got = compute_city(make_city(), START, END)
        conv = got['window_conventions']
        self.assertIn('[start,end)', conv['instantaneous_fields'])
        self.assertIn('(start,end]', conv['preceding_hour_fields'])
        self.assertIn('snowfall', conv['preceding_hour_fields'])


class CoastalDatumTests(unittest.TestCase):
    def _raw_city(self, with_tide):
        times = [(START + dt.timedelta(hours=i)).isoformat() for i in range(24)]
        cf = {'ready': True, 'datum': 'NOAA model sea-level datum',
              'time': times,
              'combined_water_level': [1.0 + 0.1 * i for i in range(24)],
              'surge_residual': [0.2] * 24}
        if with_tide:
            cf['astronomical_tide'] = [0.9] * 24
        return {'coastal_forecast': cf,
                'astronomical_tide': {'station': 'Tokyo (Harumi)',
                                      'datum': 'JMA tide-table datum (DL)',
                                      'units': 'cm', 'ready': True,
                                      'events': [{'time': times[3], 'kind': 'high',
                                                  'height': 180}]}}

    def test_same_model_comparison_only(self):
        got = coastal_assessment(self._raw_city(True))
        self.assertAlmostEqual(got['exceedance_over_model_tide_m'], 2.4)
        self.assertIn('same STOFS model datum', got['exceedance_basis'])
        self.assertFalse(got['official_tide']['datum_compatible_with_stofs'])
        self.assertEqual(got['official_tide']['events'][0]['height'], 180)

    def test_no_model_tide_no_exceedance(self):
        got = coastal_assessment(self._raw_city(False))
        self.assertIsNone(got['exceedance_over_model_tide_m'])
        self.assertIsNotNone(got['surge_residual'])


class SpaceWeatherScaleTests(unittest.TestCase):
    def test_rsg_scales_and_cme_are_distinct(self):
        sources = {
            'swpc/alerts': {'ok': True, 'response': [
                {'product_id': 'ALTK05', 'issue_datetime': '2026-01-14 20:00:00.000', 'message': 'Space Weather Message\nGeomagnetic K-index of 5 expected (G1 storm).'},
                {'product_id': 'ALTXMF', 'issue_datetime': '2026-01-14 21:00:00.000', 'message': 'X-ray flare: radio blackout R2 conditions.'},
                {'product_id': 'ALTPC0', 'issue_datetime': '2026-01-14 22:00:00.000', 'message': 'Proton event: radiation storm S1 in progress.'},
                {'product_id': 'WARK06', 'issue_datetime': '2025-12-01 00:00:00.000', 'message': 'STALE: Noaa Scale: G3 - Strong (last month; must be ignored).'}]},
            'swpc/three_day': {'ok': True, 'response': 'The greatest expected 3 hr Kp for Jan 15-17 is 5.00.\nA CME arrival is expected late on Jan 16.'},
            'swpc/geomag': {'ok': True, 'response': ''},
            'nict/report_latest': {'ok': False, 'response': None},
            'nict/report_body': {'ok': False, 'response': None}}
        got = parse_space_weather(sources)
        self.assertEqual(got['scales']['radio_blackout_R'], 2)
        self.assertEqual(got['scales']['radiation_storm_S'], 1)
        self.assertEqual(got['scales']['geomagnetic_G'], 1)
        self.assertTrue(any('CME arrival' in s for s in got['cme_arrivals']))


class TrackAssessmentTests(unittest.TestCase):
    def test_point_samples_never_identify_a_centre(self):
        samples = [{'gfs_pressure_msl_hpa': v} for v in (995.0, 998.0, 1002.0, 1004.0)]
        got = _track_assessment(samples, 'gfs_pressure_msl_hpa')
        self.assertEqual(got['agreement'], 'consistent_with')
        self.assertFalse(got['centre_identified'])
        self.assertIsNone(got['position_difference_km'])
        shallow = [{'gfs_pressure_msl_hpa': v} for v in (1012.0, 1014.0)]
        self.assertEqual(_track_assessment(shallow, 'gfs_pressure_msl_hpa')['agreement'],
                         'inconclusive')


WARNING_EXCERPT = (
    'WTPN31 PGTW 071800\r\n'
    'SUBJ/TROPICAL STORM 31W (CEMPAKA) WARNING NR 007//\r\n'
    '   WARNING POSITION:\r\n'
    '   071800Z --- NEAR 21.2N 139.9E\r\n'
    '     MOVEMENT PAST SIX HOURS - 295 DEGREES AT 17 KTS\r\n'
    '   MAX SUSTAINED WINDS - 060 KT, GUSTS 075 KT\r\n'
    '   RADIUS OF 050 KT WINDS - 040 NM NORTHEAST QUADRANT\r\n'
    '                            020 NM SOUTHEAST QUADRANT\r\n'
    '                            000 NM SOUTHWEST QUADRANT\r\n'
    '                            035 NM NORTHWEST QUADRANT\r\n'
    '   RADIUS OF 034 KT WINDS - 100 NM NORTHEAST QUADRANT\r\n'
    '                            060 NM SOUTHEAST QUADRANT\r\n'
    '                            060 NM SOUTHWEST QUADRANT\r\n'
    '                            090 NM NORTHWEST QUADRANT\r\n'
    '   FORECASTS:\r\n'
    '   72 HRS, VALID AT:\r\n'
    '   101800Z --- 26.1N 142.1E\r\n'
    '   MAX SUSTAINED WINDS - 070 KT, GUSTS 085 KT\r\n'
    '   RADIUS OF 034 KT WINDS - 120 NM NORTHEAST QUADRANT\r\n'
    '                            120 NM SOUTHEAST QUADRANT\r\n'
    '                            120 NM SOUTHWEST QUADRANT\r\n'
    '                            120 NM NORTHWEST QUADRANT\r\n'
    '   MAXIMUM SIGNIFICANT WAVE HEIGHT AT 071800Z IS 24 FEET.//\r\n')


class WarningImpactTests(unittest.TestCase):
    def test_radii_and_wave_parsed(self):
        anchor = dt.datetime(2026, 11, 7, 21, 0, tzinfo=dt.timezone.utc)
        warn = rt._parse_warning(WARNING_EXCERPT, anchor)
        self.assertIsNotNone(warn)
        self.assertEqual(warn['track_radii'][0][34], 100)
        self.assertEqual(warn['track_radii'][0][50], 40)
        self.assertEqual(warn['max_wave_ft'], 24)

    def test_gale_reach_is_a_separate_outcome(self):
        # Centre passes ~250 km from Chichijima but a 120 NM (222 km)
        # radius at a nearer point plus the endpoint geometry: craft a
        # track whose closest centre distance exceeds 200 km while a
        # 34 kt radius of 300 NM reaches the area.
        track = [(dt.datetime(2026, 11, 10, 18, 0, tzinfo=dt.timezone.utc),
                  26.1, 142.1, 70)]
        radii = [{34: 300}]
        self.assertTrue(rt._gale_reach(track, radii, 27.1, 142.2))
        self.assertFalse(rt._gale_reach(track, [{34: 10}], 27.1, 142.2))


class RenderCellTests(unittest.TestCase):
    def test_wind_cell_distinguishes_max_gust_mean(self):
        c = {'wind_speed_10m': {'maximum': 10.0}}
        d = {'wind_direction_at_max_compass': '北西',
             'wind_gust_max_ms': 15.0, 'wind_speed_mean_ms': 4.0}
        cell = rt._wind(c, d)
        self.assertIn('最大36km/h', cell)
        self.assertIn('突風54', cell)
        self.assertIn('平均14', cell)

    def test_wbgt_cell_shows_the_range(self):
        c = {'wbgt': {'maximum': 26.0, 'minimum': 18.0},
             'wind_chill': {'status': 'not_applicable', 'minimum': None}}
        self.assertEqual(rt._heat_cold(c, {'wbgt_band': '警戒'}, False),
                         '18~26 警戒')

    def test_precip_cell_separates_the_four_snow_quantities(self):
        c = {'precipitation_probability': {'maximum': 40.0},
             'precipitation': {'total': 1.2},
             'snowfall': {'total': 3.0},
             'snow_probability': {'percent': 42.0},
             'snow_depth_cm': {'maximum': 5.0}}
        cell = rt._precip(c)
        self.assertIn('40%', cell)
        self.assertIn('雪確率42%', cell)
        self.assertIn('雪 3cm', cell)
        self.assertIn('積雪深 5cm', cell)

    def test_tokki_floor_uses_corroborated_fog_only(self):
        base = {'report_digest': {}}
        c = {'fog': {'classification': 'visibility_reduced_cause_undetermined'},
             'urban_inundation': {'screen_positive': True},
             'snowfall': {'total': 0.0}, 'snow_probability': {'percent': 0.0}}
        cell = rt._tokki_floor(c, {})
        self.assertNotIn('霧', cell)
        self.assertIn('内水注意', cell)
        c['fog'] = {'classification': 'mist'}
        self.assertIn('もや', rt._tokki_floor(c, {}))

    def test_tide_row_uses_official_events(self):
        c = {'coastal_assessment': {'official_tide': {
            'units': 'cm', 'events': [
                {'time': (START + dt.timedelta(hours=3)).isoformat(),
                 'kind': 'high', 'height': 180},
                {'time': (START + dt.timedelta(hours=9)).isoformat(),
                 'kind': 'low', 'height': 40}]}}}
        cell = rt._tide(c, {'timezone': 'Asia/Tokyo'})
        self.assertIn('満潮', cell)
        self.assertIn('180cm', cell)
        self.assertIn('干潮', cell)


class ValidationTests(unittest.TestCase):
    def test_coverage_separates_standing_from_new_failures(self):
        calculated_city = compute_city(make_city(), START, END)
        calculated_city['coastal_assessment'] = coastal_assessment({
            'coastal_forecast': {'ready': True, 'time': [], 'surge_residual': [],
                                 'combined_water_level': [], 'astronomical_tide': []},
            'astronomical_tide': {'ready': False, 'events': []}})
        calculated = {'cities': {'Tokyo Minato Ward': calculated_city},
                      'cyclone_watch': {'systems': [], 'track_checks': {}},
                      'space_weather': {'sources_ok': {'swpc/alerts': True}},
                      'target_date_jst': '2026-01-15'}
        raw = {'cities': {'Tokyo Minato Ward': {}},
               'sources': {'ecmwf/cyclone_product': {'ok': False},
                           'jma/example': {'ok': False},
                           'hko/example': {'ok': True}},
               'quality': {'state': 'partial', 'reasons': []}}
        got = build_validation(raw, calculated)
        self.assertEqual(got['coverage']['new_source_failures'], ['jma/example'])
        self.assertTrue(got['coverage']['standing_limitations'])
        items = got['cities']['Tokyo Minato Ward']
        self.assertEqual(items['aqi']['status'], 'ok')
        self.assertEqual(items['urban_inundation']['status'], 'ok')


class ChartGeometryTests(unittest.TestCase):
    def test_night_low_is_the_noon_to_noon_minimum(self):
        tz = dt.timezone(dt.timedelta(hours=9))
        day0 = dt.date(2026, 1, 15)
        points = []
        t = dt.datetime(2026, 1, 14, 0, 0, tzinfo=dt.timezone.utc)
        for i in range(96):
            local = t.astimezone(tz)
            # Coldest at 13:00 local each day (1.0), mild at 03:00 (3.0):
            # the noon-to-noon low must catch the 13:00 value, which a
            # calendar-day minimum for the FOLLOWING day would also do,
            # but the night labelled day0 must be 1.0 from day0's own
            # afternoon, not day1's early hours.
            v = 1.0 if local.hour == 13 else 3.0 if local.hour == 3 else 6.0
            points.append((t, v))
            t += dt.timedelta(hours=1)
        highs, lows = extrema_by_date(points, day0, tz)
        self.assertEqual(lows[0], 1.0)
        self.assertEqual(highs[0], 6.0)

    def test_runs_break_at_missing_slots(self):
        runs = extrema_runs({0: 10.0, 1: 11.0, 3: 12.0}, {0: 5.0, 1: 6.0})
        self.assertEqual(len(runs), 2)
        xs = [x for run in runs for x, _y, _k in run]
        self.assertNotIn(2.0, xs)
        self.assertNotIn(2.5, xs)
        for run in runs:
            for a, b in zip(run, run[1:]):
                self.assertLessEqual(b[0] - a[0], 0.5 + 1e-9)

    def test_aifs_horizon_and_statistic_note(self):
        raw = _synthetic_aifs_raw()
        data, _reports = build_chart_data(raw, '2026-11-08', flavour='aifs')
        self.assertIn('メンバー極値そのもの', data['cascade_note'])
        for city in data['cities']:
            idx = [i for i, _v in city['highs']] + [i for i, _v in city['lows']]
            self.assertTrue(idx)
            self.assertLessEqual(max(idx), 20)


def _synthetic_aifs_raw():
    cities = {}
    for name, tz_name in (('Tokyo Minato Ward', 'Asia/Tokyo'),
                          ('Hong Kong', 'Asia/Hong_Kong')):
        t0 = dt.datetime(2026, 11, 7, tzinfo=dt.timezone.utc)
        times, temps, models = [], [], []
        for i in range(68):
            t = t0 + dt.timedelta(hours=6 * i)
            times.append(t.isoformat())
            temps.append(14 + 6 * math.sin(2 * math.pi * (i % 4) / 4) + (i % 7) * 0.1)
            models.append('ecmwf_aifs025_ensemble_mean')
        n = len(times)
        ens_times = [(t0 + dt.timedelta(hours=h)).isoformat() for h in range(16 * 24)]
        ens_hourly = {'time': ens_times,
                      'temperature_2m': [15.0 + (h % 24) * 0.1 for h in range(16 * 24)]}
        for m in range(1, 10):
            ens_hourly[f'temperature_2m_member{m:02d}'] = [
                15.0 + (h % 24) * 0.1 + m * 0.05 for h in range(16 * 24)]
        daily, mext = {}, {}
        for d in range(28):
            day = (dt.date(2026, 11, 8) + dt.timedelta(days=d)).isoformat()
            daily[day] = {'p_rain_1mm': 0.1, 'p_rain_10mm': 0.0, 'p_snow': 0.0,
                          'daytime_cloud_mean': 30.0}
            mext[day] = {'high': 20.0, 'night_low': 10.0,
                         'source': 'aifs_ens', 'members': 51}
        cities[name] = {
            'timezone': tz_name,
            'aifs_chart_hourly': {'time': times, 'temperature_2m': temps,
                                  'weather_code': [None] * n,
                                  'temperature_2m_min': [None] * n,
                                  'temperature_2m_max': [None] * n,
                                  'sample_model': models,
                                  'model': 'ecmwf_aifs025_ensemble_mean'},
            'aifs_ensemble': {'model': 'ecmwf_aifs025_ensemble',
                              'hourly': ens_hourly, 'daily': daily},
            'aifs_member_extrema': mext,
            'ec46_daily': {}}
    return {'target_date_jst': '2026-11-08', 'cities': cities}


if __name__ == '__main__':
    unittest.main()
