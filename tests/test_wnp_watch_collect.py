import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import wnp_watch_collect as w

UTC = dt.timezone.utc

class WatchCollectionTests(unittest.TestCase):
    def test_cycle_rollover(self):
        at = dt.datetime(2026, 12, 1, 4, 10, tzinfo=UTC)
        self.assertEqual([c.isoformat() for c in w.latest_cycles(at)], [
            '2026-12-01T00:00:00+00:00',
            '2026-11-30T12:00:00+00:00',
            '2026-11-30T00:00:00+00:00'])
    def test_regions_are_not_cleared_by_missing_evidence(self):
        now = dt.datetime(2026, 11, 25, 4, 10, tzinfo=UTC)
        with patch.object(w, 'jtwc_inventory', return_value={'status': 'inventory_unverified'}), \\
             patch.object(w, 'cyclone_cycle', return_value={'status': 'PENDING'}), \\
             patch.object(w, 'probe_index', return_value={'status': 'fetch_failed'}):
            result = w.collection(now)
        self.assertEqual(len(result['coverage_matrix']), 4)
        self.assertTrue(all(result['coverage_matrix'][r][p][h] == 'PENDING'
                            for r in w.REGIONS for p in w.PERIODS for h in w.HAZARDS))
        self.assertEqual(result['ecmwf']['cycle_order'][0], '2026-11-25T00:00:00Z')
    def test_index_not_regional_forecast(self):
        c = dt.datetime(2026, 11, 25, tzinfo=UTC)
        self.assertIn('/aifs-ens/0p25/enfo/', w.grib_index_url(c, 'aifs-ens', 'enfo', 'ef', 24))

if __name__ == '__main__':
    unittest.main()
