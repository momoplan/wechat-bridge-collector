from __future__ import annotations

import json
import os
import platform
import signal
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path

from .config import CollectorConfig


@dataclass
class AutostartResult:
    status: str
    platform: str
    launcher_path: str | None = None
    autostart_path: str | None = None
    health_url: str | None = None
    message: str | None = None


def start_command() -> dict[str, object]:
    return {
        "type": "shell_command",
        "command": [sys.executable, "-m", "wechat_bridge_collector", "start"],
        "timeoutSecs": 20,
    }


def stop_command() -> dict[str, object]:
    return {
        "type": "shell_command",
        "command": [sys.executable, "-m", "wechat_bridge_collector", "stop"],
        "timeoutSecs": 20,
    }


def install_autostart(config: CollectorConfig) -> AutostartResult:
    del config
    raise RuntimeError(
        "install-autostart has been removed. Start this connector from Baijimu so macOS "
        "attributes protected-data access to the signed Baijimu application."
    )


def start_collector(config: CollectorConfig) -> AutostartResult:
    system = platform.system().lower()
    if system == "windows":
        launcher = _write_windows_launcher(config)
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(launcher),
                "-Config",
                str(config.config_path),
                "-HealthUrl",
                config.method_base_url + "/health",
            ],
            text=True,
            capture_output=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise RuntimeError(_format_process_error(completed))
        return AutostartResult(
            status="started",
            platform=system,
            launcher_path=str(launcher),
            health_url=config.method_base_url + "/health",
            message=completed.stdout.strip() or None,
        )
    if system == "darwin":
        return _start_macos(config)
    raise RuntimeError(f"start is not supported on {platform.system()}")


def stop_collector(config: CollectorConfig) -> AutostartResult:
    system = platform.system().lower()
    if system == "darwin":
        _remove_legacy_macos_autostart()
    pid_path = _pid_path(config)
    pid = _read_pid(pid_path)
    if pid is None:
        return AutostartResult(
            status="stopped",
            platform=system,
            health_url=config.method_base_url + "/health",
            message="collector is not running",
        )
    if not _collector_process_matches(pid):
        pid_path.unlink(missing_ok=True)
        return AutostartResult(
            status="stopped",
            platform=system,
            health_url=config.method_base_url + "/health",
            message="removed stale collector pid file",
        )

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 8
    while _process_running(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _process_running(pid):
        os.kill(pid, signal.SIGKILL)
    pid_path.unlink(missing_ok=True)
    return AutostartResult(
        status="stopped",
        platform=system,
        health_url=config.method_base_url + "/health",
    )


def status(config: CollectorConfig) -> AutostartResult:
    system = platform.system().lower()
    health_url = config.method_base_url + "/health"
    ok = _health_ok(health_url)
    return AutostartResult(
        status="running" if ok else "stopped",
        platform=system,
        health_url=health_url,
    )


def _install_windows_autostart(config: CollectorConfig) -> AutostartResult:
    launcher = _write_windows_launcher(config)
    startup_dir = (
        Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )
    startup_dir.mkdir(parents=True, exist_ok=True)
    startup_cmd = startup_dir / "BaijimuWeChatCollector.cmd"
    startup_cmd.write_text(
        "@echo off\r\n"
        f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{launcher}\" "
        f"-Config \"{config.config_path}\" -HealthUrl \"{config.method_base_url}/health\"\r\n",
        encoding="ascii",
    )
    return AutostartResult(
        status="installed",
        platform="windows",
        launcher_path=str(launcher),
        autostart_path=str(startup_cmd),
        health_url=config.method_base_url + "/health",
    )


def _start_macos(config: CollectorConfig) -> AutostartResult:
    _remove_legacy_macos_autostart()
    if _health_ok(config.method_base_url + "/health"):
        return AutostartResult(
            status="running",
            platform="darwin",
            health_url=config.method_base_url + "/health",
            message="collector is already healthy",
        )

    stdout_path = Path(config.state_dir).expanduser() / "collector.log"
    stderr_path = Path(config.state_dir).expanduser() / "collector.err.log"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_log(stdout_path)
    _rotate_log(stderr_path)
    stdout = stdout_path.open("ab")
    stderr = stderr_path.open("ab")
    args = [sys.executable, "-u", "-m", "wechat_bridge_collector", "--config", str(config.config_path), "run"]
    env = dict(os.environ)
    existing_pythonpath = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(_package_root()), existing_pythonpath) if part
    )
    process = subprocess.Popen(
        args,
        stdout=stdout,
        stderr=stderr,
        env=env,
        start_new_session=True,
        close_fds=True,
    )
    stdout.close()
    stderr.close()
    _write_pid(_pid_path(config), process.pid)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _health_ok(config.method_base_url + "/health"):
            return AutostartResult(
                status="started",
                platform="darwin",
                health_url=config.method_base_url + "/health",
                message="started by the Baijimu permission host without LaunchAgent",
            )
        exit_code = process.poll()
        if exit_code is not None:
            _pid_path(config).unlink(missing_ok=True)
            detail = _tail_text(stderr_path)
            raise RuntimeError(
                f"collector exited before becoming healthy (exit {exit_code}): {detail}"
            )
        time.sleep(0.25)
    return AutostartResult(
        status="started",
        platform="darwin",
        health_url=config.method_base_url + "/health",
        message="collector start issued; health check is not ready yet",
    )


def _write_windows_launcher(config: CollectorConfig) -> Path:
    state_dir = Path(config.state_dir).expanduser()
    launcher_dir = state_dir / "launchers" / "windows"
    launcher_dir.mkdir(parents=True, exist_ok=True)
    launcher = launcher_dir / "start-collector.ps1"
    launcher.write_text(
        _render_resource(
            "windows",
            "start-collector.ps1",
            {
                "PYTHON": sys.executable,
                "STATE_DIR": str(state_dir),
            },
        ),
        encoding="utf-8",
    )
    return launcher


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.baijimu.wechat-bridge-collector.plist"


def _package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _render_resource(platform_dir: str, name: str, values: dict[str, str]) -> str:
    template = (
        resources.files(__package__)
        .joinpath("scripts")
        .joinpath(platform_dir)
        .joinpath(name)
        .read_text(encoding="utf-8")
    )
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def _remove_legacy_macos_autostart() -> None:
    plist = _macos_plist_path()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/com.baijimu.wechat-bridge-collector"],
        text=True,
        capture_output=True,
        timeout=15,
    )
    plist.unlink(missing_ok=True)


def _pid_path(config: CollectorConfig) -> Path:
    return Path(config.state_dir).expanduser() / "collector.pid"


def _read_pid(path: Path) -> int | None:
    try:
        pid = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 1 and _process_running(pid) else None


def _write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(str(pid), encoding="ascii")
    os.replace(tmp, path)


def _process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _collector_process_matches(pid: int) -> bool:
    if platform.system().lower() != "darwin":
        return _process_running(pid)
    completed = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        text=True,
        capture_output=True,
        timeout=5,
    )
    return completed.returncode == 0 and "wechat_bridge_collector" in completed.stdout


def _rotate_log(path: Path, max_bytes: int = 5 * 1024 * 1024, backups: int = 3) -> None:
    try:
        if path.stat().st_size < max_bytes:
            return
    except FileNotFoundError:
        return
    for index in range(backups, 0, -1):
        source = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
        target = path.with_name(f"{path.name}.{index}")
        if source.exists():
            os.replace(source, target)


def _tail_text(path: Path, limit: int = 4096) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return "no collector error output"


def _health_ok(url: str) -> bool:
    if not shutil.which("curl"):
        return False
    completed = subprocess.run(
        ["curl", "-fsS", url],
        text=True,
        capture_output=True,
        timeout=5,
    )
    return completed.returncode == 0


def _format_process_error(completed: subprocess.CompletedProcess[str]) -> str:
    parts = [f"command failed with exit code {completed.returncode}"]
    if completed.stdout:
        parts.append(f"stdout:\n{completed.stdout}")
    if completed.stderr:
        parts.append(f"stderr:\n{completed.stderr}")
    return "\n".join(parts)


def result_json(result: AutostartResult) -> str:
    return json.dumps(asdict(result), ensure_ascii=False, indent=2)
