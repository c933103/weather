"""AIFS chart: cascade markers, ensemble extraction label, icon routing,
and a full AIFS-flavour render smoke."""
import datetime as dt
import math
import sys
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import collect
import render_chart
from specialists import extract_temperature_ensemble


class CascadeMarkerTests(unittest.TestCase):
    def test_combined_temperature_custom_markers(self):
        first = {'time': ['2026-11-08T00:00:00+00:00', '2026-11-08T06:00:00+00:00'],
                 'temperature_2m': [10.0, 11.0]}
        tail = {'time': ['2026-11-08T06:00:00+00:00', '2026-11-08T12:00:00+00:00'],
                'temperature_2m': [12.0, 13.0]}
        out = collect.combined_temperature(first, tail, None,
                                           first_marker='ecmwf_aifs025_ensemble_mean')
        self.assertEqual(out['sample_model'],
                         ['ecmwf_aifs025_ensemble_mean',
                          'ecmwf_aifs025_ensemble_mean',
                          'ecmwf_ec46_ensemble_mean_36km'])
        self.assertTrue(out['model'].startswith('ecmwf_aifs025_ensemble_mean+'))

    def test_combined_temperature_aifs_only_no_tail(self):
        # The AIFS chart series carries no second model: an empty tail
        # contributes nothing and the model string is the AIFS marker alone.
        first = {'time': ['2026-11-08T00:00:00+00:00', '2026-11-08T06:00:00+00:00'],
                 'temperature_2m': [10.0, 11.0]}
        out = collect.combined_temperature(first, {}, None,
                                           first_marker='ecmwf_aifs025_ensemble_mean')
        self.assertEqual(out['sample_model'],
                         ['ecmwf_aifs025_ensemble_mean'] * 2)
        self.assertEqual(out['model'], 'ecmwf_aifs025_ensemble_mean')


class ExtractLabelTests(unittest.TestCase):
    def test_extract_temperature_ensemble_aifs_label(self):
        hourly = {'time': [], 'temperature_2m': [], 'temperature_2m_member01': [],
                  'precipitation': [], 'precipitation_member01': [],
                  'cloud_cover': [], 'cloud_cover_member01': []}
        base = dt.datetime(2026, 11, 8, tzinfo=dt.timezone.utc)
        for h in range(24):
            hourly['time'].append((base + dt.timedelta(hours=h)).isoformat())
            hourly['temperature_2m'].append(10.0)
            hourly['temperature_2m_member01'].append(11.0)
            hourly['precipitation'].append(0.0)
            hourly['precipitation_member01'].append(2.0)
            hourly['cloud_cover'].append(30.0)
            hourly['cloud_cover_member01'].append(40.0)
        src = {'ok': True, 'response': {'hourly': hourly}}
        out = extract_temperature_ensemble(src, base, base + dt.timedelta(days=1),
                                           'Asia/Tokyo', model='ecmwf_aifs025_ensemble')
        self.assertEqual(out['model'], 'ecmwf_aifs025_ensemble')
        self.assertTrue(out['daily'])


def _aifs_row():
    tz = ZoneInfo('Asia/Tokyo')
    day = dt.date(2026, 11, 8)
    times, temps, models = [], [], []
    t0 = dt.datetime(2026, 11, 7, tzinfo=dt.timezone.utc)
    for i in range(40):
        t = t0 + dt.timedelta(hours=6 * i)
        times.append(t.isoformat())
        temps.append(12 + 5 * math.sin(i))
        models.append('ecmwf_aifs025_ensemble_mean')
    row = {'aifs_chart_hourly': {'time': times, 'temperature_2m': temps,
                                 'weather_code': [None] * len(times),
                                 'sample_model': models,
                                 'model': 'ecmwf_aifs025_ensemble_mean'},
           'aifs_ensemble': {'daily': {day.isoformat(): {
               'p_rain_1mm': 0.6, 'p_rain_10mm': 0.1, 'p_snow': 0.0,
               'daytime_cloud_mean': 40.0}}}}
    return row, day, tz


class IconRoutingTests(unittest.TestCase):
    def test_daily_icon_aifs_ensemble_routing(self):
        row, day, tz = _aifs_row()
        # AIFS-ENS-majority day: the icon comes from AIFS member
        # probabilities (p_rain_1mm 0.6 -> rain), never from the mean
        # series' (absent) weather codes.
        self.assertEqual(
            render_chart.daily_icon(row, day, tz, hourly_key='aifs_chart_hourly',
                                    ens_key='aifs_ensemble'), '🌧')


def _synthetic_aifs_raw():
    cities = {}
    for name, tz_name in (('Tokyo Minato Ward', 'Asia/Tokyo'),
                          ('Hong Kong', 'Asia/Hong_Kong')):
        t0 = dt.datetime(2026, 11, 7, tzinfo=dt.timezone.utc)
        times, temps, models = [], [], []
        # AIFS-like span only: 68 six-hourly stamps = 17 days, all AIFS.
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
        daily = {}
        mext = {}
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


class RenderSmokeTests(unittest.TestCase):
    def test_render_aifs_flavour_smoke(self):
        raw = _synthetic_aifs_raw()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'chart_aifs.png'
            coverage = render_chart.render(raw, '2026-11-08', out, flavour='aifs')
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 10000)
            # Only the AIFS span is plotted: 16 complete local days of the
            # 17-day series carry a day high; the rest of the 28-day frame
            # stays blank (no EC46 fill).
            self.assertEqual(coverage['Tokyo Minato Ward']['high_days'], 16)
            self.assertEqual(coverage['Hong Kong']['high_days'], 16)
        note = render_chart.cascade_note(
            {name: raw['cities'][name] for name in raw['cities']},
            dt.date(2026, 11, 8), hourly_key='aifs_chart_hourly')
        self.assertIn('AIFSアンサンブル平均', note)
        self.assertNotIn('EC46', note)

    def test_aifs_beyond_horizon_slots_blank(self):
        from chart_data import build_chart_data
        raw = _synthetic_aifs_raw()
        data, _ = build_chart_data(raw, '2026-11-08', flavour='aifs')
        tokyo = data['cities'][0]
        # Day 0 (11/08) is inside the AIFS span: a real icon and grade.
        self.assertNotEqual(tokyo['icons'][0]['emoji'], '')
        self.assertIsNotNone(tokyo['grades'][0])
        # The last AIFS-sampled local date is 11/24 (slot 16); from 11/25
        # (slot 17) on, slots are blank: empty emoji, no grade.
        self.assertEqual(tokyo['icons'][17], {'emoji': '', 'asset': None})
        self.assertIsNone(tokyo['grades'][17])
        self.assertEqual(tokyo['icons'][27], {'emoji': '', 'asset': None})
        self.assertIsNone(tokyo['grades'][27])


class DailyExtremaTests(unittest.TestCase):
    def test_daily_extrema_helper(self):
        hourly = {'time': ['2026-11-07T15:00:00+00:00', '2026-11-07T18:00:00+00:00',
                           '2026-11-08T03:00:00+00:00'],
                  'temperature_2m': [9.0, 14.0, 7.0]}
        out = collect._daily_extrema(hourly, 'Asia/Tokyo')
        # 15:00Z/18:00Z are 00:00/03:00 JST on the 8th; 03:00Z is 12:00 JST.
        self.assertEqual(out, {'2026-11-08': {'high': 14.0, 'low': 7.0}})


if __name__ == '__main__':
    unittest.main()
