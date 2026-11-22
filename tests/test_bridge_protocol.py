import http.client
import json
import threading
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from wechat_bridge_collector.app import Connector, create_server
from wechat_bridge_collector.config import ConnectorConfig

MANIFEST_PATH = Path(__file__).resolve().parents[1] / "connector.json"
MESSAGE = "messageReceived"
EVENT = {"event": MESSAGE, "eventId": "event-1", "payload": {"messageId": "message-1"}}
PAIR_CODE = "C" * 12


@contextmanager
def service(handler):
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            worker.join()


def handler_for(callback):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.process(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

        def do_GET(self):
            self.process(None)

        def process(self, body):
            status, data = callback(self.path, self.headers, body)
            self.send_response(status)
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

    return Handler


def call(port, path, body=None, token="host-token"):
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5)) as client:
        client.request("GET" if body is None else "POST", path,
                       None if body is None else json.dumps(body),
                       {"Authorization": f"Bearer {token}"})
        response = client.getresponse()
        return response.status, json.loads(response.read())


def make_connector(tmp_path, bridge_port):
    manifest = json.loads(MANIFEST_PATH.read_text())
    return Connector(ConnectorConfig(manifest, tmp_path, "host-token", "event-token",
                                     f"http://127.0.0.1:{bridge_port}/v1/local-app-events",
                                     "registered-app-id"))


def pairing_request(port):
    return {"address": f"http://127.0.0.1:{port}", "code": PAIR_CODE}


def pairable(callback):
    token = None

    def handle(path, headers, body):
        nonlocal token
        if path == "/v1/pair":
            assert "Authorization" not in headers
            if body["code"] != PAIR_CODE:
                return 401, {"code": "PAIRING_DENIED"}
            token = body["token"]
            assert len(token) == 43
            return 200, {"protocolVersion": 1}
        assert headers["Authorization"] == f"Bearer {token}"
        return callback(path, headers, body)

    return handler_for(handle)


def test_pair_and_proxy_preserve_query_contract(tmp_path):
    requests = []

    def source(path, headers, body):
        requests.append((path, body))
        if path == "/v1/status":
            return 200, {"sourceAccess": {"status": "ready"}}
        return 200, {"data": {"account": {"accountId": "sample"}}}

    connector = make_connector(tmp_path, 1)
    with service(pairable(source)) as port, create_server(connector, 0) as server:
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            target = server.server_port
            pairing = pairing_request(port)
            assert call(target, "/invoke/getAccountProfile", {})[0] == 503
            assert call(target, "/management/v1/pair-collector", pairing, "wrong")[0] == 401
            assert call(target, "/management/v1/pair-collector", {**pairing, "code": "Z" * 12})[0] == 400
            assert not connector.settings.exists()
            assert call(target, "/management/v1/pair-collector", pairing)[0] == 200
            assert call(target, "/invoke/getAccountProfile", {}, "wrong")[0] == 401
            status, result = call(target, "/invoke/getAccountProfile", {})
            assert status == 200
            assert result["errorCode"] == "0"
            assert result["data"] == {"account": {"accountId": "sample"}}
            assert requests[-1] == ("/v1/query/getAccountProfile", {})
            assert call(target, "/management/v1/import-keys", {})[0] == 404
            settings = json.loads(connector.settings.read_text())
            assert settings["address"] == pairing["address"]
            assert set(settings) == {"address", "token"}
            assert PAIR_CODE not in connector.settings.read_text()
            assert make_connector(tmp_path, 1).client().status()["status"] == "ready"
        finally:
            server.shutdown()
            worker.join()


@pytest.mark.parametrize("event_name", [MESSAGE, "contactsChanged"])
@pytest.mark.parametrize("bridge_reply", ["no_subscribers", "queued", "wrong_receipt", "failure", "timeout"])
def test_event_confirmation_matches_the_delivery_result(tmp_path, monkeypatch, event_name, bridge_reply):
    event = {**EVENT, "event": event_name}
    acknowledged = []
    submitted = []

    def source(path, headers, body):
        if path == "/v1/status":
            return 200, {"sourceAccess": {"status": "ready"}}
        if path == "/v1/events/next":
            return 200, {"data": event}
        acknowledged.append(body)
        return 200, {"data": None}

    def record_submission(path, headers, body):
        assert path == "/v1/local-app-events"
        assert headers["Authorization"] == "Bearer event-token"
        assert body == {**event, "appId": "registered-app-id"}
        submitted.append(body["eventId"])

    def bridge(path, headers, body):
        record_submission(path, headers, body)
        if bridge_reply == "failure":
            return 503, {}
        if bridge_reply == "wrong_receipt":
            return 202, {"status": "no_subscribers", "eventId": "wrong", "appId": body["appId"]}
        return 202, {"status": bridge_reply, "eventId": event["eventId"], "appId": body["appId"]}

    if bridge_reply == "timeout":
        class TimedOutConnection:
            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, method, path, body, headers):
                assert method == "POST"
                record_submission(path, headers, json.loads(body))

            def getresponse(self):
                raise TimeoutError

            def close(self):
                pass

        monkeypatch.setattr("wechat_bridge_collector.bridge.HTTPConnection", TimedOutConnection)

    with service(pairable(source)) as source_port, service(handler_for(bridge)) as bridge_port:
        connector = make_connector(tmp_path, bridge_port)
        connector.pair(pairing_request(source_port))
        result = connector.forward_one(event_name)

        should_confirm = bridge_reply == "queued" or (bridge_reply == "no_subscribers" and event_name == MESSAGE)
        if should_confirm:
            assert acknowledged == [{"event": event_name, "eventId": event["eventId"]}]
            assert result == ("queued" if bridge_reply == "queued" else "skipped_no_subscribers")
            assert submitted == [event["eventId"]]
        else:
            assert result == ("no_subscribers" if bridge_reply == "no_subscribers" else "rejected")
            assert connector.forward_one(event_name) == result
            assert acknowledged == []
            assert submitted == [event["eventId"], event["eventId"]]


@pytest.mark.parametrize("address", [
    "http://192.0.2.1:12345", "http://localhost.example:12345",
    "http://127.0.0.1:12345@192.0.2.1:12345", "http://127.0.0.1:12345/path",
    "https://127.0.0.1:12345",
])
def test_pair_rejects_non_loopback_origins_and_non_origin_addresses(tmp_path, address):
    connector = make_connector(tmp_path, 1)
    with pytest.raises(ValueError, match="本机 HTTP 地址"):
        connector.pair({"address": address, "code": PAIR_CODE})
    assert not connector.settings.exists()
