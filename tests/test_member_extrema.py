"""Member extrema for ensemble-sourced chart days.

An extremum of the ensemble-MEAN series is biased at every lead (measured
on the 2026-11-07 bundle's IFS ENS: highs understated ~0.19 °C on average,
worst 0.78; night lows overstated ~0.19). Ensemble-sourced days therefore
plot the mean of the members' own daily extrema, and their icons come from
member probabilities, for the IFS ENS tier as well as the EC46 tail.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from specialists import extract_ec46_daily, extract_temperature_ensemble
from render_chart import daily_icon, extrema_by_date


def hours(start, n, step=1):
    return [(start + timedelta(hours=step * i)).isoformat().replace('+00:00', 'Z')
            for i in range(n)]


class EnsDailyTests(unittest.TestCase):
    def test_member_extrema_and_probabilities(self):
        start = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)  # 11-08 00:00 JST
        ts = hours(start, 48)
        control = [10.0] * 48
        member = [10.0] * 48
        control[14] = 16.0   # 11-08 daytime peak, member 00
        member[13] = 18.0    # 11-08 daytime peak, member 01
        control[20] = 4.0    # night 11-08->09 low, member 00
        member[21] = 2.0     # night low, member 01
        src = {'ok': True, 'response': {'hourly': {
            'time': ts,
            'temperature_2m': control, 'temperature_2m_member01': member,
            'precipitation': [0.0] * 48, 'precipitation_member01': [0.1] * 48,
            'cloud_cover': [90] * 48, 'cloud_cover_member01': [90] * 48},
            'hourly_units': {}}}
        out = extract_temperature_ensemble(
            src, start, start + timedelta(days=2), 'Asia/Tokyo')
        day = out['daily']['2026-11-08']
        self.assertEqual(day['temp_high_member_mean'], 17.0)       # (16+18)/2
        self.assertEqual(day['temp_nightlow_member_mean'], 3.0)    # (4+2)/2
        self.assertEqual(day['p_rain_1mm'], 0.5)                   # one member sums 4.8mm
        self.assertEqual(day['daytime_cloud_mean'], 90.0)


class Ec46TempExtremaTests(unittest.TestCase):
    def test_temp_member_extrema_merged(self):
        start = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
        ts = hours(start, 8, step=6)  # 6-hourly stamps over 2 local days
        src = {'ok': True, 'response': {'hourly': {
            'time': ts,
            'precipitation_member01': [0.0] * 8, 'precipitation_member02': [0.0] * 8,
            'snowfall_member01': [0.0] * 8, 'snowfall_member02': [0.0] * 8,
            'cloud_cover_member01': [10] * 8, 'cloud_cover_member02': [10] * 8,
            'temperature_2m_member01': [10, 15, 12, 9, 11, 16, 13, 10],
            'temperature_2m_member02': [10, 13, 12, 9, 11, 14, 13, 10]}}}
        out = extract_ec46_daily(src, 'Asia/Tokyo')
        day = out['2026-11-08']
        self.assertEqual(day['temp_high_member_mean'], 14.0)  # (15+13)/2
        self.assertEqual(day['temp_members'], 2)


class ExtremaOverrideTests(unittest.TestCase):
    def _points(self, marker):
        start = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
        pts = []
        for i in range(72):
            v = 20.0 if i == 14 else 10.0  # sampled (mean-series) peak 20
            pts.append((start + timedelta(hours=i), v, None, None, marker))
        return pts

    def test_ensemble_day_uses_member_extremum(self):
        from datetime import date
        highs, lows = extrema_by_date(
            self._points('ecmwf_ifs025_ensemble_mean'), date(2026, 11, 8),
            ZoneInfo('Asia/Tokyo'),
            member_extrema={'2026-11-08': {'high': 17.5, 'night_low': 3.5}})
        self.assertEqual(highs[0], 17.5)
        self.assertEqual(lows[0], 3.5)

    def test_deterministic_day_ignores_override(self):
        from datetime import date
        highs, _ = extrema_by_date(
            self._points('ecmwf_ifs_9km'), date(2026, 11, 8),
            ZoneInfo('Asia/Tokyo'),
            member_extrema={'2026-11-08': {'high': 17.5, 'night_low': 3.5}})
        self.assertEqual(highs[0], 20.0)


class EnsIconTests(unittest.TestCase):
    def test_ens_majority_day_uses_member_probabilities(self):
        from datetime import date
        start = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
        row = {'chart_hourly': {
                   'time': hours(start, 24), 'weather_code': [None] * 24,
                   'sample_model': ['ecmwf_ifs025_ensemble_mean'] * 24},
               'temperature_ensemble': {'daily': {'2026-11-08': {
                   'p_rain_1mm': 0.6, 'p_rain_10mm': 0.1, 'p_snow': None,
                   'daytime_cloud_mean': 90}}}}
        self.assertEqual(daily_icon(row, date(2026, 11, 8), ZoneInfo('Asia/Tokyo')), '🌧')


if __name__ == '__main__':
    unittest.main()
