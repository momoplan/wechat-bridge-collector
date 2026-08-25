from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import LEGACY_STATE_DIR, CollectorConfig
from .setup_keys import setup_collector
from .wechat_source import WeChatSource


class SourceNotReady(RuntimeError):
    pass


class SourceRuntime:
    """Owns the optional data source and the user-controlled setup lifecycle."""

    def __init__(
        self,
        config: CollectorConfig,
        *,
        source_factory: Callable[[CollectorConfig], WeChatSource] = WeChatSource,
        setup: Callable[..., dict[str, str]] = setup_collector,
    ):
        legacy_path = LEGACY_STATE_DIR / "all_keys.json"
        private_dir = os.environ.get("BAIJIMU_LOCAL_APP_DATA_DIR", "").strip()
        configured_state_dir = Path(config.state_dir).expanduser()
        private_path = Path(private_dir).expanduser() if private_dir else None
        host_owns_state = private_path is not None and configured_state_dir in {
            LEGACY_STATE_DIR,
            private_path,
        }
        configured_path = Path(config.keys_file).expanduser() if config.keys_file else None
        if host_owns_state and (configured_path is None or configured_path == legacy_path):
            private_keys_path = private_path / "all_keys.json"
            try:
                if not private_keys_path.is_file() and legacy_path.is_file():
                    _atomic_write_private_bytes(private_keys_path, legacy_path.read_bytes())
                config.keys_file = str(private_keys_path)
                config.save()
            except OSError:
                config.keys_file = (
                    str(legacy_path) if legacy_path.is_file() else str(private_keys_path)
                )
        elif (
            configured_state_dir == LEGACY_STATE_DIR
            and not config.keys_file
            and legacy_path.is_file()
        ):
            config.keys_file = str(legacy_path)
        self.config = config
        self._source_factory = source_factory
        self._setup = setup
        self._lock = threading.RLock()
        self._source: WeChatSource | None = None
        self._status = "keys_missing"
        self._detail = "尚未获取微信数据库密钥"
        self._checked_at = _epoch_ms()
        self._job_running = False

    def initialize(self) -> dict[str, Any]:
        keys_path = self.keys_path
        if not keys_path.is_file():
            with self._lock:
                self._source = None
                self._update_locked("keys_missing", "尚未获取微信数据库密钥")
            return self.snapshot()

        try:
            source = self._source_factory(self.config)
            source.assert_source_access()
        except Exception as exc:
            with self._lock:
                self._source = None
                self._update_locked("failed", _safe_error(exc))
            return self.snapshot()

        with self._lock:
            self._source = source
            self._update_locked("ready", "")
        return self.snapshot()

    def initialize_async(self) -> dict[str, Any]:
        return self._start_job("checking", "正在检查本地数据库和密钥", self.initialize)

    def acquire_keys(self) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            self._setup(self.config, force=True, extract_keys=True)
            return self.initialize()

        return self._start_job("acquiring", "正在从已登录的微信进程获取密钥", work)

    def retry_setup(self) -> dict[str, Any]:
        return self._start_job("checking", "正在重新检查密钥和数据库权限", self.initialize)

    def import_keys(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._job_running:
                raise ValueError("已有密钥操作正在进行，请完成后重试")
            self._job_running = True
        try:
            return self._import_keys(payload)
        finally:
            with self._lock:
                self._job_running = False

    def _import_keys(self, payload: dict[str, Any]) -> dict[str, Any]:
        document = payload.get("document")
        validate_key_document(document)
        metadata_db_dir = document.get("_db_dir") if isinstance(document, dict) else None
        if not self.config.db_dir and isinstance(metadata_db_dir, str) and Path(metadata_db_dir).expanduser().is_dir():
            self.config.db_dir = str(Path(metadata_db_dir).expanduser())
        self.config.keys_file = str(self.keys_path)
        self.config.save()
        previous = self.keys_path.read_bytes() if self.keys_path.is_file() else None
        _atomic_write_private_json(self.keys_path, document)
        result = self.initialize()
        if result["status"] != "ready" and previous is not None:
            _atomic_write_private_bytes(self.keys_path, previous)
            failed_detail = result["detail"]
            self.initialize()
            raise ValueError(f"导入的密钥无法读取微信数据库，已恢复原密钥：{failed_detail}")
        return result

    @property
    def keys_path(self) -> Path:
        return Path(self.config.keys_file).expanduser() if self.config.keys_file else self.config.default_keys_path

    def require_source(self) -> WeChatSource:
        with self._lock:
            if self._source is not None and self._status == "ready":
                return self._source
            detail = self._detail or "微信数据源尚未就绪"
        raise SourceNotReady(detail)

    def source_or_none(self) -> WeChatSource | None:
        with self._lock:
            return self._source

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self._status,
                "detail": self._detail,
                "checkedAtEpochMs": self._checked_at,
                "busy": self._job_running,
                "canAcquire": not self._job_running,
                "canImport": not self._job_running,
                "canRetry": not self._job_running,
            }

    def probe_summary(self) -> dict[str, Any]:
        with self._lock:
            source = self._source
        if source is not None:
            return {
                "db_dir": source.db_dir,
                "keys_file": source.runtime["keys_file"],
                "key_count": len(source.all_keys),
                "message_db_count": len(source.msg_db_keys),
            }
        return {
            "db_dir": self.config.db_dir or "自动检测",
            "keys_file": str(self.keys_path),
            "key_count": 0,
            "message_db_count": 0,
        }

    def _start_job(
        self,
        status: str,
        detail: str,
        target: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        with self._lock:
            if self._job_running:
                return self.snapshot()
            self._job_running = True
            self._update_locked(status, detail)

        def runner() -> None:
            try:
                target()
            except Exception as exc:
                with self._lock:
                    self._source = None
                    self._update_locked("failed", _safe_error(exc))
            finally:
                with self._lock:
                    self._job_running = False

        threading.Thread(target=runner, name="wechat-source-setup", daemon=True).start()
        return self.snapshot()

    def _update_locked(self, status: str, detail: str) -> None:
        self._status = status
        self._detail = detail
        self._checked_at = _epoch_ms()


def validate_key_document(document: Any) -> None:
    if not isinstance(document, dict):
        raise ValueError("密钥文件必须是 JSON object")
    valid_entries = 0
    for name, value in document.items():
        if name.startswith("_"):
            continue
        if not isinstance(value, dict):
            raise ValueError(f"密钥条目 {name} 必须是 JSON object")
        if value.get("plain") is True:
            valid_entries += 1
            continue
        enc_key = value.get("enc_key")
        if not isinstance(enc_key, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", enc_key):
            raise ValueError(f"密钥条目 {name} 的 enc_key 必须是 64 位十六进制字符串")
        valid_entries += 1
    if valid_entries == 0:
        raise ValueError("密钥文件中没有可用的微信数据库密钥")


def _atomic_write_private_json(path: Path, document: dict[str, Any]) -> None:
    _atomic_write_private_bytes(
        path,
        (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _atomic_write_private_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(content)
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_error(error: Exception) -> str:
    detail = str(error).strip() or error.__class__.__name__
    return detail[:2000]


def _epoch_ms() -> int:
    return int(time.time() * 1000)
