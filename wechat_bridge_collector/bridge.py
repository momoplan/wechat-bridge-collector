from __future__ import annotations

import json
from http.client import HTTPConnection, HTTPException
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .config import ConnectorConfig


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
        "mentionedUserIds": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
            "description": "Individual WeChat usernames from the native group mention list. An empty list means no individual was mentioned; missing means unknown.",
        },
        "selfUserId": {
            "type": "string",
            "minLength": 1,
            "description": "Native username of the local WeChat user, resolved independently of accountId.",
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
    "required": ["protocolVersion", "accountId", "streamId", "syncId", "sequence",
                 "mode", "batchIndex", "batchCount", "observedContactCount", "contacts", "source", "platform"],
    "properties": {
        "protocolVersion": {"type": "string", "enum": ["1.0.0"]},
        "accountId": {"type": "string", "minLength": 1},
        "streamId": {"type": "string", "minLength": 1},
        "syncId": {"type": "string", "minLength": 1},
        "sequence": {"type": "integer", "minimum": 1},
        "mode": {"type": "string", "enum": ["initial", "delta"]},
        "batchIndex": {"type": "integer", "minimum": 0},
        "batchCount": {"type": "integer", "minimum": 1},
        "observedContactCount": {"type": "integer", "minimum": 0},
        "contacts": {
            "type": "array", "maxItems": 200,
            "items": {
                "type": "object",
                "required": ["contactId", "displayName", "nickName", "remark", "sourceDeleted"],
                "properties": {
                    "contactId": {"type": "string", "minLength": 1},
                    "displayName": {"type": "string"},
                    "nickName": {"type": "string"},
                    "remark": {"type": "string"},
                    "sourceDeleted": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        },
        "source": {"type": "string", "enum": ["wechat-local-db"]},
        "platform": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}


@dataclass(frozen=True)
class BridgeClient:
    config: ConnectorConfig

    def submit(self, event: dict) -> str:
        origin = urlsplit(self.config.bridge_events_url)
        if (origin.scheme != "http" or origin.hostname != "127.0.0.1"
                or not origin.port or origin.username or origin.password
                or origin.query or origin.fragment):
            raise ValueError("Bridge Agent 事件入口无效")
        body = {"appId": self.config.app_id, "event": event["event"],
                "eventId": event["eventId"], "payload": event["payload"]}
        if event.get("occurredAt"):
            body["occurredAt"] = event["occurredAt"]
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.config.bridge_event_token}"}
        connection = HTTPConnection(origin.hostname, origin.port, timeout=10)
        try:
            connection.request("POST", origin.path, json.dumps(body), headers)
            response = connection.getresponse()
            if response.status != 202:
                return "rejected"
            receipt = json.loads(response.read(65536))
        except (OSError, HTTPException, ValueError):
            return "rejected"
        finally:
            connection.close()
        if (not isinstance(receipt, dict) or receipt.get("eventId") != event["eventId"]
                or receipt.get("appId") != self.config.app_id):
            return "rejected"
        status = receipt.get("status")
        return status if status in ("queued", "no_subscribers") else "rejected"
