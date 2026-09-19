import importlib.util
import json
from pathlib import Path
from uuid import UUID

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_version_content", ROOT / "tools" / "build_version_content.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def receipt():
    return {
        "contractVersion": "1.0.0",
        "errorCode": "0",
        "data": {
            "artifactId": str(UUID(int=1)),
            "fileName": "wechat-bridge-collector-3.1.7-source.zip",
            "sizeBytes": 123,
        },
    }


def test_version_content_declares_source_archive_for_every_supported_host():
    manifest = json.loads((ROOT / "connector.json").read_text(encoding="utf-8"))
    targets = json.loads(
        (ROOT / "release" / "distribution-targets.json").read_text(encoding="utf-8")
    )
    content = MODULE.build_version_content(manifest, receipt(), targets)

    assert content["applicationType"] == "connector"
    assert content["sourceRevision"] == "v3.1.7"
    assert content["manifest"] == manifest
    assert {(item["platform"], item["architecture"]) for item in content["artifacts"]} == {
        ("macos", "universal"),
        ("windows", "universal"),
        ("linux", "universal"),
    }
    assert {item["artifactId"] for item in content["artifacts"]} == {
        receipt()["data"]["artifactId"]
    }


def test_version_content_rejects_non_installable_source_target():
    manifest = json.loads((ROOT / "connector.json").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="source/source"):
        MODULE.build_version_content(
            manifest,
            receipt(),
            {
                "schemaVersion": "1.0.0",
                "targets": [{"platform": "source", "architecture": "source"}],
            },
        )


def test_version_content_rejects_artifact_from_another_release():
    manifest = json.loads((ROOT / "connector.json").read_text(encoding="utf-8"))
    targets = json.loads(
        (ROOT / "release" / "distribution-targets.json").read_text(encoding="utf-8")
    )
    wrong_receipt = receipt()
    wrong_receipt["data"]["fileName"] = "wechat-bridge-collector-3.1.6-source.zip"

    with pytest.raises(ValueError, match="immutable release"):
        MODULE.build_version_content(manifest, wrong_receipt, targets)
