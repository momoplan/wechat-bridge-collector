from __future__ import annotations

import json
import os
import platform
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


LEGACY_STATE_DIR = Path.home() / ".wechat-bridge-collector"
DEFAULT_BRIDGE_BASE_URL = "http://127.0.0.1:18081"
DEFAULT_KEYS_FILE_NAME = "all_keys.json"
DEFAULT_DECRYPTED_DIR_NAME = "decrypted"
CONNECTOR_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "connector.json"
PACKAGED_WECHAT_DECRYPT_DIR = Path(__file__).resolve().parents[1] / "vendor" / "wechat-decrypt"


def default_state_dir() -> Path:
    value = os.environ.get("BAIJIMU_LOCAL_APP_DATA_DIR", "").strip()
    return Path(value).expanduser() if value else LEGACY_STATE_DIR


def registered_app_id() -> str:
    value = os.environ.get("BAIJIMU_LOCAL_APP_ID", "").strip()
    if value:
        return value
    try:
        value = str(
            json.loads(CONNECTOR_MANIFEST_PATH.read_text(encoding="utf-8"))["appId"]
        ).strip()
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("无法从宿主环境或 connector.json 解析本地应用 appId") from exc
    if not value:
        raise RuntimeError("connector.json appId 不能为空")
    return value


@dataclass
class CollectorConfig:
    bridge_base_url: str = DEFAULT_BRIDGE_BASE_URL
    event_name: str = "messageReceived"
    poll_interval_secs: float = 2.0
    batch_size: int = 200
    state_dir: str = field(default_factory=lambda: str(default_state_dir()))
    method_host: str = "127.0.0.1"
    method_port: int = 18082
    bridge_event_token: str | None = None
    wechat_decrypt_dir: str | None = None
    wechat_decrypt_config: str | None = None
    db_dir: str | None = None
    keys_file: str | None = None
    decrypted_dir: str | None = None
    include_text: bool = True
    include_outgoing: bool = True

    @property
    def app_id(self) -> str:
        return registered_app_id()

    @property
    def state_path(self) -> Path:
        return Path(self.state_dir).expanduser() / "state.json"

    @property
    def config_path(self) -> Path:
        return Path(self.state_dir).expanduser() / "config.json"

    @property
    def default_keys_path(self) -> Path:
        return Path(self.state_dir).expanduser() / DEFAULT_KEYS_FILE_NAME

    @property
    def default_decrypted_path(self) -> Path:
        return Path(self.state_dir).expanduser() / DEFAULT_DECRYPTED_DIR_NAME

    @property
    def bridge_events_url(self) -> str:
        return os.environ.get(
            "BAIJIMU_LOCAL_APP_EVENT_ENDPOINT",
            self.bridge_base_url.rstrip("/") + "/v1/local-app-events",
        )

    @property
    def method_base_url(self) -> str:
        return f"http://{self.method_host}:{int(self.method_port)}"

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "CollectorConfig":
        explicit_path = path is not None
        if path is None:
            path = default_state_dir() / "config.json"
        path = Path(path).expanduser()
        source_path = path
        legacy_config_path = LEGACY_STATE_DIR / "config.json"
        if (
            not explicit_path
            and not source_path.exists()
            and source_path != legacy_config_path
            and legacy_config_path.is_file()
        ):
            source_path = legacy_config_path
        if not source_path.exists():
            cfg = cls()
        else:
            raw = json.loads(source_path.read_text(encoding="utf-8"))
            cfg = cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})

        if not explicit_path:
            cfg.state_dir = str(default_state_dir())

        if token_file := os.environ.get("BAIJIMU_LOCAL_APP_EVENT_TOKEN_FILE"):
            try:
                cfg.bridge_event_token = Path(token_file).read_text(encoding="utf-8").strip()
            except OSError:
                pass
        cfg.wechat_decrypt_dir = (
            os.environ.get("WECHAT_DECRYPT_DIR") or cfg.wechat_decrypt_dir
        )
        if source_path != path:
            cfg.save(path)
        return cfg

    def resolved_wechat_decrypt_dir(self) -> Path:
        candidates = []
        if self.wechat_decrypt_dir:
            candidates.append(Path(self.wechat_decrypt_dir).expanduser())
        candidates.extend(
            [
                PACKAGED_WECHAT_DECRYPT_DIR,
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
            "Install or upgrade the complete WeChat Connector package, or set "
            "WECHAT_DECRYPT_DIR / collector config `wechat_decrypt_dir` "
            "to an explicit source directory."
        )

    def load_wechat_decrypt_runtime(self) -> dict[str, str]:
        wd_dir = self.resolved_wechat_decrypt_dir()
        raw: dict[str, str] = {}
        if self.wechat_decrypt_config:
            cfg_path = Path(self.wechat_decrypt_config).expanduser()
            if cfg_path.exists():
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))

        db_dir = self.db_dir or raw.get("db_dir")
        if not db_dir:
            db_dir = _auto_detect_db_dir()
        if not db_dir:
            raise RuntimeError(
                "WeChat db_storage directory was not configured. "
                "Run `wechat-bridge-collector setup`, or set collector `db_dir`."
            )

        def resolve_path(value: str | None, default_path: Path) -> str:
            value = value or str(default_path)
            p = Path(value).expanduser()
            if not p.is_absolute():
                p = Path(self.state_dir).expanduser() / p
            return str(p)

        return {
            "wechat_decrypt_dir": str(wd_dir),
            "db_dir": str(Path(db_dir).expanduser()),
            "keys_file": resolve_path(self.keys_file or raw.get("keys_file"), self.default_keys_path),
            "decrypted_dir": resolve_path(
                self.decrypted_dir or raw.get("decrypted_dir"),
                self.default_decrypted_path,
            ),
        }

    def save(self, path: str | os.PathLike[str] | None = None) -> Path:
        if path is None:
            path = Path(self.state_dir).expanduser() / "config.json"
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = asdict(self)
        data.pop("bridge_event_token", None)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path


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
