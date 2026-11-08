"""Message-3 outlook tables: the structured special-weather outlook."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from render_tables import render_outlook
from compute import regional_digest


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
                    'gfs/cyclone_check_31w': {'ok': True, 'samples': [
                        {'latitude': 24.0, 'longitude': 141.0}]},
                    'ecmwf_opendata/cyclone_check_31w': {'ok': True, 'samples': [
                        {'latitude': 24.0, 'longitude': 141.0}]}}},
            'space_weather': {'extreme': True, 'max_g_scale_in_outlook': 2,
                              'max_kp_in_outlook': 5.0, 'warning_or_alert_ids': []},
            'regional_digest': {
                'ulaanbaatar': {'max_pressure_hpa': 1046.4, 'tendency_hpa': 2.3},
                'manchuria': {'min_pressure_hpa': 998.2, 'latitude': 44.1,
                              'longitude': 126.9, 'min_at_utc': '2026-11-10T00:00:00+00:00'},
                'sea_of_japan': {'min_pressure_hpa': 1004.6, 'latitude': 38.2,
                                 'longitude': 133.5, 'min_at_utc': '2026-11-09T06:00:00+00:00'}}}
    base.update(kw)
    return base


class RenderOutlookTests(unittest.TestCase):
    def test_table3_system_row_and_threat_floor(self):
        t3, _t4 = render_outlook({}, calculated())
        lines = t3.strip().split('\n')
        self.assertEqual(lines[0], '| 名称 | 位置 | 移動・強度 | 脅威 |')
        row = lines[2]
        self.assertIn('CEMPAKA（31W・TS）', row)
        self.assertIn('19.0N 144.2E', row)
        self.assertIn('WNW 16kt・最大35kt・突風45kt', row)
        self.assertTrue(row.split('|')[4].strip().startswith('火山列島 約'))
        self.assertTrue(row.split('|')[4].strip().endswith('km（サンプル）'))

    def test_table3_no_systems_is_one_line(self):
        calc = calculated()
        calc['cyclone_watch'] = {**calc['cyclone_watch'], 'systems': []}
        t3, _t4 = render_outlook({}, calc)
        self.assertEqual(t3, '熱帯低気圧：なし\n')

    def test_threat_floor_far_and_missing(self):
        calc = calculated()
        calc['cyclone_watch']['track_checks'] = {
            'gfs/cyclone_check_31w': {'ok': True, 'samples': [
                {'latitude': 10.0, 'longitude': 160.0}]}}
        t3, _t4 = render_outlook({}, calc)
        self.assertIn('対象地域から1,000km超（サンプル）', t3)
        calc['cyclone_watch']['track_checks'] = {}
        t3, _t4 = render_outlook({}, calc)
        self.assertIn('| ― |', t3)

    def test_table4_fixed_rows(self):
        _t3, t4 = render_outlook({}, calculated())
        lines = t4.strip().split('\n')
        self.assertEqual(lines[0], '| 項目 | 状況 | 対象・根拠 |')
        self.assertIn('| 季節風 | 特記なし｜UB域 格子最大 1046hPa・傾向 +2.3 | JTWC ABPW・EC46地域格子 |', lines)
        self.assertIn('満洲 格子最小 998hPa（44.1N 126.9E・11/10 00:00UTC）', t4)
        self.assertIn('日本海 格子最小 1005hPa（38.2N 133.5E・11/09 06:00UTC）', t4)
        self.assertIn('| 宇宙天気 | G2級・Kp 5（警報・注意報 なし） | SWPC・NICT |', lines)
        self.assertIn('サイクロンプロダクト 取得不可（HTTP 404・6基準時刻）', t4)
        self.assertIn('進路サンプル GFS 有・ECMWF開放データ 有', t4)
        self.assertFalse(any('判定' in ln for ln in lines))

    def test_table4_quiet_space_weather_and_monsoon_mention(self):
        calc = calculated()
        calc['space_weather'] = {'extreme': False}
        calc['cyclone_watch']['monsoon_mentions'] = ['MONSOON TROUGH ...', '... MONSOON ...']
        _t3, t4 = render_outlook({}, calc)
        self.assertIn('| 宇宙天気 | 特記なし | SWPC・NICT |', t4)
        self.assertIn('ABPWに言及あり（2行）', t4)


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


if __name__ == '__main__':
    unittest.main()
