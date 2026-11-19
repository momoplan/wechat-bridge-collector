from __future__ import annotations

import base64
import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .wechat_source import WeChatSource

CURSOR_MAX_LENGTH = 4096


def _cursor(binding: str, session_key: str) -> str:
    payload = json.dumps([binding, session_key], ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _cursor_key(cursor: Any, binding: str) -> str:
    if not isinstance(cursor, str) or not 0 < len(cursor) <= CURSOR_MAX_LENGTH:
        raise ValueError("invalid session directory cursor")
    try:
        payload = base64.b64decode(cursor, altchars=b"-_", validate=True)
        value = json.loads(payload)
    except ValueError as exc:
        raise ValueError("invalid session directory cursor") from exc
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("invalid session directory cursor")
    received_binding, key = value
    if received_binding != binding or not isinstance(key, str) or not key or _cursor(binding, key) != cursor:
        raise ValueError("invalid session directory cursor")
    return key


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
    after = _cursor_key(parameters["cursor"], binding) if "cursor" in parameters else ""
    titles = source.session_directory_titles()
    items = []
    for session_key in sorted(titles):
        title = titles[session_key]
        if session_key <= after or (keyword not in session_key.casefold() and keyword not in (title or "").casefold()):
            continue
        if len(items) == limit:
            return {"items": items, "nextCursor": _cursor(binding, items[-1]["sessionKey"])}
        item = {"sessionKey": session_key}
        if title is not None:
            item["title"] = title
        items.append(item)
    return {"items": items, "nextCursor": None}
