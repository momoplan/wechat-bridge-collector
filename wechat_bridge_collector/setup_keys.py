from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import CollectorConfig


def setup_collector(cfg: CollectorConfig, *, force: bool = False, extract_keys: bool = True) -> dict[str, str]:
    state_dir = Path(cfg.state_dir).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)

    if not cfg.db_dir:
        runtime = cfg.load_wechat_decrypt_runtime()
        cfg.db_dir = runtime["db_dir"]
    if not cfg.keys_file:
        cfg.keys_file = str(cfg.default_keys_path)
    if not cfg.decrypted_dir:
        cfg.decrypted_dir = str(cfg.default_decrypted_path)

    cfg.save()

    keys_path = Path(cfg.keys_file).expanduser()
    if keys_path.exists() and not force:
        return {
            "status": "ready",
            "config_path": str(cfg.config_path),
            "keys_file": str(keys_path),
            "db_dir": cfg.db_dir,
        }

    if not extract_keys:
        return {
            "status": "config_written",
            "config_path": str(cfg.config_path),
            "keys_file": str(keys_path),
            "db_dir": cfg.db_dir,
        }

    extract_wechat_keys(cfg, keys_path)
    return {
        "status": "keys_extracted",
        "config_path": str(cfg.config_path),
        "keys_file": str(keys_path),
        "db_dir": cfg.db_dir,
    }


def extract_wechat_keys(cfg: CollectorConfig, output_path: Path) -> None:
    system = os.uname().sysname.lower() if hasattr(os, "uname") else ""
    if system == "darwin":
        _extract_macos_keys(cfg, output_path)
        return

    wd_dir = cfg.resolved_wechat_decrypt_dir()
    script = wd_dir / "find_all_keys.py"
    if not script.is_file():
        raise RuntimeError(f"wechat-decrypt key extraction script not found: {script}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["WECHAT_DECRYPT_APP_DIR"] = str(wd_dir)
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(output_path.parent),
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(_format_extract_error(result.stdout, result.stderr))


def _extract_macos_keys(cfg: CollectorConfig, output_path: Path) -> None:
    wd_dir = cfg.resolved_wechat_decrypt_dir()
    source = wd_dir / "find_all_keys_macos.c"
    if not source.is_file():
        raise RuntimeError(f"wechat-decrypt macOS scanner source not found: {source}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    binary = output_path.parent / "find_all_keys_macos"
    _compile_macos_scanner(source, binary)

    result = subprocess.run(
        [str(binary)],
        cwd=str(output_path.parent),
        text=True,
        capture_output=True,
        timeout=180,
    )
    combined = f"{result.stdout}\n{result.stderr}"
    if "task_for_pid" in combined:
        _resign_wechat_or_raise(combined)
    if result.returncode != 0:
        raise RuntimeError(_format_extract_error(result.stdout, result.stderr))

    generated = output_path.parent / "all_keys.json"
    if not generated.is_file():
        raise RuntimeError(
            "wechat-decrypt macOS scanner did not generate all_keys.json.\n"
            + _format_extract_error(result.stdout, result.stderr)
        )
    if generated != output_path:
        generated.replace(output_path)


def _compile_macos_scanner(source: Path, binary: Path) -> None:
    result = subprocess.run(
        ["cc", "-O2", "-o", str(binary), str(source), "-framework", "Foundation"],
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(_format_extract_error(result.stdout, result.stderr))
    subprocess.run(["codesign", "-s", "-", str(binary)], text=True, capture_output=True, timeout=30)


def _resign_wechat_or_raise(previous_output: str) -> None:
    app_path = _find_wechat_app()
    if not app_path:
        raise RuntimeError(
            "macOS blocked task_for_pid and WeChat.app was not found.\n"
            "Install WeChat, then run: sudo wechat-bridge-collector setup --force"
        )
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise RuntimeError(
            "macOS blocked task_for_pid. Run setup with administrator privileges:\n"
            "  sudo wechat-bridge-collector setup --force\n\n"
            + previous_output
        )

    entitlements = _read_entitlements(app_path)
    entitlements["com.apple.security.get-task-allow"] = True
    fd, ent_path = tempfile.mkstemp(suffix=".plist")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(plistlib.dumps(entitlements, fmt=plistlib.FMT_XML))
        result = subprocess.run(
            ["codesign", "--force", "--sign", "-", "--entitlements", ent_path, str(app_path)],
            text=True,
            capture_output=True,
            timeout=90,
        )
    finally:
        Path(ent_path).unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(
            "Failed to re-sign WeChat while preserving entitlements.\n"
            + _format_extract_error(result.stdout, result.stderr)
        )
    raise RuntimeError(
        "WeChat was re-signed with get-task-allow. Fully quit and reopen WeChat, "
        "then run: sudo wechat-bridge-collector setup --force"
    )


def _find_wechat_app() -> Path | None:
    for candidate in (Path.home() / "Applications/WeChat.app", Path("/Applications/WeChat.app")):
        if candidate.is_dir():
            return candidate
    return None


def _read_entitlements(app_path: Path) -> dict:
    result = subprocess.run(
        ["codesign", "-d", "--entitlements", ":-", str(app_path)],
        text=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode == 0 and result.stdout:
        try:
            return plistlib.loads(result.stdout)
        except Exception:
            return {}
    return {}


def _format_extract_error(stdout: str, stderr: str) -> str:
    parts = []
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    if stderr:
        parts.append(f"stderr:\n{stderr}")
    return "\n".join(parts) or "key extraction failed"
