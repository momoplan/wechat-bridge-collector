from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ConnectorConfig:
    manifest: dict
    data_dir: Path
    management_token: str
    bridge_event_token: str
    bridge_events_url: str
    app_id: str

    @classmethod
    def load(cls) -> ConnectorConfig:
        manifest = json.loads(Path("connector.json").read_text(encoding="utf-8"))
        return cls(manifest, Path(os.environ["BAIJIMU_LOCAL_APP_DATA_DIR"]),
                   read_token("BAIJIMU_LOCAL_APP_TOKEN_FILE"),
                   read_token("BAIJIMU_LOCAL_APP_EVENT_TOKEN_FILE"),
                   os.environ["BAIJIMU_LOCAL_APP_EVENT_ENDPOINT"],
                   os.environ["BAIJIMU_LOCAL_APP_ID"])

    @property
    def port(self) -> int:
        origin = urlsplit(self.manifest["transport"]["baseUrl"])
        if origin.scheme != "http" or origin.hostname != "127.0.0.1" or not origin.port:
            raise ValueError("连接器清单必须使用有效的本机 HTTP 地址")
        return origin.port


def read_token(variable: str) -> str:
    value = Path(os.environ[variable]).read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("百积木本地应用凭证不可用")
    return value
