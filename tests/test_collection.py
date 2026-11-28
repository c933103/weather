import contextlib
import datetime as dt
import io
import json
import math
import os
from pathlib import Path
import ssl
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from collect import (CITIES, CITY_FORECAST_URL, FORECAST_RECOVERY_TIMEOUT, MONSOON_FIELDS,
                     UTC, all_city_hourly_tides_ready, collect, recover_city_forecasts, request,
                     audit_hourly, audit_long, build_jobs, combined_temperature,
                     hko_tides, numeric, redact_secrets, report_date,
                     source_freshness)
from read_bundle import validate_manifest, verify_bytes


class ForecastRecoveryTests(unittest.TestCase):
    def test_first_pass_forecast_network_retries_keep_existing_backoff(self):
        error = urllib.error.URLError(TimeoutError('The handshake operation timed out'))
        with (patch('urllib.request.urlopen', side_effect=error) as fetch,
              patch('collect.time.sleep') as sleep):
            result = request(CITY_FORECAST_URL, {'models': 'ecmwf_ifs'})
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        self.assertTrue(result['transient_network_error'])
        self.assertTrue(all(a['elapsed_seconds'] >= 0 for a in result['attempts']))

    def test_http_and_certificate_errors_are_not_replayed(self):
        jobs = build_jobs(dt.date(2026, 11, 28))
        names = ['hong_kong/ifs', 'hong_kong/gfs', 'hong_kong/air']
        errors = [
            urllib.error.HTTPError(CITY_FORECAST_URL, 429, 'rate limited', {}, io.BytesIO(b'')),
            urllib.error.URLError(ssl.SSLCertVerificationError('certificate verification failed')),
            urllib.error.URLError(TimeoutError('The handshake operation timed out')),
        ]
        selected = {name: jobs[name] for name in names}
        failed = {}
        for name, error in zip(names, errors):
            with patch('urllib.request.urlopen', side_effect=error):
                failed[name] = request(jobs[name][0], jobs[name][1], retries=0)
        with patch('collect.request') as fetch:
            self.assertEqual(recover_city_forecasts(selected, failed, dict.fromkeys(names, 0)), {})
        fetch.assert_not_called()

    def test_deferred_forecast_retries_count_cooldown_from_the_failed_request(self):
        name = 'taipei/ifs'
        jobs = {name: build_jobs(dt.date(2026, 11, 28))[name]}
        sources = {name: {'ok': False, 'transient_network_error': True, 'attempts': []}}

        for cooldown, expected_start in [(20, 40), (60, 70)]:
            with self.subTest(cooldown=cooldown):
                now = 40

                def sleep(seconds):
                    nonlocal now
                    now += seconds

                def fetch_forecast(*args, **kwargs):
                    self.assertEqual(now, expected_start)
                    return {'ok': True, 'attempts': [{'ok': True}]}

                with (patch('collect.random.uniform', return_value=cooldown) as draw,
                      patch('collect.time.monotonic', side_effect=lambda: now),
                      patch('collect.time.sleep', side_effect=sleep),
                      patch('collect.request', side_effect=fetch_forecast)):
                    recovered = recover_city_forecasts(jobs, sources, {name: 10})
                draw.assert_called_once_with(20, 60)
                self.assertTrue(recovered[name]['ok'])

    def test_recovery_stops_starting_requests_after_its_budget(self):
        names = ['hong_kong/ifs_long', 'taipei/ifs', 'taipei/gfs']
        all_jobs = build_jobs(dt.date(2026, 11, 28))
        jobs = {name: all_jobs[name] for name in names}
        sources = {name: {'ok': False, 'transient_network_error': True,
                          'url': CITY_FORECAST_URL, 'attempts': []} for name in names}
        now = 0

        def expire():
            nonlocal now
            now = 5

        running = threading.Barrier(2, action=expire, timeout=2)

        def slow_request(url, params, *, retries, timeout):
            self.assertEqual((retries, timeout), (0, 5))
            running.wait()
            return {'ok': True, 'attempts': [{'ok': True}]}

        with (patch('collect.FORECAST_RECOVERY_BUDGET', 5),
              patch('collect.time.monotonic', side_effect=lambda: now),
              patch('collect.request', side_effect=slow_request) as fetch):
            recovered = recover_city_forecasts(jobs, sources, dict.fromkeys(names, -60))
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(set(recovered), {'taipei/ifs', 'taipei/gfs'})
        self.assertTrue(all(r['ok'] for r in recovered.values()))

    def test_late_recovery_feeds_existing_coverage_gates_and_writes_partial_manifest(self):
        # Replay the seven exhausted starts, with healthy forecast traffic later
        # in the first batch. Use the real collector, transport retry and audits.
        day = dt.date(2026, 11, 28)
        start = dt.datetime(2026, 11, 27, 21, tzinfo=UTC)
        jobs = build_jobs(day)
        missing = {'hong_kong/ifs', 'hong_kong/gfs', 'hong_kong/ifs_long', 'hong_kong/ifs_monsoon',
                   'taipei/ifs', 'taipei/gfs', 'taipei/ifs_monsoon'}
        by_url = {url + '?' + urllib.parse.urlencode(params): name
                  for name, (url, params, _) in jobs.items() if url == CITY_FORECAST_URL}
        lock = threading.Lock()

        class Response(io.BytesIO):
            status = 200
            headers = {}

        for permanent, incomplete in ((False, False), (True, False), (False, True)):
            with self.subTest(permanent=permanent, incomplete=incomplete):
                calls = {}
                later_forecast_started = threading.Event()

                def offline_get(req, timeout):
                    url = req.full_url
                    name = by_url.get(url)
                    if name is None:
                        raise urllib.error.HTTPError(url, 404, 'fixture', {}, io.BytesIO(b''))
                    with lock:
                        attempts = calls.setdefault(name, [])
                        attempts.append(timeout)
                        count = len(attempts)
                    if name in missing:
                        if count <= 3 or permanent:
                            raise urllib.error.URLError(TimeoutError('The handshake operation timed out'))
                        self.assertTrue(later_forecast_started.is_set())
                    else:
                        later_forecast_started.set()
                    params = jobs[name][1]
                    times = [(start + dt.timedelta(hours=h)).isoformat() for h in range(-24, 321)]
                    hourly = {field: [1.0] * len(times) for field in params['hourly'].split(',')}
                    if incomplete and name == 'taipei/ifs':
                        hourly['pressure_msl'][24] = None
                    return Response(json.dumps({'latitude': params['latitude'], 'longitude': params['longitude'],
                                                'hourly': {'time': times, **hourly}}).encode())

                with tempfile.TemporaryDirectory() as directory:
                    out = Path(directory)
                    with (patch('urllib.request.urlopen', side_effect=offline_get),
                          patch('collect.FORECAST_RETRY_COOLDOWN', (0, 0)),
                          patch('collect.time.sleep'), patch('coastal.collect_coastal', return_value={}),
                          patch('collect.collect_tracks', return_value={'ok': False, 'status': 'unpublished'}),
                          contextlib.redirect_stdout(io.StringIO())):
                        raw = collect(day, out)
                    manifest = json.loads((out / 'latest' / 'manifest.json').read_text())

                self.assertEqual(manifest['target_date_jst'], day.isoformat())
                self.assertEqual(manifest['quality']['state'], 'partial')  # Other fixture feeds are absent.
                self.assertEqual(manifest['quality']['core_24h_ready'], not (permanent or incomplete))
                self.assertEqual(manifest['quality']['gfs_24h_ready'], not permanent)
                for name, (url, _, _) in jobs.items():
                    if url != CITY_FORECAST_URL:
                        continue
                    if name not in missing:
                        self.assertEqual(calls[name], [25])
                        continue
                    self.assertEqual(calls[name], [25, 25, 25, FORECAST_RECOVERY_TIMEOUT])
                    source = raw['sources'][name]
                    first, second, third, recovery = source['attempts']
                    self.assertTrue(all(not a['ok'] and 'handshake' in a['error']['message']
                                        for a in (first, second, third)))
                    self.assertEqual(recovery['phase'], 'recovery')
                    self.assertEqual(recovery['url'], first['url'])
                    self.assertEqual(manifest['source_status'][name]['ok'], not permanent)
                for city in ('Hong Kong', 'Taipei'):
                    self.assertEqual(raw['cities'][city]['checks']['monsoon_7d']['ready'], not permanent)
                if incomplete:
                    pressure = raw['cities']['Taipei']['checks']['core_24h']['variables']['pressure_msl']
                    self.assertEqual(pressure['valid_slots'], 23)


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.start = dt.datetime(2026, 11, 7, 21, tzinfo=UTC)
        self.hourly = {'time': [(self.start + dt.timedelta(hours=i)).isoformat() for i in range(24)],
                       'temperature_2m': [20.0] * 24}

    def test_complete_exact_report_window(self):
        self.assertTrue(audit_hourly(self.hourly, ['temperature_2m'], self.start)['ready'])

    def test_null_duplicate_missing_field_and_wrong_day_fail(self):
        self.hourly['temperature_2m'][5] = None
        self.assertFalse(audit_hourly(self.hourly, ['temperature_2m'], self.start)['ready'])
        self.hourly['temperature_2m'][5] = 20
        self.hourly['time'][6] = self.hourly['time'][5]
        self.assertFalse(audit_hourly(self.hourly, ['temperature_2m'], self.start)['ready'])
        self.assertFalse(audit_hourly(self.hourly, ['pressure_msl'], self.start)['ready'])
        self.assertFalse(audit_hourly(self.hourly, ['temperature_2m'], self.start + dt.timedelta(days=1))['ready'])

    def test_non_finite_and_booleans_are_not_forecast_values(self):
        for bad in (math.nan, math.inf, True, '20', None):
            self.assertFalse(numeric(bad))

    def test_28_days_needs_both_boundaries(self):
        stop = self.start + dt.timedelta(days=28)
        hr = {'time': [(self.start + dt.timedelta(hours=i)).isoformat() for i in range(0, 673, 6)],
              'temperature_2m': [20] * 113}
        self.assertTrue(audit_long(hr, self.start, stop)['ready'])
        hr['time'].pop(); hr['temperature_2m'].pop()
        self.assertFalse(audit_long(hr, self.start, stop)['ready'])

    def test_ec46_tail_never_replaces_ifs_or_fills_hourly_values(self):
        a = {'time': ['2026-11-07T00:00', '2026-11-07T01:00'], 'temperature_2m': [21, 22]}
        b = {'time': ['2026-11-07T00:00', '2026-11-07T06:00'], 'temperature_2m': [19, 23]}
        merged = combined_temperature(a, b)
        self.assertEqual(merged['temperature_2m'], [21, 22, 23])
        self.assertEqual(len(merged['time']), 3)
        self.assertIn('ec46', merged['sample_model'][-1])

    def test_target_date_before_and_after_delivery(self):
        self.assertEqual(str(report_date(dt.datetime(2026, 11, 7, 20, tzinfo=UTC))), '2026-11-08')
        self.assertEqual(str(report_date(dt.datetime(2026, 11, 7, 22, tzinfo=UTC))), '2026-11-09')

    def test_stale_warning_is_not_current(self):
        source = {'response': {'reportDatetime': '2026-05-28T10:16:00+09:00'}}
        self.assertEqual(source_freshness(source, self.start)['freshness']['status'], 'stale')

    def test_hko_hour_24_rolls_into_following_local_day(self):
        source = {'ok': True, 'response': {'data': [['11', '08'] + ['1.2'] * 24]}}
        tide = hko_tides(source, 2026, dt.datetime(2026, 11, 8, 15, tzinfo=UTC),
                         dt.datetime(2026, 11, 8, 17, tzinfo=UTC))
        self.assertEqual(tide['time'], ['2026-11-08T15:00:00+00:00', '2026-11-08T16:00:00+00:00'])

    def test_api_secrets_redacted_from_urls_errors_and_nested_values(self):
        with patch.dict(os.environ, {'CWA_API_KEY': 'private-token'}):
            value = redact_secrets({'url': 'https://example.test?Authorization=private-token',
                                    'attempts': [{'error': 'private-token'}]})
            self.assertNotIn('private-token', str(value))

    def test_report_reader_rejects_wrong_date_and_stale_bundle(self):
        m = {'schema': 'east_asia_weather_raw_v3', 'target_date_jst': '2026-11-08',
             'collection_utc': self.start.isoformat(),
             'files': {k: {} for k in ('hong_kong', 'taipei', 'tokyo_minato', 'takamatsu', 'official')}}
        validate_manifest(m, '2026-11-08', self.start+dt.timedelta(hours=1), 3)
        with self.assertRaisesRegex(ValueError, 'WRONG_REPORT_DATE'):
            validate_manifest(m, '2026-11-09', self.start, 3)
        with self.assertRaisesRegex(ValueError, 'STALE_OR_FUTURE_COLLECTION'):
            validate_manifest(m, '2026-11-08', self.start+dt.timedelta(hours=4), 3)

    def test_report_reader_rejects_corrupt_data(self):
        with self.assertRaisesRegex(ValueError, 'DATASET_CHECKSUM_MISMATCH'):
            verify_bytes(b'changed', {'bytes': 7, 'sha256': '0'*64})


class MonsoonFeedJobTests(unittest.TestCase):
    """The dedicated long IFS series feeding the monsoon digest: one
    job per report city, exactly the digest's five fields, long enough
    to cover the 7-day window (169 stamps) with scheduling margin."""

    def test_every_city_has_a_long_monsoon_job(self):
        jobs = build_jobs(dt.date(2026, 11, 10))
        for key in CITIES:
            self.assertIn(f'{key}/ifs_monsoon', jobs)
            url, params, numeric_job = jobs[f'{key}/ifs_monsoon']
            self.assertTrue(numeric_job)
            self.assertEqual(params['models'], 'ecmwf_ifs')
            self.assertEqual(params['hourly'].split(','), MONSOON_FIELDS)
            self.assertGreaterEqual(
                params['forecast_days'] + params['past_days'], 10)
            # The core 24-hour series is untouched by the long feed.
            self.assertEqual(jobs[f'{key}/ifs'][1]['forecast_days'], 3)

    def test_hourly_tide_gate_taipei_events_can_stand_for_empty_reference(self):
        # Regression, run 43092342505 (2026-11-12): the CWA ocean platform
        # answered HTTP 200 with an empty chart_data, so Taipei's retained
        # hourly reference had 0 rows while its official event table was
        # ready — the collection must not fail on the reference alone.
        def city(key, n=24, ref_ready=None, events_ready=False):
            tide = {'time': ['t'] * n, 'ready': events_ready}
            if ref_ready is not None:
                tide['hourly_reference'] = {'ready': ref_ready,
                                            'time': ['t'] * (24 if ref_ready else 0)}
            return {'key': key, 'astronomical_tide': tide}
        full = [city('hong_kong'), city('tokyo_minato'), city('takamatsu'),
                city('taipei', n=0, ref_ready=True)]
        self.assertTrue(all_city_hourly_tides_ready(full))
        taipei_events_only = [city('hong_kong'), city('tokyo_minato'),
                              city('takamatsu'),
                              city('taipei', n=0, ref_ready=False, events_ready=True)]
        self.assertTrue(all_city_hourly_tides_ready(taipei_events_only))
        taipei_nothing = [city('hong_kong'), city('tokyo_minato'),
                          city('takamatsu'),
                          city('taipei', n=0, ref_ready=False, events_ready=False)]
        self.assertFalse(all_city_hourly_tides_ready(taipei_nothing))
        other_city_missing = [city('hong_kong'), city('tokyo_minato', n=23),
                              city('takamatsu'),
                              city('taipei', n=0, ref_ready=True)]
        self.assertFalse(all_city_hourly_tides_ready(other_city_missing))

    def test_monsoon_fields_are_exactly_what_the_digest_reads(self):
        self.assertEqual(sorted(MONSOON_FIELDS),
                         sorted(['temperature_2m', 'pressure_msl',
                                 'wind_speed_10m', 'wind_direction_10m',
                                 'wind_gusts_10m']))


class DelayedReportLookbackTests(unittest.TestCase):
    def test_late_edition_uses_numeric_prior_day_air_snow_and_ec46_bracket(self):
        from collect import AIR_FIELDS, parse_time
        from specialists import REGIONAL_FIELDS, REGIONS, extract_region, extract_snow, regional_grid_points
        day = dt.date(2026, 11, 26)
        start = dt.datetime(2026, 11, 25, 21, tzinfo=UTC)
        midnight = start + dt.timedelta(hours=3)
        end = start + dt.timedelta(hours=384)
        jobs = build_jobs(day)

        # Mirror the Nov 26 archive: native UTC responses were forward-only.
        # The provider honors past_days with numeric values, not merely times.
        def payload(params, fields, pace):
            t0 = midnight - dt.timedelta(days=params.get('past_days', 0))
            size = (params['forecast_days'] + params.get('past_days', 0)) * 24 // pace
            return {'time': [(t0 + dt.timedelta(hours=i * pace)).isoformat() for i in range(size)],
                    **{field: [0.5] * size for field in fields}}
        for city in CITIES:
            with self.subTest(city=city):
                air = jobs[f'{city}/air'][1]
                self.assertEqual(air['domains'], 'cams_global')
                self.assertEqual(audit_hourly(payload(dict(air, past_days=0), AIR_FIELDS, 1),
                                              AIR_FIELDS, start)['timestamp_slots'], 21)
                recovered = payload(air, AIR_FIELDS, 1)
                self.assertTrue(audit_hourly(recovered, AIR_FIELDS, start)['ready'])
                recovered[AIR_FIELDS[0]][21] = None
                self.assertFalse(audit_hourly(recovered, AIR_FIELDS, start)['ready'])

                snow = jobs[f'{city}/snow_ensemble'][1]
                self.assertEqual(snow['models'], 'ecmwf_ifs025')
                fields = ['snowfall'] + [f'snowfall_member{i:02d}' for i in range(1, 51)]
                def snow_check(params, fields):
                    return extract_snow({'ok': True, 'response': {'hourly': payload(params, fields, 1)}},
                                        start, audit_hourly)['check']
                self.assertEqual(snow_check(dict(snow, past_days=0), fields)['timestamp_slots'], 22)
                self.assertTrue(snow_check(snow, fields)['ready'])
                self.assertFalse(snow_check(snow, fields[:-1])['ready'])

        for region in REGIONS:
            with self.subTest(region=region):
                ec46 = jobs[f'regional/{region}'][1]
                self.assertEqual(ec46['models'], 'ecmwf_ec46_ensemble_mean')
                def grid(params):
                    return [{'latitude': lat, 'longitude': lon, 'hourly': payload(params, REGIONAL_FIELDS, 6)}
                            for lat, lon in regional_grid_points(region)]
                missed = extract_region(region, {'ok': True, 'response': grid(dict(ec46, past_days=0))},
                                        start, end, parse_time, numeric)
                self.assertFalse(missed['ready'])
                self.assertTrue(all(all(row['fields'].values()) and not row['time_coverage']
                                    for row in missed['checks']))
                response = grid(ec46)
                self.assertTrue(extract_region(region, {'ok': True, 'response': response},
                                               start, end, parse_time, numeric)['ready'])
                response[0]['hourly']['pressure_msl'][3] = None  # 18 UTC bracket
                self.assertFalse(extract_region(region, {'ok': True, 'response': response},
                                                start, end, parse_time, numeric)['ready'])


if __name__ == '__main__':
    unittest.main()
