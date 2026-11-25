"""Message-3 outlook tables: the structured special-weather outlook.

Analysis first (user correction, 2026-11-08): Table 3 reads the current
JTWC warning text, Table 4 separates 分析 from 数値・根拠.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from render_tables import render_outlook
from compute import regional_digest, parse_space_weather, jtwc_advisory_from_source
from validate import build_validation, check_products

WARNING = """WTPN31 PGTW 072100
MSGID/GENADMIN/JOINT TYPHOON WRNCEN PEARL HARBOR HI//
SUBJ/TROPICAL STORM 31W (CEMPAKA) WARNING NR 007//
RMKS/
1. TROPICAL STORM 31W (CEMPAKA) WARNING NR 007
   WARNING POSITION:
   071800Z --- NEAR 21.2N 139.9E
     MOVEMENT PAST SIX HOURS - 295 DEGREES AT 17 KTS
   PRESENT WIND DISTRIBUTION:
   MAX SUSTAINED WINDS - 060 KT, GUSTS 075 KT
   FORECASTS:
   12 HRS, VALID AT:
   080600Z --- 22.1N 137.3E
   MAX SUSTAINED WINDS - 070 KT, GUSTS 085 KT
   72 HRS, VALID AT:
   101800Z --- 26.1N 142.1E
   MAX SUSTAINED WINDS - 070 KT, GUSTS 085 KT
"""

FAR_WARNING = """WTPN31 PGTW 072100
SUBJ/TROPICAL STORM 31W (CEMPAKA) WARNING NR 007//
   WARNING POSITION:
   071800Z --- NEAR 10.0N 160.0E
     MOVEMENT PAST SIX HOURS - 270 DEGREES AT 10 KTS
   PRESENT WIND DISTRIBUTION:
   MAX SUSTAINED WINDS - 040 KT, GUSTS 050 KT
   FORECASTS:
   12 HRS, VALID AT:
   080600Z --- 10.5N 158.0E
   MAX SUSTAINED WINDS - 045 KT, GUSTS 055 KT
"""


def raw(warning=WARNING):
    if warning is None:
        return {}
    return {'collection_utc': '2026-11-08T01:17:15+00:00',
            'sources': {'jtwc/warning_31w': {'ok': True, 'response': warning}}}


def calculated(**kw):
    base = {'cyclone_watch': {
                'systems': [{'category': 'TROPICAL STORM', 'id': '31W',
                             'name': 'CEMPAKA', 'latitude': 19.0, 'longitude': 144.2,
                             'movement_text': 'WEST-NORTHWESTWARD', 'movement_kt': 16,
                             'max_winds_kt': 35, 'gusts_kt': 45}],
                'monsoon_mentions': [],
                'ecmwf_cyclone_product': {'ok': False, 'http_status': 404,
                                          'attempts': [{}] * 6,
                                          'base_time_used': None},
                'track_checks': {
                    'gfs/cyclone_check_31w': {'ok': True, 'samples': []},
                    'ecmwf_opendata/cyclone_check_31w': {'ok': True, 'samples': []}}},
            'space_weather': {'extreme': True, 'max_g_scale_in_outlook': 2,
                              'max_kp_in_outlook': 5.0, 'warning_or_alert_ids': [],
                              'nict': {'kakioka_k_max_24h': 3}},
            'regional_digest': {
                'ulaanbaatar': {'max_pressure_hpa': 1046.4, 'tendency_hpa': 2.3},
                'manchuria': {'min_pressure_hpa': 998.2, 'latitude': 44.1,
                              'longitude': 126.9, 'min_at_utc': '2026-11-10T00:00:00+00:00'},
                'sea_of_japan': {'min_pressure_hpa': 1004.6, 'latitude': 38.2,
                                 'longitude': 133.5, 'min_at_utc': '2026-11-09T06:00:00+00:00'}}}
    base.update(kw)
    return base


class RenderOutlookTests(unittest.TestCase):
    def test_table3_reads_the_current_warning(self):
        t3, _t4 = render_outlook(raw(), calculated())
        lines = t3.strip().split('\n')
        self.assertEqual(lines[0], '| 名称 | 現在位置・強度 | 移動 | 脅威・最接近 |')
        row = lines[2]
        self.assertIn('CEMPAKA（31W・TS）', row)
        # Warning values, NOT the lagging ABPW digest (19.0N 144.2E / 35 kt).
        self.assertIn('21.2N 139.9E・最大60kt・突風75kt', row)
        self.assertNotIn('144.2E', row)
        self.assertIn('| WNW 17kt |', row)
        threat = row.split('|')[4].strip()
        self.assertIn('火山列島 約', threat)
        self.assertIn('JST', threat)
        self.assertIn('kt級', threat)

    def test_table3_warning_track_far_from_all_areas(self):
        t3, _t4 = render_outlook(raw(FAR_WARNING), calculated())
        self.assertIn('対象地域への接近なし（最接近 ', t3)

    def test_table3_no_warning_falls_back_to_advisory(self):
        t3, _t4 = render_outlook(raw(None), calculated())
        row = t3.strip().split('\n')[2]
        self.assertIn('19.0N 144.2E・最大35kt・突風45kt', row)
        self.assertIn('| WNW 16kt |', row)
        self.assertIn('―（警報未発出）', row)

    def test_table3_cyclone_summary_requires_explicit_current_wnp_advisory(self):
        # Real ABPW field structure, including different regional advice.
        prefix = ('ABPW10 PGTW 231200\r\n'
                  'SUBJ/SIGNIFICANT TROPICAL WEATHER ADVISORY FOR THE WESTERN AND SOUTH\r\n'
                  'PACIFIC OCEANS REISSUED/')
        region = '1. WESTERN NORTH PACIFIC AREA (180 TO MALAY PENINSULA):\r\n'
        header = prefix + '231300Z-240600ZNOV2026//\r\nRMKS/\r\n' + region
        as_of = '2026-11-23T21:00:00+00:00'
        quiet = '   A. TROPICAL CYCLONE SUMMARY: NONE.\r\n'
        south = '2. SOUTH PACIFIC AREA (WEST COAST OF SOUTH AMERICA TO 135 EAST):\r\n' + quiet
        unverified = '熱帯低気圧：JTWCの情報を確認できず\n'
        cases = [
            ({'ok': True, 'response': header + quiet + south}, 'quiet', 'ok', '熱帯低気圧：なし\n'),
            ({'ok': True, 'response': header.replace('231300Z-240600Z', '221300Z-230600Z') + quiet},
             'expired', 'invalid', unverified),
            ({'ok': True, 'response': header.replace('231300Z-240600Z', '240000Z-240600Z') + quiet},
             'not_yet_valid', 'invalid', unverified),
            ({'ok': True, 'response': 'ABPW10 PGTW 231200\r\n' + region + quiet},
             'validity_unverified', 'invalid', unverified),
            ({'ok': False, 'response': header + quiet + south}, 'unavailable', 'missing', unverified),
            (None, 'unavailable', 'missing', unverified),
            ({'ok': True, 'response': 'HTTP 200, no bulletin'}, 'unrecognized', 'invalid', unverified),
            ({'ok': True, 'response': header + '   A. TROPICAL CYCLONE SUMMARY: NO OTHER TROPICAL CYCLONES.\r\n' + south},
             'unrecognized', 'invalid', unverified),
        ]
        for source, status, validation_status, line in cases:
            with self.subTest(status=status, source=source):
                watch = jtwc_advisory_from_source(source, as_of)
                self.assertEqual(watch['advisory_status'], status)
                data = {**calculated(), 'cyclone_watch': watch}
                t3, _ = render_outlook(raw(), data)
                self.assertEqual(t3, line + '\n熱帯擾乱（JTWC・北西太平洋）：情報を確認できず\n')
                hazards = build_validation({}, data)['hazards']
                self.assertEqual(hazards['jtwc_advisory']['status'], validation_status)
                self.assertEqual(hazards['cyclone_verification']['status'],
                                 'ok' if status == 'quiet' else 'missing')
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp)
                    (path / 'table3.md').write_text(t3, encoding='utf-8')
                    self.assertEqual(check_products({}, path)['table3']['status'], 'ok')

    def test_table3_disturbances_are_independent_and_bounded_to_current_wnp_section_b(self):
        # Bulletin structure as in JTWC ABPW and the 2019 JTWC report (p. 114).
        header = ('ABPW10 PGTW 250600\n'
                  'SUBJ/SIGNIFICANT TROPICAL WEATHER ADVISORY FOR THE WESTERN AND SOUTH\n'
                  'PACIFIC OCEANS/250600Z-260600ZNOV2026//\nRMKS/\n'
                  '1. WESTERN NORTH PACIFIC AREA (180 TO MALAY PENINSULA):\n')
        quiet = '   A. TROPICAL CYCLONE SUMMARY: NONE.\n'
        b = '   B. TROPICAL DISTURBANCE SUMMARY:'
        invest = ' THE AREA OF CONVECTION (INVEST 94W) IS LOCATED NEAR 20.7N 128.2E.\n'
        south = '2. SOUTH PACIFIC AREA:\n' + b + ' THE AREA OF CONVECTION (INVEST 97P).\n'
        cases = [
            (quiet + b + ' NONE.\n   C. SUBTROPICAL SYSTEM SUMMARY: NONE.\n' + south,
             '2026-11-25T21:00:00Z', 'none', [], 'なし', 'ok'),
            (quiet + b + invest + '   C. SUBTROPICAL SYSTEM SUMMARY: INVEST 91W.\n' + south,
             '2026-11-25T21:00:00Z', 'reported', ['94W'], '記載あり（94W）', 'caution'),
            (quiet + b + '\n      (1) AREA OF CONVECTION NEAR 7N 150E.\n' + south,
             '2026-11-25T21:00:00Z', 'reported', [], '記載あり', 'caution'),
            (quiet + b + ' NONE OBSERVED EARLIER; SECTION INCOMPLETE.\n' + south,
             '2026-11-25T21:00:00Z', 'unverified', [], '情報を確認できず', 'missing'),
            (quiet + south, '2026-11-25T21:00:00Z', 'unverified', [], '情報を確認できず', 'missing'),
            (quiet + b + invest + south, '2026-11-26T06:00:00Z', 'unverified', [], '情報を確認できず', 'missing'),
            (b + invest + south, '2026-11-25T21:00:00Z', 'reported', ['94W'], '記載あり（94W）', 'caution'),
        ]
        for body, as_of, status, identifiers, report, validation in cases:
            with self.subTest(body=body, as_of=as_of):
                watch = jtwc_advisory_from_source({'ok': True, 'response': header + body}, as_of)
                self.assertEqual(watch['disturbance_status'], status)
                self.assertEqual(watch['disturbance_invest_ids'], identifiers)
                report_data = {'cyclone_watch': watch}
                t3, _ = render_outlook(raw(), report_data)
                self.assertIn('熱帯擾乱（JTWC・北西太平洋）：' + report, t3)
                self.assertNotIn('91W', t3)
                self.assertNotIn('97P', t3)
                if status == 'reported':
                    self.assertIn('対象地域への影響は未評価', t3)
                if body.startswith(quiet) and as_of.startswith('2026-11-25'):
                    self.assertTrue(t3.startswith('熱帯低気圧：なし\n'))
                else:
                    self.assertTrue(t3.startswith('熱帯低気圧：JTWCの情報を確認できず\n'))
                self.assertEqual(build_validation({}, report_data)['hazards']['jtwc_disturbances']['status'],
                                 validation)

    def test_corrected_jtwc_archived_advisory_preserves_invests_and_expiry(self):
        # NTSB DCA19FM045, Weather Study Attachment 2, pp. 5–7, ID 10109554.
        # Abridged: retain the weather wire heading, subject and both invest tags.
        text = ('ABPW10 PGTW 292330 COR\r\n'
                'SUBJ/SIGNIFICANT TROPICAL WEATHER ADVISORY FOR THE WESTERN AND\r\n'
                '/SOUTH PACIFIC OCEANS REISSUED CORRECTED/292330Z-300600ZJUL2019//\r\n'
                'RMKS/\r\n1. WESTERN NORTH PACIFIC AREA (180 TO MALAY PENINSULA):\r\n'
                ' A. TROPICAL CYCLONE SUMMARY: NONE.\r\n'
                ' B. TROPICAL DISTURBANCE SUMMARY:\r\n'
                '  (1) AREA OF CONVECTION (INVEST 92W).\r\n'
                '  (2) AREA OF CONVECTION (INVEST 93W).\r\n'
                '2. SOUTH PACIFIC AREA:\r\n'
                ' A. TROPICAL CYCLONE SUMMARY: NONE.\r\n'
                ' B. TROPICAL DISTURBANCE SUMMARY: NONE.//\r\n')
        source = {'ok': True, 'response': text}
        current = jtwc_advisory_from_source(source, '2019-07-30T00:00:00Z')
        self.assertEqual(current['advisory_status'], 'quiet')
        self.assertEqual(current['disturbance_status'], 'reported')
        self.assertEqual(current['disturbance_invest_ids'], ['92W', '93W'])
        t3, _ = render_outlook(raw(), {'cyclone_watch': current})
        self.assertIn('記載あり（92W、93W）', t3)
        self.assertIn('対象地域への影響は未評価', t3)
        expired = jtwc_advisory_from_source(source, '2019-07-30T06:00:00Z')
        self.assertEqual(expired['advisory_status'], 'expired')
        self.assertEqual(expired['disturbance_invest_ids'], [])
        ordinary = text.replace('292330 COR', '300600').replace(
            ' REISSUED CORRECTED/292330Z-300600Z', '/300600Z-310600Z')
        self.assertEqual(jtwc_advisory_from_source(
            {'ok': True, 'response': ordinary}, '2019-07-30T06:00:00Z'
        )['disturbance_invest_ids'], ['92W', '93W'])
        for malformed in (
            text.replace('292330 COR', '292330 XYZ'),
            text.replace('REISSUED CORRECTED', 'REISSUED FOREVER'),
            text.replace('300600ZJUL2019', '310600ZJUL2019'),
        ):
            status = jtwc_advisory_from_source(
                {'ok': True, 'response': malformed}, '2019-07-30T00:00:00Z')
            self.assertIn(status['advisory_status'], ('unrecognized', 'validity_unverified'))
            self.assertEqual(status['disturbance_invest_ids'], [])

    def test_archived_line_wrapped_jtwc_cyclone_uses_only_section_a(self):
        # DCA19FM045 Attachment 2, p. 10: Wipha 08W with wraps in the location,
        # as well as between ESTIMATED and AT. Section B is not a cyclone source.
        header = ('ABPW10 PGTW 310600\n'
                  'SUBJ/SIGNIFICANT TROPICAL WEATHER ADVISORY FOR THE WESTERN AND\n'
                  '/SOUTH PACIFIC OCEANS/310600ZJUL2019-010600ZAUG2019//\n'
                  'RMKS/\n1. WESTERN NORTH PACIFIC AREA:\n')
        entry = (' (1) AT 31JUL19 0000Z, TROPICAL DEPRESSION 08W (WIPHA) WAS\n'
                 'LOCATED NEAR 18.6N 112.9E. TRACKED NORTHWESTWARD AT 06 KNOTS.\n'
                 'WINDS WERE ESTIMATED\nAT 30\nKNOTS GUSTING TO 40 KNOTS.\n')
        footer = (' B. TROPICAL DISTURBANCE SUMMARY: AREA OF CONVECTION (INVEST 93W).\n'
                  '2. SOUTH PACIFIC AREA:\n A. TROPICAL CYCLONE SUMMARY: NONE.//\n')

        def at(body):
            return jtwc_advisory_from_source({'ok': True, 'response': header + body},
                                             '2019-07-31T12:00:00Z')

        active = at(' A. TROPICAL CYCLONE SUMMARY:\n' + entry + footer)
        self.assertEqual(active['advisory_status'], 'active')
        self.assertEqual(active['systems'], [
            {'category': 'TROPICAL DEPRESSION', 'id': '08W', 'name': 'WIPHA',
             'latitude': 18.6, 'longitude': 112.9, 'movement_text': 'NORTHWESTWARD',
             'movement_kt': 6, 'max_winds_kt': 30, 'gusts_kt': 40}])
        self.assertEqual(active['disturbance_invest_ids'], ['93W'])
        pair = at(' A. TROPICAL CYCLONE SUMMARY:\n'
                  ' (1) TROPICAL STORM 07W (SEVEN) WAS LOCATED NEAR 20N 150E.\n'
                  + entry.replace(' (1)', ' (2)') + footer)
        self.assertEqual([item['id'] for item in pair['systems']], ['07W', '08W'])
        self.assertIsNone(pair['systems'][0]['max_winds_kt'])
        self.assertEqual(pair['systems'][1]['max_winds_kt'], 30)
        quiet = at(' A. TROPICAL CYCLONE SUMMARY: NONE.\n' + footer.replace(
            '2. SOUTH PACIFIC AREA:', entry + '2. SOUTH PACIFIC AREA:'))
        self.assertEqual(quiet['advisory_status'], 'quiet')
        self.assertEqual(quiet['systems'], [])

    def test_jtwc_checks_window_boundaries_and_explicit_year_rollover(self):
        text = ('ABPW10 PGTW 310600\n'
                'SUBJ/SIGNIFICANT TROPICAL WEATHER ADVISORY FOR THE WESTERN AND\n'
                '/SOUTH PACIFIC OCEANS/311800ZDEC2026-010600ZJAN2027//\n'
                'RMKS/\n1. WESTERN NORTH PACIFIC AREA:\n'
                '   A. TROPICAL CYCLONE SUMMARY: NONE.\n')
        source = {'ok': True, 'response': text}
        current = jtwc_advisory_from_source(source, '2027-01-01T03:00:00+00:00')
        self.assertEqual(current['advisory_status'], 'quiet')
        self.assertEqual(current['valid_from_utc'], '2026-12-31T18:00:00+00:00')
        self.assertEqual(current['valid_until_utc'], '2027-01-01T06:00:00+00:00')
        for as_of, status in [('2026-12-31T18:00:00+00:00', 'quiet'),
                              ('2027-01-01T06:00:00+00:00', 'expired'),
                              (None, 'validity_unverified')]:
            self.assertEqual(jtwc_advisory_from_source(source, as_of)['advisory_status'], status)
        source['response'] = text.replace('DEC2026', '')
        self.assertEqual(jtwc_advisory_from_source(source, '2027-01-01T03:00:00+00:00')
                         ['advisory_status'], 'validity_unverified')

    def test_available_bufr_does_not_verify_missing_jtwc(self):
        watch = jtwc_advisory_from_source(None)
        watch['ifs_cyclone_tracks'] = {'oper': {'status': 'available'}}
        hazards = build_validation({}, {'cyclone_watch': watch})['hazards']
        self.assertEqual(hazards['jtwc_advisory']['status'], 'missing')
        self.assertEqual(hazards['cyclone_verification']['status'], 'caution')

    def test_table4_analysis_and_values_separated(self):
        _t3, t4 = render_outlook(raw(), calculated())
        lines = t4.strip().split('\n')
        self.assertEqual(lines[0], '| 項目 | 分析 | 数値・根拠 |')
        self.assertIn('| 季節風 | モンスーンの特記なし。シベリア高気圧に大きな変化なし '
                      '| UB域 格子最大 1046hPa・72h傾向 +2.3hPa |', lines)
        self.assertIn('| 温帯低気圧 | 満洲に発達した低気圧の兆候（格子値） |', t4)
        self.assertIn('満洲 格子最小 998hPa（44.1N 126.9E・11/10 00:00UTC）', t4)
        self.assertIn('日本海 格子最小 1005hPa（38.2N 133.5E・11/09 06:00UTC）', t4)
        self.assertIn('| 宇宙天気 | 地磁気嵐 G2級。4都市の日常生活への直接の影響は限定的 '
                      '| Kp最大 5・柿岡K最大 3 |', lines)
        self.assertIn('| ECMWF検証 | ECMWF製品は取得不可。進路サンプルで部分的に代替確認 '
                      '| プロダクト HTTP 404・6基準時刻遡及｜進路サンプル GFS 有・ECMWF開放データ 有 |', lines)
        self.assertFalse(any('判定' in ln for ln in lines))

    def test_table4_monsoon_city_surge(self):
        calc = calculated()
        calm = {'ready': True, 'max_24h_temp_drop_c': -2.0,
                'drop_end_local': '11-12 06:00', 'max_24h_pressure_rise_hpa': 3.0,
                'rise_end_local': '11-12 06:00', 'northerly_max_ms': 8.0,
                'northerly_gust_ms': 12.0, 'northerly_at_local': '11-12 06:00',
                'surge_signature': False, 'surge_coverage_complete': True}
        surge = {**calm, 'max_24h_temp_drop_c': -6.5,
                 'max_24h_pressure_rise_hpa': 7.2, 'northerly_max_ms': 12.3,
                 'surge_signature': True}
        calc['cities'] = {'Hong Kong': {'monsoon': surge},
                          'Taipei': {'monsoon': calm},
                          'Tokyo Minato Ward': {'monsoon': calm},
                          'Takamatsu': {'monsoon': calm}}
        _t3, t4 = render_outlook(raw(), calc)
        self.assertIn('香港に寒気サージの指標あり', t4)
        self.assertIn('4市指標 ', t4)
        self.assertIn('香港 低下 -6.5℃（11-12 06:00）・昇圧 +7.2hPa・北寄り最大 12.3m/s（サージ指標）', t4)
        self.assertIn('台北 低下 -2.0℃', t4)

    def test_table4_monsoon_cities_no_surge(self):
        calc = calculated()
        calm = {'ready': True, 'max_24h_temp_drop_c': -1.0,
                'drop_end_local': '11-12 06:00', 'max_24h_pressure_rise_hpa': 2.0,
                'rise_end_local': '11-12 06:00', 'northerly_max_ms': 5.0,
                'northerly_gust_ms': None, 'northerly_at_local': '11-12 06:00',
                'surge_signature': False, 'surge_coverage_complete': True}
        calc['cities'] = {name: {'monsoon': calm} for name in
                          ('Hong Kong', 'Taipei', 'Tokyo Minato Ward', 'Takamatsu')}
        _t3, t4 = render_outlook(raw(), calc)
        self.assertIn('4市に寒気サージの指標なし', t4)

    def test_table4_weakening_high_quiet_space_and_monsoon_mention(self):
        calc = calculated()
        calc['regional_digest']['ulaanbaatar']['tendency_hpa'] = -25.3
        calc['space_weather'] = {'extreme': False, 'max_kp_in_outlook': 2.0}
        calc['cyclone_watch']['monsoon_mentions'] = ['MONSOON TROUGH ...', '... MONSOON ...']
        _t3, t4 = render_outlook(raw(), calc)
        self.assertIn('シベリア高気圧は弱化傾向（寒気の南下は弱まる）', t4)
        self.assertIn('モンスーンの言及あり（ABPW 2行）', t4)
        self.assertIn('ABPW言及 2行', t4)
        self.assertIn('| 宇宙天気 | 特記なし | Kp最大 2 |', t4)


    def test_expired_swpc_warning_not_rendered_as_current_event(self):
        sw = parse_space_weather({
            'swpc/three_day': {'ok': True,
                'response': 'The greatest expected 3 hr Kp for Nov 14-Nov 16 2026 is 3.33.\n'
                            'Rationale: No G1 (Minor) or greater geomagnetic storms are expected.'},
            'swpc/alerts': {'ok': True, 'response': [{
                'product_id': 'WARK05', 'issue_datetime': '2026-11-07 19:35:00',
                'message': 'WARNING: Geomagnetic K-index of 5 expected\n'
                           'Valid From: 2026 Nov 07 1935 UTC\n'
                           'Valid To: 2026 Nov 08 2100 UTC\n'
                           'NOAA Scale: G1 - Minor'}]},
        }, as_of='2026-11-14T20:00:00+00:00')
        self.assertIn('WARK05', sw['historical_warning_or_alert_ids'])
        self.assertEqual(sw['warning_or_alert_ids'], [])
        calc = calculated()
        calc['space_weather'] = sw
        _t3, table = render_outlook(raw(), calc)
        self.assertIn('| 宇宙天気 | 特記なし | Kp最大 3.3 |', table)
        self.assertNotIn('WARK05', table)
        self.assertNotIn('警報級の事象', table)

    def test_table4_unverified_space_forecast_is_not_tokki_nashi(self):
        # Regression (PR #1 author update): the digest marks an
        # unparsable SWPC forecast geomagnetic_forecast_status=
        # 'unverified'; the rendered row must say so, not 特記なし.
        calc = calculated()
        calc['space_weather'] = {'extreme': False, 'max_kp_in_outlook': 2.0,
                                 'geomagnetic_forecast_status': 'unverified'}
        _t3, t4 = render_outlook(raw(), calc)
        self.assertIn('| 宇宙天気 | 地磁気予報は未検証 |', t4)
        # A parsed no-storm forecast still renders as 特記なし.
        calc['space_weather'] = {'extreme': False, 'max_kp_in_outlook': 2.0,
                                 'geomagnetic_forecast_status': 'no_storm_forecast'}
        _t3, t4 = render_outlook(raw(), calc)
        self.assertIn('| 宇宙天気 | 特記なし | Kp最大 2 |', t4)

    def test_table4_etc_reads_the_widened_grids(self):
        calc = calculated()
        calc['regional_digest']['east_china_sea'] = {
            'min_pressure_hpa': 995.0, 'latitude': 30.5, 'longitude': 127.0,
            'min_at_utc': '2026-11-10T06:00:00+00:00'}
        calc['regional_digest']['okhotsk'] = {
            'min_pressure_hpa': 1001.0, 'latitude': 50.0, 'longitude': 149.0,
            'min_at_utc': '2026-11-10T06:00:00+00:00'}
        _t3, t4 = render_outlook(raw(), calc)
        self.assertIn('| 温帯低気圧 | 東シナ海に発達した低気圧の兆候（格子値） |', t4)
        self.assertIn('東シナ海 格子最小 995hPa（30.5N 127.0E・11/10 06:00UTC）', t4)
        self.assertIn('オホーツク 格子最小 1001hPa（50.0N 149.0E・11/10 06:00UTC）', t4)

    def test_table4_monsoon_analysis_names_surge_windows(self):
        # Regression (user, 2026-11-12): the 分析 sentence named the surged
        # cities but left WHEN to the 数値・根拠 column only.
        calc = calculated()
        calc['cities'] = {
            'Hong Kong': {'monsoon': {
                'ready': True, 'surge_signature': True,
                'surge_coverage_complete': True,
                'surge_event': {'start_local': '11-18 02:00',
                                'end_local': '11-19 02:00',
                                'temp_drop_c': -8.0, 'pressure_rise_hpa': 6.7},
                'max_24h_temp_drop_c': -8.0, 'drop_end_local': '11-19 02:00',
                'max_24h_pressure_rise_hpa': 7.2, 'northerly_max_ms': 6.9}},
            'Tokyo Minato Ward': {'monsoon': {
                'ready': True, 'surge_signature': True,
                'surge_coverage_complete': True,
                'surge_event': {'start_local': '11-16 16:00',
                                'end_local': '11-17 16:00',
                                'temp_drop_c': -8.0, 'pressure_rise_hpa': 15.1},
                'max_24h_temp_drop_c': -8.0, 'drop_end_local': '11-17 16:00',
                'max_24h_pressure_rise_hpa': 17.4, 'northerly_max_ms': 4.4}}}
        _t3, t4 = render_outlook(raw(), calc)
        self.assertIn('香港 11/18 02:00→11/19 02:00', t4)
        self.assertIn('東京港区 11/16 16:00→11/17 16:00', t4)
        self.assertIn('に寒気サージの指標あり', t4)


    def test_table4_hazard_gust_and_cme_carry_timing(self):
        # Audit (user, 2026-11-12): same defect class as the monsoon surge
        # windows — a gust finding stated no time although the digest
        # carries the gust-max time, and a CME mention dropped the SWPC
        # line that states the expected arrival.
        calc = calculated()
        calc['cities'] = {'Hong Kong': {'report_digest': {
            'wind_gust_max_ms': 28.4,
            'wind_gust_max_time_local': '11-13 03:00'}}}
        calc['space_weather'] = {**calc['space_weather'], 'cme_arrivals': [
            'A CME is expected to arrive on Nov 14 at approximately 06 UT']}
        _t3, t4 = render_outlook(raw(), calc)
        self.assertIn('強風（香港 突風28.4m/s・11/13 03:00）', t4)
        self.assertIn('突風最大 28.4m/s（香港・11/13 03:00）', t4)
        self.assertIn('CME原文：A CME is expected to arrive on Nov 14', t4)


class RegionalDigestTests(unittest.TestCase):
    def _raw(self):
        def point(lat, lon, base):
            times = [f'2026-11-0{7 + i // 4}T{(i % 4) * 6:02d}:00' for i in range(12)]
            return {'latitude': lat, 'longitude': lon,
                    'hourly': {'time': times,
                               'pressure_msl': [base + i for i in range(12)]}}
        return {'regional_forecasts': {
            'ulaanbaatar': {'grid_forecasts': [point(47.9, 106.9, 1040.0)]},
            'manchuria': {'grid_forecasts': [point(44.1, 126.9, 1005.0)]},
            'sea_of_japan': {'grid_forecasts': [point(38.2, 133.5, 1010.0)]}}}

    def test_digest_statistics(self):
        d = regional_digest(self._raw())
        self.assertEqual(d['ulaanbaatar']['max_pressure_hpa'], 1051.0)
        self.assertIn('tendency_hpa', d['ulaanbaatar'])
        self.assertEqual(d['manchuria']['min_pressure_hpa'], 1005.0)
        self.assertEqual(d['manchuria']['latitude'], 44.1)
        self.assertEqual(d['sea_of_japan']['min_pressure_hpa'], 1010.0)

    def test_reject_spatially_invalid_pressure_minimum(self):
        raw = self._raw()
        sea = raw['regional_forecasts']['sea_of_japan']
        wrong = dict(sea['grid_forecasts'][0])
        wrong['latitude'] = 34.1452
        wrong['longitude'] = 140.09853  # Pacific, not Sea of Japan
        wrong['hourly'] = dict(wrong['hourly'])
        wrong['hourly']['pressure_msl'] = [900.0] * 12
        sea['grid_forecasts'].append(wrong)
        sea['checks'] = [{'spatial_ready': True}, {'spatial_ready': False}]
        digest = regional_digest(raw)
        self.assertEqual(digest['sea_of_japan']['min_pressure_hpa'], 1010.0)
        self.assertEqual(digest['sea_of_japan']['longitude'], 133.5)

    def test_digest_covers_the_widened_etc_grids(self):
        # Grid set widened 2026-11-12 (user): south-coast / baiu corridors
        # and the northern lows, not only the two winter grids.
        def point(lat, lon, base):
            times = [f'2026-11-0{7 + i // 4}T{(i % 4) * 6:02d}:00' for i in range(12)]
            return {'latitude': lat, 'longitude': lon,
                    'hourly': {'time': times,
                               'pressure_msl': [base + i for i in range(12)]}}
        raw = {'regional_forecasts': {
            'east_china_sea': {'grid_forecasts': [point(30.5, 127.0, 1002.0)]},
            'tokaido': {'grid_forecasts': [point(34.0, 138.0, 1008.0)]},
            'okhotsk': {'grid_forecasts': [point(50.0, 149.0, 990.0)]}}}
        d = regional_digest(raw)
        self.assertEqual(d['east_china_sea']['min_pressure_hpa'], 1002.0)
        self.assertEqual(d['tokaido']['min_pressure_hpa'], 1008.0)
        self.assertEqual(d['okhotsk']['min_pressure_hpa'], 990.0)


if __name__ == '__main__':
    unittest.main()
