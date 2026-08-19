import shutil
import sqlite3
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_bridge_collector.wechat_source import (
    DBCache,
    DatabaseSnapshotError,
    read_wal_snapshot,
    wal_checksum,
)


class DBCacheTest(unittest.TestCase):
    def test_committed_wal_growth_is_applied_without_full_database_decrypt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "db_storage"
            rel = Path("message") / "message_0.db"
            source_db = db_dir / rel
            source_db.parent.mkdir(parents=True)
            connection = sqlite3.connect(source_db)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA wal_autocheckpoint=0")
                connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
                connection.execute("INSERT INTO messages(body) VALUES ('first')")
                connection.commit()

                attempts = {"count": 0}

                def fake_full_decrypt(_db_path, out_path, _enc_key):
                    attempts["count"] += 1
                    shutil.copyfile(source_db, out_path)

                cache = self._cache(root, db_dir, rel)
                with patch("wechat_bridge_collector.wechat_source.full_decrypt", fake_full_decrypt), patch(
                    "wechat_bridge_collector.wechat_source.decrypt_page",
                    lambda _key, page, _page_number: page,
                ), patch.object(
                    cache,
                    "_assert_sqlite_healthy",
                    wraps=cache._assert_sqlite_healthy,
                ) as healthy:
                    first_path = cache.get(rel.as_posix())
                    self.assertEqual(attempts["count"], 1)
                    with sqlite3.connect(first_path) as snapshot:
                        self.assertEqual(snapshot.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)

                    connection.execute("INSERT INTO messages(body) VALUES ('second')")
                    connection.commit()
                    second_path = cache.get(rel.as_posix())

                self.assertEqual(first_path, second_path)
                self.assertEqual(attempts["count"], 1)
                self.assertEqual(healthy.call_count, 1)
                with sqlite3.connect(second_path) as snapshot:
                    self.assertEqual(snapshot.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
            finally:
                connection.close()

    def test_uncommitted_and_corrupt_wal_suffix_is_not_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "source.db"
            connection = sqlite3.connect(database)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA wal_autocheckpoint=0")
                connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY)")
                connection.commit()
                wal_path = Path(str(database) + "-wal")
                committed = read_wal_snapshot(wal_path)

                original_document = wal_path.read_bytes()
                magic = struct.unpack(">I", original_document[:4])[0]
                checksum_order = ">" if magic == 0x377F0683 else "<"
                source_frame = committed.frames[-1]
                frame_prefix = struct.pack(">II", source_frame.page_number, 0)
                frame_checksum = wal_checksum(
                    frame_prefix + source_frame.encrypted_page,
                    committed.checksum,
                    checksum_order,
                )
                wal_path.write_bytes(
                    original_document
                    + frame_prefix
                    + original_document[16:24]
                    + struct.pack(">II", *frame_checksum)
                    + source_frame.encrypted_page
                )
                uncommitted = read_wal_snapshot(wal_path)
                self.assertEqual(len(uncommitted.frames), len(committed.frames))
                self.assertTrue(uncommitted.pending)

                wal_path.write_bytes(original_document)
                connection.execute("INSERT INTO messages VALUES (1)")
                connection.commit()
                committed_insert = read_wal_snapshot(wal_path)
                document = bytearray(wal_path.read_bytes())
                document[-1] ^= 0xFF
                wal_path.write_bytes(document)
                corrupt = read_wal_snapshot(wal_path)
                self.assertLess(len(corrupt.frames), len(committed_insert.frames))
                self.assertTrue(corrupt.pending)
            finally:
                connection.close()

    def test_concurrent_refresh_is_single_flight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "db_storage"
            rel = Path("message") / "message_0.db"
            source_db = db_dir / rel
            source_db.parent.mkdir(parents=True)
            self._write_sqlite_db(source_db)
            cache = self._cache(root, db_dir, rel)
            attempts = {"count": 0}
            paths: list[str | None] = []

            def fake_full_decrypt(_db_path, out_path, _enc_key):
                attempts["count"] += 1
                time.sleep(0.05)
                shutil.copyfile(source_db, out_path)

            with patch("wechat_bridge_collector.wechat_source.full_decrypt", fake_full_decrypt):
                threads = [threading.Thread(target=lambda: paths.append(cache.get(rel.as_posix()))) for _ in range(6)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            self.assertEqual(attempts["count"], 1)
            self.assertEqual(len(set(paths)), 1)

    def test_retries_until_decrypted_snapshot_is_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "db_storage"
            rel = Path("message") / "message_0.db"
            source_db = db_dir / rel
            source_db.parent.mkdir(parents=True)
            self._write_sqlite_db(source_db)

            attempts = {"count": 0}

            def fake_full_decrypt(_db_path, out_path, _enc_key):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    Path(out_path).write_bytes(b"not sqlite")
                else:
                    shutil.copyfile(source_db, out_path)

            cache = DBCache({rel.as_posix(): {"enc_key": "00" * 32}}, str(db_dir))
            cache.cache_dir = root / "cache"
            cache.cache_dir.mkdir()
            cache._metadata_path = cache.cache_dir / "_snapshots.json"

            with patch("wechat_bridge_collector.wechat_source.full_decrypt", fake_full_decrypt), patch(
                "wechat_bridge_collector.wechat_source.decrypt_wal", lambda *_args: None
            ):
                path = cache.get(rel.as_posix())

            self.assertEqual(attempts["count"], 2)
            self.assertTrue(path)
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_raises_when_snapshot_never_becomes_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "db_storage"
            rel = Path("message") / "message_0.db"
            source_db = db_dir / rel
            source_db.parent.mkdir(parents=True)
            self._write_sqlite_db(source_db)

            cache = DBCache({rel.as_posix(): {"enc_key": "00" * 32}}, str(db_dir))
            cache.cache_dir = root / "cache"
            cache.cache_dir.mkdir()
            cache._metadata_path = cache.cache_dir / "_snapshots.json"

            def fake_full_decrypt(_db_path, out_path, _enc_key):
                Path(out_path).write_bytes(b"not sqlite")

            with patch("wechat_bridge_collector.wechat_source.full_decrypt", fake_full_decrypt), patch(
                "wechat_bridge_collector.wechat_source.decrypt_wal", lambda *_args: None
            ):
                with self.assertRaises(DatabaseSnapshotError):
                    cache.get(rel.as_posix())

    @staticmethod
    def _write_sqlite_db(path: Path) -> None:
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE Msg_0123456789abcdef0123456789abcdef (local_id INTEGER)")
            conn.execute("INSERT INTO Msg_0123456789abcdef0123456789abcdef VALUES (1)")

    @staticmethod
    def _cache(root: Path, db_dir: Path, rel: Path) -> DBCache:
        cache = DBCache({rel.as_posix(): {"enc_key": "00" * 32}}, str(db_dir))
        cache.cache_dir = root / "cache"
        cache.cache_dir.mkdir()
        cache._metadata_path = cache.cache_dir / "_snapshots.json"
        cache._cache.clear()
        return cache


if __name__ == "__main__":
    unittest.main()
