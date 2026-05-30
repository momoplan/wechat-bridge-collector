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


if __name__ == "__main__":
    unittest.main()
