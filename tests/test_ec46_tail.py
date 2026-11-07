"""EC46 tail: member-probability icons and source daily-extreme fields.

The EC46 ensemble mean's weather_code is not a categorical forecast and the
mean series' day-to-day variance is damped by construction; these tests pin
the replacements (member probabilities for icons, per-interval min/max
fields for extrema) so the tail cannot silently regress to mean-derived
codes or sampled extrema.
"""
import datetime as dt
import sys
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from collect import combined_temperature
from render_chart import daily_icon, ec46_probability_icon, extrema_by_date
from specialists import extract_ec46_daily


class Ec46DailyTests(unittest.TestCase):
    def test_member_probabilities(self):
        times = ['2026-11-07T00:00', '2026-11-07T06:00',
                 '2026-11-07T12:00', '2026-11-07T18:00']
        hourly = {'time': times,
                  'precipitation_member01': [3.0, 3.0, 3.0, 3.0],
                  'precipitation_member02': [0.0, 0.0, 0.0, 0.0],
                  'snowfall_member01': [0.25, 0.25, 0.25, 0.25],
                  'snowfall_member02': [0.0, 0.0, 0.0, 0.0],
                  'cloud_cover_member01': [10, 10, 10, 10],
                  'cloud_cover_member02': [30, 30, 30, 30]}
        out = extract_ec46_daily({'ok': True, 'response': {'hourly': hourly}}, 'UTC')
        day = out['2026-11-07']
        self.assertEqual(day['members'], 2)
        self.assertEqual(day['p_rain_1mm'], 0.5)
        self.assertEqual(day['p_rain_10mm'], 0.5)
        self.assertEqual(day['p_snow'], 0.5)
        self.assertEqual(day['daytime_cloud_mean'], 20.0)

    def test_missing_members_yield_no_claim(self):
        self.assertEqual(extract_ec46_daily({'ok': False}, 'UTC'), {})


class CombinedTemperatureTests(unittest.TestCase):
    def test_interval_extremes_carried_from_ec46_tier(self):
        ifs = {'time': ['2026-11-07T00:00'], 'temperature_2m': [20.0],
               'weather_code': [0]}
        ec46 = {'time': ['2026-11-07T06:00'], 'temperature_2m': [15.0],
                'weather_code': [61],
                'temperature_2m_min': [13.5], 'temperature_2m_max': [17.5]}
        merged = combined_temperature(ifs, ec46)
        self.assertEqual(merged['temperature_2m_max'], [None, 17.5])
        self.assertEqual(merged['temperature_2m_min'], [None, 13.5])


class ProbabilityIconTests(unittest.TestCase):
    def test_branches(self):
        self.assertEqual(ec46_probability_icon({'p_snow': 0.6}), '❄')
        self.assertEqual(ec46_probability_icon({'p_rain_10mm': 0.5}), '🌧')
        self.assertEqual(ec46_probability_icon({'p_rain_1mm': 0.5}), '🌧')
        self.assertEqual(ec46_probability_icon({'p_rain_1mm': 0.3}), '🌦')
        self.assertEqual(ec46_probability_icon(
            {'p_rain_1mm': 0.1, 'daytime_cloud_mean': 10}), '☀')
        self.assertEqual(ec46_probability_icon(
            {'p_rain_1mm': 0.1, 'daytime_cloud_mean': 90}), '☁')
        self.assertEqual(ec46_probability_icon(None), '―')
        self.assertEqual(ec46_probability_icon({}), '―')

    def test_ec46_day_never_uses_mean_weather_code(self):
        # Every sample of the day is EC46 and code 95 (thunderstorm); with no
        # member probabilities the icon must be the explicit no-claim marker,
        # never the mean's code.
        times = [f'2026-11-20T{h:02d}:00' for h in (0, 6, 12, 18)]
        row = {'chart_hourly': {
            'time': times, 'weather_code': [95] * 4,
            'sample_model': ['ecmwf_ec46_ensemble_mean_36km'] * 4}}
        self.assertEqual(daily_icon(row, dt.date(2026, 11, 20), ZoneInfo('UTC')), '―')
        row['ec46_daily'] = {'2026-11-20': {'p_rain_1mm': 0.2,
                                            'daytime_cloud_mean': 30}}
        self.assertEqual(daily_icon(row, dt.date(2026, 11, 20), ZoneInfo('UTC')), '🌤')


class ExtremaIntervalTests(unittest.TestCase):
    def test_interval_extremes_beat_sampled_values(self):
        tz = ZoneInfo('UTC')
        points = []
        base = dt.datetime(2026, 11, 7, tzinfo=dt.timezone.utc)
        for h in range(0, 48):
            t = base + dt.timedelta(hours=h)
            points.append((t, 10.0, 8.0, 12.0))
        highs, lows = extrema_by_date(points, dt.date(2026, 11, 7), tz, max_days=1)
        self.assertEqual(highs[0], 12.0)
        self.assertEqual(lows[0], 8.0)


if __name__ == '__main__':
    unittest.main()
