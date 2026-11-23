import base64
import time
from pathlib import Path

import pytest

from wechat_bridge_collector.selected_upload_session import SelectedUploads


def test_selected_original_reaches_upload_with_host_read_message_identity(monkeypatch):
    monkeypatch.setenv("BAIJIMU_LOCAL_APP_MODULE_ENDPOINT", "fixture")
    row = {"accountId": "account", "conversationId": "group", "messageId": "original-id", "messageType": "voice"}
    received = []

    def query(method, arguments):
        assert (method, arguments) == ("getMessageById", {"messageId": row["messageId"]})
        return {"message": row}

    def store(message, opened, name, mime):
        received.append((message, opened.read(), name, mime))
        return "receipt"

    uploads = SelectedUploads(query, store)
    request = {**row, "sizeBytes": 5, "mimeType": "application/octet-stream", "fileName": "clip.silk"}
    with pytest.raises(ValueError):
        uploads.begin({**request, "accountId": "other"})
    start = uploads.begin(request)
    action = lambda kind, **args: uploads.update({"ticket": start["ticket"], "action": kind, **args})
    try:
        with pytest.raises(ValueError):
            action("finish")
        with pytest.raises(ValueError):
            action("chunk", offset=1, bytes="YQ==")
        action("chunk", offset=0, bytes=base64.b64encode(b"voice").decode())
        action("finish")
        for _ in range(100):
            result = action("status")
            if result["state"] != "uploading":
                break
            time.sleep(.01)
        assert (result["fileId"], result["messageId"]) == ("receipt", row["messageId"])
        message, data, name, mime = received[0]
        assert (message.account_id, message.conversation_id, message.message_id) == ("account", "group", "original-id")
        assert (data, name, mime) == (b"voice", "clip.silk", "audio/silk")
        assert len(received) == 1 and uploads.active.file.closed
    finally:
        uploads.timer.cancel()


def test_disabled_host_and_cancel(monkeypatch):
    row = {"messageId": "m", "accountId": "a", "conversationId": "c", "messageType": "file"}
    uploads = SelectedUploads(lambda *_: {"message": row})
    request = {**row, "fileName": "x", "sizeBytes": 0, "mimeType": "text/plain"}
    monkeypatch.delenv("BAIJIMU_LOCAL_APP_MODULE_ENDPOINT", raising=False)
    with pytest.raises(ValueError, match="宿主"):
        uploads.begin(request)
    monkeypatch.setenv("BAIJIMU_LOCAL_APP_MODULE_ENDPOINT", "fixture")
    start = uploads.begin(request)
    with pytest.raises(ValueError):
        uploads.update({"action": "cancel", "ticket": "other"})
    cancellation = uploads.update({"action": "cancel", "ticket": start["ticket"]})
    assert cancellation["state"] == "cancelled" and uploads.active.file.closed
