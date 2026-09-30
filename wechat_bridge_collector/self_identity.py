"""Resolve the account sender identity from its built-in self-transfer channel.

Only sender metadata is read. No message bodies, folder-name heuristics, fixed
Name2Id row numbers, persisted projections, or guessed incoming direction.
"""
from contextlib import closing
import hashlib
import sqlite3
from urllib.parse import quote

# WeChat protocol identity of the same-account phone/desktop transfer channel.
SELF_TRANSFER_USERNAME = "filehelper"
SELF_TRANSFER_TABLE = "Msg_" + hashlib.md5(SELF_TRANSFER_USERNAME.encode()).hexdigest()


def resolve_self_sender(snapshot, message_databases):
    candidates = set()
    if not message_databases:
        return None
    try:
        for key in message_databases:
            with snapshot(key) as path:
                if not path:
                    return None
                with closing(sqlite3.connect("file:" + quote(str(path)) + "?mode=ro", uri=True)) as db:
                    exists = db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (SELF_TRANSFER_TABLE,),
                    ).fetchone()
                    if not exists:
                        continue
                    rows = db.execute(
                        f"SELECT DISTINCT n.user_name FROM [{SELF_TRANSFER_TABLE}] m "
                        "LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id "
                        "WHERE (m.local_type & 4294967295)=1"
                    ).fetchall()
                    for (sender,) in rows:
                        if (not isinstance(sender, str) or not sender.strip()
                                or sender != sender.strip() or sender == SELF_TRANSFER_USERNAME
                                or "@" in sender or any(ord(c) < 32 for c in sender)):
                            return None
                        candidates.add(sender)
                        if len(candidates) != 1:
                            return None
    except (OSError, sqlite3.Error):
        return None
    return next(iter(candidates)) if len(candidates) == 1 else None
