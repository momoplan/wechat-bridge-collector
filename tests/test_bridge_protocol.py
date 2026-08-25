import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_bridge_collector.bridge import BridgeClient, BridgeResponse
from wechat_bridge_collector.config import CollectorConfig
from wechat_bridge_collector.query_server import load_or_create_management_token


class BridgeProtocolTest(unittest.TestCase):
    def test_event_uses_host_app_id_and_v3_payload_field(self):
        with patch.dict(
            "os.environ",
            {"BAIJIMU_LOCAL_APP_ID": "registered-app-id"},
            clear=False,
        ):
            client = BridgeClient(CollectorConfig(bridge_event_token="event-secret"))
            with patch.object(
                client,
                "_post_json",
                return_value=BridgeResponse(True, 202, ""),
            ) as post:
                response = client.emit_message(
                    {"messageId": "message-1"},
                    "event-1",
                    "2026-08-26T00:00:00Z",
                )

        self.assertTrue(response.ok)
        request = post.call_args.args[1]
        self.assertEqual(request["appId"], "registered-app-id")
        self.assertNotIn("connectorId", request)
        self.assertEqual(post.call_args.args[2], "event-secret")

    def test_management_token_comes_from_host_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "management-token"
            token_file.write_text("m" * 64 + "\n", encoding="utf-8")
            with patch.dict(
                "os.environ",
                {"BAIJIMU_LOCAL_APP_TOKEN_FILE": str(token_file)},
                clear=False,
            ):
                self.assertEqual(load_or_create_management_token(), "m" * 64)


if __name__ == "__main__":
    unittest.main()
