from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from itertools import islice
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .wechat_source import WeChatSource

CURSOR_MAX_LENGTH = 4096


@dataclass(frozen=True)
class SessionDirectoryEntry:
    session_key: str
    last_activity: int
    title: str | None

    @property
    def order(self) -> tuple[int, str]:
        return -self.last_activity, self.session_key

    def as_item(self) -> dict[str, str]:
        item = {"sessionKey": self.session_key}
        if self.title is not None:
            item["title"] = self.title
        return item


def _cursor(binding: str, order: tuple[int, str]) -> str:
    reverse_timestamp, session_key = order
    payload = json.dumps(
        [binding, -reverse_timestamp, session_key], ensure_ascii=False, separators=(",", ":")
    )
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _cursor_order(cursor: Any, binding: str) -> tuple[int, str]:
    if not isinstance(cursor, str) or not 0 < len(cursor) <= CURSOR_MAX_LENGTH:
        raise ValueError("invalid session directory cursor")
    try:
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
    except ValueError as exc:
        raise ValueError("invalid session directory cursor") from exc
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("invalid session directory cursor; reload the first page")
    received_binding, timestamp, key = value
    if received_binding != binding or type(timestamp) is not int or timestamp < 0 or not isinstance(key, str) or not key:
        raise ValueError("invalid session directory cursor")
    order = -timestamp, key
    if _cursor(binding, order) != cursor:
        raise ValueError("invalid session directory cursor")
    return order


def list_session_directory(source: WeChatSource, parameters: dict[str, Any]) -> dict[str, Any]:
    if parameters.keys() - {"keyword", "cursor", "limit"}:
        raise ValueError("unknown session directory parameter")
    limit = parameters.get("limit")
    keyword = parameters.get("keyword", "")
    if type(limit) is not int or not 1 <= limit <= 200 or not isinstance(keyword, str):
        raise ValueError("invalid session directory parameters")

    keyword = keyword.casefold()
    scope = json.dumps([source.account_profile()["accountId"], keyword], ensure_ascii=False)
    binding = hashlib.sha256(scope.encode()).hexdigest()
    after = _cursor_order(parameters["cursor"], binding) if "cursor" in parameters else None
    entries = sorted(source.session_directory_entries(), key=lambda entry: entry.order)
    matches = (
        entry for entry in entries
        if (after is None or entry.order > after)
        and (keyword in entry.session_key.casefold() or keyword in (entry.title or "").casefold())
    )
    page = list(islice(matches, limit + 1))
    return {
        "items": [entry.as_item() for entry in page[:limit]],
        "nextCursor": _cursor(binding, page[limit - 1].order) if len(page) > limit else None,
    }
