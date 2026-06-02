from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import re
import sqlite3
import struct
import sys
import tempfile
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import zstandard as zstd
from Crypto.Cipher import AES

from .config import CollectorConfig
from .state import CollectorState, Cursor


PAGE_SZ = 4096
SALT_SZ = 16
IV_SZ = 16
RESERVE_SZ = 80
SQLITE_HDR = b"SQLite format 3\x00"
WAL_HEADER_SZ = 32
WAL_FRAME_HEADER_SZ = 24
MSG_TABLE_RE = re.compile(r"^Msg_[0-9a-f]{32}$")
_ZSTD = zstd.ZstdDecompressor()
_XML_PARSE_MAX_LEN = 200_000
_XML_UNSAFE_RE = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)

TYPE_LABELS = {
    1: ("text", "文本"),
    3: ("image", "图片"),
    34: ("voice", "语音"),
    42: ("contact_card", "名片"),
    43: ("video", "视频"),
    47: ("sticker", "表情"),
    48: ("location", "位置"),
    49: ("app", "链接/文件"),
    50: ("call", "通话"),
    10000: ("system", "系统"),
    10002: ("recall", "撤回"),
}
TYPE_NAME_TO_CODES = {
    "text": {1},
    "image": {3},
    "voice": {34},
    "video": {43},
    "sticker": {47},
    "emoji": {47},
    "location": {48},
    "app": {49},
    "file": {49},
    "contact_card": {42},
    "namecard": {42},
    "call": {50},
    "system": {10000},
    "recall": {10002},
}


@dataclass
class MessageCandidate:
    event_id: str
    payload: dict[str, Any]
    occurred_at: str
    cursor_key: str
    cursor: Cursor


class DBCache:
    def __init__(self, keys: dict[str, Any], db_dir: str):
        self.keys = keys
        self.db_dir = db_dir
        self.cache_dir = Path(tempfile.gettempdir()) / "wechat_bridge_collector_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, tuple[float, float, str]] = {}

    def get(self, rel_key: str) -> str | None:
        key_info = self._get_key_info(rel_key)
        if not key_info:
            return None
        db_path = Path(self.db_dir) / rel_key.replace("\\", os.sep).replace("/", os.sep)
        wal_path = Path(str(db_path) + "-wal")
        if not db_path.exists():
            return None
        try:
            db_mt = db_path.stat().st_mtime
            wal_mt = wal_path.stat().st_mtime if wal_path.exists() else 0
        except OSError:
            return None
        cached = self._cache.get(rel_key)
        if cached and cached[0] == db_mt and cached[1] == wal_mt and Path(cached[2]).exists():
            return cached[2]

        out_path = str(self.cache_dir / (hashlib.md5(rel_key.encode()).hexdigest()[:16] + ".db"))
        enc_key = bytes.fromhex(key_info["enc_key"])
        full_decrypt(str(db_path), out_path, enc_key)
        if wal_path.exists():
            decrypt_wal(str(wal_path), out_path, enc_key)
        self._cache[rel_key] = (db_mt, wal_mt, out_path)
        return out_path

    def _get_key_info(self, rel_path: str) -> dict[str, Any] | None:
        normalized = rel_path.replace("\\", "/")
        variants = [
            rel_path,
            normalized,
            normalized.replace("/", "\\"),
            normalized.replace("/", os.sep),
        ]
        for candidate in variants:
            value = self.keys.get(candidate)
            if isinstance(value, dict) and "enc_key" in value:
                return value
        return None


class WeChatSource:
    """wechat-decrypt-backed source.

    `ylytdeng/wechat-decrypt` is a script repository, not an importable Python
    package. The collector therefore depends on a local clone and loads its
    `key_utils.py` helpers plus the `config.json/all_keys.json` files it
    produces.
    """

    def __init__(self, config: CollectorConfig):
        self.config = config
        self.runtime = config.load_wechat_decrypt_runtime()
        self.wechat_decrypt_dir = self.runtime["wechat_decrypt_dir"]
        self.key_utils = _load_module_from_file(
            "wechat_decrypt_key_utils",
            Path(self.wechat_decrypt_dir) / "key_utils.py",
        )
        keys_file = self.runtime["keys_file"]
        if not Path(keys_file).exists():
            raise RuntimeError(
                f"wechat-decrypt keys file does not exist: {keys_file}. "
                "Run wechat-decrypt key extraction first."
            )
        with open(keys_file, encoding="utf-8") as f:
            raw_keys = json.load(f)
        self.all_keys = self.key_utils.strip_key_metadata(raw_keys)
        self.db_dir = self.runtime["db_dir"]
        self.decrypted_dir = self.runtime["decrypted_dir"]
        self.cache = DBCache(self.all_keys, self.db_dir)
        self.msg_db_keys = find_msg_db_keys(self.all_keys)

    def probe(self) -> dict[str, Any]:
        names = self.contact_names()
        session_state = self.read_session_state()
        msg_tables = 0
        for rel_key in self.msg_db_keys:
            path = self.cache.get(rel_key)
            if not path:
                continue
            with closing(sqlite3.connect(path)) as conn:
                msg_tables += conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                ).fetchone()[0]
        return {
            "wechat_decrypt_dir": self.wechat_decrypt_dir,
            "db_dir": self.db_dir,
            "keys_file": self.runtime["keys_file"],
            "key_count": len(self.all_keys),
            "message_db_count": len(self.msg_db_keys),
            "message_table_count": msg_tables,
            "session_count": len(session_state),
            "contact_name_count": len(names),
        }

    def contact_names(self) -> dict[str, str]:
        return {
            row["username"]: row["displayName"]
            for row in self.contacts(limit=100_000)
            if row.get("username")
        }

    def contacts(self, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        path = self.cache.get(os.path.join("contact", "contact.db"))
        if not path:
            return []
        with closing(sqlite3.connect(path)) as conn:
            try:
                rows = conn.execute("SELECT username, nick_name, remark FROM contact").fetchall()
            except sqlite3.Error:
                return []
        query_l = query.strip().lower()
        contacts = []
        for username, nick, remark in rows:
            if not username:
                continue
            display = remark or nick or username
            item = {
                "username": username,
                "displayName": display,
                "nickName": nick or "",
                "remark": remark or "",
                "isGroup": "@chatroom" in username,
            }
            if query_l and not any(
                query_l in str(value or "").lower()
                for value in (username, display, nick, remark)
            ):
                continue
            contacts.append(item)
        contacts.sort(key=lambda item: (not item["remark"], item["displayName"].lower()))
        return contacts[:normalize_limit(limit, 100_000)]

    def read_session_state(self) -> dict[str, int]:
        path = self.cache.get(os.path.join("session", "session.db"))
        if not path:
            return {}
        with closing(sqlite3.connect(path)) as conn:
            rows = conn.execute(
                """
                SELECT username, last_timestamp
                FROM SessionTable
                WHERE last_timestamp > 0
                """
            ).fetchall()
        return {username: int(ts or 0) for username, ts in rows if username}

    def recent_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        path = self.cache.get(os.path.join("session", "session.db"))
        if not path:
            return []
        names = self.contact_names()
        with closing(sqlite3.connect(path)) as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT username, unread_count, summary, last_timestamp,
                           last_msg_type, last_msg_sender, last_sender_display_name
                    FROM SessionTable
                    WHERE last_timestamp > 0
                    ORDER BY last_timestamp DESC
                    LIMIT ?
                    """,
                    (normalize_limit(limit, 200),),
                ).fetchall()
            except sqlite3.Error:
                rows = conn.execute(
                    """
                    SELECT username, 0, '', last_timestamp, 0, '', ''
                    FROM SessionTable
                    WHERE last_timestamp > 0
                    ORDER BY last_timestamp DESC
                    LIMIT ?
                    """,
                    (normalize_limit(limit, 200),),
                ).fetchall()
        sessions = []
        for username, unread, summary, ts, msg_type, sender, sender_name in rows:
            if not username:
                continue
            summary_text = decompress_content(summary, 4 if isinstance(summary, bytes) else None) or ""
            is_group = "@chatroom" in username
            sender_id, text = parse_message_content(summary_text, is_group)
            sender_id = sender or sender_id or ""
            sessions.append(
                {
                    "conversationId": username,
                    "conversationName": names.get(username, username),
                    "isGroup": is_group,
                    "unreadCount": int(unread or 0),
                    "summary": text,
                    "lastTimestamp": int(ts or 0),
                    "lastOccurredAt": timestamp_to_iso(int(ts or 0)),
                    "lastMessageType": TYPE_LABELS.get(int(msg_type or 0) & 0xFFFFFFFF, ("unknown", f"type={msg_type or 0}"))[0],
                    "lastSenderId": sender_id,
                    "lastSenderName": names.get(sender_id, sender_name or sender_id),
                }
            )
        return sessions

    def bootstrap_state(self, state: CollectorState, backfill_seconds: int = 0) -> None:
        sessions = self.read_session_state()
        state.sessions = sessions
        if backfill_seconds > 0:
            floor = int(datetime.now(tz=timezone.utc).timestamp()) - int(backfill_seconds)
            self._bootstrap_all_message_tables(state, Cursor(create_time=floor, local_id=0))
        else:
            self._bootstrap_all_message_tables(state)

    def _bootstrap_all_message_tables(self, state: CollectorState, fixed_cursor: Cursor | None = None) -> None:
        for rel_key in self.msg_db_keys:
            path = self.cache.get(rel_key)
            if not path:
                continue
            try:
                with closing(sqlite3.connect(path)) as conn:
                    rows = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                    ).fetchall()
                    for (table_name,) in rows:
                        if not MSG_TABLE_RE.fullmatch(table_name):
                            continue
                        cursor = fixed_cursor or self._max_cursor_with_conn(conn, table_name)
                        state.set_cursor(self._cursor_key(rel_key, table_name), cursor.create_time, cursor.local_id)
            except sqlite3.Error:
                continue

    def changed_usernames(self, state: CollectorState) -> tuple[dict[str, int], list[str]]:
        current = self.read_session_state()
        changed = [
            username
            for username, ts in current.items()
            if ts > int(state.sessions.get(username) or 0)
        ]
        return current, changed

    def iter_new_messages(self, state: CollectorState, usernames: Iterable[str], batch_size: int) -> Iterable[MessageCandidate]:
        names = self.contact_names()
        for username in usernames:
            for rel_key, table_name, path in self._message_tables_for_username(username):
                cursor_key = self._cursor_key(rel_key, table_name)
                cursor = state.cursor_for(cursor_key) or Cursor()
                yield from self._query_table(path, rel_key, table_name, username, names, cursor, batch_size)

    def get_chat_history(
        self,
        chat: str,
        limit: int = 50,
        offset: int = 0,
        start_time: Any = "",
        end_time: Any = "",
        oldest_first: bool = False,
        message_types: list[str] | None = None,
    ) -> dict[str, Any]:
        ctx = self.resolve_chat(chat)
        if not ctx:
            raise ValueError(f"找不到聊天对象: {chat}")
        type_filter = resolve_type_filter(message_types)
        start_ts, end_ts = parse_time_range(start_time, end_time)
        messages = self._query_messages_for_username(
            ctx["conversationId"],
            limit=limit,
            offset=offset,
            start_ts=start_ts,
            end_ts=end_ts,
            oldest_first=oldest_first,
            type_filter=type_filter,
        )
        return {
            "conversation": ctx,
            "messages": messages,
            "limit": normalize_limit(limit, 500),
            "offset": normalize_offset(offset),
            "hasMoreHint": len(messages) >= normalize_limit(limit, 500),
        }

    def search_messages(
        self,
        keyword: str,
        chat: str = "",
        limit: int = 20,
        offset: int = 0,
        start_time: Any = "",
        end_time: Any = "",
    ) -> dict[str, Any]:
        keyword = str(keyword or "").strip()
        if not keyword:
            raise ValueError("keyword 不能为空")
        start_ts, end_ts = parse_time_range(start_time, end_time)
        if str(chat or "").strip():
            ctx = self.resolve_chat(chat)
            if not ctx:
                raise ValueError(f"找不到聊天对象: {chat}")
            usernames = [ctx["conversationId"]]
        else:
            ctx = None
            usernames = self.known_conversation_ids()
        all_messages: list[dict[str, Any]] = []
        for username in usernames:
            all_messages.extend(
                self._query_messages_for_username(
                    username,
                    limit=normalize_limit(limit, 500) + normalize_offset(offset),
                    offset=0,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    oldest_first=False,
                    keyword=keyword,
                )
            )
        all_messages.sort(key=lambda item: (int(item["timestamp"]), int(item["localId"])), reverse=True)
        normalized_limit = normalize_limit(limit, 500)
        normalized_offset = normalize_offset(offset)
        page = all_messages[normalized_offset : normalized_offset + normalized_limit]
        return {
            "conversation": ctx,
            "keyword": keyword,
            "messages": page,
            "limit": normalized_limit,
            "offset": normalized_offset,
            "hasMoreHint": len(all_messages) > normalized_offset + normalized_limit,
        }

    def get_message_by_id(self, message_id: str) -> dict[str, Any] | None:
        rel_key, table_name, local_id = parse_message_id(message_id)
        path = self.cache.get(rel_key)
        if not path:
            return None
        username = self.username_for_table(table_name) or ""
        names = self.contact_names()
        with closing(sqlite3.connect(path)) as conn:
            id_to_username = load_name2id_maps(conn)
            has_ct = has_column(conn, table_name, "WCDB_CT_message_content")
            ct_expr = "WCDB_CT_message_content" if has_ct else "NULL"
            try:
                row = conn.execute(
                    f"""
                    SELECT local_id, local_type, create_time, real_sender_id,
                           message_content, {ct_expr}
                    FROM [{table_name}]
                    WHERE local_id = ?
                    LIMIT 1
                    """,
                    (local_id,),
                ).fetchone()
            except sqlite3.Error:
                return None
        if not row:
            return None
        if not username:
            username = self.username_for_message_row(row, names) or ""
        candidate = self._build_candidate(row, rel_key, table_name, username, names, id_to_username)
        return candidate.payload if candidate else None

    def get_chat_images(self, chat: str, limit: int = 20, offset: int = 0, start_time: Any = "", end_time: Any = "") -> dict[str, Any]:
        return self.get_chat_history(
            chat,
            limit=limit,
            offset=offset,
            start_time=start_time,
            end_time=end_time,
            oldest_first=False,
            message_types=["image"],
        )

    def get_voice_messages(self, chat: str, limit: int = 20, offset: int = 0, start_time: Any = "", end_time: Any = "") -> dict[str, Any]:
        return self.get_chat_history(
            chat,
            limit=limit,
            offset=offset,
            start_time=start_time,
            end_time=end_time,
            oldest_first=False,
            message_types=["voice"],
        )

    def resolve_chat(self, query: str) -> dict[str, Any] | None:
        query = str(query or "").strip()
        if not query:
            return None
        names = self.contact_names()
        candidates = []
        for username in self.known_conversation_ids():
            display = names.get(username, username)
            item = {
                "conversationId": username,
                "conversationName": display,
                "isGroup": "@chatroom" in username,
            }
            candidates.append(item)
        query_l = query.lower()
        for item in candidates:
            if item["conversationId"].lower() == query_l:
                return item
        for item in candidates:
            if item["conversationName"].lower() == query_l:
                return item
        for item in candidates:
            if query_l in item["conversationName"].lower() or query_l in item["conversationId"].lower():
                return item
        return None

    def known_conversation_ids(self) -> list[str]:
        usernames = set(self.read_session_state())
        usernames.update(row["username"] for row in self.contacts(limit=100_000))
        return sorted(username for username in usernames if username)

    def _message_tables_for_username(self, username: str) -> list[tuple[str, str, str]]:
        table_name = "Msg_" + hashlib.md5(username.encode()).hexdigest()
        matches = []
        for rel_key in self.msg_db_keys:
            path = self.cache.get(rel_key)
            if not path:
                continue
            try:
                with closing(sqlite3.connect(path)) as conn:
                    exists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table_name,),
                    ).fetchone()
                if exists:
                    matches.append((rel_key, table_name, path))
            except sqlite3.Error:
                continue
        return matches

    def _query_messages_for_username(
        self,
        username: str,
        limit: int,
        offset: int = 0,
        start_ts: int | None = None,
        end_ts: int | None = None,
        oldest_first: bool = False,
        keyword: str = "",
        type_filter: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        names = self.contact_names()
        candidate_limit = normalize_limit(limit, 500) + normalize_offset(offset)
        collected: list[dict[str, Any]] = []
        for rel_key, table_name, path in self._message_tables_for_username(username):
            with closing(sqlite3.connect(path)) as conn:
                id_to_username = load_name2id_maps(conn)
                rows = self._query_table_rows(
                    conn,
                    table_name,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    type_filter=type_filter,
                    limit=candidate_limit,
                    oldest_first=oldest_first,
                )
            for row in rows:
                candidate = self._build_candidate(row, rel_key, table_name, username, names, id_to_username)
                if not candidate:
                    continue
                text = str(candidate.payload.get("text") or "")
                if keyword and keyword.lower() not in text.lower():
                    continue
                collected.append(candidate.payload)
        collected.sort(
            key=lambda item: (int(item["timestamp"]), int(item["localId"])),
            reverse=not oldest_first,
        )
        normalized_offset = normalize_offset(offset)
        normalized_limit = normalize_limit(limit, 500)
        return collected[normalized_offset : normalized_offset + normalized_limit]

    def _query_table_rows(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        start_ts: int | None = None,
        end_ts: int | None = None,
        type_filter: set[int] | None = None,
        limit: int = 50,
        oldest_first: bool = False,
    ) -> list[tuple[Any, ...]]:
        has_ct = has_column(conn, table_name, "WCDB_CT_message_content")
        ct_expr = "WCDB_CT_message_content" if has_ct else "NULL"
        clauses = []
        params: list[Any] = []
        if start_ts is not None:
            clauses.append("create_time >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("create_time <= ?")
            params.append(end_ts)
        if type_filter:
            placeholders = ",".join("?" for _ in type_filter)
            clauses.append(f"(local_type & 4294967295) IN ({placeholders})")
            params.extend(sorted(type_filter))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        order = "ASC" if oldest_first else "DESC"
        params.append(normalize_limit(limit, 1000))
        return conn.execute(
            f"""
            SELECT local_id, local_type, create_time, real_sender_id,
                   message_content, {ct_expr}
            FROM [{table_name}]
            {where}
            ORDER BY create_time {order}, local_id {order}
            LIMIT ?
            """,
            params,
        ).fetchall()

    def username_for_table(self, table_name: str) -> str | None:
        if not MSG_TABLE_RE.fullmatch(table_name):
            return None
        target = table_name.removeprefix("Msg_")
        for username in self.known_conversation_ids():
            if hashlib.md5(username.encode()).hexdigest() == target:
                return username
        return None

    def username_for_message_row(self, row: tuple[Any, ...], names: dict[str, str]) -> str | None:
        _local_id, _local_type, _create_time, real_sender_id, raw_content, ct = row
        content = decompress_content(raw_content, ct) or ""
        sender, _text = parse_message_content(content, True)
        if sender and sender in names:
            return sender
        return None

    @staticmethod
    def _max_cursor_with_conn(conn: sqlite3.Connection, table_name: str) -> Cursor:
        row = conn.execute(
            f"SELECT create_time, local_id FROM [{table_name}] "
            "ORDER BY create_time DESC, local_id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return Cursor()
        return Cursor(create_time=int(row[0] or 0), local_id=int(row[1] or 0))

    def _query_table(self, db_path: str, rel_key: str, table_name: str, username: str, names: dict[str, str], cursor: Cursor, batch_size: int) -> Iterable[MessageCandidate]:
        with closing(sqlite3.connect(db_path)) as conn:
            id_to_username = load_name2id_maps(conn)
            has_ct = has_column(conn, table_name, "WCDB_CT_message_content")
            ct_expr = "WCDB_CT_message_content" if has_ct else "NULL"
            rows = conn.execute(
                f"""
                SELECT local_id, local_type, create_time, real_sender_id,
                       message_content, {ct_expr}
                FROM [{table_name}]
                WHERE create_time > ?
                   OR (create_time = ? AND local_id > ?)
                ORDER BY create_time ASC, local_id ASC
                LIMIT ?
                """,
                (cursor.create_time, cursor.create_time, cursor.local_id, batch_size),
            ).fetchall()
            for row in rows:
                candidate = self._build_candidate(row, rel_key, table_name, username, names, id_to_username)
                if candidate:
                    yield candidate

    def _build_candidate(self, row: tuple[Any, ...], rel_key: str, table_name: str, username: str, names: dict[str, str], id_to_username: dict[int, str]) -> MessageCandidate | None:
        local_id, local_type, create_time, real_sender_id, raw_content, ct = row
        local_id = int(local_id or 0)
        local_type = int(local_type or 0)
        create_time = int(create_time or 0)
        content = decompress_content(raw_content, ct) or ""
        is_group = "@chatroom" in username
        sender_from_content, text = parse_message_content(content, is_group)
        sender_username = id_to_username.get(int(real_sender_id or 0), "") or sender_from_content
        conversation_name = names.get(username, username)
        sender_name = names.get(sender_username, sender_username)
        base_type = local_type & 0xFFFFFFFF
        type_name, type_label = TYPE_LABELS.get(base_type, ("unknown", f"type={local_type}"))

        message_id = f"{rel_key}:{table_name}:{local_id}"
        event_id = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
        occurred_at = datetime.fromtimestamp(create_time, tz=timezone.utc).isoformat()
        direction = direction_for(is_group, username, sender_username)
        if direction == "outgoing" and not self.config.include_outgoing:
            return None

        payload: dict[str, Any] = {
            "messageId": message_id,
            "dbPath": rel_key,
            "tableName": table_name,
            "localId": local_id,
            "conversationId": username,
            "conversationName": conversation_name,
            "isGroup": is_group,
            "senderId": sender_username,
            "senderName": sender_name,
            "direction": direction,
            "messageType": type_name,
            "messageTypeLabel": type_label,
            "timestamp": create_time,
            "occurredAt": occurred_at,
            "source": "wechat-local-db",
            "platform": platform.system().lower(),
        }
        if self.config.include_text:
            payload["text"] = format_text_for_type(type_name, text, local_id)

        return MessageCandidate(
            event_id=event_id,
            payload=payload,
            occurred_at=occurred_at,
            cursor_key=self._cursor_key(rel_key, table_name),
            cursor=Cursor(create_time=create_time, local_id=local_id),
        )

    @staticmethod
    def _cursor_key(rel_key: str, table_name: str) -> str:
        return f"{rel_key}#{table_name}"


def _load_module_from_file(name: str, path: Path):
    if not path.exists():
        raise RuntimeError(f"required wechat-decrypt module not found: {path}")
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"failed to load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_msg_db_keys(all_keys: dict[str, Any]) -> list[str]:
    keys = []
    for key, value in all_keys.items():
        if not isinstance(value, dict) or "enc_key" not in value:
            continue
        normalized = key.replace("\\", "/")
        if normalized.startswith("message/") and re.search(r"message_\d+\.db$", normalized):
            keys.append(key)
    return sorted(keys)


def decrypt_page(enc_key: bytes, page_data: bytes, pgno: int) -> bytes:
    iv = page_data[PAGE_SZ - RESERVE_SZ : PAGE_SZ - RESERVE_SZ + IV_SZ]
    if pgno == 1:
        encrypted = page_data[SALT_SZ : PAGE_SZ - RESERVE_SZ]
        decrypted = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(encrypted)
        return bytes(bytearray(SQLITE_HDR + decrypted + b"\x00" * RESERVE_SZ))
    encrypted = page_data[: PAGE_SZ - RESERVE_SZ]
    decrypted = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(encrypted)
    return decrypted + b"\x00" * RESERVE_SZ


def full_decrypt(db_path: str, out_path: str, enc_key: bytes) -> None:
    file_size = os.path.getsize(db_path)
    total_pages = file_size // PAGE_SZ
    if file_size % PAGE_SZ:
        total_pages += 1
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(db_path, "rb") as fin, open(out_path, "wb") as fout:
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if len(page) < PAGE_SZ:
                if page:
                    page += b"\x00" * (PAGE_SZ - len(page))
                else:
                    break
            fout.write(decrypt_page(enc_key, page, pgno))


def decrypt_wal(wal_path: str, out_path: str, enc_key: bytes) -> None:
    if not os.path.exists(wal_path) or os.path.getsize(wal_path) <= WAL_HEADER_SZ:
        return
    frame_size = WAL_FRAME_HEADER_SZ + PAGE_SZ
    with open(wal_path, "rb") as wf, open(out_path, "r+b") as df:
        wal_hdr = wf.read(WAL_HEADER_SZ)
        wal_salt1 = struct.unpack(">I", wal_hdr[16:20])[0]
        wal_salt2 = struct.unpack(">I", wal_hdr[20:24])[0]
        while wf.tell() + frame_size <= os.path.getsize(wal_path):
            fh = wf.read(WAL_FRAME_HEADER_SZ)
            if len(fh) < WAL_FRAME_HEADER_SZ:
                break
            pgno = struct.unpack(">I", fh[0:4])[0]
            frame_salt1 = struct.unpack(">I", fh[8:12])[0]
            frame_salt2 = struct.unpack(">I", fh[12:16])[0]
            encrypted_page = wf.read(PAGE_SZ)
            if len(encrypted_page) < PAGE_SZ:
                break
            if pgno == 0 or pgno > 1_000_000:
                continue
            if frame_salt1 != wal_salt1 or frame_salt2 != wal_salt2:
                continue
            df.seek((pgno - 1) * PAGE_SZ)
            df.write(decrypt_page(enc_key, encrypted_page, pgno))


def load_name2id_maps(conn: sqlite3.Connection) -> dict[int, str]:
    try:
        rows = conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
    except sqlite3.Error:
        return {}
    return {int(rowid): user_name for rowid, user_name in rows if user_name}


def normalize_limit(value: Any, maximum: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = 50
    return max(1, min(limit, maximum))


def normalize_offset(value: Any) -> int:
    try:
        offset = int(value)
    except (TypeError, ValueError):
        offset = 0
    return max(0, offset)


def timestamp_to_iso(timestamp: int) -> str | None:
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def parse_time_range(start_time: Any, end_time: Any) -> tuple[int | None, int | None]:
    start_ts = parse_time_value(start_time, is_end=False)
    end_ts = parse_time_value(end_time, is_end=True)
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise ValueError("startTime 不能晚于 endTime")
    return start_ts, end_ts


def parse_time_value(value: Any, is_end: bool) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    normalized = text.replace("T", " ")
    formats = [
        ("%Y-%m-%d %H:%M:%S", False),
        ("%Y-%m-%d %H:%M", False),
        ("%Y-%m-%d", True),
    ]
    for fmt, date_only in formats:
        try:
            dt = datetime.strptime(normalized, fmt)
            if date_only and is_end:
                dt = dt.replace(hour=23, minute=59, second=59)
            return int(dt.timestamp())
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text)
        return int(dt.timestamp())
    except ValueError as exc:
        raise ValueError(f"无法解析时间: {value}") from exc


def resolve_type_filter(message_types: list[str] | None) -> set[int] | None:
    if not message_types:
        return None
    codes: set[int] = set()
    unknown: list[str] = []
    for item in message_types:
        key = str(item or "").strip().lower()
        if not key:
            continue
        if key.isdigit():
            codes.add(int(key))
            continue
        mapped = TYPE_NAME_TO_CODES.get(key)
        if mapped:
            codes.update(mapped)
        else:
            unknown.append(key)
    if unknown:
        raise ValueError(f"未知消息类型: {', '.join(unknown)}")
    return codes or None


def parse_message_id(message_id: str) -> tuple[str, str, int]:
    parts = str(message_id or "").rsplit(":", 2)
    if len(parts) != 3:
        raise ValueError("messageId 格式不正确")
    rel_key, table_name, local_id_text = parts
    if not rel_key or not MSG_TABLE_RE.fullmatch(table_name):
        raise ValueError("messageId 格式不正确")
    try:
        local_id = int(local_id_text)
    except ValueError as exc:
        raise ValueError("messageId localId 不正确") from exc
    return rel_key, table_name, local_id


def has_column(conn: sqlite3.Connection, table_name: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info([{table_name}])").fetchall()
    return any(row[1] == column for row in rows)


def decompress_content(content: Any, ct: Any) -> str | None:
    if ct and int(ct) == 4 and isinstance(content, bytes):
        try:
            return _ZSTD.decompress(content).decode("utf-8", errors="replace")
        except Exception:
            return None
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    if content is None:
        return ""
    return str(content)


def parse_message_content(content: str, is_group: bool) -> tuple[str, str]:
    if is_group and ":\n" in content:
        sender, text = content.split(":\n", 1)
        return sender, text
    return "", content


def format_text_for_type(type_name: str, text: str, local_id: int) -> str:
    if type_name == "image" and not text:
        return f"[图片] local_id={local_id}"
    if type_name == "sticker":
        return "[表情]"
    if type_name == "voice":
        return text or "[语音]"
    if type_name == "video":
        return text or "[视频]"
    if type_name == "app":
        return summarize_app_xml(text) or "[链接/文件]"
    if text and text.lstrip().startswith("<"):
        return summarize_app_xml(text) or summarize_xml_text(text) or "[XML消息]"
    return text


def summarize_app_xml(text: str) -> str | None:
    root = parse_xml_root(text)
    if root is None:
        return None
    title = first_text(root, [".//appmsg/title", ".//item/title", ".//template_header/title"])
    desc = first_text(root, [".//appmsg/des", ".//item/digest", ".//topnew/digest"])
    app_type = first_text(root, [".//appmsg/type"])
    if app_type == "6":
        return f"[文件] {title}".strip() if title else "[文件]"
    if title and desc and title != desc:
        return f"{title}\n{desc}"
    if title:
        return title
    if desc:
        return desc
    return None


def summarize_xml_text(text: str) -> str | None:
    root = parse_xml_root(text)
    if root is None:
        return None
    if root.find(".//emoji") is not None:
        return "[表情]"
    return None


def parse_xml_root(text: str) -> ET.Element | None:
    if not text or len(text) > _XML_PARSE_MAX_LEN or _XML_UNSAFE_RE.search(text):
        return None
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def first_text(root: ET.Element, paths: list[str]) -> str:
    for path in paths:
        value = root.findtext(path)
        if value:
            value = re.sub(r"\s+", " ", value).strip()
            if value:
                return value
    return ""


def direction_for(is_group: bool, conversation_username: str, sender_username: str) -> str:
    if is_group:
        return "unknown"
    if not sender_username:
        return "unknown"
    if sender_username == conversation_username:
        return "incoming"
    return "outgoing"
