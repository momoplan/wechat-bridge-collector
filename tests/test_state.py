import tempfile
import unittest
from pathlib import Path

from wechat_bridge_collector.state import CollectorState


class CollectorStateTest(unittest.TestCase):
    def test_save_load_and_monotonic_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = CollectorState()
            state.sessions["alice"] = 10
            state.set_cursor("db#table", 20, 3)
            state.set_cursor("db#table", 19, 99)
            state.save(path)

            loaded = CollectorState.load(path)
            self.assertEqual(loaded.sessions["alice"], 10)
            self.assertEqual(loaded.cursors["db#table"].create_time, 20)
            self.assertEqual(loaded.cursors["db#table"].local_id, 3)


if __name__ == "__main__":
    unittest.main()

