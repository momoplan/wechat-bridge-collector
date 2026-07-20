from pathlib import Path
from unittest.mock import patch

import pytest

from wechat_bridge_collector.wechat_source import WeChatSource


def _source(db_dir: Path) -> WeChatSource:
    source = WeChatSource.__new__(WeChatSource)
    source.db_dir = str(db_dir)
    source.msg_db_keys = []
    return source


def test_source_access_accepts_readable_database_directory(tmp_path: Path):
    contact_dir = tmp_path / "contact"
    contact_dir.mkdir()
    (contact_dir / "contact.db").write_bytes(b"database")
    _source(tmp_path).assert_source_access()


def test_source_access_reports_full_disk_access_guidance(tmp_path: Path):
    source = _source(tmp_path)
    with patch("wechat_bridge_collector.wechat_source.os.scandir", side_effect=PermissionError):
        with pytest.raises(RuntimeError, match="完全磁盘访问"):
            source.assert_source_access()


def test_source_access_reports_protected_database_file(tmp_path: Path):
    contact_dir = tmp_path / "contact"
    contact_dir.mkdir()
    database = contact_dir / "contact.db"
    database.write_bytes(b"database")
    source = _source(tmp_path)
    original_open = Path.open

    def protected_open(path: Path, *args, **kwargs):
        if path == database:
            raise PermissionError
        return original_open(path, *args, **kwargs)

    with patch.object(Path, "open", protected_open):
        with pytest.raises(RuntimeError, match="完全磁盘访问"):
            source.assert_source_access()
