import json
from pathlib import Path

from wechat_bridge_collector import __version__
from wechat_bridge_collector.bridge import CONTACT_EVENT_PAYLOAD_SCHEMA, MESSAGE_EVENT_PAYLOAD_SCHEMA


ROOT = Path(__file__).resolve().parents[1]


def test_connector_manifest_declares_local_app_capabilities():
    manifest = json.loads((ROOT / "connector.json").read_text(encoding="utf-8"))
    assert manifest["schemaVersion"] == "3.0.0"
    assert manifest["appId"] == "36d35399-a0cd-11f1-8622-00163e3536cb"
    assert "id" not in manifest
    assert "serviceRegistrationFiles" not in manifest
    assert "services" not in manifest
    assert manifest["runtime"]["command"] == "wechat-bridge-collector-python"
    assert not (ROOT / "bin" / "macos-x86_64" / "wechat-bridge-collector").exists()
    assert manifest["runtime"]["startPolicy"] == "manual"
    assert manifest["version"] == "7.1.1"
    assert manifest["version"] == __version__
    assert manifest["source"]["repo"] == "https://gitee.com/zxflimit_admin/wechat-bridge-collector.git"
    assert manifest["source"]["revision"] == f"v{manifest['version']}"
    assert manifest["runtime"]["command"].endswith("-python")
    assert manifest["hostRequirements"] == {
        "minimumVersion": "0.6.0",
        "capabilities": ["connector.process.host-managed.v1"],
    }
    assert manifest["upgradeReview"] == {
        "configuration": "declared",
        "interfaces": "declared",
        "database": "not_applicable",
    }
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.10"' in pyproject
    assert not (ROOT / "requirements.lock").read_text(encoding="utf-8").strip()
    assert not manifest.get("permissions")
    assert manifest["runtime"]["processOwnership"] == "host"
    assert manifest["runtime"]["args"] == ["run"]
    assert manifest["legacyAutostartLabels"] == ["com.baijimu.wechat-bridge-collector"]
    assert manifest["ui"] == {
        "type": "embedded",
        "entry": "ui/index.html",
        "title": "微信消息",
        "defaultView": True,
    }
    assert manifest["management"]["auth"]["type"] == "connector_token"
    assert manifest["management"]["operations"]["getChatHistory"]["path"] == "/management/v1/chat-history"
    assert manifest["management"]["operations"]["getAccountProfile"]["path"] == "/management/v1/account-profile"
    assert manifest["management"]["operations"]["getContactSnapshot"]["path"] == "/management/v1/contact-snapshot"
    operations = manifest["management"]["operations"]
    assert operations["pairCollector"]["path"] == "/management/v1/pair-collector"
    assert not {"acquireKeys", "importKeys", "retrySetup"} & operations.keys()
    for asset in ("index.html", "app.js", "styles.css", "time-range.mjs"):
        assert (ROOT / "ui" / asset).is_file()
    assert not list((ROOT / "vendor").rglob("find_all_keys*"))

    assert manifest["transport"]["type"] == "http"
    assert manifest["runtime"]["stopArgs"] == ["stop"]
    assert manifest["events"][0]["name"] == "messageReceived"
    assert manifest["events"][0]["payload_schema"] == MESSAGE_EVENT_PAYLOAD_SCHEMA
    assert manifest["events"][1]["name"] == "contactsChanged"
    assert manifest["events"][1]["payload_schema"] == CONTACT_EVENT_PAYLOAD_SCHEMA
    assert "conversationId" in manifest["events"][0]["payload_schema"]["properties"]
    assert "accountId" in manifest["events"][0]["payload_schema"]["required"]
    assert "senderName" in manifest["events"][0]["payload_schema"]["properties"]
    event_schema = manifest["events"][0]["payload_schema"]
    assert event_schema["properties"]["timestamp"]["type"] == "integer"
    assert "occurredAt" not in event_schema["properties"]
    for method in manifest["methods"]:
        properties = method["input_schema"].get("properties", {})
        for field in ("startTime", "endTime"):
            if field in properties:
                assert properties[field]["type"] == "integer"


def test_embedded_ui_converts_date_filters_to_epoch_milliseconds():
    app_source = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
    assert 'from "./time-range.mjs"' in app_source
    assert app_source.count("...dateInputsToEpochRange(") == 2
    assert 'startTime: elements["history-start"].value' not in app_source
    assert 'endTime: elements["history-end"].value' not in app_source
    assert 'startTime: elements["search-start"].value' not in app_source
    assert 'endTime: elements["search-end"].value' not in app_source


def test_jenkins_pipeline_only_validates_and_packages_source():
    pipeline = (ROOT / "Jenkinsfile.wechat-release").read_text(encoding="utf-8")
    job_config = (ROOT / "jenkins-wechat-release.xml").read_text(encoding="utf-8")
    assert "agent { label 'artifact-builder' }" in pipeline
    assert "agent any" not in pipeline
    assert "label 'built-in'" not in pipeline
    assert "label 'artifact-small'" not in pipeline
    assert "label 'artifact-rust'" not in pipeline
    assert "label 'artifact-heavy'" not in pipeline
    assert "node --test tests/*.test.mjs" in pipeline
    assert 'git rev-parse refs/remotes/origin/main' in pipeline
    assert "stage('Package source artifact')" in pipeline
    assert "archiveArtifacts" in pipeline
    assert "git push" not in pipeline
    assert "git ls-remote" not in pipeline
    assert "withCredentials" not in pipeline
    assert "local-app publish" not in pipeline
    assert "<defaultValue>7.1.1</defaultValue>" in job_config
