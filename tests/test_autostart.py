from pathlib import Path
from unittest.mock import patch

import pytest

from wechat_bridge_collector.autostart import (
    _read_pid,
    _rotate_log,
    install_autostart,
    start_command,
    stop_command,
)
from wechat_bridge_collector.config import CollectorConfig


def test_service_commands_use_python_module_and_include_stop():
    assert start_command()["command"][-1] == "start"
    assert stop_command()["command"][-1] == "stop"
    assert "wechat_bridge_collector" in start_command()["command"]


def test_install_autostart_is_removed():
    with pytest.raises(RuntimeError, match="removed"):
        install_autostart(CollectorConfig())


def test_read_pid_rejects_stale_process(tmp_path: Path):
    pid_path = tmp_path / "collector.pid"
    pid_path.write_text("12345", encoding="ascii")
    with patch("wechat_bridge_collector.autostart._process_running", return_value=False):
        assert _read_pid(pid_path) is None


def test_rotate_log_keeps_bounded_backups(tmp_path: Path):
    log_path = tmp_path / "collector.err.log"
    log_path.write_text("newest", encoding="utf-8")
    log_path.with_name(log_path.name + ".1").write_text("older", encoding="utf-8")
    _rotate_log(log_path, max_bytes=1, backups=2)
    assert not log_path.exists()
    assert log_path.with_name(log_path.name + ".1").read_text(encoding="utf-8") == "newest"
    assert log_path.with_name(log_path.name + ".2").read_text(encoding="utf-8") == "older"
