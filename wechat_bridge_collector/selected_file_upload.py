"""Send one explicitly chosen export using the calling installation's host session."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPSConnection
from ipaddress import ip_address
import json
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit
from uuid import uuid4


class UploadError(Exception):
    """Public failure reason without credentials, file paths or signed addresses."""


@dataclass(frozen=True)
class MessageAttachment:
    account_id: str
    conversation_id: str
    message_id: str
    attachment_key: str


def call_host(method: str, arguments: dict) -> dict:
    endpoint = urlsplit(os.environ["BAIJIMU_LOCAL_APP_MODULE_ENDPOINT"])
    if (endpoint.scheme != "http" or not endpoint.hostname or not ip_address(endpoint.hostname).is_loopback
            or endpoint.username is not None or endpoint.password is not None or endpoint.query
            or endpoint.fragment or endpoint.path != "/v1/local-app-module-calls"):
        raise UploadError("Invalid local host endpoint")
    credential = Path(os.environ["BAIJIMU_LOCAL_APP_EVENT_TOKEN_FILE"]).read_text(encoding="utf8").strip()
    request_id = str(uuid4())
    body = json.dumps({"requestId": request_id, "moduleKey": "desktop_wechat_channel",
                       "method": method, "arguments": arguments}).encode()
    with closing(HTTPConnection(endpoint.hostname, endpoint.port, timeout=70)) as host:
        host.request("POST", endpoint.path, body,
                     {"Authorization": "Bearer " + credential, "Content-Type": "application/json"})
        response = host.getresponse()
        if response.status != 200:
            raise UploadError(f"Local host returned HTTP {response.status}")
        receipt = json.load(response)
    if receipt.get("requestId") != request_id or receipt.get("success") is not True:
        raise UploadError("Module did not confirm the request")
    result = receipt.get("data")
    if not isinstance(result, dict):
        raise UploadError("Module returned an invalid receipt")
    return result


def send_signed_file(transfer: dict, opened, size: int) -> None:
    target = urlsplit(transfer["url"])
    expiry = datetime.fromisoformat(transfer["expiresAt"].replace("Z", "+00:00"))
    if (target.scheme != "https" or not target.hostname or target.username is not None
            or target.password is not None or target.fragment or transfer.get("method") != "PUT"
            or transfer.get("contractVersion") != "1.1.0"
            or expiry.tzinfo is None or expiry <= datetime.now(timezone.utc)):
        raise UploadError("Invalid or expired upload slot")
    headers = dict(transfer["headers"])
    for name in list(headers):
        key = name.lower()
        if key in {"authorization", "cookie", "proxy-authorization", "host", "transfer-encoding", "connection"}:
            raise UploadError("Unexpected upload header")
        if key == "content-length":
            if str(headers.pop(name)) != str(size):
                raise UploadError("Upload slot size mismatch")
    headers["Content-Length"] = str(size)
    path = target.path or "/"
    if target.query:
        path += "?" + target.query
    with closing(HTTPSConnection(target.hostname, target.port, timeout=60)) as storage:
        storage.putrequest("PUT", path, skip_accept_encoding=True)
        for name, value in headers.items():
            storage.putheader(name, value)
        storage.endheaders()
        remaining = size
        while remaining:
            chunk = opened.read(min(remaining, 1 << 16))
            if not chunk:
                raise UploadError("Selected file changed during upload")
            storage.send(chunk)
            remaining -= len(chunk)
        if opened.read(1) or storage.getresponse().status not in range(200, 300):
            raise UploadError("Object storage did not confirm this upload")


def upload_selected_file(message: MessageAttachment, selected_path: Path, mime: str, *,
                         invoke=call_host, send=send_signed_file) -> str:
    """Call only from a local user selection. No network request accepts a filesystem path."""
    # Nonblocking open also lets us reject special files without hanging on a FIFO.
    def open_selected(path, flags):
        return os.open(path, flags | getattr(os, "O_NONBLOCK", 0))

    with open(selected_path, "rb", opener=open_selected) as opened:
        metadata = os.fstat(opened.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise UploadError("Select a regular file")
        size = metadata.st_size
        prepared = invoke("prepareFileUpload", {
            "accountId": message.account_id, "conversationId": message.conversation_id,
            "messageId": message.message_id, "attachmentKey": message.attachment_key,
            "fileName": selected_path.name, "mimeType": mime, "sizeBytes": size,
        })
        upload_id = prepared.get("uploadId")
        if not isinstance(upload_id, str) or not upload_id:
            raise UploadError("Missing upload identifier")
        result = prepared
        if prepared.get("status") == "upload":
            send(prepared["transfer"], opened, size)
            current = os.fstat(opened.fileno())
            if (current.st_size, current.st_mtime_ns) != (size, metadata.st_mtime_ns):
                raise UploadError("Selected file changed during upload")
            result = invoke("completeFileUpload", {"uploadId": upload_id})
        if (result.get("status") != "complete" or result.get("uploadId") != upload_id
                or type(result.get("sizeBytes")) is not int or result["sizeBytes"] != size
                or result.get("mimeType") != mime or not isinstance(result.get("fileId"), str)
                or not result["fileId"]):
            raise UploadError("File receipt does not match the selected original")
        return result["fileId"]
