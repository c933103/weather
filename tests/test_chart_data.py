"""Chart horizon and axis-label rules (user, 2026-11-15).

Both charts print at most 320 hours — 14 daily slots — and every date on
the axis carries its day of week beside it.
"""
import datetime as dt
import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from chart_data import CHART_DAYS, date_label, extrema_by_date


class TestChartHorizon(unittest.TestCase):
    def test_chart_days_is_the_320h_slot_count(self):
        self.assertEqual(CHART_DAYS, 14)

    def test_extrema_default_window_matches_chart_days(self):
        default = inspect.signature(extrema_by_date).parameters['max_days'].default
        self.assertEqual(default, CHART_DAYS)


class TestDateLabel(unittest.TestCase):
    def test_weekday_kanji_beside_each_date(self):
        # 2026-11-15 is a Sunday; the week runs 日月火水木金土 from there.
        expected = ['日', '月', '火', '水', '木', '金', '土']
        for offset, kanji in enumerate(expected):
            d = dt.date(2026, 11, 15) + dt.timedelta(days=offset)
            self.assertEqual(date_label(d), f"{d.strftime('%m/%d')}({kanji})")

    def test_label_shape(self):
        self.assertEqual(date_label(dt.date(2026, 11, 16)), '11/16(月)')
        self.assertEqual(date_label(dt.date(2026, 11, 30)), '11/30(月)')


if __name__ == '__main__':
    unittest.main()
