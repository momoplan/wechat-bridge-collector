"""Durable contact differences and an at-least-once event outbox.

The baseline describes changes committed to the outbox, not completed CRM work.
It must be updated in the same transaction as the corresponding events.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

EVENT_NAME = "contactsChanged"
PROTOCOL_VERSION = "1.0.0"
MAX_BATCH_CONTACTS = 200
MAX_PAYLOAD_BYTES = 64 * 1024


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def contact_record(item: dict) -> dict:
    contact_id = item.get("username")
    if not isinstance(contact_id, str) or not contact_id.strip():
        raise ValueError("contact username must be a nonempty string")
    result = {"contactId": contact_id}
    for field in ("displayName", "nickName", "remark"):
        value = item.get(field)
        if not isinstance(value, str):
            raise ValueError(f"contact {field} must be a string")
        result[field] = value
    deleted = item.get("isDeleted", False)
    if not isinstance(deleted, bool):
        raise ValueError("contact isDeleted must be boolean")
    # Source metadata only: absence from a scan is never a deletion signal.
    result["sourceDeleted"] = deleted
    return result


class ContactSync:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(path), isolation_level=None)
        try:
            if str(path) != ":memory:":
                Path(path).chmod(0o600)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA synchronous=FULL")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f"unsupported contact sync state version: {version}")
            if version == 1:
                # Never silently reconstruct missing baseline/outbox tables.
                self.db.execute("SELECT account_id,stream_id,sequence FROM accounts LIMIT 0")
                self.db.execute("SELECT account_id,contact_id,fingerprint FROM contacts LIMIT 0")
                self.db.execute("SELECT id,event_id,payload FROM outbox LIMIT 0")
                return
            if self.db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]:
                raise ValueError("unrecognized contact sync state; original file retained")
            self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE accounts (
                    account_id TEXT PRIMARY KEY,
                    stream_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL
                );
                CREATE TABLE contacts (
                    account_id TEXT NOT NULL,
                    contact_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    PRIMARY KEY (account_id, contact_id)
                );
                CREATE TABLE outbox (
                    id INTEGER PRIMARY KEY,
                    event_id TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL
                );
                PRAGMA user_version=1;
                COMMIT;
            """)
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    def pending(self) -> dict | None:
        row = self.db.execute("SELECT event_id,payload FROM outbox ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        return {"eventId": row["event_id"], "payload": json.loads(row["payload"])}

    def acknowledge(self, event_id: str) -> None:
        # Only the oldest event can be acknowledged; failed sends retain their ID.
        self.db.execute(
            "DELETE FROM outbox WHERE id=(SELECT MIN(id) FROM outbox) AND event_id=?",
            (event_id,),
        )

    def prepare(self, snapshot: dict) -> int:
        """Atomically save a complete observation and only its changed records.

        Drain the previous outbox before scanning again, bounding pending data to
        one observation even while the network is down.
        """
        account = snapshot["account"]
        for field in ("accountId", "source", "platform"):
            if not isinstance(account.get(field), str) or not account[field].strip():
                raise ValueError(f"contact account {field} is required")
        if snapshot.get("hasMore") or snapshot.get("total") != len(snapshot["contacts"]):
            raise ValueError("incomplete contact observation")
        records = [contact_record(item) for item in snapshot["contacts"]]
        records.sort(key=lambda item: item["contactId"])
        if len({item["contactId"] for item in records}) != len(records):
            raise ValueError("duplicate contact IDs in observation")
        account_id = account["accountId"]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.pending() is not None:
                raise ValueError("pending contacts must be delivered before the next observation")
            saved = self.db.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
            stream_id = saved["stream_id"] if saved else str(uuid.uuid4())
            sequence = saved["sequence"] if saved else 0
            previous = dict(self.db.execute(
                "SELECT contact_id,fingerprint FROM contacts WHERE account_id=?", (account_id,),
            ))
            changes = []
            fingerprints = []
            for item in records:
                fingerprint = hashlib.sha256(encode(item).encode("utf-8")).hexdigest()
                if previous.get(item["contactId"]) != fingerprint:
                    changes.append(item)
                    fingerprints.append((account_id, item["contactId"], fingerprint))
            if saved and not changes:
                self.db.execute("COMMIT")
                return 0
            base = {
                "protocolVersion": PROTOCOL_VERSION,
                "accountId": account_id,
                "source": account["source"],
                "platform": account["platform"],
                "streamId": stream_id,
                "syncId": str(uuid.uuid4()),
                "mode": "delta" if saved else "initial",
                "observedContactCount": len(records),
            }
            batches: list[list[dict]] = [[]]
            # Reserve metadata space conservatively; validate exact bytes below.
            envelope_bytes = len(encode(base).encode("utf-8")) + 256
            batch_bytes = envelope_bytes
            for item in changes:
                size = len(encode(item).encode("utf-8")) + 1
                if size + envelope_bytes > MAX_PAYLOAD_BYTES:
                    raise ValueError("contact exceeds event payload size limit")
                if batches[-1] and (len(batches[-1]) >= MAX_BATCH_CONTACTS
                                    or batch_bytes + size > MAX_PAYLOAD_BYTES):
                    batches.append([])
                    batch_bytes = envelope_bytes
                batches[-1].append(item)
                batch_bytes += size
            for index, batch in enumerate(batches):
                sequence += 1
                payload = {**base, "sequence": sequence, "batchIndex": index,
                           "batchCount": len(batches), "contacts": batch}
                body = encode(payload)
                if len(body.encode("utf-8")) > MAX_PAYLOAD_BYTES:
                    raise ValueError("contact event exceeds payload size limit")
                self.db.execute("INSERT INTO outbox(event_id,payload) VALUES (?,?)",
                                (f"contacts:{stream_id}:{sequence}", body))
            self.db.executemany(
                "INSERT INTO contacts VALUES (?,?,?) ON CONFLICT(account_id,contact_id) "
                "DO UPDATE SET fingerprint=excluded.fingerprint", fingerprints,
            )
            self.db.execute(
                "INSERT INTO accounts VALUES (?,?,?) ON CONFLICT(account_id) "
                "DO UPDATE SET sequence=excluded.sequence", (account_id, stream_id, sequence),
            )
            self.db.execute("COMMIT")
            return len(batches)
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
