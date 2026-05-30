from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import CollectorConfig


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

    def register_service(self) -> BridgeResponse:
        registration = {
            "name": self.config.service_name,
            "description": "Local WeChat message collector.",
            "transport": {
                "type": "http",
                "baseUrl": "http://127.0.0.1:0",
            },
            "methods": [],
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

