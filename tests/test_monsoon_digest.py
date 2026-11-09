"""Per-city monsoon / cold-surge digest (compute.monsoon_digest).

The 季節風 row's evidence used to be the Ulaanbaatar grid and the ABPW
text only; these tests pin the per-city limb: strongest 24h temperature
fall and pressure rise over the seven days from the window start, the
strongest wind inside the city's monsoon sector, and the surge
signature requiring both limbs past threshold.
"""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from compute import monsoon_digest

START = dt.datetime(2026, 11, 8, 21, 0, tzinfo=dt.timezone.utc)  # 11-09 06:00 JST


def _city(temp_fn, press_fn, wind_fn, tz='Asia/Hong_Kong'):
    times, temps, press, speeds, dirs, gusts = [], [], [], [], [], []
    for i in range(8 * 24):
        t = START + dt.timedelta(hours=i)
        times.append(t.isoformat())
        temps.append(temp_fn(i))
        press.append(press_fn(i))
        s, d, g = wind_fn(i)
        speeds.append(s)
        dirs.append(d)
        gusts.append(g)
    return {'timezone': tz,
            'hourly': {'time': times, 'temperature_2m': temps,
                       'pressure_msl': press, 'wind_speed_10m': speeds,
                       'wind_direction_10m': dirs, 'wind_gusts_10m': gusts}}


def _surge_city():
    def temp(i):
        if i < 48:
            return 20.0
        if i < 72:
            return 20.0 - 7.0 * (i - 48) / 24
        return 13.0

    def press(i):
        if i < 48:
            return 1010.0
        if i < 72:
            return 1010.0 + 8.0 * (i - 48) / 24
        return 1018.0

    def wind(i):
        return (12.5, 30.0, 20.0) if i == 60 else (3.0, 180.0, 5.0)

    return _city(temp, press, wind)


class MonsoonDigestTests(unittest.TestCase):
    def test_surge_signature_and_extremes(self):
        d = monsoon_digest(_surge_city(), 'Hong Kong', START)
        self.assertTrue(d['ready'])
        self.assertEqual(d['max_24h_temp_drop_c'], -7.0)
        self.assertEqual(d['max_24h_pressure_rise_hpa'], 8.0)
        self.assertTrue(d['surge_signature'])
        self.assertEqual(d['northerly_max_ms'], 12.5)
        self.assertEqual(d['northerly_gust_ms'], 20.0)
        # Drop ends 24h after the ramp completes its first full day:
        # local (UTC+8) rendering of 2026-11-11T21:00Z.
        self.assertEqual(d['drop_end_local'], '11-12 05:00')

    def test_weak_drop_no_signature(self):
        city = _surge_city()
        city['hourly']['temperature_2m'] = [
            20.0 if i < 48 else 17.0 for i in range(8 * 24)]
        d = monsoon_digest(city, 'Hong Kong', START)
        self.assertTrue(d['ready'])
        self.assertEqual(d['max_24h_temp_drop_c'], -3.0)
        self.assertFalse(d['surge_signature'])

    def test_southerly_wind_not_counted_as_northerly(self):
        def wind(i):
            return (15.0, 150.0, 22.0)
        city = _city(lambda i: 20.0, lambda i: 1010.0, wind)
        d = monsoon_digest(city, 'Hong Kong', START)
        self.assertTrue(d['ready'])
        self.assertIsNone(d['northerly_max_ms'])

    def test_missing_series_not_ready(self):
        d = monsoon_digest({'timezone': 'UTC', 'hourly': {}}, 'Taipei', START)
        self.assertFalse(d['ready'])


class MonsoonLongFeedTests(unittest.TestCase):
    """Dedicated long feed (2026-11-09): the digest's 7-day window is
    fed by city['monsoon_hourly']; the core 'hourly' series reaches
    only ~51 h into the window, so on the core series alone coverage
    can never complete, while the long series completes it and can
    carry a surge the core series never reaches."""

    def _truncated_core(self, hours=51):
        city = _city(lambda i: 20.0, lambda i: 1010.0,
                     lambda i: (3.0, 180.0, 5.0))
        for field in ('temperature_2m', 'pressure_msl', 'wind_speed_10m',
                      'wind_direction_10m', 'wind_gusts_10m', 'time'):
            city['hourly'][field] = city['hourly'][field][:hours]
        return city

    def _tail_surge_long(self):
        def temp(i):
            if i < 120:
                return 20.0
            if i < 144:
                return 20.0 - 7.0 * (i - 120) / 24
            return 13.0

        def press(i):
            if i < 120:
                return 1010.0
            if i < 144:
                return 1010.0 + 8.0 * (i - 120) / 24
            return 1018.0

        return _city(temp, press, lambda i: (3.0, 180.0, 5.0))['hourly']

    def test_core_series_alone_cannot_complete_coverage(self):
        d = monsoon_digest(self._truncated_core(), 'Hong Kong', START)
        self.assertTrue(d['ready'])
        self.assertEqual(d['coverage']['temperature_hours'], 51)
        self.assertFalse(d['surge_coverage_complete'])

    def test_long_series_completes_coverage_and_tail_surge(self):
        city = self._truncated_core()
        city['monsoon_hourly'] = self._tail_surge_long()
        d = monsoon_digest(city, 'Hong Kong', START)
        self.assertTrue(d['ready'])
        self.assertIn('monsoon_hourly', d['series_sources'])
        self.assertEqual(d['coverage']['expected_hours'], 169)
        self.assertEqual(d['coverage']['temperature_hours'], 169)
        self.assertEqual(d['coverage']['pressure_hours'], 169)
        self.assertTrue(d['coverage']['complete'])
        self.assertTrue(d['surge_coverage_complete'])
        # The surge sits at hours 120-144, far beyond the core series.
        self.assertTrue(d['surge_signature'])
        self.assertIsNotNone(d['surge_event'])

    def test_long_series_alone_is_enough(self):
        city = {'timezone': 'Asia/Hong_Kong',
                'monsoon_hourly': self._tail_surge_long()}
        d = monsoon_digest(city, 'Hong Kong', START)
        self.assertTrue(d['ready'])
        self.assertTrue(d['surge_coverage_complete'])
        self.assertTrue(d['surge_signature'])


if __name__ == '__main__':
    unittest.main()
