from __future__ import annotations

import http.client
import json
import re
from urllib.parse import quote


class CollectorUnavailable(RuntimeError):
    pass


class CollectorRejected(ValueError):
    pass


class CollectorClient:
    def __init__(self, address: str, token: str):
        address = address.strip() if isinstance(address, str) else ""
        match = re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):([0-9]{1,5})/?", address, re.IGNORECASE)
        if not match or not 0 < int(match[1]) <= 65535:
            raise ValueError("请输入采集器的本机 HTTP 地址并包含端口")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise ValueError("采集器访问凭证格式无效")
        self.port = int(match[1])
        self.address = f"http://127.0.0.1:{self.port}"
        self.token = token

    def request(self, path: str, payload: dict | None = None, *, authenticated: bool = True) -> dict:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {"Authorization": f"Bearer {self.token}"} if authenticated else {}
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
        if not authenticated and code == 401:
            raise CollectorRejected("配对码无效、已使用或已过期")
        if code == 400:
            message = result.get("message")
            raise CollectorRejected(message if isinstance(message, str) else "采集器拒绝了该请求")
        if code != 200:
            raise CollectorUnavailable("独立采集器当前不可用，请检查运行及配对状态")
        return result

    def pair(self, code: str) -> dict:
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12}", code.strip()):
            raise ValueError("请输入采集器显示的一次性配对码")
        try:
            response = self.request("/v1/pair", {"code": code.strip(), "token": self.token},
                                    authenticated=False)
        except CollectorUnavailable as error:
            # The server may have recorded the credential before the response was lost.
            try:
                return self.status()
            except CollectorUnavailable:
                raise error from None
        if response.get("protocolVersion") != 1:
            raise CollectorUnavailable("采集器配对协议版本不受支持")
        return self.status()

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
