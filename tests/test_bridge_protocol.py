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
SOURCE_TOKEN = "A" * 43


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


def pairing_file(tmp_path, port):
    path = tmp_path / "pairing.json"
    path.write_text(json.dumps({"protocolVersion": 1, "port": port, "token": SOURCE_TOKEN}))
    return path


def test_pair_and_proxy_preserve_query_contract(tmp_path):
    requests = []

    def source(path, headers, body):
        assert headers["Authorization"] == f"Bearer {SOURCE_TOKEN}"
        requests.append((path, body))
        if path == "/v1/status":
            return 200, {"sourceAccess": {"status": "ready"}}
        return 200, {"data": {"account": {"accountId": "sample"}}}

    connector = make_connector(tmp_path, 1)
    with service(handler_for(source)) as port, create_server(connector, 0) as server:
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            target = server.server_port
            path = pairing_file(tmp_path, port)
            assert call(target, "/invoke/getAccountProfile", {})[0] == 503
            assert call(target, "/management/v1/pair-collector", {"pairingFile": str(path)}, "wrong")[0] == 401
            assert call(target, "/management/v1/pair-collector", {"pairingFile": str(path)})[0] == 200
            assert call(target, "/invoke/getAccountProfile", {}, "wrong")[0] == 401
            status, result = call(target, "/invoke/getAccountProfile", {})
            assert status == 200
            assert result["errorCode"] == "0"
            assert result["data"] == {"account": {"accountId": "sample"}}
            assert requests[-1] == ("/v1/query/getAccountProfile", {})
            assert call(target, "/management/v1/import-keys", {})[0] == 404
            assert SOURCE_TOKEN not in connector.settings.read_text()
        finally:
            server.shutdown()
            worker.join()


@pytest.mark.parametrize("bridge_reply", ["no_subscribers", "queued", "wrong_receipt", "failure"])
def test_confirmation_requires_matching_queued_receipt(tmp_path, bridge_reply):
    acknowledged = []

    def source(path, headers, body):
        assert headers["Authorization"] == f"Bearer {SOURCE_TOKEN}"
        if path == "/v1/status":
            return 200, {"sourceAccess": {"status": "ready"}}
        if path == "/v1/events/next":
            return 200, {"data": EVENT}
        acknowledged.append(body)
        return 200, {"data": None}

    def bridge(path, headers, body):
        assert path == "/v1/local-app-events"
        assert headers["Authorization"] == "Bearer event-token"
        assert body == {**EVENT, "appId": "registered-app-id"}
        if bridge_reply == "failure":
            return 503, {}
        event_id = "wrong" if bridge_reply == "wrong_receipt" else EVENT["eventId"]
        return 202, {"status": bridge_reply, "eventId": event_id, "appId": body["appId"]}

    with service(handler_for(source)) as source_port, service(handler_for(bridge)) as bridge_port:
        connector = make_connector(tmp_path, bridge_port)
        connector.pair({"pairingFile": str(pairing_file(tmp_path, source_port))})
        status = connector.forward_one(MESSAGE)
        if bridge_reply == "queued":
            assert acknowledged == [{"event": MESSAGE, "eventId": EVENT["eventId"]}]
            assert status == "queued"
        else:
            assert acknowledged == []
            assert status == ("no_subscribers" if bridge_reply == "no_subscribers" else "rejected")
