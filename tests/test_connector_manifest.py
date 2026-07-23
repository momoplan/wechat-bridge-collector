import json
from pathlib import Path

from wechat_bridge_collector.bridge import MESSAGE_EVENT_PAYLOAD_SCHEMA


ROOT = Path(__file__).resolve().parents[1]


def test_connector_manifest_references_service_registration():
    manifest = json.loads((ROOT / "connector.json").read_text(encoding="utf-8"))
    assert manifest["schemaVersion"] == "1.2"
    assert manifest["id"] == "com.baijimu.connector.wechat"
    assert manifest["serviceRegistrationFiles"] == ["service-registration.json"]
    assert manifest["runtime"]["command"] == "wechat-bridge-collector-python"
    assert manifest["runtime"]["startPolicy"] == "manual"
    assert manifest["version"] == "0.6.0"
    assert manifest["permissions"][0]["id"] == "macos.fullDiskAccess"
    assert manifest["legacyAutostartLabels"] == ["com.baijimu.wechat-bridge-collector"]
    assert manifest["ui"] == {
        "type": "embedded",
        "entry": "ui/index.html",
        "title": "微信消息",
        "defaultView": True,
    }
    assert manifest["management"]["auth"]["type"] == "connector_token"
    assert manifest["management"]["operations"]["getChatHistory"]["path"] == "/management/v1/chat-history"
    assert manifest["management"]["operations"]["acquireKeys"]["path"] == "/management/v1/acquire-keys"
    assert manifest["management"]["operations"]["importKeys"]["path"] == "/management/v1/import-keys"
    assert manifest["management"]["operations"]["retrySetup"]["path"] == "/management/v1/retry-setup"
    for asset in ("index.html", "app.js", "styles.css"):
        assert (ROOT / "ui" / asset).is_file()
    assert "installAutostart" not in manifest["hooks"]

    registration = json.loads((ROOT / "service-registration.json").read_text(encoding="utf-8"))
    assert registration["name"] == "wechatLocal"
    assert registration["transport"]["type"] == "http"
    assert registration["startCommand"] == {
        "type": "shell_command",
        "command": ["wechat-bridge-collector-python", "start"],
        "timeoutSecs": 20,
    }
    assert registration["stopCommand"] == {
        "type": "shell_command",
        "command": ["wechat-bridge-collector-python", "stop"],
        "timeoutSecs": 20,
    }
    assert registration["events"][0]["name"] == "messageReceived"
    assert registration["events"][0]["payload_schema"] == MESSAGE_EVENT_PAYLOAD_SCHEMA
    assert "conversationId" in registration["events"][0]["payload_schema"]["properties"]
    assert "senderName" in registration["events"][0]["payload_schema"]["properties"]
