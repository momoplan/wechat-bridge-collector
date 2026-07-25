import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_bridge_collector.config import CollectorConfig


class CollectorConfigTest(unittest.TestCase):
    def test_load_reads_connector_event_credential_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "event-publisher-token"
            token_file.write_text("connector-event-secret\n", encoding="utf-8")

            with patch.dict(
                "os.environ",
                {
                    "BAIJIMU_CONNECTOR_EVENT_TOKEN_FILE": str(token_file),
                },
                clear=False,
            ):
                cfg = CollectorConfig.load(Path(tmp) / "missing.json")

        self.assertEqual(cfg.bridge_event_token, "connector-event-secret")

    def test_save_writes_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.json"
            cfg = CollectorConfig(connector_id="com.example.wechat")
            written = cfg.save(path)

            self.assertEqual(written, path)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["connector_id"], "com.example.wechat")
            self.assertNotIn("service_name", saved)

    def test_default_runtime_uses_collector_owned_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "collector"
            wd_dir = Path(tmp) / "wechat-decrypt"
            wd_dir.mkdir()
            (wd_dir / "key_utils.py").write_text("", encoding="utf-8")
            db_dir = Path(tmp) / "db_storage"
            db_dir.mkdir()

            cfg = CollectorConfig(
                state_dir=str(state_dir),
                wechat_decrypt_dir=str(wd_dir),
                db_dir=str(db_dir),
            )
            runtime = cfg.load_wechat_decrypt_runtime()

        self.assertEqual(runtime["keys_file"], str(state_dir / "all_keys.json"))
        self.assertEqual(runtime["decrypted_dir"], str(state_dir / "decrypted"))

    def test_explicit_keys_file_is_still_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd_dir = Path(tmp) / "wechat-decrypt"
            wd_dir.mkdir()
            (wd_dir / "key_utils.py").write_text("", encoding="utf-8")
            db_dir = Path(tmp) / "db_storage"
            db_dir.mkdir()
            keys_file = Path(tmp) / "external-keys.json"

            cfg = CollectorConfig(
                wechat_decrypt_dir=str(wd_dir),
                db_dir=str(db_dir),
                keys_file=str(keys_file),
            )
            runtime = cfg.load_wechat_decrypt_runtime()

        self.assertEqual(runtime["keys_file"], str(keys_file))


if __name__ == "__main__":
    unittest.main()
