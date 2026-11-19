import json
import sqlite3
import urllib.request
from pathlib import Path

import pytest

from test_contacts import create_source, write_contact_db
from test_recent_sessions import StaticCache
from wechat_bridge_collector.config import CollectorConfig
from wechat_bridge_collector.query_server import QueryMethodServer, dispatch_method
from wechat_bridge_collector.wechat_source import DatabaseSnapshotError


@pytest.fixture
def directory_source(tmp_path: Path):
    contact_db = tmp_path / "contact.db"
    session_db = tmp_path / "session.db"
    write_contact_db(contact_db)
    with sqlite3.connect(session_db) as conn:
        conn.execute("CREATE TABLE SessionTable (username TEXT, last_timestamp INTEGER)")
        conn.executemany(
            "INSERT INTO SessionTable VALUES (?, ?)",
            [
                ("alice", 100), ("inactive@chatroom", 101), ("deleted@chatroom", 102),
                *((f"session-{index:03}", index + 1) for index in range(205)),
            ],
        )
    source = create_source(contact_db)
    source.cache = StaticCache({"session/session.db": session_db, "contact/contact.db": contact_db})
    source._session_state_cache = None
    return source


def test_remote_method_exposes_native_group_key_and_title(directory_source):
    server = QueryMethodServer(CollectorConfig(method_port=0), directory_source)
    server.start()
    try:
        request = urllib.request.Request(
            server.base_url + "/invoke/listSessionDirectory",
            data=json.dumps({"keyword": "护理服务", "limit": 10}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.load(response)
        assert result["errorCode"] == "0"
        assert result["data"] == {
            "items": [{"sessionKey": "named@chatroom", "title": "护理服务群"}],
            "nextCursor": None,
        }
    finally:
        server.stop()


def test_directory_paginates_over_real_sessions_and_current_groups(directory_source):
    def page(**params):
        return dispatch_method(directory_source, "listSessionDirectory", {"limit": 200, **params})

    first = page()
    second = page(cursor=first["nextCursor"])
    assert [len(first["items"]), len(second["items"])] == [200, 8]
    assert second["nextCursor"] is None
    items = first["items"] + second["items"]
    keys = [item["sessionKey"] for item in items]
    assert keys == sorted(set(keys))
    assert "inactive@chatroom" not in keys
    assert "deleted@chatroom" not in keys
    assert "bob" not in keys
    assert {"sessionKey": "alice", "title": "爱丽丝"} in items
    assert {"sessionKey": "unnamed@chatroom", "title": "爱丽丝、鲍勃"} in items
    assert {"sessionKey": "session-204"} in items
    with pytest.raises(ValueError, match="invalid session directory cursor"):
        page(keyword="another query", cursor=first["nextCursor"])
    directory_source.db_dir = "/example/different-account/db_storage"
    with pytest.raises(ValueError, match="invalid session directory cursor"):
        page(cursor=first["nextCursor"])


def test_directory_reports_unavailable_session_database(directory_source):
    directory_source.cache.paths.pop("session/session.db")
    with pytest.raises(DatabaseSnapshotError, match="session database is unavailable"):
        dispatch_method(directory_source, "listSessionDirectory", {"limit": 10})
