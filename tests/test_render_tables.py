"""Pipe-table generation: the settled cell rules, encoded in the workflow."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from render_tables import render_outlook, render_tables


def city(**kw):
    base = {'temperature_2m': {'minimum': 10.0, 'maximum': 16.0},
            'apparent_temperature': {'minimum': 8.0, 'maximum': 14.0},
            'wbgt': {'maximum': 15.2}, 'wind_chill': {'status': 'not_applicable'},
            'relative_humidity_2m': {'minimum': 49.0, 'maximum': 74.0},
            'precipitation_probability': {'maximum': 63.0},
            'precipitation': {'total': 0.5}, 'snowfall': {'total': 0.0},
            'snow_probability': {'percent': 0.0},
            'wind_speed_10m': {'maximum': 4.26},
            'aqi': {'maximum': 71.0,
                    'leading_component_at_worst_hour': [71.0, 'us_aqi_pm2_5', 'T20']},
            'uv_index': {'maximum': 3.35}, 'visibility': {'minimum': 8340.0},
            'model_crosscheck': {'temperature_max_difference_c': 1.2},
            'report_digest': {'dominant_weather_code': 3, 'humidity_mean': 62.0,
                              'pressure_mean_hpa': 1030.1, 'pressure_mean_change_24h': 7.4,
                              'wind_direction_at_max_compass': '北', 'wbgt_band': 'ほぼ安全',
                              'hazard_flags': {'high_pm': False, 'frost': False}}}
    base.update(kw)
    return base


class RenderTablesTests(unittest.TestCase):
    def _inputs(self):
        names = ['Hong Kong', 'Taipei', 'Tokyo Minato Ward', 'Takamatsu']
        calc = {'cities': {n: city() for n in names}}
        raw = {'cities': {n: {'raw_air_quality': {'time': ['T20'],
                                                  'us_aqi_pm2_5': [71.0],
                                                  'us_aqi_ozone': [40.0]}}
                          for n in names}}
        return raw, calc

    def test_table1_cells(self):
        t1, _t2 = render_tables(*self._inputs())
        lines = t1.strip().split('\n')
        self.assertEqual(lines[0], '| | 香港 | 台北 | 東京港区 | 高松 |')
        self.assertIn('| 天気 | 曇 | 曇 | 曇 | 曇 |', lines)
        self.assertIn('| 気温 | 10~16 | 10~16 | 10~16 | 10~16 |', lines)
        self.assertIn('| 暑寒 | 15.2 ほぼ安全 | 15.2 ほぼ安全 | 15.2 ほぼ安全 | 15.2 ほぼ安全 |', lines)
        self.assertIn('| 湿度 | 62% (49~74) | 62% (49~74) | 62% (49~74) | 62% (49~74) |', lines)
        self.assertIn('| 降水 | 63% 0.5mm | 63% 0.5mm | 63% 0.5mm | 63% 0.5mm |', lines)
        self.assertIn('| 風 | 最大4.3m/s 北 卓越― | 最大4.3m/s 北 卓越― | 最大4.3m/s 北 卓越― | 最大4.3m/s 北 卓越― |', lines)
        self.assertFalse(any('モデル差' in ln for ln in lines))

    def test_table2_cells_and_tokki_floor(self):
        _t1, t2 = render_tables(*self._inputs())
        lines = t2.strip().split('\n')
        self.assertIn('| AQI | 71 普通 PM2.5 | 71 普通 PM2.5 | 71 普通 PM2.5 | 71 普通 PM2.5 |', lines)
        self.assertIn('| UV | 3.4 中 | 3.4 中 | 3.4 中 | 3.4 中 |', lines)
        self.assertIn('| 気圧 | 1030.1 (+7.4) | 1030.1 (+7.4) | 1030.1 (+7.4) | 1030.1 (+7.4) |', lines)
        self.assertIn('| 特記 | 無 | 無 | 無 | 無 |', lines)

    def test_wind_chill_and_second_aqi_leader(self):
        raw, calc = self._inputs()
        tokyo = calc['cities']['Tokyo Minato Ward']
        tokyo['wind_chill'] = {'status': 'applicable', 'minimum': 7.63}
        raw['cities']['Tokyo Minato Ward']['raw_air_quality']['us_aqi_ozone'] = [65.0]
        t1, t2 = render_tables(raw, calc)
        self.assertIn('風冷 7.6', t1)
        self.assertIn('71 普通 PM2.5・O3', t2)


class MixedBufrOutlookTests(unittest.TestCase):
    def test_decoded_zero_tracks_is_not_labeled_unavailable_when_other_stream_is_missing(self):
        for good, bad in (('oper', 'enfo'), ('enfo', 'oper')):
            for status, detail in (('unpublished', 'ファイル未公開'),
                                   ('unavailable', '通信・HTTPエラー'),
                                   ('malformed', 'データ形式異常'),
                                   (None, '取得状況不明')):
                with self.subTest(good=good, status=status):
                    streams = {good: {'status': 'no_regional_tracks',
                                      'cycle_utc': '2026-11-21T00:00:00+00:00', 'tracks': []}}
                    if status is not None:
                        streams[bad] = {'status': status}
                    watch = {'ifs_cyclone_tracks': streams,
                             'ecmwf_cyclone_product': {'http_status': 404}}
                    _table3, table4 = render_outlook({}, {'cyclone_watch': watch})
                    line, = (line for line in table4.splitlines() if line.startswith('| ECMWF検証 |'))
                    analysis, values = line.split(' | ')[1:3]
                    self.assertIn(f'{good}は対象海域の進路0件', analysis)
                    self.assertIn(f'{bad}は{detail}', analysis)
                    self.assertNotIn('ECMWF製品・代替サンプルとも取得不可', analysis)
                    self.assertIn(f'IFS BUFR {good} 2026-11-21T00:00:00Z・0件', values)

    def test_partial_bufr_status_does_not_hide_available_chart(self):
        watch = {'ifs_cyclone_tracks': {'oper': {'status': 'no_regional_tracks'},
                                        'enfo': {'status': 'unpublished'}},
                 'ecmwf_cyclone_product': {'ok': True, 'base_time_used': '2026-11-21T00:00:00Z'}}
        _table3, table4 = render_outlook({}, {'cyclone_watch': watch})
        self.assertIn('operは対象海域の進路0件', table4)
        self.assertIn('従来照合：ECMWFサイクロンプロダクトを取得し進路と照合', table4)

    def test_two_decoded_streams_with_zero_tracks_keep_the_regional_summary(self):
        watch = {'ifs_cyclone_tracks': {stream: {'status': 'no_regional_tracks'}
                                        for stream in ('oper', 'enfo')}}
        _table3, table4 = render_outlook({}, {'cyclone_watch': watch})
        self.assertIn('中期IFS BUFRを取得。対象海域に抽出された進路は0件', table4)


if __name__ == '__main__':
    unittest.main()
