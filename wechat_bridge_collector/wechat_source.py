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
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import closing, contextmanager
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


class DatabaseSnapshotError(RuntimeError):
    """Raised when a consistent decrypted SQLite snapshot cannot be built."""


@dataclass(frozen=True)
class WalFrame:
    page_number: int
    database_pages: int
    encrypted_page: bytes
    checksum: tuple[int, int]


@dataclass(frozen=True)
class WalSnapshot:
    generation: tuple[int, int, int, int, int, int] | None
    frames: tuple[WalFrame, ...]
    pending: bool = False

    @property
    def checksum(self) -> tuple[int, int] | None:
        return self.frames[-1].checksum if self.frames else None

    @property
    def database_pages(self) -> int | None:
        return self.frames[-1].database_pages if self.frames else None


@dataclass
class CacheEntry:
    db_signature: tuple[int, int, int, int]
    key_hash: str
    path: str
    wal_generation: tuple[int, int, int, int, int, int] | None
    wal_frame_count: int
    wal_checksum: tuple[int, int] | None


class DBCache:
    def __init__(self, keys: dict[str, Any], db_dir: str):
        self.keys = keys
        self.db_dir = db_dir
        self.cache_dir = Path(tempfile.gettempdir()) / "wechat_bridge_collector_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, CacheEntry] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._metadata_lock = threading.Lock()
        self._metadata_path = self.cache_dir / "_snapshots.json"
        self._load_persistent_cache()

    def get(self, rel_key: str) -> str | None:
        """Return a refreshed snapshot path.

        Production readers should use ``snapshot()`` so the per-database lock
        remains held until their SQLite connection is closed. ``get`` remains
        available for probes and compatibility with existing integrations.
        """
        with self._lock_for(rel_key):
            return self._refresh_locked(rel_key)

    @contextmanager
    def snapshot(self, rel_key: str):
        """Yield a stable decrypted snapshot while excluding refresh writers."""
        with self._lock_for(rel_key):
            yield self._refresh_locked(rel_key)

    def _refresh_locked(self, rel_key: str) -> str | None:
        key_info = self._get_key_info(rel_key)
        if not key_info:
            return None
        db_path = Path(self.db_dir) / rel_key.replace("\\", os.sep).replace("/", os.sep)
        wal_path = Path(str(db_path) + "-wal")
        if not db_path.exists():
            return None
        db_signature = self._db_signature(db_path)
        if not db_signature:
            return None
        enc_key = bytes.fromhex(key_info["enc_key"])
        key_hash = hashlib.sha256(enc_key).hexdigest()[:16]
        cached = self._cache.get(rel_key)
        wal_snapshot = read_wal_snapshot(wal_path)
        if wal_snapshot.pending:
            raise DatabaseSnapshotError(f"WAL transaction has not reached a commit boundary for {rel_key}")
        if cached and cached.key_hash == key_hash and Path(cached.path).exists():
            if cached.db_signature == db_signature:
                if self._wal_unchanged(cached, wal_snapshot):
                    return cached.path
                if self._wal_extends(cached, wal_snapshot):
                    try:
                        self._apply_incremental(rel_key, cached, wal_snapshot, enc_key)
                        return cached.path
                    except DatabaseSnapshotError:
                        pass

        out_path = str(
            self.cache_dir
            / (
                hashlib.md5(f"{self.db_dir}:{rel_key}".encode()).hexdigest()[:16]
                + f"-{key_hash}.db"
            )
        )
        last_error: Exception | None = None
        for attempt in range(3):
            current_db_signature = self._db_signature(db_path)
            if not current_db_signature:
                return None
            wal_snapshot = read_wal_snapshot(wal_path)
            if wal_snapshot.pending:
                raise DatabaseSnapshotError(f"WAL transaction has not reached a commit boundary for {rel_key}")
            descriptor, tmp_path = tempfile.mkstemp(
                prefix=f".{hashlib.md5(rel_key.encode()).hexdigest()[:16]}.{os.getpid()}.{attempt}.",
                suffix=".tmp",
                dir=self.cache_dir,
            )
            os.close(descriptor)
            try:
                full_decrypt(str(db_path), tmp_path, enc_key)
                apply_wal_snapshot(tmp_path, wal_snapshot, enc_key)
                after_db_signature = self._db_signature(db_path)
                if after_db_signature != current_db_signature:
                    raise DatabaseSnapshotError("source main database changed while building decrypted snapshot")
                self._assert_sqlite_healthy(tmp_path)
                os.replace(tmp_path, out_path)
                self._cache[rel_key] = CacheEntry(
                    db_signature=after_db_signature,
                    key_hash=key_hash,
                    path=out_path,
                    wal_generation=wal_snapshot.generation,
                    wal_frame_count=len(wal_snapshot.frames),
                    wal_checksum=wal_snapshot.checksum,
                )
                self._save_persistent_cache()
                return out_path
            except (OSError, sqlite3.Error, DatabaseSnapshotError) as exc:
                last_error = exc
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                time.sleep(0.05)
        raise DatabaseSnapshotError(f"failed to build healthy SQLite snapshot for {rel_key}: {last_error}")

    def source_signature(self, rel_key: str) -> tuple[int, int, int, int, int, int] | None:
        db_path = Path(self.db_dir) / rel_key.replace("\\", os.sep).replace("/", os.sep)
        wal_path = Path(str(db_path) + "-wal")
        if not db_path.exists():
            return None
        db_signature = self._db_signature(db_path)
        try:
            wal_stat = wal_path.stat()
        except FileNotFoundError:
            wal_stat = None
        except OSError:
            return None
        if not db_signature:
            return None
        return (
            db_signature[0],
            db_signature[1],
            wal_stat.st_mtime_ns if wal_stat else 0,
            wal_stat.st_size if wal_stat else 0,
            db_signature[2],
            db_signature[3],
        )

    def _lock_for(self, rel_key: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(rel_key, threading.RLock())

    @staticmethod
    def _wal_unchanged(cached: CacheEntry, current: WalSnapshot) -> bool:
        return (
            cached.wal_generation == current.generation
            and cached.wal_frame_count == len(current.frames)
            and cached.wal_checksum == current.checksum
        )

    @staticmethod
    def _wal_extends(cached: CacheEntry, current: WalSnapshot) -> bool:
        if cached.wal_generation != current.generation:
            return False
        if len(current.frames) <= cached.wal_frame_count:
            return False
        if cached.wal_frame_count == 0:
            return cached.wal_checksum is None
        return current.frames[cached.wal_frame_count - 1].checksum == cached.wal_checksum

    def _apply_incremental(
        self,
        rel_key: str,
        cached: CacheEntry,
        current: WalSnapshot,
        enc_key: bytes,
    ) -> None:
        frames = current.frames[cached.wal_frame_count :]
        if not frames:
            return
        marker = self._update_marker(rel_key)
        marker.write_text("refreshing\n", encoding="utf-8")
        original_size = os.path.getsize(cached.path)
        original_pages: dict[int, bytes] = {}
        remove_marker = False
        try:
            with open(cached.path, "r+b") as target:
                for frame in frames:
                    offset = (frame.page_number - 1) * PAGE_SZ
                    if frame.page_number not in original_pages:
                        target.seek(offset)
                        original_pages[frame.page_number] = target.read(PAGE_SZ)
                    target.seek(offset)
                    target.write(decrypt_page(enc_key, frame.encrypted_page, frame.page_number))
                if current.database_pages:
                    target.truncate(current.database_pages * PAGE_SZ)
                target.flush()
                os.fsync(target.fileno())
            cached.wal_frame_count = len(current.frames)
            cached.wal_checksum = current.checksum
            self._save_persistent_cache()
            remove_marker = True
        except Exception as exc:
            try:
                with open(cached.path, "r+b") as target:
                    target.truncate(original_size)
                    for page_number, page in original_pages.items():
                        target.seek((page_number - 1) * PAGE_SZ)
                        target.write(page)
                    target.flush()
                    os.fsync(target.fileno())
                remove_marker = True
            except Exception as rollback_error:
                raise DatabaseSnapshotError(
                    f"incremental WAL refresh and rollback both failed for {rel_key}: {rollback_error}"
                ) from exc
            raise DatabaseSnapshotError(f"incremental WAL refresh failed for {rel_key}: {exc}") from exc
        finally:
            if remove_marker:
                marker.unlink(missing_ok=True)

    def _update_marker(self, rel_key: str) -> Path:
        token = hashlib.md5(f"{self.db_dir}:{rel_key}".encode()).hexdigest()[:16]
        return self.cache_dir / f".{token}.updating"

    def _load_persistent_cache(self) -> None:
        try:
            raw = json.loads(self._metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        for rel_key, item in raw.items():
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            if not path or not Path(path).exists():
                continue
            db_signature = item.get("dbSignature")
            if not isinstance(db_signature, list) or len(db_signature) != 4:
                continue
            key_hash = str(item.get("keyHash") or "")
            if not key_hash:
                continue
            if self._update_marker(str(rel_key)).exists():
                continue
            wal_generation = item.get("walGeneration")
            if wal_generation is not None and (
                not isinstance(wal_generation, list) or len(wal_generation) != 6
            ):
                continue
            wal_checksum = item.get("walChecksum")
            if wal_checksum is not None and (
                not isinstance(wal_checksum, list) or len(wal_checksum) != 2
            ):
                continue
            try:
                self._cache[str(rel_key)] = CacheEntry(
                    db_signature=tuple(int(value) for value in db_signature),
                    key_hash=key_hash,
                    path=path,
                    wal_generation=(
                        tuple(int(value) for value in wal_generation)
                        if wal_generation is not None
                        else None
                    ),
                    wal_frame_count=int(item.get("walFrameCount") or 0),
                    wal_checksum=(
                        tuple(int(value) for value in wal_checksum)
                        if wal_checksum is not None
                        else None
                    ),
                )
            except (TypeError, ValueError):
                continue

    def _save_persistent_cache(self) -> None:
        with self._metadata_lock:
            data = {
                rel_key: {
                    "dbSignature": list(entry.db_signature),
                    "keyHash": entry.key_hash,
                    "path": entry.path,
                    "walGeneration": list(entry.wal_generation) if entry.wal_generation else None,
                    "walFrameCount": entry.wal_frame_count,
                    "walChecksum": list(entry.wal_checksum) if entry.wal_checksum else None,
                }
                for rel_key, entry in self._cache.items()
            }
            tmp_path = self._metadata_path.with_name(
                f".{self._metadata_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                tmp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp_path, self._metadata_path)
            except OSError:
                tmp_path.unlink(missing_ok=True)

    @staticmethod
    def _db_signature(db_path: Path) -> tuple[int, int, int, int] | None:
        try:
            db_stat = db_path.stat()
        except OSError:
            return None
        return (
            db_stat.st_mtime_ns,
            db_stat.st_size,
            db_stat.st_dev,
            db_stat.st_ino,
        )

    @staticmethod
    def _assert_sqlite_healthy(path: str) -> None:
        with closing(sqlite3.connect(path)) as conn:
            row = conn.execute("PRAGMA quick_check").fetchone()
        if not row or row[0] != "ok":
            detail = row[0] if row else "no quick_check result"
            raise DatabaseSnapshotError(f"SQLite quick_check failed: {detail}")

    @classmethod
    def _is_sqlite_healthy(cls, path: str) -> bool:
        try:
            cls._assert_sqlite_healthy(path)
            return True
        except (OSError, sqlite3.Error, DatabaseSnapshotError):
            return False

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


@contextmanager
def cached_snapshot(cache: Any, rel_key: str):
    """Use the locking cache contract while retaining lightweight test doubles."""
    snapshot = getattr(cache, "snapshot", None)
    if callable(snapshot):
        with snapshot(rel_key) as path:
            yield path
        return
    yield cache.get(rel_key)


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
        self._contacts_cache: tuple[tuple[float, int] | None, list[dict[str, Any]]] | None = None
        self._contact_names_cache: tuple[tuple[float, int] | None, dict[str, str]] | None = None
        self._session_state_cache: tuple[tuple[float, int] | None, dict[str, int]] | None = None
        self._message_tables_cache: dict[str, tuple[tuple[Any, ...], list[tuple[str, str]]]] = {}
        self._message_tables_index_path = self.cache.cache_dir / (
            hashlib.md5(f"{self.db_dir}:message-table-index".encode()).hexdigest()[:16] + ".json"
        )

    def probe(self) -> dict[str, Any]:
        self.assert_source_access()
        names = self.contact_names()
        session_state = self.read_session_state()
        msg_tables = 0
        for rel_key in self.msg_db_keys:
            with cached_snapshot(self.cache, rel_key) as path:
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

    def assert_source_access(self) -> None:
        db_dir = Path(self.db_dir)
        try:
            db_dir.stat()
            if not db_dir.is_dir():
                raise RuntimeError(f"WeChat database directory does not exist: {db_dir}")
            candidates = [
                os.path.join("contact", "contact.db"),
                os.path.join("session", "session.db"),
                *self.msg_db_keys,
            ]
            for rel_key in candidates:
                source_path = db_dir / rel_key.replace("\\", os.sep).replace("/", os.sep)
                try:
                    source_path.stat()
                except FileNotFoundError:
                    continue
                with source_path.open("rb") as handle:
                    handle.read(1)
                with cached_snapshot(self.cache, rel_key) as snapshot:
                    if snapshot:
                        return
            raise RuntimeError(
                f"No WeChat database could be decrypted with the configured keys under: {db_dir}"
            )
        except PermissionError as exc:
            raise _full_disk_access_error() from exc
        except OSError as exc:
            if exc.errno in {1, 13}:
                raise _full_disk_access_error() from exc
            raise

    def contact_names(self) -> dict[str, str]:
        contacts, signature = self._all_contacts()
        cached = self._contact_names_cache
        if cached and cached[0] == signature:
            return cached[1]
        names = {
            row["username"]: row["displayName"]
            for row in contacts
            if row.get("username")
        }
        self._contact_names_cache = (signature, names)
        return names

    def account_profile(self) -> dict[str, Any]:
        account_id = Path(self.db_dir).parent.name.strip()
        if not account_id:
            raise RuntimeError("cannot resolve WeChat account ID from db_storage path")
        return {
            "accountId": account_id,
            "displayName": account_id,
            "source": "wechat-local-db",
            "platform": platform.system().lower(),
        }

    def contacts(
        self,
        query: str = "",
        limit: int = 50,
        offset: int = 0,
        include_groups: bool = True,
    ) -> list[dict[str, Any]]:
        all_contacts, _signature = self._all_contacts()
        query_l = query.strip().lower()
        contacts = []
        for item in all_contacts:
            if not include_groups and item["isGroup"]:
                continue
            if query_l and not any(
                query_l in str(value or "").lower()
                for value in (item["username"], item["displayName"], item["nickName"], item["remark"])
            ):
                continue
            contacts.append(dict(item))
        contacts.sort(key=lambda item: (not item["remark"], item["displayName"].lower()))
        normalized_offset = normalize_offset(offset)
        normalized_limit = normalize_limit(limit, 100_000)
        return contacts[normalized_offset : normalized_offset + normalized_limit]

    def contact_snapshot(
        self,
        limit: int = 500,
        offset: int = 0,
        include_groups: bool = False,
    ) -> dict[str, Any]:
        all_contacts, signature = self._all_contacts()
        filtered = [item for item in all_contacts if include_groups or not item["isGroup"]]
        filtered.sort(key=lambda item: (not item["remark"], item["displayName"].lower()))
        normalized_offset = normalize_offset(offset)
        normalized_limit = normalize_limit(limit, 500)
        page = [dict(item) for item in filtered[normalized_offset : normalized_offset + normalized_limit]]
        return {
            "account": self.account_profile(),
            "snapshotToken": contact_snapshot_token(signature),
            "contacts": page,
            "offset": normalized_offset,
            "limit": normalized_limit,
            "total": len(filtered),
            "hasMore": normalized_offset + len(page) < len(filtered),
        }

    def _all_contacts(self) -> tuple[list[dict[str, Any]], tuple[float, int] | None]:
        rel_key = os.path.join("contact", "contact.db")
        with cached_snapshot(self.cache, rel_key) as path:
            if not path:
                return [], None
            signature = file_signature(path)
            cached = self._contacts_cache
            if cached and cached[0] == signature:
                return cached[1], signature
            with closing(sqlite3.connect(path)) as conn:
                try:
                    rows = conn.execute("SELECT username, nick_name, remark FROM contact").fetchall()
                except sqlite3.Error:
                    return [], signature
        contacts = []
        for username, nick, remark in rows:
            if not username:
                continue
            display = remark or nick or username
            contacts.append({
                "username": username,
                "displayName": display,
                "nickName": nick or "",
                "remark": remark or "",
                "isGroup": "@chatroom" in username,
            })
        self._contacts_cache = (signature, contacts)
        return contacts, signature

    def read_session_state(self) -> dict[str, int]:
        rel_key = os.path.join("session", "session.db")
        with cached_snapshot(self.cache, rel_key) as path:
            if not path:
                return {}
            signature = file_signature(path)
            cached = self._session_state_cache
            if cached and cached[0] == signature:
                return dict(cached[1])
            with closing(sqlite3.connect(path)) as conn:
                rows = conn.execute(
                    """
                    SELECT username, last_timestamp
                    FROM SessionTable
                    WHERE last_timestamp > 0
                    """
                ).fetchall()
        state = {username: int(ts or 0) for username, ts in rows if username}
        self._session_state_cache = (signature, state)
        return dict(state)

    def recent_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        rel_key = os.path.join("session", "session.db")
        with cached_snapshot(self.cache, rel_key) as path:
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
        readable_usernames = self._usernames_with_message_tables(
            username for username, *_rest in rows if username
        )
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
                    "historyAvailable": username in readable_usernames,
                    "unreadCount": int(unread or 0),
                    "summary": text,
                    "lastTimestamp": epoch_seconds_to_millis(int(ts)) if int(ts or 0) > 0 else None,
                    "lastMessageType": TYPE_LABELS.get(int(msg_type or 0) & 0xFFFFFFFF, ("unknown", f"type={msg_type or 0}"))[0],
                    "lastSenderId": sender_id,
                    "lastSenderName": names.get(sender_id, sender_name or sender_id),
                }
            )
        return sessions

    def _usernames_with_message_tables(self, usernames: Iterable[str]) -> set[str]:
        table_to_username = {
            "Msg_" + hashlib.md5(username.encode()).hexdigest(): username
            for username in usernames
            if username
        }
        if not table_to_username:
            return set()

        table_names = tuple(table_to_username)
        placeholders = ",".join("?" for _ in table_names)
        readable: set[str] = set()
        for rel_key in self.msg_db_keys:
            with cached_snapshot(self.cache, rel_key) as path:
                if not path:
                    continue
                try:
                    with closing(sqlite3.connect(path)) as conn:
                        rows = conn.execute(
                            f"SELECT name FROM sqlite_master "
                            f"WHERE type='table' AND name IN ({placeholders})",
                            table_names,
                        ).fetchall()
                except sqlite3.Error:
                    continue
            readable.update(
                table_to_username[table_name]
                for (table_name,) in rows
                if table_name in table_to_username
            )
        return readable

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
            with cached_snapshot(self.cache, rel_key) as path:
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
            for rel_key, table_name in self._message_tables_for_username(username):
                cursor_key = self._cursor_key(rel_key, table_name)
                cursor = state.cursor_for(cursor_key) or Cursor()
                yield from self._query_table(rel_key, table_name, username, names, cursor, batch_size)

    def get_chat_history(
        self,
        conversation_id: str,
        limit: int = 50,
        offset: int = 0,
        start_time: Any = "",
        end_time: Any = "",
        oldest_first: bool = False,
        message_types: list[str] | None = None,
    ) -> dict[str, Any]:
        ctx = self.conversation_context(conversation_id)
        self.require_message_tables(ctx["conversationId"])
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
        conversation_id: str = "",
        limit: int = 20,
        offset: int = 0,
        start_time: Any = "",
        end_time: Any = "",
    ) -> dict[str, Any]:
        keyword = str(keyword or "").strip()
        if not keyword:
            raise ValueError("keyword 不能为空")
        start_ts, end_ts = parse_time_range(start_time, end_time)
        if str(conversation_id or "").strip():
            ctx = self.conversation_context(conversation_id)
            self.require_message_tables(ctx["conversationId"])
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
        with cached_snapshot(self.cache, rel_key) as path:
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

    def get_chat_images(self, conversation_id: str, limit: int = 20, offset: int = 0, start_time: Any = "", end_time: Any = "") -> dict[str, Any]:
        return self.get_chat_history(
            conversation_id,
            limit=limit,
            offset=offset,
            start_time=start_time,
            end_time=end_time,
            oldest_first=False,
            message_types=["image"],
        )

    def get_voice_messages(self, conversation_id: str, limit: int = 20, offset: int = 0, start_time: Any = "", end_time: Any = "") -> dict[str, Any]:
        return self.get_chat_history(
            conversation_id,
            limit=limit,
            offset=offset,
            start_time=start_time,
            end_time=end_time,
            oldest_first=False,
            message_types=["voice"],
        )

    def conversation_context(self, conversation_id: str) -> dict[str, Any]:
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            raise ValueError("conversationId 不能为空")
        names = self.contact_names()
        return {
            "conversationId": conversation_id,
            "conversationName": names.get(conversation_id, conversation_id),
            "isGroup": "@chatroom" in conversation_id,
        }

    def require_message_tables(self, conversation_id: str) -> None:
        if not self._message_tables_for_username(conversation_id):
            raise ValueError(f"找不到会话消息表: {conversation_id}")

    def known_conversation_ids(self) -> list[str]:
        usernames = set(self.read_session_state())
        contacts, _signature = self._all_contacts()
        usernames.update(row["username"] for row in contacts)
        return sorted(username for username in usernames if username)

    def _message_tables_for_username(self, username: str) -> list[tuple[str, str]]:
        table_name = "Msg_" + hashlib.md5(username.encode()).hexdigest()
        signature = self._message_table_index_signature()
        self._load_message_tables_cache(signature)
        cached = self._message_tables_cache.get(username)
        if cached and cached[0] == signature:
            return list(cached[1])
        matches = []
        for rel_key in self.msg_db_keys:
            with cached_snapshot(self.cache, rel_key) as path:
                if not path:
                    continue
                try:
                    with closing(sqlite3.connect(path)) as conn:
                        exists = conn.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (table_name,),
                        ).fetchone()
                    if exists:
                        matches.append((rel_key, table_name))
                except sqlite3.Error:
                    continue
        self._message_tables_cache[username] = (signature, matches)
        self._save_message_tables_cache(signature)
        return matches

    def _message_table_index_signature(self) -> tuple[Any, ...]:
        source_signature = getattr(self.cache, "source_signature", None)
        if callable(source_signature):
            return tuple((rel_key, source_signature(rel_key)) for rel_key in self.msg_db_keys)
        return tuple((rel_key, source_db_file_signature(self.db_dir, rel_key)) for rel_key in self.msg_db_keys)

    def _load_message_tables_cache(self, signature: tuple[Any, ...]) -> None:
        if self._message_tables_cache:
            return
        try:
            raw = json.loads(self._message_tables_index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if decode_message_table_signature(raw.get("signature")) != signature:
            return
        users = raw.get("users")
        if not isinstance(users, dict):
            return
        loaded: dict[str, tuple[tuple[Any, ...], list[tuple[str, str]]]] = {}
        for username, entries in users.items():
            if not isinstance(entries, list):
                continue
            matches = []
            for entry in entries:
                if (
                    isinstance(entry, list)
                    and len(entry) in {2, 3}
                    and all(isinstance(value, str) for value in entry[:2])
                ):
                    matches.append((entry[0], entry[1]))
            loaded[str(username)] = (signature, matches)
        self._message_tables_cache = loaded

    def _save_message_tables_cache(self, signature: tuple[Any, ...]) -> None:
        users = {
            username: [list(entry) for entry in matches]
            for username, (cached_signature, matches) in self._message_tables_cache.items()
            if cached_signature == signature
        }
        data = {
            "signature": encode_message_table_signature(signature),
            "users": users,
        }
        tmp_path = self._message_tables_index_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_path, self._message_tables_index_path)
        except OSError:
            try:
                tmp_path.unlink()
            except OSError:
                pass

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
        for rel_key, table_name in self._message_tables_for_username(username):
            with cached_snapshot(self.cache, rel_key) as path:
                if not path:
                    continue
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

    def _query_table(self, rel_key: str, table_name: str, username: str, names: dict[str, str], cursor: Cursor, batch_size: int) -> list[MessageCandidate]:
        candidates: list[MessageCandidate] = []
        with cached_snapshot(self.cache, rel_key) as db_path:
            if not db_path:
                return candidates
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
                        candidates.append(candidate)
        return candidates

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
            "timestamp": epoch_seconds_to_millis(create_time),
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


def read_wal_snapshot(wal_path: Path) -> WalSnapshot:
    """Read the valid committed prefix of a live SQLite WAL.

    Appends after this read are intentionally ignored, matching SQLite's
    ``mxFrame`` reader model. A torn or uncommitted suffix never reaches the
    decrypted snapshot.
    """
    document: bytes | None = None
    for _attempt in range(3):
        try:
            document = wal_path.read_bytes()
            with wal_path.open("rb") as handle:
                current_header = handle.read(WAL_HEADER_SZ)
        except FileNotFoundError:
            return WalSnapshot(None, (), False)
        except OSError as exc:
            raise DatabaseSnapshotError(f"failed to read WAL snapshot: {exc}") from exc
        if len(document) <= WAL_HEADER_SZ:
            return WalSnapshot(None, (), False)
        if current_header == document[:WAL_HEADER_SZ]:
            break
    else:
        raise DatabaseSnapshotError("WAL generation changed while capturing snapshot")

    header = document[:WAL_HEADER_SZ]
    magic, version, page_size, checkpoint, salt1, salt2, stored_s0, stored_s1 = struct.unpack(">8I", header)
    if magic not in {0x377F0682, 0x377F0683}:
        raise DatabaseSnapshotError(f"unsupported SQLite WAL magic: 0x{magic:08x}")
    if page_size == 1:
        page_size = 65536
    if page_size != PAGE_SZ:
        raise DatabaseSnapshotError(f"unsupported SQLite WAL page size: {page_size}")
    checksum_order = ">" if magic == 0x377F0683 else "<"
    calculated = wal_checksum(header[:24], (0, 0), checksum_order)
    if calculated != (stored_s0, stored_s1):
        raise DatabaseSnapshotError("SQLite WAL header checksum mismatch")

    generation = (magic, version, page_size, checkpoint, salt1, salt2)
    valid_frames: list[WalFrame] = []
    last_commit = 0
    running_checksum = calculated
    frame_size = WAL_FRAME_HEADER_SZ + page_size
    offset = WAL_HEADER_SZ
    pending = False
    while offset + frame_size <= len(document):
        frame_header = document[offset : offset + WAL_FRAME_HEADER_SZ]
        encrypted_page = document[
            offset + WAL_FRAME_HEADER_SZ : offset + WAL_FRAME_HEADER_SZ + page_size
        ]
        page_number, database_pages, frame_salt1, frame_salt2, frame_s0, frame_s1 = struct.unpack(
            ">6I", frame_header
        )
        if (frame_salt1, frame_salt2) != (salt1, salt2):
            break
        if page_number == 0 or page_number > 0xFFFFFFFE:
            pending = True
            break
        calculated = wal_checksum(frame_header[:8] + encrypted_page, running_checksum, checksum_order)
        if calculated != (frame_s0, frame_s1):
            pending = True
            break
        running_checksum = calculated
        valid_frames.append(
            WalFrame(
                page_number=page_number,
                database_pages=database_pages,
                encrypted_page=encrypted_page,
                checksum=calculated,
            )
        )
        if database_pages:
            last_commit = len(valid_frames)
        offset += frame_size

    if len(valid_frames) > last_commit:
        pending = True
    if offset < len(document) and len(document) - offset < frame_size:
        pending = True
    return WalSnapshot(generation, tuple(valid_frames[:last_commit]), pending)


def wal_checksum(data: bytes, seed: tuple[int, int], byte_order: str) -> tuple[int, int]:
    if len(data) % 8:
        raise ValueError("WAL checksum input must be a multiple of 8 bytes")
    values = struct.unpack(f"{byte_order}{len(data) // 4}I", data)
    s0, s1 = seed
    for index in range(0, len(values), 2):
        s0 = (s0 + values[index] + s1) & 0xFFFFFFFF
        s1 = (s1 + values[index + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def apply_wal_snapshot(out_path: str, snapshot: WalSnapshot, enc_key: bytes) -> None:
    if not snapshot.frames:
        return
    with open(out_path, "r+b") as target:
        for frame in snapshot.frames:
            target.seek((frame.page_number - 1) * PAGE_SZ)
            target.write(decrypt_page(enc_key, frame.encrypted_page, frame.page_number))
        if snapshot.database_pages:
            target.truncate(snapshot.database_pages * PAGE_SZ)


def decrypt_wal(wal_path: str, out_path: str, enc_key: bytes) -> None:
    """Compatibility wrapper that applies only a verified committed WAL prefix."""
    apply_wal_snapshot(out_path, read_wal_snapshot(Path(wal_path)), enc_key)


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


def epoch_seconds_to_millis(timestamp: int) -> int:
    return timestamp * 1000


def parse_time_range(start_time: Any, end_time: Any) -> tuple[int | None, int | None]:
    start_ts = parse_time_value(start_time, is_end=False)
    end_ts = parse_time_value(end_time, is_end=True)
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise ValueError("startTime 不能晚于 endTime")
    return start_ts, end_ts


def parse_time_value(value: Any, is_end: bool) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("时间范围必须使用 Unix epoch 毫秒整数")
    if value < 100_000_000_000:
        raise ValueError("时间范围必须使用毫秒，不能传秒级时间戳")
    return value // 1000


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


def file_signature(path: str) -> tuple[float, int] | None:
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return stat.st_mtime, stat.st_size


def contact_snapshot_token(signature: tuple[float, int] | None) -> str:
    if signature is None:
        return "missing"
    modified_at, size = signature
    return f"{int(modified_at * 1000)}:{size}"


def source_db_file_signature(db_dir: str, rel_key: str) -> tuple[float, int] | None:
    path = Path(db_dir) / rel_key.replace("\\", os.sep).replace("/", os.sep)
    return file_signature(str(path))


def _full_disk_access_error() -> RuntimeError:
    return RuntimeError(
        "无法读取微信本地数据库。请在 macOS 系统设置 > 隐私与安全性 > "
        "完全磁盘访问中启用“百积木”，重启百积木后再手动启动 WeChat Connector。"
    )


def encode_message_table_signature(
    signature: tuple[Any, ...],
) -> list[list[Any]]:
    return [
        [rel_key, list(db_sig) if db_sig else None]
        for rel_key, db_sig in signature
    ]


def decode_message_table_signature(raw: Any) -> tuple[Any, ...] | None:
    if not isinstance(raw, list):
        return None
    decoded = []
    for item in raw:
        if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str):
            return None
        rel_key = item[0]
        sig_raw = item[1]
        if sig_raw is None:
            db_sig = None
        elif isinstance(sig_raw, list) and len(sig_raw) >= 2:
            try:
                db_sig = tuple(
                    float(value) if isinstance(value, float) else int(value)
                    for value in sig_raw
                )
            except (TypeError, ValueError):
                return None
        else:
            return None
        decoded.append((rel_key, db_sig))
    return tuple(decoded)


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
