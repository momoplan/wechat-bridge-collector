import hashlib
import sqlite3
from pathlib import Path

from wechat_bridge_collector.state import CollectorState
from wechat_bridge_collector.wechat_source import WeChatSource


class StaticCache:
    def __init__(self, paths: dict[str, Path]):
        self.paths = paths

    def get(self, rel_key: str) -> str | None:
        path = self.paths.get(rel_key)
        return str(path) if path else None


def create_source(session_db: Path, message_db: Path) -> WeChatSource:
    source = object.__new__(WeChatSource)
    source.cache = StaticCache(
        {
            "session/session.db": session_db,
            "message/message_0.db": message_db,
        }
    )
    source.msg_db_keys = ["message/message_0.db"]
    source.contact_names = lambda: {
        "readable@chatroom": "可读群聊",
        "brandsessionholder": "brandsessionholder",
    }
    return source


def write_session_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE SessionTable (
                username TEXT,
                unread_count INTEGER,
                summary TEXT,
                last_timestamp INTEGER,
                last_msg_type INTEGER,
                last_msg_sender TEXT,
                last_sender_display_name TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO SessionTable VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("readable@chatroom", 2, "群聊摘要", 20, 1, "sender", "发送者"),
                ("brandsessionholder", 3, "系统聚合摘要", 10, 1, "", ""),
            ],
        )


def write_message_db(path: Path) -> None:
    table_name = "Msg_" + hashlib.md5(b"readable@chatroom").hexdigest()
    with sqlite3.connect(path) as conn:
        conn.execute(f"CREATE TABLE [{table_name}] (local_id INTEGER)")


def test_recent_sessions_returns_summaries_without_opening_message_databases(tmp_path: Path):
    session_db = tmp_path / "session.db"
    write_session_db(session_db)
    source = create_source(session_db, tmp_path / "unavailable-message.db")

    class SessionOnlyCache(StaticCache):
        def get(self, rel_key):
            assert rel_key == "session/session.db", "session list must not read message shards"
            return super().get(rel_key)

    source.cache = SessionOnlyCache(source.cache.paths)
    sessions = source.recent_sessions(limit=20)

    assert [session["conversationId"] for session in sessions] == [
        "readable@chatroom",
        "brandsessionholder",
    ]
    assert sessions[0]["summary"] == "群聊摘要"
    assert sessions[1]["summary"] == "系统聚合摘要"
    assert all("historyAvailable" not in session for session in sessions)


def test_unchanged_session_poll_never_opens_message_database(tmp_path: Path):
    session_db = tmp_path / "session.db"
    message_db = tmp_path / "message.db"
    write_session_db(session_db)
    write_message_db(message_db)

    class TrackingCache(StaticCache):
        def __init__(self, paths):
            super().__init__(paths)
            self.accessed = []

        def get(self, rel_key):
            self.accessed.append(rel_key)
            return super().get(rel_key)

    source = create_source(session_db, message_db)
    source.cache = TrackingCache(source.cache.paths)
    source._session_state_cache = None
    state = CollectorState()
    state.sessions = {"readable@chatroom": 20, "brandsessionholder": 10}

    current, changed = source.changed_usernames(state)

    assert current == state.sessions
    assert changed == []
    assert source.cache.accessed == ["session/session.db"]
