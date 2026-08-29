from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import CollectorConfig


METHOD_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "getAccountProfile",
        "description": "Return the stable local WeChat account identity currently backed by this collector.",
        "path": "/invoke/getAccountProfile",
        "httpMethod": "POST",
        "timeoutSecs": 30,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "getContactSnapshot",
        "description": "Read a paginated, checkpointed snapshot of direct WeChat contacts; groups are excluded by default.",
        "path": "/invoke/getContactSnapshot",
        "httpMethod": "POST",
        "timeoutSecs": 30,
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 500},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "includeGroups": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "getRecentSessions",
        "description": "List recent WeChat conversations with latest-message summaries and whether each conversation has readable history.",
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
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "includeGroups": {"type": "boolean", "default": True},
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
                "startTime": {"type": "integer", "description": "Inclusive Unix epoch milliseconds."},
                "endTime": {"type": "integer", "description": "Inclusive Unix epoch milliseconds."},
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
                "startTime": {"type": "integer", "description": "Inclusive Unix epoch milliseconds."},
                "endTime": {"type": "integer", "description": "Inclusive Unix epoch milliseconds."},
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
                "startTime": {"type": "integer", "description": "Inclusive Unix epoch milliseconds."},
                "endTime": {"type": "integer", "description": "Inclusive Unix epoch milliseconds."},
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
                "startTime": {"type": "integer", "description": "Inclusive Unix epoch milliseconds."},
                "endTime": {"type": "integer", "description": "Inclusive Unix epoch milliseconds."},
            },
            "additionalProperties": False,
        },
    },
]

MESSAGE_EVENT_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Payload emitted for each WeChat message observed in the local database.",
    "required": [
        "accountId",
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
        "source",
        "platform",
    ],
    "properties": {
        "accountId": {
            "type": "string",
            "description": "Current local WeChat account ID owning the work device event.",
        },
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
            "description": "Message create time as Unix epoch milliseconds.",
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

CONTACT_EVENT_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["accountId", "snapshotToken", "phase", "source", "platform"],
    "properties": {
        "accountId": {"type": "string"},
        "snapshotToken": {"type": "string"},
        "phase": {"type": "string", "enum": ["started", "contact", "completed"]},
        "contactId": {"type": "string"},
        "displayName": {"type": "string"},
        "nickName": {"type": "string"},
        "remark": {"type": "string"},
        "contactCount": {"type": "integer"},
        "source": {"type": "string", "enum": ["wechat-local-db"]},
        "platform": {"type": "string"},
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

    def emit_message(self, payload: dict[str, Any], event_id: str, occurred_at: str | None) -> BridgeResponse:
        return self.emit_event(self.config.event_name, payload, event_id, occurred_at)

    def emit_event(
        self,
        event_name: str,
        payload: dict[str, Any],
        event_id: str,
        occurred_at: str | None = None,
    ) -> BridgeResponse:
        request = {
            "appId": self.config.app_id,
            "event": event_name,
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
