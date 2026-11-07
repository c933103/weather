import datetime as dt
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from collect import jtwc_system_ids
from compute import (chart_confidence_for_city, compass16, grade_from_spread,
                     hazard_flags, parse_jtwc_abpw)
from specialists import extract_temperature_ensemble

ABPW_SAMPLE = (
    "ABPW10 PGTW 070600\r\n"
    "SUBJ/SIGNIFICANT TROPICAL WEATHER ADVISORY FOR THE WESTERN AND SOUTH \r\n"
    "PACIFIC OCEANS/070600Z-080600ZNOV2026//\r\n"
    "1. WESTERN NORTH PACIFIC AREA (180 TO MALAY PENINSULA):\r\n"
    "   A. TROPICAL CYCLONE SUMMARY:\r\n"
    "      (1) AT 07NOV26 0000Z, TROPICAL STORM 31W (CEMPAKA) WAS LOCATED \r\n"
    "NEAR 19.0N 144.2E, APPROXIMATELY 321 NM NORTH OF ANDERSEN AFB, AND HAD \r\n"
    "TRACKED WEST-NORTHWESTWARD AT 16 KNOTS OVER THE PAST SIX HOURS. MAXIMUM \r\n"
    "SUSTAINED SURFACE WINDS WERE ESTIMATED AT 35 KNOTS GUSTING TO 45 KNOTS. \r\n"
    "SEE REF A (WTPN31 PGTW 070300) FOR FURTHER DETAILS.\r\n"
    "      (2) NO OTHER TROPICAL CYCLONES.\r\n"
    "   B. TROPICAL DISTURBANCE SUMMARY: NONE.\r\n"
    "   C. SUBTROPICAL SYSTEM SUMMARY: NONE.\r\n"
    "2. SOUTH PACIFIC AREA (WEST COAST OF SOUTH AMERICA TO 135 EAST):\r\n"
    "   A. TROPICAL CYCLONE SUMMARY: NONE.\r\n")


class JtwcTests(unittest.TestCase):
    def test_system_ids_from_advisory(self):
        self.assertEqual(jtwc_system_ids(ABPW_SAMPLE), ['31W'])
        self.assertEqual(jtwc_system_ids('no systems here'), [])
        self.assertEqual(jtwc_system_ids(None), [])

    def test_abpw_digest(self):
        digest = parse_jtwc_abpw(ABPW_SAMPLE)
        self.assertEqual(len(digest['systems']), 1)
        system = digest['systems'][0]
        self.assertEqual(system['id'], '31W')
        self.assertEqual(system['name'], 'CEMPAKA')
        self.assertEqual(system['category'], 'TROPICAL STORM')
        self.assertEqual(system['latitude'], 19.0)
        self.assertEqual(system['longitude'], 144.2)
        self.assertEqual(system['movement_kt'], 16)
        self.assertEqual(system['max_winds_kt'], 35)
        self.assertEqual(system['gusts_kt'], 45)
        self.assertTrue(digest['no_other_cyclones'])
        self.assertTrue(digest['disturbances_none'])
        self.assertTrue(digest['subtropical_none'])

    def test_abpw_empty(self):
        digest = parse_jtwc_abpw(None)
        self.assertEqual(digest['systems'], [])


class HelperTests(unittest.TestCase):
    def test_grade_thresholds(self):
        self.assertEqual(grade_from_spread(1.2), '高')
        self.assertEqual(grade_from_spread(1.21), '中')
        self.assertEqual(grade_from_spread(2.2), '中')
        self.assertEqual(grade_from_spread(2.21), '低')
        self.assertEqual(grade_from_spread(None), '低')

    def test_hazard_flag_thresholds(self):
        self.assertTrue(hazard_flags(55, None, None)['high_pm'])
        self.assertTrue(hazard_flags(None, 155, None)['high_pm'])
        self.assertFalse(hazard_flags(54.9, 154, None)['high_pm'])
        self.assertTrue(hazard_flags(None, None, 0.0)['frost'])
        self.assertFalse(hazard_flags(None, None, 0.1)['frost'])
        self.assertFalse(hazard_flags(None, None, None)['frost'])

    def test_compass(self):
        self.assertEqual(compass16(0), '北')
        self.assertEqual(compass16(90), '東')
        self.assertEqual(compass16(225), '南西')
        self.assertIsNone(compass16(None))


def synthetic_city():
    start = dt.datetime(2026, 11, 7, 15, tzinfo=dt.timezone.utc)  # 2026-11-08 00:00 JST
    times = [(start + dt.timedelta(hours=i)).isoformat() for i in range(72)]
    hourly = {'time': times}
    fields = ['temperature_2m'] + [f'temperature_2m_member{m:02d}' for m in range(1, 12)]
    for index, field in enumerate(fields):
        values = []
        for i in range(72):
            day = i // 24
            base = 20.0 if day == 0 else 15.0 + index * 1.0  # day 2 spreads members widely
            values.append(base + (i % 24) * 0.1)
        hourly[field] = values
    return {'timezone': 'Asia/Tokyo', 'temperature_ensemble': {'hourly': hourly}}


class ConfidenceTests(unittest.TestCase):
    def test_extract_temperature_ensemble(self):
        city = synthetic_city()
        source = {'ok': True, 'response': {'hourly': city['temperature_ensemble']['hourly'],
                                           'hourly_units': {}}}
        block = extract_temperature_ensemble(source, dt.datetime(2026, 11, 7, 21, tzinfo=dt.timezone.utc),
                                             dt.datetime(2026, 11, 23, 21, tzinfo=dt.timezone.utc))
        self.assertEqual(block['check']['member_fields'], 12)
        self.assertFalse(block['check']['members_ready'])  # fixture has only 12 fields

    def test_confidence_grades(self):
        grades = chart_confidence_for_city(synthetic_city(), dt.date(2026, 11, 8))
        self.assertEqual(grades['2026-11-08']['basis'], 'ensemble_spread')
        self.assertEqual(grades['2026-11-08']['grade'], '高')
        self.assertEqual(grades['2026-11-09']['grade'], '低')
        beyond = grades['2026-11-18']
        self.assertEqual(beyond['basis'], 'outside_ensemble_coverage')
        self.assertEqual(beyond['grade'], '低')

    def test_confidence_missing_ensemble(self):
        self.assertEqual(chart_confidence_for_city({'timezone': 'Asia/Tokyo'}, dt.date(2026, 11, 8)), {})


if __name__ == '__main__':
    unittest.main()
