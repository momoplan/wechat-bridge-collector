import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from wechat_bridge_collector.config import CollectorConfig
from wechat_bridge_collector.query_server import QueryMethodServer
from wechat_bridge_collector.source_runtime import SourceRuntime, validate_key_document


class FakeSource:
    def __init__(self, config):
        self.db_dir = config.db_dir or "/wechat"
        self.runtime = {"keys_file": str(config.default_keys_path)}
        self.all_keys = {"message/message_0.db": {"enc_key": "a" * 64}}
        self.msg_db_keys = ["message/message_0.db"]

    def assert_source_access(self):
        return None

    def recent_sessions(self, limit=20):
        return [{"conversationId": "ready", "limit": limit}]


class SourceRuntimeTest(unittest.TestCase):
    def test_missing_keys_management_import_and_hot_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = CollectorConfig(state_dir=tmp, db_dir="/wechat", method_port=0)
            runtime = SourceRuntime(config, source_factory=FakeSource)
            self.assertEqual(runtime.initialize()["status"], "keys_missing")

            token = "d" * 64
            server = QueryMethodServer(config, management_token=token, source_runtime=runtime)
            server.start()
            try:
                headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                state = _request(server.base_url + "/management/v1/state", headers=headers)
                self.assertEqual(state["data"]["sourceAccess"]["status"], "keys_missing")

                with self.assertRaises(urllib.error.HTTPError) as raised:
                    _request(
                        server.base_url + "/management/v1/recent-sessions",
                        headers=headers,
                        document={},
                    )
                self.assertEqual(raised.exception.code, 503)

                imported = _request(
                    server.base_url + "/management/v1/import-keys",
                    headers=headers,
                    document={"document": {"message/message_0.db": {"enc_key": "a" * 64}}},
                )
                self.assertEqual(imported["data"]["status"], "ready")
                sessions = _request(
                    server.base_url + "/management/v1/recent-sessions",
                    headers=headers,
                    document={"limit": 3},
                )
                self.assertEqual(sessions["data"]["sessions"][0]["limit"], 3)
                if os.name != "nt":
                    self.assertEqual(Path(tmp, "all_keys.json").stat().st_mode & 0o777, 0o600)
            finally:
                server.stop()

    def test_invalid_key_document_and_duplicate_acquire_are_rejected_or_coalesced(self):
        with self.assertRaisesRegex(ValueError, "64 位"):
            validate_key_document({"message.db": {"enc_key": "bad"}})

        calls = []
        release = threading.Event()

        def setup(_config, **_kwargs):
            calls.append(1)
            release.wait(2)
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            runtime = SourceRuntime(CollectorConfig(state_dir=tmp), source_factory=FakeSource, setup=setup)
            runtime.acquire_keys()
            runtime.acquire_keys()
            time.sleep(0.05)
            self.assertEqual(len(calls), 1)
            release.set()

    def test_failed_rotation_restores_previous_working_key_file(self):
        class KeyCheckingSource(FakeSource):
            def __init__(self, config):
                document = json.loads(config.default_keys_path.read_text())
                key = document["message/message_0.db"]["enc_key"]
                if key.startswith("b"):
                    raise RuntimeError("wrong key")
                super().__init__(config)

        with tempfile.TemporaryDirectory() as tmp:
            runtime = SourceRuntime(CollectorConfig(state_dir=tmp, db_dir="/wechat"), source_factory=KeyCheckingSource)
            runtime.import_keys({"document": {"message/message_0.db": {"enc_key": "a" * 64}}})
            with self.assertRaisesRegex(ValueError, "已恢复原密钥"):
                runtime.import_keys({"document": {"message/message_0.db": {"enc_key": "b" * 64}}})
            saved = json.loads(Path(tmp, "all_keys.json").read_text())
            self.assertEqual(saved["message/message_0.db"]["enc_key"], "a" * 64)
            self.assertEqual(runtime.snapshot()["status"], "ready")

    def test_legacy_default_key_is_copied_to_connector_private_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp, "state")
            private_dir = Path(tmp, "private")
            state_dir.mkdir()
            legacy = state_dir / "all_keys.json"
            legacy.write_text(json.dumps({"message/message_0.db": {"enc_key": "a" * 64}}))
            with patch.dict(os.environ, {"BAIJIMU_CONNECTOR_DATA_DIR": str(private_dir)}):
                runtime = SourceRuntime(CollectorConfig(state_dir=str(state_dir)), source_factory=FakeSource)
            self.assertEqual(runtime.keys_path, private_dir / "all_keys.json")
            self.assertEqual(json.loads(runtime.keys_path.read_text()), json.loads(legacy.read_text()))
            if os.name != "nt":
                self.assertEqual(runtime.keys_path.stat().st_mode & 0o777, 0o600)


def _request(url, *, headers, document=None):
    data = None if document is None else json.dumps(document).encode()
    request = urllib.request.Request(url, data=data, headers=headers, method="GET" if data is None else "POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode())


if __name__ == "__main__":
    unittest.main()
