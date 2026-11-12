"""Message-3 outlook tables: the structured special-weather outlook.

Analysis first (user correction, 2026-11-08): Table 3 reads the current
JTWC warning text, Table 4 separates 分析 from 数値・根拠.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from render_tables import render_outlook
from compute import regional_digest

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

    def test_table3_no_systems_is_one_line(self):
        calc = calculated()
        calc['cyclone_watch'] = {**calc['cyclone_watch'], 'systems': []}
        t3, _t4 = render_outlook(raw(), calc)
        self.assertEqual(t3, '熱帯低気圧：なし\n')

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
