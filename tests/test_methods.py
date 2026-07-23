import json
import sys
import unittest
import urllib.error
import urllib.request

from wechat_bridge_collector.autostart import start_command
from wechat_bridge_collector.bridge import BridgeClient, MESSAGE_EVENT_PAYLOAD_SCHEMA, METHOD_DECLARATIONS
from wechat_bridge_collector.config import CollectorConfig
from wechat_bridge_collector.query_server import (
    QueryMethodServer,
    SourceAccessState,
    dispatch_method,
)
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
            captured["data"]["healthCheck"],
            {
                "type": "http",
                "path": "/health",
                "timeoutSecs": 2,
                "expectStatus": 200,
            },
        )
        self.assertEqual(
            [method["name"] for method in captured["data"]["methods"]],
            [method["name"] for method in METHOD_DECLARATIONS],
        )
        self.assertEqual(captured["data"]["startCommand"], start_command())
        self.assertIn("messageReceived", [event["name"] for event in captured["data"]["events"]])
        self.assertEqual(captured["data"]["events"][0]["payload_schema"], MESSAGE_EVENT_PAYLOAD_SCHEMA)

    def test_start_command_uses_collector_cli(self):
        command = start_command()
        self.assertEqual(command["type"], "shell_command")
        self.assertEqual(command["command"], [sys.executable, "-m", "wechat_bridge_collector", "start"])
        self.assertEqual(command["timeoutSecs"], 20)


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

    def test_management_api_requires_token_and_dispatches_declared_operations(self):
        class FakeSource:
            db_dir = "/wechat"
            runtime = {"keys_file": "/state/all_keys.json"}
            all_keys = {
                "message/message_0.db": {},
                "contact/contact.db": {},
            }
            msg_db_keys = ["message/message_0.db"]

            def recent_sessions(self, limit=20):
                return [{"conversationId": "alice", "limit": limit}]

        token = "a" * 64
        server = QueryMethodServer(
            CollectorConfig(method_port=0),
            FakeSource(),
            management_token=token,
        )
        server.start()
        try:
            unauthorized = urllib.request.Request(
                server.base_url + "/management/v1/recent-sessions",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(unauthorized, timeout=5)
            self.assertEqual(raised.exception.code, 401)

            request = urllib.request.Request(
                server.base_url + "/management/v1/recent-sessions",
                data=b'{"limit":3}',
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"]["sessions"][0]["limit"], 3)

            state_request = urllib.request.Request(
                server.base_url + "/management/v1/state",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(state_request, timeout=5) as response:
                state = json.loads(response.read().decode("utf-8"))["data"]
            self.assertEqual(state["product"], "微信")
            self.assertEqual(state["probe"]["key_count"], 2)
        finally:
            server.stop()

    def test_management_state_remains_available_while_source_access_is_pending(self):
        class FakeSource:
            db_dir = "/protected/wechat"
            runtime = {"keys_file": "/state/all_keys.json"}
            all_keys = {"message/message_0.db": {}}
            msg_db_keys = ["message/message_0.db"]

            def recent_sessions(self, limit=20):
                raise AssertionError("source method must not run before access is ready")

        token = "c" * 64
        access_state = SourceAccessState()
        server = QueryMethodServer(
            CollectorConfig(method_port=0),
            FakeSource(),
            management_token=token,
            access_state=access_state,
        )
        server.start()
        try:
            headers = {"Authorization": f"Bearer {token}"}
            state_request = urllib.request.Request(
                server.base_url + "/management/v1/state",
                headers=headers,
            )
            with urllib.request.urlopen(state_request, timeout=5) as response:
                state = json.loads(response.read().decode("utf-8"))["data"]
            self.assertEqual(state["sourceAccess"]["status"], "checking")

            sessions_request = urllib.request.Request(
                server.base_url + "/management/v1/recent-sessions",
                data=b"{}",
                headers={**headers, "Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(sessions_request, timeout=5)
            self.assertEqual(raised.exception.code, 503)
            payload = json.loads(raised.exception.read().decode("utf-8"))
            self.assertEqual(payload["error"]["code"], "SOURCE_NOT_READY")
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
