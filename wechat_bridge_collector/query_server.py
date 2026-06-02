from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from .config import CollectorConfig
from .wechat_source import WeChatSource, normalize_limit


class QueryMethodServer:
    def __init__(self, config: CollectorConfig, source: WeChatSource):
        self.config = config
        self.source = source
        self._server = ThreadingHTTPServer((config.method_host, int(config.method_port)), self._handler_class())
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._server.serve_forever, name="wechat-query-method-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        source = self.source

        class Handler(BaseHTTPRequestHandler):
            server_version = "WeChatBridgeCollector/0.2"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/health":
                    self._write_json(200, {"ok": True})
                    return
                self._write_json(404, error_response("NOT_FOUND", "unknown path"))

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                if not path.startswith("/invoke/"):
                    self._write_json(404, error_response("NOT_FOUND", "unknown path"))
                    return
                method = unquote(path.removeprefix("/invoke/"))
                try:
                    payload = self._read_json()
                    result = dispatch_method(source, method, payload)
                    self._write_json(200, {"success": True, "data": result, "error": None})
                except ValueError as exc:
                    self._write_json(400, error_response("BAD_REQUEST", str(exc)))
                except Exception as exc:
                    self._write_json(500, error_response("INTERNAL_ERROR", str(exc)))

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                try:
                    value = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    raise ValueError("请求体不是有效 JSON") from exc
                if not isinstance(value, dict):
                    raise ValueError("请求体必须是 JSON object")
                return value

            def _write_json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


def error_response(code: str, message: str) -> dict[str, Any]:
    return {
        "success": False,
        "data": None,
        "error": {
            "code": code,
            "message": message,
        },
    }


def dispatch_method(source: WeChatSource, method: str, payload: dict[str, Any]) -> Any:
    handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
        "getRecentSessions": lambda p: {
            "sessions": source.recent_sessions(limit=p.get("limit", 20)),
            "limit": normalize_limit(p.get("limit", 20), 200),
        },
        "getContacts": lambda p: {
            "contacts": source.contacts(query=str(p.get("query") or ""), limit=p.get("limit", 50)),
            "limit": normalize_limit(p.get("limit", 50), 500),
        },
        "getChatHistory": lambda p: source.get_chat_history(
            require_string(p, "chat"),
            limit=p.get("limit", 50),
            offset=p.get("offset", 0),
            start_time=p.get("startTime") or p.get("start_time") or "",
            end_time=p.get("endTime") or p.get("end_time") or "",
            oldest_first=bool(p.get("oldestFirst", p.get("oldest_first", False))),
            message_types=p.get("messageTypes") or p.get("message_types"),
        ),
        "searchMessages": lambda p: source.search_messages(
            require_string(p, "keyword"),
            chat=str(p.get("chat") or ""),
            limit=p.get("limit", 20),
            offset=p.get("offset", 0),
            start_time=p.get("startTime") or p.get("start_time") or "",
            end_time=p.get("endTime") or p.get("end_time") or "",
        ),
        "getMessageById": lambda p: {"message": source.get_message_by_id(require_string(p, "messageId"))},
        "getChatImages": lambda p: source.get_chat_images(
            require_string(p, "chat"),
            limit=p.get("limit", 20),
            offset=p.get("offset", 0),
            start_time=p.get("startTime") or p.get("start_time") or "",
            end_time=p.get("endTime") or p.get("end_time") or "",
        ),
        "getVoiceMessages": lambda p: source.get_voice_messages(
            require_string(p, "chat"),
            limit=p.get("limit", 20),
            offset=p.get("offset", 0),
            start_time=p.get("startTime") or p.get("start_time") or "",
            end_time=p.get("endTime") or p.get("end_time") or "",
        ),
    }
    handler = handlers.get(method)
    if not handler:
        raise ValueError(f"unknown method: {method}")
    return handler(payload)


def require_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 不能为空")
    return value.strip()
