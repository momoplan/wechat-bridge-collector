from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .bridge import BridgeClient
from .collector_client import CollectorClient, CollectorRejected, CollectorUnavailable
from .config import ConnectorConfig
from .selected_upload_session import SelectedUploads


COMPLETED_DELIVERY = {"queued", "skipped_no_subscribers"}
SHUTDOWN_CONNECT_TIMEOUT = 5
SHUTDOWN_RESPONSE_TIMEOUT = 2


class Connector:
    def __init__(self, config: ConnectorConfig):
        self.config = config
        self.settings = config.data_dir / "collector-connection.json"
        self.stop = threading.Event()
        self.pair_lock = threading.Lock()
        self.relay_status = {}
        self.bridge = BridgeClient(config)
        self.uploads = SelectedUploads(lambda method, args: self.client().query(method, args))
        self.events = tuple(event["name"] for event in config.manifest["events"])

    def client(self) -> CollectorClient:
        try:
            connection = json.loads(self.settings.read_text(encoding="utf-8"))
            return CollectorClient(connection["address"], connection["token"])
        except FileNotFoundError as exc:
            raise CollectorUnavailable("请先连接独立微信采集器") from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CollectorUnavailable("采集器配对配置无效，请重新配对") from exc

    def pair(self, payload: dict) -> dict:
        client = CollectorClient(payload.get("address"), secrets.token_urlsafe(32))
        code = payload.get("code")
        with self.pair_lock:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.settings.parent, delete=False
                ) as file:
                    temporary = Path(file.name)
                    json.dump({"address": client.address, "token": client.token}, file)
                    file.flush()
                    os.fsync(file.fileno())
                status = client.pair(code)
                temporary.replace(self.settings)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return {"sourceAccess": status}

    def status(self) -> dict:
        try:
            source = self.client().status()
        except CollectorUnavailable as exc:
            source = {"status": "unavailable", "detail": str(exc)}
        return {"product": self.config.manifest["name"], "version": self.config.manifest["version"],
                "sourceAccess": source, "delivery": dict(self.relay_status)}

    def forward(self, event_name: str) -> None:
        while not self.stop.is_set():
            try:
                self.relay_status[event_name] = self.forward_one(event_name)
            except (CollectorUnavailable, CollectorRejected, OSError, ValueError):
                self.relay_status[event_name] = "unavailable"
            if self.relay_status[event_name] not in COMPLETED_DELIVERY:
                self.stop.wait(1)

    def forward_one(self, event_name: str) -> str:
        collector = self.client()
        event = collector.request("/v1/events/next", {"event": event_name}).get("data")
        if event is None:
            return "idle"
        if (not isinstance(event, dict) or event.get("event") != event_name
                or not isinstance(event.get("eventId"), str) or not event["eventId"]
                or not isinstance(event.get("payload"), dict)):
            return "invalid_event"
        result = self.bridge.submit(event)
        if result == "no_subscribers" and event_name == "messageReceived":
            result = "skipped_no_subscribers"
        if result in COMPLETED_DELIVERY:
            collector.request("/v1/events/ack", {"event": event_name, "eventId": event["eventId"]})
        return result


def create_server(connector: Connector, port: int) -> ThreadingHTTPServer:
    config = connector.config
    operations = {operation["path"]: name for name, operation in
                  config.manifest["management"]["operations"].items()}
    methods = {item["name"] for item in config.manifest["methods"]}
    expected_authorization = f"Bearer {config.management_token}".encode()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(12)

        def log_message(self, _format, *_args):
            return

        def reply(self, status, body):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            if hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected_authorization):
                return True
            self.reply(401, {"errorCode": "UNAUTHORIZED", "value": "本地应用鉴权失败"})
            return False

        def do_GET(self):
            if self.path == "/health":
                self.reply(200, {"ok": True})
            elif self.authorized():
                if operations.get(self.path) == "runtimeState":
                    self.reply(200, {"ok": True, "data": connector.status()})
                else:
                    self.reply(404, {"errorCode": "NOT_FOUND", "value": "未知路径"})

        def do_POST(self):
            if not self.authorized():
                return
            managed = self.path.startswith("/management/")
            operation = operations.get(self.path) if managed else self.path.removeprefix("/invoke/")
            try:
                if self.path == "/management/v1/shutdown":
                    self.reply(200, {"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 256 * 1024:
                    raise ValueError("请求大小无效")
                payload = json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(payload, dict):
                    raise ValueError("请求必须是 JSON 对象")
                if managed and operation == "pairCollector":
                    result = connector.pair(payload)
                elif managed and operation == "selectedAttachment":
                    result = (connector.uploads.begin(payload) if payload.get("action") == "begin"
                              else connector.uploads.update(payload))
                elif operation in methods and (managed or self.path.startswith("/invoke/")):
                    result = connector.client().query(operation, payload)
                else:
                    self.failure(managed, 404, "NOT_FOUND", "未知方法")
                    return
                response = {"ok": True, "data": result} if managed else {
                    "errorCode": "0", "value": "成功", "data": result,
                    "systemCurrentTime": time.time_ns() // 1_000_000,
                }
                self.reply(200, response)
            except CollectorUnavailable as exc:
                self.failure(managed, 503, "SOURCE_NOT_READY", str(exc))
            except ValueError as exc:
                self.failure(managed, 400, "BAD_REQUEST", str(exc))
            except OSError:
                self.close_connection = True

        def failure(self, managed, status, code, message):
            response = {"ok": False, "error": {"code": code, "message": message}} if managed else {
                "errorCode": code, "value": message, "data": None,
                "systemCurrentTime": time.time_ns() // 1_000_000,
            }
            self.reply(status, response)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> int:
    parser = argparse.ArgumentParser(description="百积木桌面微信连接器")
    parser.add_argument("action", choices=("run", "stop"))
    args = parser.parse_args()
    config = ConnectorConfig.load()
    if args.action == "stop":
        connection = HTTPConnection("127.0.0.1", config.port, timeout=SHUTDOWN_CONNECT_TIMEOUT)
        try:
            # Windows can take just over two seconds to report that a loopback port is closed.
            try:
                connection.connect()
            except ConnectionRefusedError:
                return 0
            connection.sock.settimeout(SHUTDOWN_RESPONSE_TIMEOUT)
            connection.request("POST", "/management/v1/shutdown",
                               headers={"Authorization": f"Bearer {config.management_token}"})
            return 0 if connection.getresponse().status == 200 else 1
        finally:
            connection.close()
    os.umask(0o077)
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    connector = Connector(config)
    with create_server(connector, config.port) as server:
        workers = [threading.Thread(target=connector.forward, args=(event,), daemon=True)
                   for event in connector.events]
        try:
            for worker in workers:
                worker.start()
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            connector.stop.set()
    return 0
