import json
import sys
import unittest
import urllib.error
import urllib.request

from wechat_bridge_collector.app import load_complete_contact_snapshot, retry_delay
from wechat_bridge_collector.autostart import start_command
from wechat_bridge_collector.config import CollectorConfig
from wechat_bridge_collector.query_server import (
    QueryMethodServer,
    SourceAccessState,
    dispatch_method,
)
from wechat_bridge_collector.wechat_source import (
    epoch_seconds_to_millis,
    parse_message_id,
    parse_time_range,
    resolve_type_filter,
)


class ConnectorLifecycleTest(unittest.TestCase):
    def test_start_command_uses_collector_cli(self):
        command = start_command()
        self.assertEqual(command["type"], "shell_command")
        self.assertEqual(command["command"], [sys.executable, "-m", "wechat_bridge_collector", "start"])
        self.assertEqual(command["timeoutSecs"], 20)

    def test_snapshot_retry_uses_bounded_exponential_backoff(self):
        self.assertEqual(retry_delay(2.0, 1), 2.0)
        self.assertEqual(retry_delay(2.0, 2), 4.0)
        self.assertEqual(retry_delay(2.0, 6), 60.0)
        self.assertEqual(retry_delay(2.0, 20), 60.0)

    def test_complete_contact_snapshot_reads_every_page(self):
        class FakeSource:
            def contact_snapshot(self, limit=500, offset=0, include_groups=False):
                contacts = [{"username": f"user-{index}"} for index in range(7)]
                page = contacts[offset : offset + limit]
                return {
                    "account": {"accountId": "sales", "source": "test", "platform": "darwin"},
                    "snapshotToken": "snapshot-1",
                    "contacts": page,
                    "offset": offset,
                    "limit": limit,
                    "total": len(contacts),
                    "hasMore": offset + len(page) < len(contacts),
                }

        snapshot = load_complete_contact_snapshot(FakeSource(), page_size=3)
        self.assertEqual(len(snapshot["contacts"]), 7)
        self.assertEqual(snapshot["total"], 7)
        self.assertFalse(snapshot["hasMore"])


class QueryServerTest(unittest.TestCase):
    def test_dispatch_and_http_response(self):
        class FakeSource:
            def account_profile(self):
                return {"accountId": "wxid_sales", "displayName": "wxid_sales"}

            def contact_snapshot(self, limit=500, offset=0, include_groups=False):
                return {"contacts": [], "limit": limit, "offset": offset, "includeGroups": include_groups}

            def recent_sessions(self, limit=20):
                return [{"conversationId": "alice", "limit": limit}]

            def contacts(self, query="", limit=50, offset=0, include_groups=True):
                return [{"username": "alice", "query": query, "limit": limit, "offset": offset, "includeGroups": include_groups}]

        result = dispatch_method(FakeSource(), "getContacts", {"query": "ali", "limit": 3})
        self.assertEqual(result["contacts"][0]["username"], "alice")
        snapshot = dispatch_method(FakeSource(), "getContactSnapshot", {"offset": 25, "includeGroups": False})
        self.assertEqual(snapshot["offset"], 25)
        self.assertEqual(dispatch_method(FakeSource(), "getAccountProfile", {})["account"]["accountId"], "wxid_sales")

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
            self.assertEqual(payload["errorCode"], "0")
            self.assertIsInstance(payload["systemCurrentTime"], int)
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
        self.assertEqual(epoch_seconds_to_millis(1_785_984_572), 1_785_984_572_000)
        start, end = parse_time_range(1_780_358_400_000, 1_780_444_799_999)
        self.assertEqual(start, 1_780_358_400)
        self.assertEqual(end, 1_780_444_799)
        with self.assertRaisesRegex(ValueError, "毫秒"):
            parse_time_range(1_780_358_400, None)
        self.assertEqual(resolve_type_filter(["text", "image"]), {1, 3})
        self.assertEqual(
            parse_message_id("message/message_0.db:Msg_0123456789abcdef0123456789abcdef:123"),
            ("message/message_0.db", "Msg_0123456789abcdef0123456789abcdef", 123),
        )


if __name__ == "__main__":
    unittest.main()
