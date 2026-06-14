from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
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


def install_autostart(config: CollectorConfig) -> AutostartResult:
    system = platform.system().lower()
    if system == "windows":
        return _install_windows_autostart(config)
    if system == "darwin":
        return _install_macos_autostart(config)
    raise RuntimeError(f"install-autostart is not supported on {platform.system()}")


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


def _install_macos_autostart(config: CollectorConfig) -> AutostartResult:
    plist = _macos_plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    stdout_path = Path(config.state_dir).expanduser() / "collector.log"
    stderr_path = Path(config.state_dir).expanduser() / "collector.err.log"
    plist.write_text(
        _render_resource(
            "macos",
            "com.baijimu.wechat-bridge-collector.plist",
            {
                "PYTHON": sys.executable,
                "CONFIG": str(config.config_path),
                "STATE_DIR": str(Path(config.state_dir).expanduser()),
                "STDOUT": str(stdout_path),
                "STDERR": str(stderr_path),
            },
        ),
        encoding="utf-8",
    )
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)],
        text=True,
        capture_output=True,
        timeout=15,
    )
    completed = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
        text=True,
        capture_output=True,
        timeout=15,
    )
    if completed.returncode != 0:
        raise RuntimeError(_format_process_error(completed))
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.baijimu.wechat-bridge-collector"],
        text=True,
        capture_output=True,
        timeout=15,
    )
    return AutostartResult(
        status="installed",
        platform="darwin",
        launcher_path=str(plist),
        autostart_path=str(plist),
        health_url=config.method_base_url + "/health",
    )


def _start_macos(config: CollectorConfig) -> AutostartResult:
    plist = _macos_plist_path()
    if plist.exists():
        completed = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.baijimu.wechat-bridge-collector"],
            text=True,
            capture_output=True,
            timeout=15,
        )
        if completed.returncode != 0:
            raise RuntimeError(_format_process_error(completed))
        return AutostartResult(
            status="started",
            platform="darwin",
            launcher_path=str(plist),
            health_url=config.method_base_url + "/health",
        )

    stdout_path = Path(config.state_dir).expanduser() / "collector.log"
    stderr_path = Path(config.state_dir).expanduser() / "collector.err.log"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout = stdout_path.open("ab")
    stderr = stderr_path.open("ab")
    args = [sys.executable, "-u", "-m", "wechat_bridge_collector", "--config", str(config.config_path), "run"]
    subprocess.Popen(
        args,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
        close_fds=True,
    )
    return AutostartResult(
        status="started",
        platform="darwin",
        health_url=config.method_base_url + "/health",
        message="started without LaunchAgent; run install-autostart for login startup",
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
