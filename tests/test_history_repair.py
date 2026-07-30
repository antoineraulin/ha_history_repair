"""Unit tests for history_repair core logic."""
import sys
import os
import unittest
from unittest.mock import MagicMock

# ponytail: mock homeassistant & voluptuous modules if not installed in host python env
try:
    import voluptuous
except ImportError:
    vol = MagicMock()
    sys.modules["voluptuous"] = vol

try:
    import homeassistant
except ImportError:
    ha = MagicMock()
    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.components"] = ha.components
    sys.modules["homeassistant.components.recorder"] = ha.components.recorder
    sys.modules["homeassistant.components.recorder.db_schema"] = ha.components.recorder.db_schema
    sys.modules["homeassistant.components.recorder.util"] = ha.components.recorder.util
    sys.modules["homeassistant.core"] = ha.core
    sys.modules["homeassistant.helpers"] = ha.helpers
    sys.modules["homeassistant.helpers.config_validation"] = ha.helpers.config_validation
    sys.modules["homeassistant.util"] = ha.util
    
    # Mock utc_to_timestamp and utc_from_timestamp
    import datetime
    ha.util.dt.utc_to_timestamp = lambda dt: dt.timestamp() if isinstance(dt, datetime.datetime) else dt
    ha.util.dt.utc_from_timestamp = lambda ts: datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from custom_components.history_repair.__init__ import _is_zero, _to_float, _ts_from_dt, _find_zero_intervals


class TestHistoryRepairLogic(unittest.TestCase):
    def test_is_zero(self):
        self.assertTrue(_is_zero("0"))
        self.assertTrue(_is_zero("0.0"))
        self.assertTrue(_is_zero("0.00"))
        self.assertTrue(_is_zero("-0.0"))
        self.assertFalse(_is_zero("21.5"))
        self.assertFalse(_is_zero("unavailable"))
        self.assertFalse(_is_zero(None))

    def test_to_float(self):
        self.assertEqual(_to_float("21.5"), 21.5)
        self.assertEqual(_to_float("0"), 0.0)
        self.assertIsNone(_to_float("unavailable"))
        self.assertIsNone(_to_float(None))

    def test_ts_from_dt(self):
        self.assertEqual(_ts_from_dt(100.0), 100.0)
        self.assertIsNone(_ts_from_dt(None))

    def test_find_zero_intervals_mock(self):
        session = MagicMock()
        meta_mock = MagicMock()
        meta_mock.metadata_id = 1
        session.query.return_value.filter.return_value.first.return_value = meta_mock

        # Setup rows: 20.0 -> 0.0 -> 0.0 -> 21.0
        row1 = ("20.0", 1000.0)
        row2 = ("0.0", 1005.0)
        row3 = ("0.0", 1010.0)
        row4 = ("21.0", 1015.0)

        query_mock = MagicMock()
        query_mock.order_by.return_value.all.return_value = [row1, row2, row3, row4]
        session.query.return_value.filter.return_value = query_mock

        res = _find_zero_intervals(session, "sensor.test", None, None)
        self.assertEqual(res["entity_id"], "sensor.test")
        self.assertEqual(res["zero_intervals_found"], 1)
        self.assertEqual(len(res["intervals"]), 1)

        inv = res["intervals"][0]
        self.assertEqual(inv["zero_count"], 2)
        self.assertEqual(inv["previous_valid_value"], 20.0)
        self.assertEqual(inv["start_ts"], 1005.0)
        self.assertEqual(inv["end_ts"], 1010.0)


if __name__ == "__main__":
    unittest.main()
