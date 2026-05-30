import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_bridge_collector.config import CollectorConfig


class CollectorConfigTest(unittest.TestCase):
    def test_load_reads_bridge_agent_local_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_config = Path(tmp) / "agent-config.json"
            agent_config.write_text(
                json.dumps(
                    {
                        "runtime": {
                            "event_server_token": "event-secret",
                            "service_registration_token": "register-secret",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {
                    "WS_BRIDGE_CONFIG": str(agent_config),
                    "BRIDGE_AGENT_EVENT_TOKEN": "",
                    "BRIDGE_AGENT_SERVICE_REGISTRATION_TOKEN": "",
                },
                clear=False,
            ):
                cfg = CollectorConfig.load(Path(tmp) / "missing.json")

        self.assertEqual(cfg.bridge_event_token, "event-secret")
        self.assertEqual(cfg.service_registration_token, "register-secret")

    def test_env_tokens_override_bridge_agent_local_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_config = Path(tmp) / "agent-config.json"
            agent_config.write_text(
                json.dumps(
                    {
                        "runtime": {
                            "event_server_token": "event-secret",
                            "service_registration_token": "register-secret",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {
                    "WS_BRIDGE_CONFIG": str(agent_config),
                    "BRIDGE_AGENT_EVENT_TOKEN": "event-env",
                    "BRIDGE_AGENT_SERVICE_REGISTRATION_TOKEN": "register-env",
                },
                clear=False,
            ):
                cfg = CollectorConfig.load(Path(tmp) / "missing.json")

        self.assertEqual(cfg.bridge_event_token, "event-env")
        self.assertEqual(cfg.service_registration_token, "register-env")

    def test_save_writes_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.json"
            cfg = CollectorConfig(service_name="wechatTest")
            written = cfg.save(path)

            self.assertEqual(written, path)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["service_name"], "wechatTest")

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
