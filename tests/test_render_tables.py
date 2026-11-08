"""Pipe-table generation: the settled cell rules, encoded in the workflow."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from render_tables import render_tables


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
        self.assertIn('| 暑寒 | ― | ― | 15.2 ほぼ安全 | 15.2 ほぼ安全 |', lines)
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


if __name__ == '__main__':
    unittest.main()
