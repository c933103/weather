"""Regression tests: current exact-area warnings and time-aligned STOFS levels."""
import datetime as dt
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from official_hazards import (parse_jma_xml, parse_jma_json, parse_cwa_js,
                              parse_wra_kml, summarize)
from compute import coastal_assessment
from render_tables import _tokki_floor

NOW = dt.datetime(2026, 11, 18, 20, 2, tzinfo=dt.timezone.utc)
END = NOW + dt.timedelta(hours=25)


def source(body):
    return {'ok': True, 'response': body, 'freshness': {'status': 'current'},
            'fetched_at_utc': NOW.isoformat()}


def jma_xml(area='1310300', status='継続', kind='強風注意報'):
    return f'''<?xml version="1.0"?>
<Report><Head><ReportDateTime>2026-11-19T04:58:00+09:00</ReportDateTime>
<InfoType>発表</InfoType></Head>
<Body><Item><Kind><Name>波浪注意報</Name><Code>16</Code>
<Status>継続</Status></Kind><Area><Name>東京地方</Name>
<Code>130010</Code></Area><FullStatus>一部</FullStatus></Item>
<Item><Kind><Name>{kind}</Name><Code>15</Code><Status>{status}</Status></Kind>
<Area><Name>港区</Name><Code>{area}</Code></Area></Item></Body></Report>'''


class JmaOfficialTests(unittest.TestCase):
    def test_municipality_only_not_parent_region(self):
        got = parse_jma_xml(source(jma_xml()), '1310300', NOW)
        self.assertEqual(got['status'], 'ready')
        self.assertEqual([x['name'] for x in got['active']], ['強風注意報'])
        self.assertNotIn('波浪注意報', [x['name'] for x in got['active']])

    def test_cancellation_is_not_active(self):
        got = parse_jma_xml(source(jma_xml(status='解除')), '1310300', NOW)
        self.assertEqual(got['active'], [])
        self.assertEqual(got['cancelled'][0]['status'], '解除')

    def test_missing_city_does_not_inherit_parent(self):
        got = parse_jma_xml(source(jma_xml(area='1320100')), '1310300', NOW)
        self.assertEqual(got['status'], 'unassessed')

    def test_stale_xml_does_not_claim_current(self):
        past = jma_xml().replace('2026-11-19T04:58', '2026-11-17T04:58')
        got = parse_jma_xml(source(past), '1310300', NOW)
        self.assertEqual(got['status'], 'stale')

    def test_json_cancellation_does_not_resurrect_old_warning(self):
        recent = {'reportDatetime': '2026-11-19T04:58:00+09:00',
                  'warning': {'class20Items': [{'areaCode': '1310300',
                   'kinds': [{'code': '15', 'status': '解除'}]}]}}
        old = {'reportDatetime': '2026-11-19T00:00:00+09:00',
               'warning': {'class20Items': [{'areaCode': '1310300',
                'kinds': [{'code': '15', 'status': '発表'}]}]}}
        got = parse_jma_json(source([old, recent]), '1310300', NOW)
        self.assertEqual(got['active'], [])
        self.assertEqual(got['cancelled'][0]['code'], '15')


class TaiwanOfficialTests(unittest.TestCase):
    def test_cwa_exact_scope_does_not_include_taipei_via_new_taipei(self):
        js = r"""var WarnAll = ['W25'];
var WarnContent = {'W25': {'C': {'title': '陸上強風特報',
 'issued': '2026/11/18 22:25', 'validto': '2026/11/19 23:00',
 'content_web': '二、特報區域\n【黃色燈號】\n新北市、桃園市。\n三、注意事項'},
 'E': {'title': 'Strong Wind Advisory'}}};"""
        got = parse_cwa_js(source(js), NOW, END)
        self.assertEqual(got['status'], 'ready')
        self.assertEqual(got['warnings'][0]['areas'], ['新北市', '桃園市'])
        self.assertFalse(got['warnings'][0]['applies_taipei'])

    def test_cwa_unknown_scope_not_asserted_no_warning(self):
        js = """var WarnAll = ['W26'];
var WarnContent = {'W26': {'C': {'title': '大雨特報',
 'issued': '2026/11/18 23:00', 'validto': '2026/11/20 12:00',
 'content_web': '臺北市可能有大雨'}}};"""
        got = parse_cwa_js(source(js), NOW, END)
        self.assertEqual(got['status'], 'caution')
        self.assertIsNone(got['warnings'][0]['applies_taipei'])

    def test_wra_urban_and_river_are_distinct(self):
        none = """<?xml version="1.0"?>
        <kml><Folder><name>目前無淹水警示</name></Folder></kml>"""
        river = """<?xml version="1.0"?>
        <kml><Folder><Placemark><name>臺北市 河川水位二級警戒</name>
        </Placemark></Folder></kml>"""
        urban = parse_wra_kml(source(none), 'urban_inundation')
        river_state = parse_wra_kml(source(river), 'river_level')
        self.assertEqual(urban['status'], 'ready')
        self.assertFalse(urban['taipei_applicability'])
        self.assertEqual(river_state['warnings'][0]['level'], '二')
        self.assertTrue(river_state['taipei_applicability'])

    def test_wra_network_index_is_not_an_empty_warning(self):
        network_only = """<kml><NetworkLink><name>水位警示</name>
        <Link><href>https://example.org/a.kml</href></Link></NetworkLink></kml>"""
        self.assertEqual(parse_wra_kml(source(network_only), 'river_level')['status'], 'unassessed')

    def test_summary_separates_wra_urban_and_river(self):
        sources = {
            'wra/inundation_warning': source('<kml><Folder><name>目前無淹水警示</name></Folder></kml>'),
            'wra/river_level_warning': source('<kml><Folder><name>目前無水位警示</name></Folder></kml>')}
        got = summarize(sources, NOW.isoformat(), END.isoformat())
        self.assertEqual(got['regional']['wra_urban_inundation']['status'], 'ready')
        self.assertEqual(got['regional']['wra_river_level']['status'], 'ready')
        self.assertEqual(got['cities']['taipei']['status'], 'missing')


class AlignedStormSurgeTests(unittest.TestCase):
    def _city(self, tide):
        times = [(NOW + dt.timedelta(hours=i)).isoformat() for i in range(24)]
        combined = [1.0] * 24
        combined[1] = 3.0
        return {'coastal_forecast': {
                'ready': True, 'datum': 'NOAA same model datum', 'time': times,
                'combined_water_level': combined,
                'astronomical_tide': tide, 'surge_residual': [0.15] * 24},
                'astronomical_tide': {'ready': True, 'station': 'Harumi',
                  'datum': 'JMA DL', 'events': []}}

    def test_max_of_aligned_hourly_differences_not_difference_of_maxima(self):
        tide = [0.0] * 24
        tide[1] = 2.9  # different hour maximizes total and model tide
        got = coastal_assessment(self._city(tide))
        self.assertEqual(got['exceedance_over_model_tide_m'], 1.0)
        self.assertEqual(got['exceedance_time_utc'], NOW.isoformat())
        self.assertEqual(got['aligned_hours'], 24)
        self.assertEqual(got['surge_residual']['maximum'], 0.15)

    def test_missing_hour_disables_matched_comparison_but_not_residual(self):
        tide = [0.0] * 24
        tide[3] = None
        got = coastal_assessment(self._city(tide))
        self.assertIsNone(got['exceedance_over_model_tide_m'])
        self.assertEqual(got['aligned_hours'], 0)
        self.assertIsNotNone(got['surge_residual'])


class RenderOfficialTests(unittest.TestCase):
    def test_tokkis_include_only_verified_city_warning_and_separate_wra(self):
        official = {
            'status': 'ready', 'active': [{'name': '強風注意報'}],
            'urban_inundation': {'status': 'ready', 'warnings': [{'level': '一'}]},
            'river_level': {'status': 'ready', 'warnings': [{'level': '二'}]}}
        out = _tokki_floor({}, {}, official)
        self.assertIn('公式強風注意報', out)
        self.assertIn('淹水一級警戒', out)
        self.assertIn('河川水位二級警戒', out)

    def test_unavailable_official_source_never_prints_no_hazard(self):
        self.assertIn('公式警報一部未検証',
                      _tokki_floor({}, {}, {'status': 'missing', 'active': []}))


if __name__ == '__main__':
    unittest.main()
