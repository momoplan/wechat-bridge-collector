from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from . import __version__
from .config import CollectorConfig
from .source_runtime import SourceNotReady, SourceRuntime
from .wechat_source import WeChatSource, normalize_limit


MANAGEMENT_PATHS = {
    "/management/v1/recent-sessions": "getRecentSessions",
    "/management/v1/contacts": "getContacts",
    "/management/v1/chat-history": "getChatHistory",
    "/management/v1/search": "searchMessages",
    "/management/v1/message": "getMessageById",
}

SETUP_PATHS = {
    "/management/v1/acquire-keys": "acquire",
    "/management/v1/import-keys": "import",
    "/management/v1/retry-setup": "retry",
}


class SourceAccessState:
    def __init__(self, ready: bool = False):
        self._lock = threading.Lock()
        self._status = "ready" if ready else "checking"
        self._detail = ""
        self._checked_at = int(time.time() * 1000)

    def mark_checking(self) -> None:
        self._update("checking", "")

    def mark_ready(self) -> None:
        self._update("ready", "")

    def mark_error(self, detail: str) -> None:
        self._update("error", detail)

    def require_ready(self) -> None:
        snapshot = self.snapshot()
        if snapshot["status"] == "ready":
            return
        detail = snapshot["detail"] or "正在等待微信数据库访问权限"
        raise SourceAccessUnavailable(detail)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self._status,
                "detail": self._detail,
                "checkedAtEpochMs": self._checked_at,
            }

    def _update(self, status: str, detail: str) -> None:
        with self._lock:
            self._status = status
            self._detail = detail
            self._checked_at = int(time.time() * 1000)


class SourceAccessUnavailable(RuntimeError):
    pass


class QueryMethodServer:
    def __init__(
        self,
        config: CollectorConfig,
        source: WeChatSource | None = None,
        management_token: str | None = None,
        access_state: SourceAccessState | None = None,
        source_runtime: SourceRuntime | None = None,
    ):
        self.config = config
        self.source = source
        self.source_runtime = source_runtime
        if self.source_runtime is None and source is None:
            raise ValueError("source or source_runtime is required")
        self.management_token = management_token or load_or_create_management_token()
        self.access_state = access_state or SourceAccessState(ready=True)
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
        config = self.config
        source = self.source
        source_runtime = self.source_runtime
        management_token = self.management_token
        access_state = self.access_state

        class Handler(BaseHTTPRequestHandler):
            server_version = f"WeChatBridgeCollector/{__version__}"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/health":
                    self._write_json(200, {"ok": True})
                    return
                if path == "/management/v1/state":
                    if not self._management_authorized(management_token):
                        self._write_json(401, management_error("UNAUTHORIZED", "management authorization required"))
                        return
                    try:
                        self._write_json(
                            200,
                            {"ok": True, "data": runtime_state(config, source, access_state, source_runtime)},
                        )
                    except Exception as exc:
                        self._write_json(500, management_error("INTERNAL_ERROR", str(exc)))
                    return
                self._write_json(404, error_response("NOT_FOUND", "unknown path"))

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                if path.startswith("/management/"):
                    if not self._management_authorized(management_token):
                        self._write_json(401, management_error("UNAUTHORIZED", "management authorization required"))
                        return
                    method = MANAGEMENT_PATHS.get(path)
                    setup_method = SETUP_PATHS.get(path)
                    if setup_method:
                        try:
                            payload = self._read_json()
                            result = dispatch_setup(source_runtime, setup_method, payload)
                            self._write_json(202 if setup_method != "import" else 200, {"ok": True, "data": result})
                        except ValueError as exc:
                            self._write_json(400, management_error("BAD_REQUEST", str(exc)))
                        except Exception as exc:
                            self._write_json(500, management_error("INTERNAL_ERROR", str(exc)))
                        return
                    if not method:
                        self._write_json(404, management_error("NOT_FOUND", "unknown management path"))
                        return
                    try:
                        payload = self._read_json()
                        current_source = require_source(source, source_runtime, access_state)
                        result = dispatch_method(current_source, method, payload)
                        self._write_json(200, {"ok": True, "data": result})
                    except SourceAccessUnavailable as exc:
                        self._write_json(
                            503,
                            management_error("SOURCE_NOT_READY", str(exc)),
                        )
                    except ValueError as exc:
                        self._write_json(400, management_error("BAD_REQUEST", str(exc)))
                    except Exception as exc:
                        self._write_json(500, management_error("INTERNAL_ERROR", str(exc)))
                    return
                if not path.startswith("/invoke/"):
                    self._write_json(404, error_response("NOT_FOUND", "unknown path"))
                    return
                method = unquote(path.removeprefix("/invoke/"))
                try:
                    payload = self._read_json()
                    current_source = require_source(source, source_runtime, access_state)
                    result = dispatch_method(current_source, method, payload)
                    self._write_json(200, {"success": True, "data": result, "error": None})
                except SourceAccessUnavailable as exc:
                    self._write_json(
                        503,
                        error_response("SOURCE_NOT_READY", str(exc)),
                    )
                except ValueError as exc:
                    self._write_json(400, error_response("BAD_REQUEST", str(exc)))
                except Exception as exc:
                    self._write_json(500, error_response("INTERNAL_ERROR", str(exc)))

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return {}
                if length > 256 * 1024:
                    raise ValueError("请求体不能超过 256KB")
                raw = self.rfile.read(length)
                try:
                    value = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    raise ValueError("请求体不是有效 JSON") from exc
                if not isinstance(value, dict):
                    raise ValueError("请求体必须是 JSON object")
                return value

            def _management_authorized(self, expected: str) -> bool:
                authorization = self.headers.get("Authorization") or ""
                prefix = "Bearer "
                provided = authorization[len(prefix) :] if authorization.startswith(prefix) else ""
                return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))

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


def management_error(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
        },
    }


def load_or_create_management_token() -> str:
    data_dir_value = os.environ.get("BAIJIMU_CONNECTOR_DATA_DIR", "").strip()
    if not data_dir_value:
        return secrets.token_hex(32)
    data_dir = Path(data_dir_value).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        data_dir.chmod(0o700)
    path = data_dir / "management-token"
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if len(token) >= 32:
        if os.name != "nt":
            path.chmod(0o600)
        return token
    token = secrets.token_hex(32)
    temporary = path.with_name(f".management-token.{os.getpid()}.tmp")
    temporary.write_text(token + "\n", encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o600)
    os.replace(temporary, path)
    if os.name != "nt":
        path.chmod(0o600)
    return token


def runtime_state(
    config: CollectorConfig,
    source: WeChatSource | None,
    access_state: SourceAccessState,
    source_runtime: SourceRuntime | None = None,
) -> dict[str, Any]:
    if source_runtime is not None:
        source_access = source_runtime.snapshot()
        probe = source_runtime.probe_summary()
    else:
        assert source is not None
        source_access = access_state.snapshot()
        probe = {
            "db_dir": source.db_dir,
            "keys_file": source.runtime["keys_file"],
            "key_count": len(source.all_keys),
            "message_db_count": len(source.msg_db_keys),
        }
    return {
        "product": "微信",
        "version": __version__,
        "serviceName": config.service_name,
        "includeText": config.include_text,
        "includeOutgoing": config.include_outgoing,
        "sourceAccess": source_access,
        "setup": source_access,
        "probe": probe,
    }


def require_source(
    source: WeChatSource | None,
    source_runtime: SourceRuntime | None,
    access_state: SourceAccessState,
) -> WeChatSource:
    if source_runtime is not None:
        try:
            return source_runtime.require_source()
        except SourceNotReady as exc:
            raise SourceAccessUnavailable(str(exc)) from exc
    access_state.require_ready()
    assert source is not None
    return source


def dispatch_setup(
    source_runtime: SourceRuntime | None,
    method: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if source_runtime is None:
        raise ValueError("当前运行模式不支持密钥设置")
    if method == "acquire":
        return source_runtime.acquire_keys()
    if method == "import":
        return source_runtime.import_keys(payload)
    if method == "retry":
        return source_runtime.retry_setup()
    raise ValueError("unknown setup method")


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
            require_string(p, "conversationId"),
            limit=p.get("limit", 50),
            offset=p.get("offset", 0),
            start_time=p.get("startTime") or p.get("start_time") or "",
            end_time=p.get("endTime") or p.get("end_time") or "",
            oldest_first=bool(p.get("oldestFirst", p.get("oldest_first", False))),
            message_types=p.get("messageTypes") or p.get("message_types"),
        ),
        "searchMessages": lambda p: source.search_messages(
            require_string(p, "keyword"),
            conversation_id=str(p.get("conversationId") or ""),
            limit=p.get("limit", 20),
            offset=p.get("offset", 0),
            start_time=p.get("startTime") or p.get("start_time") or "",
            end_time=p.get("endTime") or p.get("end_time") or "",
        ),
        "getMessageById": lambda p: {"message": source.get_message_by_id(require_string(p, "messageId"))},
        "getChatImages": lambda p: source.get_chat_images(
            require_string(p, "conversationId"),
            limit=p.get("limit", 20),
            offset=p.get("offset", 0),
            start_time=p.get("startTime") or p.get("start_time") or "",
            end_time=p.get("endTime") or p.get("end_time") or "",
        ),
        "getVoiceMessages": lambda p: source.get_voice_messages(
            require_string(p, "conversationId"),
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
