import datetime as dt
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from collect import combined_temperature, jtwc_system_ids, parse_warning_track
from compute import (chart_confidence_for_city, compass16, frost_crosscheck,
                     grade_from_spread, hazard_flags, parse_jtwc_abpw,
                     parse_space_weather)
from specialists import extract_temperature_ensemble

ABPW_SAMPLE = (
    "ABPW10 PGTW 070600\r\n"
    "SUBJ/SIGNIFICANT TROPICAL WEATHER ADVISORY FOR THE WESTERN AND SOUTH \r\n"
    "PACIFIC OCEANS/070600Z-080600ZNOV2026//\r\n"
    "1. WESTERN NORTH PACIFIC AREA (180 TO MALAY PENINSULA):\r\n"
    "   A. TROPICAL CYCLONE SUMMARY:\r\n"
    "      (1) AT 07NOV26 0000Z, TROPICAL STORM 31W (CEMPAKA) WAS LOCATED \r\n"
    "NEAR 19.0N 144.2E, APPROXIMATELY 321 NM NORTH OF ANDERSEN AFB, AND HAD \r\n"
    "TRACKED WEST-NORTHWESTWARD AT 16 KNOTS OVER THE PAST SIX HOURS. MAXIMUM \r\n"
    "SUSTAINED SURFACE WINDS WERE ESTIMATED AT 35 KNOTS GUSTING TO 45 KNOTS. \r\n"
    "SEE REF A (WTPN31 PGTW 070300) FOR FURTHER DETAILS.\r\n"
    "      (2) NO OTHER TROPICAL CYCLONES.\r\n"
    "   B. TROPICAL DISTURBANCE SUMMARY: NONE.\r\n"
    "   C. SUBTROPICAL SYSTEM SUMMARY: NONE.\r\n"
    "2. SOUTH PACIFIC AREA (WEST COAST OF SOUTH AMERICA TO 135 EAST):\r\n"
    "   A. TROPICAL CYCLONE SUMMARY: NONE.\r\n")


class JtwcTests(unittest.TestCase):
    def test_system_ids_from_advisory(self):
        self.assertEqual(jtwc_system_ids(ABPW_SAMPLE), ['31W'])
        self.assertEqual(jtwc_system_ids('no systems here'), [])
        self.assertEqual(jtwc_system_ids(None), [])

    def test_abpw_digest(self):
        digest = parse_jtwc_abpw(ABPW_SAMPLE)
        self.assertEqual(len(digest['systems']), 1)
        system = digest['systems'][0]
        self.assertEqual(system['id'], '31W')
        self.assertEqual(system['name'], 'CEMPAKA')
        self.assertEqual(system['category'], 'TROPICAL STORM')
        self.assertEqual(system['latitude'], 19.0)
        self.assertEqual(system['longitude'], 144.2)
        self.assertEqual(system['movement_kt'], 16)
        self.assertEqual(system['max_winds_kt'], 35)
        self.assertEqual(system['gusts_kt'], 45)
        self.assertTrue(digest['no_other_cyclones'])
        self.assertTrue(digest['disturbances_none'])
        self.assertTrue(digest['subtropical_none'])

    def test_abpw_empty(self):
        digest = parse_jtwc_abpw(None)
        self.assertEqual(digest['systems'], [])


class HelperTests(unittest.TestCase):
    def test_grade_thresholds(self):
        self.assertEqual(grade_from_spread(1.2), '高')
        self.assertEqual(grade_from_spread(1.21), '中')
        self.assertEqual(grade_from_spread(2.2), '中')
        self.assertEqual(grade_from_spread(2.21), '低')
        self.assertEqual(grade_from_spread(None), '低')

    def test_hazard_flag_thresholds(self):
        self.assertTrue(hazard_flags(55, None, None)['high_pm'])
        self.assertTrue(hazard_flags(None, 155, None)['high_pm'])
        self.assertFalse(hazard_flags(54.9, 154, None)['high_pm'])
        self.assertTrue(hazard_flags(None, None, 0.0)['frost'])
        self.assertFalse(hazard_flags(None, None, 0.1)['frost'])
        self.assertFalse(hazard_flags(None, None, None)['frost'])

    def test_compass(self):
        self.assertEqual(compass16(0), '北')
        self.assertEqual(compass16(90), '東')
        self.assertEqual(compass16(225), '南西')
        self.assertIsNone(compass16(None))


def synthetic_city():
    start = dt.datetime(2026, 11, 7, 15, tzinfo=dt.timezone.utc)  # 2026-11-08 00:00 JST
    times = [(start + dt.timedelta(hours=i)).isoformat() for i in range(72)]
    hourly = {'time': times}
    fields = ['temperature_2m'] + [f'temperature_2m_member{m:02d}' for m in range(1, 12)]
    for index, field in enumerate(fields):
        values = []
        for i in range(72):
            day = i // 24
            base = 20.0 if day == 0 else 15.0 + index * 1.0  # day 2 spreads members widely
            values.append(base + (i % 24) * 0.1)
        hourly[field] = values
    return {'timezone': 'Asia/Tokyo', 'temperature_ensemble': {'hourly': hourly}}


class ConfidenceTests(unittest.TestCase):
    def test_extract_temperature_ensemble(self):
        city = synthetic_city()
        source = {'ok': True, 'response': {'hourly': city['temperature_ensemble']['hourly'],
                                           'hourly_units': {}}}
        block = extract_temperature_ensemble(source, dt.datetime(2026, 11, 7, 21, tzinfo=dt.timezone.utc),
                                             dt.datetime(2026, 11, 23, 21, tzinfo=dt.timezone.utc))
        self.assertEqual(block['check']['member_fields'], 12)
        self.assertFalse(block['check']['members_ready'])  # fixture has only 12 fields

    def test_confidence_grades(self):
        grades = chart_confidence_for_city(synthetic_city(), dt.date(2026, 11, 8))
        self.assertEqual(grades['2026-11-08']['basis'], 'ensemble_spread')
        self.assertEqual(grades['2026-11-08']['grade'], '高')
        self.assertEqual(grades['2026-11-09']['grade'], '低')
        beyond = grades['2026-11-18']
        self.assertEqual(beyond['basis'], 'outside_ensemble_coverage')
        self.assertEqual(beyond['grade'], '低')

    def test_confidence_missing_ensemble(self):
        self.assertEqual(chart_confidence_for_city({'timezone': 'Asia/Tokyo'}, dt.date(2026, 11, 8)), {})


class CascadeTests(unittest.TestCase):
    def series(self, start, hours, step, value):
        base = dt.datetime(2026, 11, 8, tzinfo=dt.timezone.utc) + dt.timedelta(hours=start)
        return {'time': [(base + dt.timedelta(hours=step * i)).isoformat() for i in range(hours)],
                'temperature_2m': [value] * hours, 'weather_code': [3] * hours}

    def test_three_tier_cascade_order_and_cutoffs(self):
        ifs = self.series(0, 10, 1, 10.0)          # hours 0..9
        ens = self.series(5, 10, 1, 20.0)          # overlaps; only hours 10..14 may enter
        ec46 = self.series(12, 6, 6, 30.0)         # overlaps; only after hour 14
        merged = combined_temperature(ifs, ec46, ens)
        models = merged['sample_model']
        self.assertEqual(models[:10], ['ecmwf_ifs_9km'] * 10)
        self.assertEqual(models[10:15], ['ecmwf_ifs025_ensemble_mean'] * 5)
        self.assertTrue(all(m == 'ecmwf_ec46_ensemble_mean_36km' for m in models[15:]))
        self.assertEqual(merged['temperature_2m'][12], 20.0)

    def test_two_tier_call_still_works(self):
        merged = combined_temperature(self.series(0, 5, 1, 10.0), self.series(5, 3, 6, 30.0))
        self.assertEqual(merged['model'], 'ecmwf_ifs_9km+ecmwf_ec46_ensemble_mean_36km')

    def test_naive_time_strings_from_open_meteo(self):
        # The real ensemble API returns naive ISO strings for a GMT request;
        # extraction must not crash comparing them with the aware window.
        city = synthetic_city()
        hourly = {k: v for k, v in city['temperature_ensemble']['hourly'].items()}
        hourly['time'] = [t.replace('+00:00', '') for t in hourly['time']]
        source = {'ok': True, 'response': {'hourly': hourly, 'hourly_units': {}}}
        block = extract_temperature_ensemble(source, dt.datetime(2026, 11, 7, 21, tzinfo=dt.timezone.utc),
                                             dt.datetime(2026, 11, 23, 21, tzinfo=dt.timezone.utc))
        self.assertGreater(block['check']['member_fields'], 0)
        self.assertGreater(block['check']['samples_in_chart_span'], 0)

    def test_ensemble_mean_series(self):
        city = synthetic_city()
        source = {'ok': True, 'response': {'hourly': city['temperature_ensemble']['hourly'],
                                           'hourly_units': {}}}
        block = extract_temperature_ensemble(source, dt.datetime(2026, 11, 7, 21, tzinfo=dt.timezone.utc),
                                             dt.datetime(2026, 11, 23, 21, tzinfo=dt.timezone.utc))
        mean = block['mean_hourly']
        self.assertEqual(len(mean['time']), 72)
        self.assertAlmostEqual(mean['temperature_2m'][0], 20.0 + 0.0, delta=1.0)


WARNING_SAMPLE = """WTPN31 PGTW 070900
SUBJ/TROPICAL STORM 31W (CEMPAKA) WARNING NR 005//
   WARNING POSITION:
   070600Z --- NEAR 20.0N 142.8E
   MAX SUSTAINED WINDS - 045 KT, GUSTS 055 KT
   FORECASTS:
   12 HRS, VALID AT:
   071800Z --- 21.2N 140.1E
   MAX SUSTAINED WINDS - 060 KT, GUSTS 075 KT
   24 HRS, VALID AT:
   080600Z --- 22.2N 137.3E
   MAX SUSTAINED WINDS - 075 KT, GUSTS 090 KT
   72 HRS, VALID AT:
   100600Z --- 25.1N 138.6E
   MAX SUSTAINED WINDS - 085 KT, GUSTS 105 KT
"""


class WarningTrackTests(unittest.TestCase):
    def test_parse(self):
        now = dt.datetime(2026, 11, 7, 12, tzinfo=dt.timezone.utc)
        track = parse_warning_track(WARNING_SAMPLE, now)
        self.assertEqual([p['tau_h'] for p in track], [0, 12, 24, 72])
        self.assertEqual((track[0]['latitude'], track[0]['longitude']), (20.0, 142.8))
        self.assertEqual(track[-1]['valid_utc'], '2026-11-10T06:00:00+00:00')
        self.assertEqual((track[-1]['latitude'], track[-1]['longitude']), (25.1, 138.6))

    def test_non_text(self):
        now = dt.datetime(2026, 11, 7, 12, tzinfo=dt.timezone.utc)
        self.assertEqual(parse_warning_track(None, now), [])


class ChartRunTests(unittest.TestCase):
    def test_final_night_low_is_kept(self):
        from render_chart import extrema_runs
        highs = {i: 20.0 + i * 0.1 for i in range(16)}
        lows = {i: 12.0 + i * 0.1 for i in range(16)}
        runs = extrema_runs(highs, lows)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][0][0], 0.0)
        self.assertEqual(runs[0][-1], (15.5, lows[15], 'low'))

    def test_missing_night_breaks_run(self):
        from render_chart import extrema_runs
        highs = {i: 20.0 for i in range(16)}
        lows = {i: 12.0 for i in range(16) if i != 7}
        runs = extrema_runs(highs, lows)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0][-1][0], 7.0)
        self.assertEqual(runs[1][0][0], 8.0)


class SpaceWeatherTests(unittest.TestCase):
    def test_parse(self):
        sources = {
            'swpc/alerts': {'ok': True, 'response': [
                {'product_id': 'WARK04', 'message': 'Space Weather Message'},
                {'product_id': 'ALTTP2', 'message': 'Space Weather Message'}]},
            'swpc/three_day': {'ok': True, 'response': 'Forecast text\nG1 (Minor) geomagnetic storm likely\nKp= 5.00'},
            'swpc/geomag': {'ok': True, 'response': 'NOAA Geomagnetic Activity\nG2 (Moderate) watch'},
        }
        digest = parse_space_weather(sources)
        self.assertEqual(digest['alerts_count'], 2)
        self.assertIn('WARK04', digest['warning_or_alert_ids'])
        self.assertEqual(digest['max_g_scale_in_outlook'], 2)
        self.assertTrue(digest['extreme'])

    def test_kp_from_current_product_phrasing(self):
        # The live 2026-11-07 products: the maximum is stated in a sentence
        # and tabulated in UT-anchored rows; neither matches a 'Kp= N' form.
        sources = {
            'swpc/three_day': {'ok': True, 'response':
                'The greatest expected 3 hr Kp for Nov 07-Nov 09 2026 is 5.00 (NOAA Scale\n'
                'NOAA Kp index breakdown Nov 07-Nov 09 2026\n'
                '             Nov 07   Nov 08   Nov 09\n'
                '00-03UT        4.67     5.00     3.67\n'
                '03-06UT        3.67     4.33     2.67\n'},
            'swpc/geomag': {'ok': True, 'response': 'NOAA Kp index forecast 07 Nov - 09 Nov\n'},
        }
        digest = parse_space_weather(sources)
        self.assertEqual(digest['max_kp_in_outlook'], 5.0)

    def test_nict_daily_report_digest(self):
        sources = {
            'nict/report_latest': {'ok': True, 'response': {'items': [{
                'id': 'd2026311_12', 'updated': '2026-11-07T21:00+09:00',
                'summary': '太陽活動はやや活発でした。',
                'body': 'https://example.invalid/d2026311_12.txt'}]}},
            'nict/report_body': {'ok': True, 'response':
                '【地磁気活動】<br>気象庁地磁気観測所（柿岡）によると、過去24時間の最大K指数は4でした。'
                '<br>今後1日間、地磁気活動は活発な状態が予想されます。'
                '<br>今後1日間、太陽活動は活発な状態が予想されます。'
                '<br>最大のものは、7日11時57分UTに発生したC6.2フレアでした。'
                '<br>11月5日 M3.5フレアが発生。'},
        }
        digest = parse_space_weather(sources)
        nict = digest['nict']
        self.assertEqual(nict['report_id'], 'd2026311_12')
        self.assertEqual(nict['kakioka_k_max_24h'], 4)
        self.assertEqual(nict['geomagnetic_outlook'], '活発な状態が')
        self.assertEqual(nict['largest_flare_class'], 'M3.5')
        self.assertTrue(digest['sources_ok']['nict/report_body'])

    def test_quiet(self):
        digest = parse_space_weather({})
        self.assertIsNone(digest['alerts_count'])
        self.assertFalse(digest['extreme'])
        self.assertIsNone(digest['nict']['report_id'])


class FrostAgreementTests(unittest.TestCase):
    def test_crosscheck_is_contextual_only(self):
        # The GFS limb is a 0-10 cm soil temperature, NOT equivalent to
        # the IFS ground-surface (skin) temperature: it can never agree
        # or disagree with the frost flag, only accompany it.
        got = frost_crosscheck(-1.0, 0.5)
        self.assertFalse(got['equivalent_variables'])
        self.assertEqual(got['relation'], 'contextual_only')
        self.assertEqual(got['gfs_variable'], 'soil_temperature_0_to_10cm')
        self.assertFalse(got['gfs_also_below_zero'])
        self.assertTrue(frost_crosscheck(-1.0, -0.5)['gfs_also_below_zero'])
        self.assertIsNone(frost_crosscheck(None, -1.0)['ifs_surface_min_c'])
        self.assertIsNone(frost_crosscheck(-1.0, None)['gfs_also_below_zero'])


if __name__ == '__main__':
    unittest.main()
