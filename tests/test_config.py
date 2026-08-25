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
                    "BAIJIMU_LOCAL_APP_EVENT_TOKEN_FILE": str(token_file),
                },
                clear=False,
            ):
                cfg = CollectorConfig.load(Path(tmp) / "missing.json")

        self.assertEqual(cfg.bridge_event_token, "connector-event-secret")

    def test_save_writes_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.json"
            cfg = CollectorConfig(bridge_event_token="runtime-secret")
            written = cfg.save(path)

            self.assertEqual(written, path)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("connector_id", saved)
            self.assertNotIn("app_id", saved)
            self.assertNotIn("bridge_event_token", saved)
            self.assertNotIn("service_name", saved)

    def test_host_supplies_app_identity_and_private_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch(
                    "wechat_bridge_collector.config.LEGACY_STATE_DIR",
                    Path(tmp) / "legacy",
                ),
                patch.dict(
                    "os.environ",
                    {
                        "BAIJIMU_LOCAL_APP_ID": "registered-app-id",
                        "BAIJIMU_LOCAL_APP_DATA_DIR": tmp,
                    },
                    clear=False,
                ),
            ):
                cfg = CollectorConfig.load()
                self.assertEqual(cfg.app_id, "registered-app-id")
                self.assertEqual(cfg.state_dir, tmp)

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
