import json
import unittest
import urllib.error
import urllib.request

from wechat_bridge_collector.bridge import BridgeClient, METHOD_DECLARATIONS
from wechat_bridge_collector.config import CollectorConfig
from wechat_bridge_collector.query_server import QueryMethodServer, dispatch_method
from wechat_bridge_collector.wechat_source import parse_message_id, parse_time_range, resolve_type_filter


class BridgeRegistrationTest(unittest.TestCase):
    def test_register_service_declares_query_methods(self):
        captured = {}

        class CapturingBridgeClient(BridgeClient):
            def _post_json(self, url, data, token=None):
                captured["url"] = url
                captured["data"] = data
                return type("Response", (), {"ok": True, "status": 201, "body": "{}"})()

        cfg = CollectorConfig(method_host="127.0.0.1", method_port=19090)
        CapturingBridgeClient(cfg).register_service("http://127.0.0.1:19091")

        self.assertEqual(captured["data"]["transport"]["baseUrl"], "http://127.0.0.1:19091")
        self.assertEqual(
            [method["name"] for method in captured["data"]["methods"]],
            [method["name"] for method in METHOD_DECLARATIONS],
        )
        self.assertIn("messageReceived", [event["name"] for event in captured["data"]["events"]])


class QueryServerTest(unittest.TestCase):
    def test_dispatch_and_http_response(self):
        class FakeSource:
            def recent_sessions(self, limit=20):
                return [{"conversationId": "alice", "limit": limit}]

            def contacts(self, query="", limit=50):
                return [{"username": "alice", "query": query, "limit": limit}]

        result = dispatch_method(FakeSource(), "getContacts", {"query": "ali", "limit": 3})
        self.assertEqual(result["contacts"][0]["username"], "alice")

        server = QueryMethodServer(CollectorConfig(method_port=0), FakeSource())
        server.start()
        try:
            body = json.dumps({"limit": 2}).encode("utf-8")
            req = urllib.request.Request(
                server.base_url + "/invoke/getRecentSessions",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(payload["success"])
            self.assertEqual(payload["data"]["sessions"][0]["conversationId"], "alice")
        finally:
            server.stop()

    def test_unknown_method_is_bad_request(self):
        class FakeSource:
            pass

        server = QueryMethodServer(CollectorConfig(method_port=0), FakeSource())
        server.start()
        try:
            req = urllib.request.Request(
                server.base_url + "/invoke/missing",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(raised.exception.code, 400)
        finally:
            server.stop()


class QueryParsingTest(unittest.TestCase):
    def test_time_type_and_message_id_parsing(self):
        start, end = parse_time_range("2026-06-02", "2026-06-02")
        self.assertLess(start, end)
        self.assertEqual(resolve_type_filter(["text", "image"]), {1, 3})
        self.assertEqual(
            parse_message_id("message/message_0.db:Msg_0123456789abcdef0123456789abcdef:123"),
            ("message/message_0.db", "Msg_0123456789abcdef0123456789abcdef", 123),
        )


if __name__ == "__main__":
    unittest.main()
