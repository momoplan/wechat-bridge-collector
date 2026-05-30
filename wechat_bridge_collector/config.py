from __future__ import annotations

import json
import os
import platform
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_STATE_DIR = Path.home() / ".wechat-bridge-collector"
DEFAULT_BRIDGE_BASE_URL = "http://127.0.0.1:18081"


@dataclass
class CollectorConfig:
    bridge_base_url: str = DEFAULT_BRIDGE_BASE_URL
    service_name: str = "wechatLocal"
    event_name: str = "messageReceived"
    poll_interval_secs: float = 2.0
    batch_size: int = 200
    state_dir: str = str(DEFAULT_STATE_DIR)
    bridge_event_token: str | None = None
    service_registration_token: str | None = None
    wechat_decrypt_dir: str | None = None
    wechat_decrypt_config: str | None = None
    db_dir: str | None = None
    keys_file: str | None = None
    decrypted_dir: str | None = None
    include_text: bool = True
    include_outgoing: bool = True

    @property
    def state_path(self) -> Path:
        return Path(self.state_dir).expanduser() / "state.json"

    @property
    def bridge_events_url(self) -> str:
        return self.bridge_base_url.rstrip("/") + "/v1/events"

    @property
    def bridge_services_url(self) -> str:
        return self.bridge_base_url.rstrip("/") + "/v1/services"

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "CollectorConfig":
        if path is None:
            path = DEFAULT_STATE_DIR / "config.json"
        path = Path(path).expanduser()
        if not path.exists():
            cfg = cls()
        else:
            raw = json.loads(path.read_text(encoding="utf-8"))
            cfg = cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})

        cfg.bridge_event_token = (
            os.environ.get("BRIDGE_AGENT_EVENT_TOKEN") or cfg.bridge_event_token
        )
        cfg.service_registration_token = (
            os.environ.get("BRIDGE_AGENT_SERVICE_REGISTRATION_TOKEN")
            or cfg.service_registration_token
        )
        cfg.wechat_decrypt_dir = (
            os.environ.get("WECHAT_DECRYPT_DIR") or cfg.wechat_decrypt_dir
        )
        return cfg

    def resolved_wechat_decrypt_dir(self) -> Path:
        candidates = []
        if self.wechat_decrypt_dir:
            candidates.append(Path(self.wechat_decrypt_dir).expanduser())
        candidates.extend(
            [
                Path.cwd() / "vendor" / "wechat-decrypt",
                Path.cwd().parent / "wechat-decrypt",
                Path.home() / "dev" / "wechat-decrypt",
            ]
        )
        for path in candidates:
            if (path / "key_utils.py").is_file():
                return path
        raise RuntimeError(
            "wechat-decrypt source directory was not found. "
            "Set WECHAT_DECRYPT_DIR or collector config `wechat_decrypt_dir` "
            "to a clone of https://github.com/ylytdeng/wechat-decrypt."
        )

    def load_wechat_decrypt_runtime(self) -> dict[str, str]:
        wd_dir = self.resolved_wechat_decrypt_dir()
        cfg_path = (
            Path(self.wechat_decrypt_config).expanduser()
            if self.wechat_decrypt_config
            else wd_dir / "config.json"
        )
        raw: dict[str, str] = {}
        if cfg_path.exists():
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))

        db_dir = self.db_dir or raw.get("db_dir")
        if not db_dir:
            db_dir = _auto_detect_db_dir()
        if not db_dir:
            raise RuntimeError(
                "WeChat db_storage directory was not configured. "
                "Run wechat-decrypt setup/main first, or set collector `db_dir`."
            )

        def resolve_path(value: str | None, default_name: str) -> str:
            value = value or raw.get(default_name) or default_name
            p = Path(value).expanduser()
            if not p.is_absolute():
                p = wd_dir / p
            return str(p)

        decrypted_dir = self.decrypted_dir or raw.get("decrypted_dir") or "decrypted"
        decrypted_path = Path(decrypted_dir).expanduser()
        if not decrypted_path.is_absolute():
            decrypted_path = wd_dir / decrypted_path

        return {
            "wechat_decrypt_dir": str(wd_dir),
            "db_dir": str(Path(db_dir).expanduser()),
            "keys_file": resolve_path(self.keys_file, "keys_file"),
            "decrypted_dir": str(decrypted_path),
        }


def _auto_detect_db_dir() -> str | None:
    system = platform.system().lower()
    if system == "darwin":
        base = Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
        pattern = "*/db_storage"
    elif system == "linux":
        base = Path.home() / "Documents/xwechat_files"
        pattern = "*/db_storage"
    elif system == "windows":
        userprofile = Path(os.environ.get("USERPROFILE", str(Path.home())))
        candidates = [
            userprofile / "Documents/xwechat_files",
            Path(os.environ.get("LOCALAPPDATA", "")) / "xwechat_files",
        ]
        matches = []
        for base in candidates:
            if base.is_dir():
                matches.extend([p for p in base.glob("*/db_storage") if p.is_dir()])
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return str(matches[0]) if matches else None
    else:
        return None

    if not base.is_dir():
        return None
    matches = [p for p in base.glob(pattern) if p.is_dir()]
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(matches[0]) if matches else None

    def save(self, path: str | os.PathLike[str] | None = None) -> Path:
        if path is None:
            path = Path(self.state_dir).expanduser() / "config.json"
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = asdict(self)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path
