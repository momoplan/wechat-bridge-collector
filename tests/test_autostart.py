import ctypes
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from wechat_bridge_collector.autostart import (
    _collector_process_matches,
    _process_running,
    _read_pid,
    _rotate_log,
    _terminate_windows_process_tree,
    _windows_process_running,
    install_autostart,
    start_command,
    start_collector,
    stop_collector,
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


def test_windows_process_probe_does_not_use_os_kill():
    with (
        patch("wechat_bridge_collector.autostart.platform.system", return_value="Windows"),
        patch(
            "wechat_bridge_collector.autostart._windows_process_running", return_value=False
        ) as windows_probe,
        patch("wechat_bridge_collector.autostart.os.kill") as kill,
    ):
        assert _process_running(12345) is False
    windows_probe.assert_called_once_with(12345)
    kill.assert_not_called()


def test_windows_process_probe_reads_exit_code_without_signaling():
    kernel32 = Mock()
    kernel32.OpenProcess.return_value = 123

    def set_still_active(_handle, exit_code):
        exit_code._obj.value = 259
        return True

    kernel32.GetExitCodeProcess.side_effect = set_still_active
    with patch.object(ctypes, "WinDLL", return_value=kernel32, create=True):
        assert _windows_process_running(12345) is True

    kernel32.OpenProcess.assert_called_once_with(0x1000, False, 12345)
    kernel32.CloseHandle.assert_called_once_with(123)


def test_windows_process_probe_treats_invalid_pid_as_stopped():
    kernel32 = Mock()
    kernel32.OpenProcess.return_value = 0
    with (
        patch.object(ctypes, "WinDLL", return_value=kernel32, create=True),
        patch.object(ctypes, "get_last_error", return_value=87, create=True),
    ):
        assert _windows_process_running(12345) is False


def test_non_windows_os_error_remains_visible():
    error = OSError("unexpected process lookup failure")
    with (
        patch("wechat_bridge_collector.autostart.platform.system", return_value="Linux"),
        patch("wechat_bridge_collector.autostart.os.kill", side_effect=error),
        pytest.raises(OSError, match="unexpected process lookup failure"),
    ):
        _process_running(12345)


def test_stop_removes_stale_pid_file(tmp_path: Path):
    config = CollectorConfig(state_dir=tmp_path)
    pid_path = tmp_path / "collector.pid"
    pid_path.write_text("12345", encoding="ascii")
    with (
        patch("wechat_bridge_collector.autostart.platform.system", return_value="Windows"),
        patch("wechat_bridge_collector.autostart._process_running", return_value=False),
    ):
        result = stop_collector(config)
    assert result.status == "stopped"
    assert result.message == "collector is not running; removed stale collector pid file"
    assert not pid_path.exists()


def test_windows_start_launches_python_directly_and_returns_when_healthy(tmp_path: Path):
    config = CollectorConfig(state_dir=str(tmp_path))
    process = Mock(pid=4242)
    process.poll.return_value = None
    with (
        patch("wechat_bridge_collector.autostart.platform.system", return_value="Windows"),
        patch("wechat_bridge_collector.autostart._health_ok", side_effect=[False, True]),
        patch(
            "wechat_bridge_collector.autostart.subprocess.Popen", return_value=process
        ) as popen,
    ):
        result = start_collector(config)

    args, options = popen.call_args
    assert args[0][0] == sys.executable
    assert args[0][-1] == "run"
    assert "powershell" not in " ".join(args[0]).lower()
    assert options["creationflags"] == 0x00000208
    assert options["close_fds"] is True
    assert "start_new_session" not in options
    assert (tmp_path / "collector.pid").read_text(encoding="ascii") == "4242"
    assert result.status == "started"
    assert result.platform == "windows"


def test_windows_process_match_requires_collector_run_command():
    completed = Mock(returncode=0, stdout='python.exe -u -m wechat_bridge_collector --config "x" run')
    with (
        patch("wechat_bridge_collector.autostart.platform.system", return_value="Windows"),
        patch("wechat_bridge_collector.autostart.subprocess.run", return_value=completed) as run,
    ):
        assert _collector_process_matches(4242) is True

    command = run.call_args.args[0]
    assert command[0] == "powershell.exe"
    assert "ProcessId = 4242" in command[-1]


def test_windows_process_match_rejects_unrelated_reused_pid():
    completed = Mock(returncode=0, stdout="C:\\Windows\\System32\\notepad.exe")
    with (
        patch("wechat_bridge_collector.autostart.platform.system", return_value="Windows"),
        patch("wechat_bridge_collector.autostart.subprocess.run", return_value=completed),
    ):
        assert _collector_process_matches(4242) is False


def test_windows_tree_termination_uses_exact_recorded_pid():
    completed = Mock(returncode=0, stdout="SUCCESS", stderr="")
    with (
        patch("wechat_bridge_collector.autostart.subprocess.run", return_value=completed) as run,
        patch("wechat_bridge_collector.autostart._process_running", return_value=False),
    ):
        _terminate_windows_process_tree(4242)

    assert run.call_args.args[0] == ["taskkill.exe", "/PID", "4242", "/T", "/F"]


def test_windows_stop_terminates_tree_and_waits_for_health_shutdown(tmp_path: Path):
    config = CollectorConfig(state_dir=str(tmp_path))
    pid_path = tmp_path / "collector.pid"
    pid_path.write_text("4242", encoding="ascii")
    with (
        patch("wechat_bridge_collector.autostart.platform.system", return_value="Windows"),
        patch("wechat_bridge_collector.autostart._process_running", return_value=True),
        patch("wechat_bridge_collector.autostart._collector_process_matches", return_value=True),
        patch("wechat_bridge_collector.autostart._terminate_windows_process_tree") as terminate,
        patch("wechat_bridge_collector.autostart._health_ok", return_value=False),
    ):
        result = stop_collector(config)

    terminate.assert_called_once_with(4242)
    assert result.status == "stopped"
    assert not pid_path.exists()


def test_rotate_log_keeps_bounded_backups(tmp_path: Path):
    log_path = tmp_path / "collector.err.log"
    log_path.write_text("newest", encoding="utf-8")
    log_path.with_name(log_path.name + ".1").write_text("older", encoding="utf-8")
    _rotate_log(log_path, max_bytes=1, backups=2)
    assert not log_path.exists()
    assert log_path.with_name(log_path.name + ".1").read_text(encoding="utf-8") == "newest"
    assert log_path.with_name(log_path.name + ".2").read_text(encoding="utf-8") == "older"
