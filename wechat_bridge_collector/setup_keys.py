from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import CollectorConfig
from .key_coverage import check_key_coverage


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
    keys_path.parent.mkdir(parents=True, exist_ok=True)
    if keys_path.exists() and not force:
        check_key_coverage(cfg.db_dir, json.loads(keys_path.read_text(encoding="utf-8")))
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

    # The scanner may fail or produce a partial key ring. Never write over the
    # installed ring before verifying the complete configured account.
    with tempfile.TemporaryDirectory(prefix=".key-refresh-", dir=keys_path.parent) as staging:
        candidate_path = Path(staging) / "all_keys.json"
        extract_wechat_keys(cfg, candidate_path)
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        if not isinstance(candidate, dict):
            raise ValueError("密钥扫描结果必须是 JSON object")
        previous = json.loads(keys_path.read_text(encoding="utf-8")) if keys_path.exists() else {}
        merged = {**previous, **candidate}
        check_key_coverage(cfg.db_dir, merged)
        candidate_path.write_text(json.dumps(merged, ensure_ascii=False) + "\n", encoding="utf-8")
        if os.name != "nt":
            candidate_path.chmod(0o600)
        os.replace(candidate_path, keys_path)
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
    if not cfg.db_dir:
        raise RuntimeError("WeChat db_storage directory was not configured")

    with tempfile.TemporaryDirectory(
        prefix=".wechat-decrypt-runtime-",
        dir=output_path.parent,
    ) as runtime_dir:
        runtime_config = {
            "db_dir": str(Path(cfg.db_dir).expanduser()),
            "keys_file": str(output_path.resolve()),
            "decrypted_dir": str(Path(cfg.decrypted_dir or cfg.default_decrypted_path).expanduser()),
        }
        Path(runtime_dir, "config.json").write_text(
            json.dumps(runtime_config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["WECHAT_DECRYPT_APP_DIR"] = runtime_dir
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
    if not output_path.is_file():
        raise RuntimeError(
            "wechat-decrypt key extraction did not generate the configured all_keys.json.\n"
            + _format_extract_error(result.stdout, result.stderr)
        )


def _extract_macos_keys(cfg: CollectorConfig, output_path: Path) -> None:
    wd_dir = cfg.resolved_wechat_decrypt_dir()
    source = wd_dir / "find_all_keys_macos.c"
    if not source.is_file():
        raise RuntimeError(f"wechat-decrypt macOS scanner source not found: {source}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    binary = output_path.parent / "find_all_keys_macos"
    _compile_macos_scanner(source, binary)

    result = subprocess.run(
        [str(binary), "0", str(Path(cfg.db_dir).expanduser())],
        cwd=str(output_path.parent),
        text=True,
        capture_output=True,
        timeout=180,
    )
    combined = f"{result.stdout}\n{result.stderr}"
    if "task_for_pid" in combined:
        raise RuntimeError(
            "macOS 不允许读取微信进程（task_for_pid）。未修改签名、未重启微信、未替换已有密钥。"
            "请先由用户处理进程读取权限，或导入覆盖当前数据库的密钥文件；"
            "重新检测不会增加系统权限。"
        )
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


def _format_extract_error(stdout: str, stderr: str) -> str:
    diagnostics = _safe_extract_diagnostics(f"{stderr}\n{stdout}")
    if not diagnostics:
        return "key extraction failed; scanner returned no safe diagnostic details"
    return "key extraction failed:\n" + "\n".join(diagnostics)


def _safe_extract_diagnostics(output: str, limit: int = 20) -> list[str]:
    diagnostic = re.compile(
        r"traceback|error|exception|failed|failure|timeout|timed out|permission|denied|"
        r"not found|missing|找不到|失败|错误|异常|超时|权限|访问|未能|未获取",
        re.IGNORECASE,
    )
    sensitive_assignment = re.compile(
        r"(?i)(image_(?:aes|xor)_key\s*=\s*)\S+|(UIN=)\d+|(wxid=)[^,\s]+"
    )
    long_hex = re.compile(r"(?i)\b[0-9a-f]{16,}\b")
    safe: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or not diagnostic.search(line):
            continue
        line = sensitive_assignment.sub(
            lambda match: f"{next(group for group in match.groups() if group)}[REDACTED]",
            line,
        )
        line = long_hex.sub("[REDACTED]", line)
        safe.append(line)
    return safe[-limit:]
