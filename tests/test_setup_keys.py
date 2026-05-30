import json
import tempfile
import unittest
from pathlib import Path

from wechat_bridge_collector.config import CollectorConfig
from wechat_bridge_collector.setup_keys import setup_collector


class SetupKeysTest(unittest.TestCase):
    def test_setup_writes_collector_owned_config_without_extracting(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd_dir = Path(tmp) / "wechat-decrypt"
            wd_dir.mkdir()
            (wd_dir / "key_utils.py").write_text("", encoding="utf-8")
            db_dir = Path(tmp) / "db_storage"
            db_dir.mkdir()
            state_dir = Path(tmp) / "state"

            cfg = CollectorConfig(
                state_dir=str(state_dir),
                wechat_decrypt_dir=str(wd_dir),
                db_dir=str(db_dir),
            )
            result = setup_collector(cfg, extract_keys=False)

            saved = json.loads((state_dir / "config.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "config_written")
        self.assertEqual(saved["keys_file"], str(state_dir / "all_keys.json"))
        self.assertEqual(saved["decrypted_dir"], str(state_dir / "decrypted"))
        self.assertEqual(saved["db_dir"], str(db_dir))


if __name__ == "__main__":
    unittest.main()
