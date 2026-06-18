import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_bridge_collector.wechat_source import DBCache, DatabaseSnapshotError


class DBCacheTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
