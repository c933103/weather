import contextlib
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import collect
import ifs_cyclone as ifs
import read_bundle
from compute import compute_bundle
from prepare_report import prepare
from render_tables import _ecmwf_analysis, _ecmwf_values
from validate import build_validation

FIXTURES = Path(__file__).parent / 'fixtures' / 'ifs-cyclone'
CYCLE = dt.datetime(2026, 11, 21, tzinfo=dt.timezone.utc)
GENEVIEVE = dt.datetime(2020, 8, 22, tzinfo=dt.timezone.utc)


def capture(cycle, stream):
    return (FIXTURES / Path(ifs.product_path(cycle, stream)).name).read_bytes()


def point(member, lead):
    return next(p for p in member['points'] if p['lead_hours'] == lead)


class DecoderTests(unittest.TestCase):
    def test_native_aifs_short_cycle_wrapper_and_long_horizon(self):
        cycle = dt.datetime(2026, 11, 25, 6, tzinfo=dt.timezone.utc)
        sample = (FIXTURES / '20261125060000-360h-enfo-tf-aifs-sample.bufr').read_bytes()
        self.assertIn('/aifs-ens/0p25/enfo/', ifs.product_path(cycle, 'enfo', 'aifs-ens'))
        self.assertIn('-360h-', ifs.product_path(cycle, 'enfo', 'aifs-ens'))
        self.assertIn('-144h-', ifs.product_path(cycle, 'enfo', 'ifs'))
        system, = ifs.decode_tracks(sample, cycle, 'enfo', 'aifs-ens')
        self.assertEqual((system['storm_id'], len(system['members'])), ('74W', 35))
        self.assertEqual(system['lead_hours'][-1], 360)
        self.assertEqual(point(system['members'][0], 360)['valid_at_utc'],
                         '2026-12-10T06:00:00+00:00')
        with self.assertRaisesRegex(ValueError, 'template'):
            ifs.decode_tracks(sample, cycle, 'enfo')
        with self.assertRaisesRegex(ValueError, 'template'):
            ifs.decode_tracks(capture(CYCLE, 'enfo'), CYCLE, 'enfo', 'aifs-ens')
        with self.assertRaisesRegex(ValueError, 'differs from requested cycle'):
            ifs.decode_tracks(sample, cycle.replace(hour=12), 'enfo', 'aifs-ens')

    def test_live_xavier_message_counts_positions_and_member_types(self):
        oper = ifs.decode_tracks(capture(CYCLE, 'oper'), CYCLE, 'oper')
        enfo = ifs.decode_tracks(capture(CYCLE, 'enfo'), CYCLE, 'enfo')
        self.assertEqual((len(oper), len(enfo)), (9, 50))
        deterministic, ensemble = (next(t for t in rows if t['storm_id'] == '04P')
                                   for rows in (oper, enfo))
        self.assertEqual(deterministic['storm_name'], 'XAVIER')
        self.assertAlmostEqual(deterministic['reported_centre']['longitude'], -164.3)
        member, = deterministic['members']
        self.assertEqual((member['member_number'], member['forecast_type']), (51, 0))
        self.assertEqual(point(member, 0)['centre_significance'], 5)
        at_24 = point(member, 24)
        self.assertAlmostEqual(at_24['latitude'], -12.4)
        self.assertAlmostEqual(at_24['longitude'], -166.9)
        self.assertEqual(at_24['valid_at_utc'], '2026-11-22T00:00:00+00:00')
        self.assertEqual([(m['member_number'], m['forecast_type']) for m in ensemble['members']],
                         [(n, 4) for n in range(1, 51)] + [(51, 0)])
        self.assertEqual({point(m, 0)['centre_significance'] for m in ensemble['members']}, {4, 5})
        self.assertEqual(sum(point(m, 6)['latitude'] is None for m in ensemble['members']), 1)
        self.assertEqual(sum(point(m, 24)['latitude'] is None for m in ensemble['members']), 0)
        # The same control is delivered in both streams.
        self.assertEqual(at_24, point(ensemble['members'][-1], 24))
        # An uncompressed, single-member record may be a perturbed forecast.
        self.assertTrue(any(len(t['members']) == 1 and t['members'][0]['forecast_type'] == 4
                            for t in enfo))

    def test_historical_genevieve_distinguishes_observation_analysis_and_control(self):
        prefix = FIXTURES / 'tracks_12E_GENEVIEVE_2020082200'
        hres, = ifs.decode_tracks(prefix.with_suffix('.hres.bufr4').read_bytes(), GENEVIEVE, 'oper')
        ens, = ifs.decode_tracks(prefix.with_suffix('.ens.bufr4').read_bytes(), GENEVIEVE, 'enfo')
        self.assertEqual((hres['storm_id'], hres['storm_name']), ('12E', 'GENEVIEVE'))
        self.assertAlmostEqual(hres['reported_centre']['latitude'], 25.4)
        self.assertAlmostEqual(hres['reported_centre']['longitude'], -115.2)
        analysis = point(hres['members'][0], 0)
        self.assertAlmostEqual(analysis['latitude'], 26.3)
        self.assertAlmostEqual(analysis['longitude'], -116.3)
        self.assertEqual(analysis['pressure_hpa'], 1000)
        self.assertAlmostEqual(analysis['max_wind']['latitude'], 26.7)
        self.assertAlmostEqual(analysis['max_wind']['speed_m_s'], 15.9)
        self.assertEqual([(m['member_number'], m['forecast_type']) for m in ens['members']],
                         [(n, 4) for n in range(1, 51)] + [(51, 1), (52, 0)])
        self.assertEqual(sum(point(m, 24)['latitude'] is not None for m in ens['members']), 26)

    def test_short_cycles_with_variable_leads_and_nominal_filenames(self):
        cases = ((6, 'oper', 6, 6), (6, 'enfo', 33, 66),
                 (18, 'oper', 9, 12), (18, 'enfo', 29, 24))
        for hour, stream, count, shortest in cases:
            with self.subTest(hour=hour, stream=stream):
                cycle = CYCLE.replace(hour=hour)
                self.assertIn(f'/{hour:02d}z/ifs/0p25/{stream}/', ifs.product_path(cycle, stream))
                self.assertTrue(ifs.product_path(cycle, stream).endswith(f'-144h-{stream}-tf.bufr'))
                tracks = ifs.decode_tracks(capture(cycle, stream), cycle, stream)
                lengths = [t['lead_hours'][-1] for t in tracks]
                self.assertEqual((len(tracks), min(lengths), max(lengths)), (count, shortest, 144))
                for track in tracks:
                    self.assertEqual(track['lead_hours'],
                                     [p['lead_hours'] for p in track['members'][0]['points']])
        self.assertTrue(ifs.product_path(CYCLE.replace(hour=12), 'enfo').endswith('-360h-enfo-tf.bufr'))

    def test_malformed_framing_and_semantic_headers_fail_whole_archive(self):
        body = capture(CYCLE, 'oper')
        first_end = int.from_bytes(body[4:7], 'big')
        corrupt = {
            'HTML': b'<html>error</html>', 'leading bytes': b'junk' + body,
            'trailing bytes': body + b'junk', 'inter-message bytes': body[:first_end] + b'junk' + body[first_end:],
            'short header': body[:7], 'truncated second message': body[:first_end + 12],
            'bad terminator': body[:-1] + b'X', 'bad edition': body[:7] + b'\x03' + body[8:],
            'zero length': body[:4] + b'\0\0\0' + body[7:],
            'invalid section': body[:8] + b'\0\0\x03' + body[11:],
        }
        for case, data in corrupt.items():
            with self.subTest(case=case), self.assertRaises(ValueError):
                ifs.decode_tracks(data, CYCLE, 'oper')
        with self.assertRaisesRegex(ValueError, 'differs from requested cycle'):
            ifs.decode_tracks(body, CYCLE.replace(hour=12), 'oper')
        with self.assertRaisesRegex(ValueError, 'Duplicate storm/member'):
            ifs.decode_tracks(body + body, CYCLE, 'oper')
        # Alter only the raw descriptor, leaving the full framing intact.
        invalid_template = bytearray(body)
        section3 = 8 + int.from_bytes(body[8:11], 'big')
        if body[17] & 0x80:
            section3 += int.from_bytes(body[section3:section3 + 3], 'big')
        invalid_template[section3 + 8] ^= 1
        with self.assertRaisesRegex(ValueError, 'template'):
            ifs.decode_tracks(bytes(invalid_template), CYCLE, 'oper')


class CollectionTests(unittest.TestCase):
    def test_response_classification(self):
        cases = [
            ((404, b''), 'unpublished', False),
            (HTTPError('https://example.test', 404, 'missing', {}, None), 'unpublished', False),
            (TimeoutError('fixture timeout'), 'unavailable', False),
            ((429, b''), 'unavailable', False), ((503, b''), 'unavailable', False),
            ((200, b''), 'empty', True), ((200, b'<html>'), 'malformed', False),
            ((200, capture(CYCLE.replace(hour=6), 'oper')), 'no_regional_tracks', True),
        ]
        for response, status, ok in cases:
            def fetch(_):
                if isinstance(response, Exception):
                    raise response
                return response

            with self.subTest(status=status), patch('ifs_cyclone._fetch', side_effect=fetch):
                now = CYCLE.replace(hour=6) if status == 'no_regional_tracks' else CYCLE
                result = ifs.collect_tracks(now, 'oper')
                self.assertEqual((result['status'], result['ok']), (status, ok))
                self.assertEqual(result['response']['tracks'], [])
                self.assertEqual(result['model_initialized_at_utc'],
                                 now.isoformat() if status == 'no_regional_tracks' else None)

    def test_http_length_mismatch_detects_cleanly_truncated_archive(self):
        body = capture(CYCLE, 'oper')
        first_end = int.from_bytes(body[4:7], 'big')
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.headers = {'Content-Length': str(len(body))}
        response.read.return_value = body[:first_end]
        with patch('urllib.request.urlopen', return_value=response):
            result = ifs.collect_tracks(CYCLE, 'oper')
        self.assertEqual(result['status'], 'malformed')
        self.assertIn('Content-Length', result['error']['message'])

    def test_mirror_walkback_and_mixed_failure(self):
        now = CYCLE.replace(hour=8)
        live = capture(CYCLE, 'oper')
        with patch('ifs_cyclone._fetch', side_effect=[(404, b''), TimeoutError(),
                                                     (200, b'html'), (200, live)]) as fetch:
            result = ifs.collect_tracks(now, 'oper')
        self.assertEqual([a['status'] for a in result['attempts']],
                         ['unpublished', 'unavailable', 'malformed', 'available'])
        self.assertEqual(fetch.call_count, 4)
        self.assertIn(ifs.ORIGINS[1], result['url'])
        self.assertEqual(result['model_initialized_at_utc'], CYCLE.isoformat())
        self.assertEqual(result['response']['sha256'], hashlib.sha256(live).hexdigest())
        self.assertEqual([s['storm_id'] for s in result['response']['tracks']], ['70W'])
        with patch('ifs_cyclone._fetch', side_effect=[TimeoutError()] + [(404, b'')] * 7):
            result = ifs.collect_tracks(now, 'oper')
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(len(result['attempts']), 8)

    def test_valid_archive_without_regional_tracks_ends_walkback(self):
        cycle = CYCLE.replace(hour=6)
        with patch('ifs_cyclone._fetch', return_value=(200, capture(cycle, 'oper'))) as fetch:
            result = ifs.collect_tracks(cycle, 'oper')
        fetch.assert_called_once()
        self.assertEqual(result['status'], 'no_regional_tracks')
        self.assertEqual(result['response']['message_count'], 6)
        self.assertEqual(result['response']['tracks'], [])

    def test_scheduled_entry_point_to_normalized_output(self):
        class CollectionClock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return CYCLE.replace(hour=9).astimezone(tz)

        def offline_product(url):
            for stream in ifs.STREAMS:
                if url.endswith(ifs.product_path(CYCLE, stream)):
                    return 200, capture(CYCLE, stream)
            return 404, b''

        def offline_source(url, params=None, **kwargs):
            outlook = ('ABPW10 PGTW 210900\n'
                       'SUBJ/SIGNIFICANT TROPICAL WEATHER ADVISORY FOR THE WESTERN AND SOUTH\n'
                       'PACIFIC OCEANS/210600Z-220600ZNOV2026//\n'
                       '1. WESTERN NORTH PACIFIC AREA (180 TO MALAY PENINSULA):\n'
                       'A. TROPICAL CYCLONE SUMMARY:\n'
                       '(1) TROPICAL STORM 31W (TEST) WAS LOCATED NEAR 20.0N 130.0E.\n'
                       'B. TROPICAL DISTURBANCE SUMMARY:\n')
            return {'ok': 'abpw' in url or url.endswith('wp3126web.txt'),
                    'http_status': 404, 'response': outlook if 'abpw' in url else None}

        guided = {'ok': True, 'response': {'samples': []}}
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with (patch.object(collect, 'dt', wraps=dt) as clock,
                  patch('collect.request', side_effect=offline_source),
                  patch('ifs_cyclone._fetch', side_effect=offline_product),
                  patch('coastal.collect_coastal', return_value={'ready': False}),
                  patch('collect.parse_warning_track', return_value=[{'test': True}]),
                  patch('collect.gfs_track_check', return_value=guided),
                  patch('collect.ecmwf_opendata_track_check', return_value=guided) as fallback,
                  contextlib.redirect_stdout(io.StringIO())):
                clock.datetime = CollectionClock
                collect.collect(CYCLE.date(), out)
            fallback.assert_called_once()
            with contextlib.redirect_stdout(io.StringIO()):
                prepare(out)
            manifest = json.loads((out / 'latest' / 'manifest.json').read_bytes())
            raw = json.loads((out / manifest['raw_artifact']['filename']).read_bytes())

            def offline_bundle(request, timeout):
                if request.full_url.endswith('/git/ref/heads/data'):
                    return io.BytesIO(json.dumps({'object': {'sha': 'a' * 40}}).encode())
                name = request.full_url.rsplit('/latest/', 1)[1]
                return io.BytesIO((out / 'latest' / name).read_bytes())

            bundle_path = out / 'local-readback.json'
            with (patch.object(read_bundle, 'dt', wraps=dt) as clock,
                  patch('urllib.request.urlopen', side_effect=offline_bundle),
                  patch.object(sys, 'argv', ['read_bundle.py', '--target-date-jst', '2026-11-21',
                                             '--out', str(bundle_path)]),
                  contextlib.redirect_stdout(io.StringIO())):
                clock.datetime = CollectionClock
                read_bundle.main()
            bundle = json.loads(bundle_path.read_bytes())
            official = bundle['sources']
            calculated = bundle['calculated']

        self.assertEqual(raw['sources']['ecmwf/cyclone_product']['http_status'], 404)
        watch = calculated['cyclone_watch']
        self.assertIn('ecmwf_opendata/cyclone_check_31w', watch['track_checks'])
        self.assertEqual({s['id'] for s in watch['systems']}, {'31W'})
        later = compute_bundle({**raw, 'window_start_utc': '2026-11-22T06:00:00+00:00'})
        late_watch = later['cyclone_watch']
        self.assertEqual(late_watch['advisory_status'], 'expired')
        self.assertEqual(late_watch['valid_until_utc'], '2026-11-22T06:00:00+00:00')
        self.assertEqual(late_watch['systems'], [])
        self.assertEqual(late_watch['ifs_cyclone_tracks'], watch['ifs_cyclone_tracks'])
        for stream, count in (('oper', 9), ('enfo', 50)):
            name = ifs.SOURCE_PREFIX + stream
            self.assertEqual(manifest['source_status'][name]['status'], 'available')
            self.assertEqual(official[name], raw['sources'][name])
            normalized = watch['ifs_cyclone_tracks'][stream]
            self.assertEqual((normalized['model'], normalized['message_count']),
                             ('ecmwf_ifs_medium_range', count))
            self.assertEqual(normalized['cycle_utc'], CYCLE.isoformat())
            self.assertEqual(normalized['tracks'], official[name]['response']['tracks'])
            self.assertFalse({'04P', '12E'} & {s['storm_id'] for s in normalized['tracks']})
        ensemble = watch['ifs_cyclone_tracks']['enfo']['tracks']
        self.assertEqual(len(watch['ifs_cyclone_tracks']['oper']['tracks']), 1)
        self.assertEqual(len(ensemble), 16)
        self.assertIn('70C', {s['storm_id'] for s in ensemble})
        regional_storm = next(s for s in ensemble if s['storm_id'] == '70W')
        self.assertEqual(len(regional_storm['members']), 49)
        self.assertTrue(any(point(m, 0)['latitude'] is None for m in regional_storm['members']))
        self.assertIn('中期IFS BUFR', _ecmwf_analysis(watch))
        self.assertIn('IFS BUFR enfo 2026-11-21T00:00:00Z', _ecmwf_values(watch))
        self.assertEqual(build_validation(raw, calculated)['hazards']['cyclone_verification']['status'],
                         'caution')
