"""Authenticated embedded UI transfer. Only bytes chosen by the user enter the spool."""
import base64
import binascii
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from uuid import uuid4

from .selected_file_upload import MessageAttachment, UploadError, upload_opened_file


@dataclass
class Selection:
    message: MessageAttachment
    name: str
    mime: str
    size: int
    ticket: str
    file: object
    state: str = "receiving"
    result: str = ""
    received: int = 0
    last_use: float = 0.0


class SelectedUploads:
    """At most one user selection per connector. Object transfers run outside the UI request."""
    idle_seconds = 120

    def __init__(self, query, upload=upload_opened_file):
        self.query = query
        self.upload = upload
        self.lock = threading.Lock()
        self.active = None
        self.timer = None

    def begin(self, request):
        if not os.environ.get("BAIJIMU_LOCAL_APP_MODULE_ENDPOINT"):
            raise ValueError("当前宿主尚未提供云端方法调用入口")
        message_id = request.get("messageId")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("缺少消息 ID")
        row = self.query("getMessageById", {"messageId": message_id}).get("message")
        if (not isinstance(row, dict) or row.get("messageId") != message_id
                or any(not isinstance(row.get(k), str) or not row[k] or row[k] != request.get(k)
                       for k in ("accountId", "conversationId"))):
            raise ValueError("当前账号或会话与原消息不匹配")
        size, name, mime = (request.get(k) for k in ("sizeBytes", "fileName", "mimeType"))
        if (type(size) is not int or size < 0 or not isinstance(name, str) or not name
                or len(name.encode()) > 255 or "/" in name or "\\" in name or not isinstance(mime, str)
                or "/" not in mime or row.get("messageType") not in {"image", "voice", "file", "app"}):
            raise ValueError("请选择这条图片、语音或文件消息的导出原件")
        if mime == "application/octet-stream" and row["messageType"] in {"image", "voice"}:
            import mimetypes
            extension = os.path.splitext(name)[1].lower()
            mime = {".silk": "audio/silk", ".amr": "audio/amr"}.get(extension) or mimetypes.guess_type(name)[0] or mime
        expected = {"image": "image/", "voice": "audio/"}.get(row["messageType"])
        if expected and not mime.startswith(expected):
            raise ValueError("文件类型与消息不符")
        message = MessageAttachment(row["accountId"], row["conversationId"], message_id, "original")
        with self.lock:
            if self.active and self.active.state in {"receiving", "uploading"}:
                raise ValueError("已有一个原件正在上传")
            if size > shutil.disk_usage(tempfile.gettempdir()).free:
                raise ValueError("临时空间不足")
            item = Selection(message, name, mime, size, str(uuid4()), tempfile.TemporaryFile())
            item.last_use = time.monotonic()
            self.active = item
            self._arm_timer(item, self.idle_seconds)
            return self._view(item)

    def update(self, request):
        with self.lock:
            item = self.active
            if item is None or item.ticket != request.get("ticket"):
                raise ValueError("这次文件选择已结束，请重新选择")
            action = request.get("action")
            if action == "chunk" and item.state == "receiving":
                data = request.get("bytes")
                if not isinstance(data, str) or not data or len(data) > 192 * 1024:
                    raise ValueError("文件分块无效")
                try:
                    chunk = base64.b64decode(data, validate=True)
                except (ValueError, binascii.Error):
                    raise ValueError("文件分块无效") from None
                if request.get("offset") != item.received or item.received + len(chunk) > item.size:
                    raise ValueError("文件分块位置不匹配")
                item.file.write(chunk)
                item.received += len(chunk)
                item.last_use = time.monotonic()
            elif action == "finish" and item.state == "receiving":
                if item.received != item.size:
                    raise ValueError("文件尚未传输完整")
                item.file.seek(0)
                self.timer.cancel()
                item.state = "uploading"
                threading.Thread(target=self._complete, args=(item,), daemon=True).start()
            elif action == "cancel" and item.state == "receiving":
                self.timer.cancel()
                item.file.close()
                item.state = "cancelled"
            elif action not in {"status", "finish"}:
                raise ValueError("当前状态无法执行此操作")
            return self._view(item)

    @staticmethod
    def _view(item):
        info = {"ticket": item.ticket, "state": item.state, "messageId": item.message.message_id,
                "received": item.received}
        if item.state == "complete":
            info["fileId"] = item.result
        if item.state == "failed":
            info["error"] = item.result
        return info

    def _arm_timer(self, item, delay):
        self.timer = threading.Timer(delay, self._expire, args=(item,))
        self.timer.daemon = True
        self.timer.start()

    def _expire(self, item):
        with self.lock:
            if self.active is not item or item.state != "receiving":
                return
            remaining = self.idle_seconds - (time.monotonic() - item.last_use)
            if remaining > 0:
                self._arm_timer(item, remaining)
                return
            item.file.close()
            item.state = "cancelled"

    def _complete(self, item):
        try:
            file_id = self.upload(item.message, item.file, item.name, item.mime)
        except UploadError as exc:
            state, result = "failed", str(exc)
        except Exception:
            # Unknown transport or malformed receipt: never expose URLs or credentials to the UI.
            state, result = "failed", "未取得匹配的完成回执，请检查宿主连接和本包上传许可后重试"
        else:
            state, result = "complete", file_id
        finally:
            item.file.close()
        with self.lock:
            item.state, item.result = state, result
