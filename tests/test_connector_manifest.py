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
    assert manifest["version"] == "3.1.2"
    assert manifest["version"] == __version__
    assert manifest["source"]["revision"] == f"v{manifest['version']}"
    assert manifest["runtime"]["command"].endswith("-python")
    assert manifest["hostRequirements"] == {
        "minimumVersion": "0.6.0",
        "capabilities": [],
    }
    assert manifest["upgradeReview"] == {
        "configuration": "declared",
        "interfaces": "declared",
        "database": "not_applicable",
    }
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.10"' in pyproject
    assert (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines() == [
        "pycryptodome==3.23.0",
        "zstandard==0.25.0",
    ]
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
    assert manifest["management"]["operations"]["getAccountProfile"]["path"] == "/management/v1/account-profile"
    assert manifest["management"]["operations"]["getContactSnapshot"]["path"] == "/management/v1/contact-snapshot"
    assert manifest["management"]["operations"]["acquireKeys"]["path"] == "/management/v1/acquire-keys"
    assert manifest["management"]["operations"]["importKeys"]["path"] == "/management/v1/import-keys"
    assert manifest["management"]["operations"]["retrySetup"]["path"] == "/management/v1/retry-setup"
    for asset in ("index.html", "app.js", "styles.css", "time-range.mjs", "session-model.mjs"):
        assert (ROOT / "ui" / asset).is_file()
    assert "installAutostart" not in manifest["hooks"]

    upstream = json.loads(
        (ROOT / "vendor" / "wechat-decrypt.upstream.json").read_text(encoding="utf-8")
    )
    dependency_files = (
        "config.py",
        "find_all_keys.py",
        "find_all_keys_linux.py",
        "find_all_keys_macos.c",
        "find_all_keys_windows.py",
        "key_scan_common.py",
        "key_utils.py",
    )
    assert upstream == {
        "repository": "https://github.com/ylytdeng/wechat-decrypt.git",
        "commit": "5e0eaa33fa1e77e533392db394644216c5ea6824",
        "normalization": "text line endings and trailing whitespace",
        "files": list(dependency_files),
    }
    vendor_dir = ROOT / "vendor" / "wechat-decrypt"
    assert {path.name for path in vendor_dir.iterdir()} == set(dependency_files)
    for dependency_file in dependency_files:
        assert (vendor_dir / dependency_file).is_file()
    assert not (ROOT / ".gitmodules").exists()

    assert manifest["transport"]["type"] == "http"
    assert manifest["runtime"]["stopArgs"] == ["stop"]
    assert manifest["events"][0]["name"] == "messageReceived"
    assert manifest["events"][0]["payload_schema"] == MESSAGE_EVENT_PAYLOAD_SCHEMA
    assert manifest["events"][1]["name"] == "contactSnapshotChanged"
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


def test_embedded_ui_disables_summary_only_sessions():
    app_source = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
    assert 'from "./session-model.mjs"' in app_source
    assert "button.disabled = !historyAvailable" in app_source
    assert 'availability.textContent = "仅摘要"' in app_source


def test_release_pipeline_uses_authenticated_remote_verification():
    pipeline = (ROOT / "Jenkinsfile.wechat-release").read_text(encoding="utf-8")
    assert "node --test tests/*.test.mjs" in pipeline
    assert 'git rev-parse refs/remotes/origin/main' in pipeline
    assert 'published_refs="$(git ls-remote "$remote"' in pipeline
    assert pipeline.count("git ls-remote") == 3
    verify_stage = pipeline.split("stage('Verify published release')", maxsplit=1)[1]
    assert "git ls-remote" not in verify_stage
