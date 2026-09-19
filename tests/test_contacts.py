import sqlite3
from pathlib import Path

from wechat_bridge_collector.wechat_source import WeChatSource


class StaticCache:
    def __init__(self, contact_db: Path):
        self.contact_db = contact_db

    def get(self, rel_key: str) -> str | None:
        return str(self.contact_db) if rel_key == "contact/contact.db" else None


def create_source(contact_db: Path, account_id: str = "current-user") -> WeChatSource:
    source = object.__new__(WeChatSource)
    source.cache = StaticCache(contact_db)
    source.db_dir = str(contact_db.parent / account_id / "db_storage")
    source._contacts_cache = None
    source._contact_names_cache = None
    return source


def write_contact_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE contact(
                id INTEGER PRIMARY KEY,
                username TEXT,
                nick_name TEXT,
                remark TEXT,
                delete_flag INTEGER,
                is_in_chat_room INTEGER
            );
            CREATE TABLE chat_room(id INTEGER PRIMARY KEY, username TEXT);
            CREATE TABLE chatroom_member(room_id INTEGER, member_id INTEGER);

            INSERT INTO contact VALUES (1, 'current-user', '当前用户', '', 0, 0);
            INSERT INTO contact VALUES (2, 'alice', '爱丽丝', '', 0, 0);
            INSERT INTO contact VALUES (3, 'bob', '鲍勃', '', 0, 0);
            INSERT INTO contact VALUES (10, 'unnamed@chatroom', '', '', 0, 1);
            INSERT INTO contact VALUES (11, 'named@chatroom', '护理服务群', '', 0, 1);
            INSERT INTO contact VALUES (12, 'inactive@chatroom', '', '', 0, 0);
            INSERT INTO contact VALUES (13, 'deleted@chatroom', '', '', 1, 1);

            INSERT INTO chat_room VALUES (10, 'unnamed@chatroom');
            INSERT INTO chat_room VALUES (11, 'named@chatroom');
            INSERT INTO chat_room VALUES (12, 'inactive@chatroom');
            INSERT INTO chat_room VALUES (13, 'deleted@chatroom');

            INSERT INTO chatroom_member VALUES (10, 1);
            INSERT INTO chatroom_member VALUES (10, 2);
            INSERT INTO chatroom_member VALUES (10, 3);
            INSERT INTO chatroom_member VALUES (11, 1);
            INSERT INTO chatroom_member VALUES (11, 2);
            INSERT INTO chatroom_member VALUES (12, 1);
            INSERT INTO chatroom_member VALUES (12, 3);
            INSERT INTO chatroom_member VALUES (13, 2);
            """
        )


def test_unnamed_groups_use_member_names_and_report_name_source(tmp_path: Path):
    contact_db = tmp_path / "contact.db"
    write_contact_db(contact_db)
    source = create_source(contact_db)

    groups = source.contacts(query="@chatroom", limit=20)

    assert {item["username"] for item in groups} == {
        "unnamed@chatroom",
        "named@chatroom",
    }
    by_username = {item["username"]: item for item in groups}
    unnamed = by_username["unnamed@chatroom"]
    assert unnamed["displayName"] == "爱丽丝、鲍勃"
    assert unnamed["displayNameSource"] == "members"
    assert unnamed["memberCount"] == 3
    named = by_username["named@chatroom"]
    assert named["displayName"] == "护理服务群"
    assert named["displayNameSource"] == "nickname"


def test_group_discovery_excludes_inactive_and_deleted_rooms_and_paginates(tmp_path: Path):
    contact_db = tmp_path / "contact.db"
    write_contact_db(contact_db)
    source = create_source(contact_db)

    first_page = source.contacts(query="@chatroom", limit=1, offset=0)
    second_page = source.contacts(query="@chatroom", limit=1, offset=1)

    assert len(first_page) == 1
    assert len(second_page) == 1
    assert first_page[0]["username"] != second_page[0]["username"]
    assert all(item["username"] != "inactive@chatroom" for item in first_page + second_page)
    assert all(item["username"] != "deleted@chatroom" for item in first_page + second_page)


def test_contact_schema_without_group_metadata_remains_readable(tmp_path: Path):
    contact_db = tmp_path / "legacy-contact.db"
    with sqlite3.connect(contact_db) as conn:
        conn.execute("CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT)")
        conn.execute("INSERT INTO contact VALUES ('legacy@chatroom', '', '')")
    source = create_source(contact_db)

    assert source.contacts(query="@chatroom", limit=20) == [
        {
            "username": "legacy@chatroom",
            "displayName": "legacy@chatroom",
            "displayNameSource": "username",
            "nickName": "",
            "remark": "",
            "isGroup": True,
            "isCurrentMember": True,
            "isDeleted": False,
            "memberCount": 0,
        }
    ]
