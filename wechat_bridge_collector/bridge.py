from __future__ import annotations

import json
import platform
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import CollectorConfig


METHOD_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "getRecentSessions",
        "description": "List recent WeChat conversations with latest-message summaries.",
        "path": "/invoke/getRecentSessions",
        "httpMethod": "POST",
        "timeoutSecs": 30,
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "getContacts",
        "description": "Search or list local WeChat contacts and group conversations.",
        "path": "/invoke/getContacts",
        "httpMethod": "POST",
        "timeoutSecs": 30,
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "default": ""},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "getChatHistory",
        "description": "Read paginated message history for one WeChat conversation.",
        "path": "/invoke/getChatHistory",
        "httpMethod": "POST",
        "timeoutSecs": 60,
        "input_schema": {
            "type": "object",
            "required": ["chat"],
            "properties": {
                "chat": {"type": "string", "description": "Conversation name, remark, group name, or wxid."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "startTime": {"type": "string", "default": ""},
                "endTime": {"type": "string", "default": ""},
                "oldestFirst": {"type": "boolean", "default": False},
                "messageTypes": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "searchMessages",
        "description": "Search local WeChat messages by keyword, optionally scoped to one conversation.",
        "path": "/invoke/searchMessages",
        "httpMethod": "POST",
        "timeoutSecs": 90,
        "input_schema": {
            "type": "object",
            "required": ["keyword"],
            "properties": {
                "keyword": {"type": "string"},
                "chat": {"type": "string", "default": ""},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 20},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "startTime": {"type": "string", "default": ""},
                "endTime": {"type": "string", "default": ""},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "getMessageById",
        "description": "Fetch one local WeChat message by collector messageId.",
        "path": "/invoke/getMessageById",
        "httpMethod": "POST",
        "timeoutSecs": 30,
        "input_schema": {
            "type": "object",
            "required": ["messageId"],
            "properties": {"messageId": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "getChatImages",
        "description": "List image messages in one WeChat conversation.",
        "path": "/invoke/getChatImages",
        "httpMethod": "POST",
        "timeoutSecs": 60,
        "input_schema": {
            "type": "object",
            "required": ["chat"],
            "properties": {
                "chat": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 20},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "startTime": {"type": "string", "default": ""},
                "endTime": {"type": "string", "default": ""},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "getVoiceMessages",
        "description": "List voice messages in one WeChat conversation.",
        "path": "/invoke/getVoiceMessages",
        "httpMethod": "POST",
        "timeoutSecs": 60,
        "input_schema": {
            "type": "object",
            "required": ["chat"],
            "properties": {
                "chat": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 20},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "startTime": {"type": "string", "default": ""},
                "endTime": {"type": "string", "default": ""},
            },
            "additionalProperties": False,
        },
    },
]


@dataclass
class BridgeResponse:
    ok: bool
    status: int
    body: str


class BridgeClient:
    def __init__(self, config: CollectorConfig):
        self.config = config

    def _headers(self, token: str | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _post_json(self, url: str, data: dict[str, Any], token: str | None = None) -> BridgeResponse:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=self._headers(token), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                return BridgeResponse(200 <= resp.status < 300, resp.status, text)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            return BridgeResponse(False, exc.code, text)
        except Exception as exc:
            return BridgeResponse(False, 0, str(exc))

    def register_service(self, method_base_url: str | None = None) -> BridgeResponse:
        base_url = method_base_url or self.config.method_base_url
        registration = {
            "name": self.config.service_name,
            "description": "Local WeChat message collector.",
            "transport": {
                "type": "http",
                "baseUrl": base_url,
            },
            "healthCheck": {
                "type": "http",
                "path": "/health",
                "timeoutSecs": 2,
                "expectStatus": 200,
            },
            "methods": METHOD_DECLARATIONS,
            "events": [
                {
                    "name": self.config.event_name,
                    "description": "Emitted when a local WeChat message is observed.",
                    "enabled": True,
                    "payload_schema": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                }
            ],
            "replace": True,
            "managed_by": "wechat-bridge-collector",
        }
        start_command = collector_start_command()
        if start_command:
            registration["startCommand"] = start_command
        return self._post_json(
            self.config.bridge_services_url,
            registration,
            self.config.service_registration_token,
        )

    def emit_message(self, payload: dict[str, Any], event_id: str, occurred_at: str | None) -> BridgeResponse:
        request = {
            "service": self.config.service_name,
            "event": self.config.event_name,
            "eventId": event_id,
            "payload": payload,
        }
        if occurred_at:
            request["occurredAt"] = occurred_at
        return self._post_json(
            self.config.bridge_events_url,
            request,
            self.config.bridge_event_token,
        )


def collector_start_command() -> dict[str, Any] | None:
    if platform.system().lower() != "darwin":
        return None
    return {
        "type": "shell_command",
        "command": [
            "/bin/sh",
            "-lc",
            "launchctl bootstrap gui/$(id -u) \"$HOME/Library/LaunchAgents/com.baijimu.wechat-bridge-collector.plist\" 2>/dev/null || true; "
            "launchctl kickstart -k gui/$(id -u)/com.baijimu.wechat-bridge-collector",
        ],
        "timeoutSecs": 15,
    }
