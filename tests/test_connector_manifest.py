import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_connector_manifest_references_service_registration():
    manifest = json.loads((ROOT / "connector.json").read_text(encoding="utf-8"))
    assert manifest["schemaVersion"] == "1.0"
    assert manifest["id"] == "com.baijimu.connector.wechat"
    assert manifest["serviceRegistrationFiles"] == ["service-registration.json"]

    registration = json.loads((ROOT / "service-registration.json").read_text(encoding="utf-8"))
    assert registration["name"] == "wechatLocal"
    assert registration["transport"]["type"] == "http"
    assert registration["startCommand"] == {
        "type": "shell_command",
        "command": ["wechat-bridge-collector", "start"],
        "timeoutSecs": 20,
    }
    assert registration["events"][0]["name"] == "messageReceived"
