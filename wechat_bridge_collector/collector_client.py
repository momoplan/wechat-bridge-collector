from __future__ import annotations

import http.client
import json
import re
from pathlib import Path
from urllib.parse import quote


class CollectorUnavailable(RuntimeError):
    pass


class CollectorRejected(ValueError):
    pass


class CollectorClient:
    def __init__(self, pairing_path: Path):
        try:
            if not pairing_path.is_file():
                raise ValueError
            with pairing_path.open("rb") as file:
                raw = file.read(4097)
            if len(raw) > 4096:
                raise ValueError
            document = json.loads(raw)
            if not isinstance(document, dict):
                raise ValueError
            port = document.get("port")
            token = document.get("token")
            if (type(document.get("protocolVersion")) is not int or document["protocolVersion"] != 1
                    or type(port) is not int or not 0 < port <= 65535
                    or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)):
                raise ValueError
        except (OSError, ValueError, RecursionError) as exc:
            raise CollectorUnavailable("无法读取独立采集器的配对文件") from exc
        self.port = port
        self.token = token

    def request(self, path: str, payload: dict | None = None) -> dict:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {"Authorization": f"Bearer {self.token}"}
            if payload is not None:
                headers["Content-Type"] = "application/json"
            connection.request("GET" if payload is None else "POST", path,
                               None if payload is None else json.dumps(payload), headers)
            response = connection.getresponse()
            maximum_bytes = 16 * 1024 * 1024
            raw = response.read(maximum_bytes + 1)
            if len(raw) > maximum_bytes:
                raise ValueError
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError
            code = response.status
        except (OSError, ValueError, http.client.HTTPException, RecursionError) as exc:
            raise CollectorUnavailable("独立采集器当前不可连接") from exc
        finally:
            connection.close()
        if code == 400:
            message = result.get("message")
            raise CollectorRejected(message if isinstance(message, str) else "采集器拒绝了该请求")
        if code != 200:
            raise CollectorUnavailable("独立采集器当前不可用，请检查运行及配对状态")
        return result

    def status(self) -> dict:
        result = self.request("/v1/status").get("sourceAccess")
        if not isinstance(result, dict) or result.get("status") not in ("checking", "ready", "failed"):
            raise CollectorUnavailable("独立采集器状态格式无效")
        return result

    def query(self, method: str, payload: dict) -> dict:
        result = self.request(f"/v1/query/{quote(method, safe='')}", payload).get("data")
        if not isinstance(result, dict):
            raise CollectorUnavailable("独立采集器查询结果无效")
        return result
