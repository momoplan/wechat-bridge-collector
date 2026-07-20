from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .autostart import start_command, stop_command
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
        "description": "Read paginated message history for one WeChat conversation by conversationId.",
        "path": "/invoke/getChatHistory",
        "httpMethod": "POST",
        "timeoutSecs": 60,
        "input_schema": {
            "type": "object",
            "required": ["conversationId"],
            "properties": {
                "conversationId": {"type": "string", "description": "WeChat conversation username from event payload.conversationId or getRecentSessions.conversationId."},
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
                "conversationId": {"type": "string", "default": "", "description": "Optional WeChat conversation username. Empty searches all conversations."},
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
            "required": ["conversationId"],
            "properties": {
                "conversationId": {"type": "string", "description": "WeChat conversation username from event payload.conversationId or getRecentSessions.conversationId."},
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
            "required": ["conversationId"],
            "properties": {
                "conversationId": {"type": "string", "description": "WeChat conversation username from event payload.conversationId or getRecentSessions.conversationId."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 20},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "startTime": {"type": "string", "default": ""},
                "endTime": {"type": "string", "default": ""},
            },
            "additionalProperties": False,
        },
    },
]

MESSAGE_EVENT_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Payload emitted for each WeChat message observed in the local database.",
    "required": [
        "messageId",
        "dbPath",
        "tableName",
        "localId",
        "conversationId",
        "conversationName",
        "isGroup",
        "senderId",
        "senderName",
        "direction",
        "messageType",
        "messageTypeLabel",
        "timestamp",
        "occurredAt",
        "source",
        "platform",
    ],
    "properties": {
        "messageId": {
            "type": "string",
            "description": "Stable collector message ID formatted as dbPath:tableName:localId.",
        },
        "dbPath": {
            "type": "string",
            "description": "Relative decrypted WeChat database path used by the collector.",
        },
        "tableName": {
            "type": "string",
            "description": "WeChat message table where the record was read.",
        },
        "localId": {
            "type": "integer",
            "description": "Local message primary key in the source table.",
        },
        "conversationId": {
            "type": "string",
            "description": "WeChat conversation username. Use this with getChatHistory/searchMessages.",
        },
        "conversationName": {
            "type": "string",
            "description": "Best-effort display name for the conversation.",
        },
        "isGroup": {
            "type": "boolean",
            "description": "Whether the conversation is a WeChat group chat.",
        },
        "senderId": {
            "type": "string",
            "description": "Sender WeChat username when resolvable; empty for system/unknown senders.",
        },
        "senderName": {
            "type": "string",
            "description": "Best-effort sender display name resolved from local contacts.",
        },
        "direction": {
            "type": "string",
            "description": "Message direction relative to the current local WeChat account. Group-chat direction can be unknown.",
            "enum": ["incoming", "outgoing", "unknown"],
        },
        "messageType": {
            "type": "string",
            "description": "Normalized message type inferred from WeChat local_type.",
            "enum": [
                "text",
                "image",
                "voice",
                "contact_card",
                "video",
                "sticker",
                "location",
                "app",
                "call",
                "system",
                "recall",
                "unknown",
            ],
        },
        "messageTypeLabel": {
            "type": "string",
            "description": "Human-readable label for messageType or the raw local_type fallback.",
        },
        "timestamp": {
            "type": "integer",
            "description": "Message create time as Unix timestamp seconds.",
        },
        "occurredAt": {
            "type": "string",
            "format": "date-time",
            "description": "Message create time in ISO 8601 UTC format.",
        },
        "source": {
            "type": "string",
            "description": "Origin of the event payload.",
            "enum": ["wechat-local-db"],
        },
        "platform": {
            "type": "string",
            "description": "Lowercase platform name reported by the local collector, for example darwin, windows, or linux.",
        },
        "text": {
            "type": "string",
            "description": "Best-effort message text or type placeholder. Present only when includeText is enabled.",
        },
    },
    "additionalProperties": False,
}


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
                    "payload_schema": MESSAGE_EVENT_PAYLOAD_SCHEMA,
                }
            ],
            "replace": True,
            "managed_by": "wechat-bridge-collector",
        }
        registration["startCommand"] = start_command()
        registration["stopCommand"] = stop_command()
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
