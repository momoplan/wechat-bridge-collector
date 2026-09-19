#!/usr/bin/env python3
"""Build immutable local-app VersionContent from one uploaded source archive."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGETS = ROOT / "release" / "distribution-targets.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 191:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def artifact_from_receipt(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("artifact receipt must be a JSON object")
    receipt = document.get("data", document)
    if not isinstance(receipt, dict):
        raise ValueError("artifact receipt data must be a JSON object")
    artifact_id = require_text(receipt.get("artifactId"), "artifactId")
    if UUID(artifact_id).int == 0:
        raise ValueError("artifactId must not be nil")
    file_name = require_text(receipt.get("fileName"), "fileName")
    size_bytes = receipt.get("sizeBytes")
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes <= 0:
        raise ValueError("sizeBytes must be a positive integer")
    return {
        "artifactId": artifact_id,
        "fileName": file_name,
        "sizeBytes": size_bytes,
    }


def distribution_targets(document: Any) -> list[dict[str, str]]:
    if not isinstance(document, dict) or document.get("schemaVersion") != "1.0.0":
        raise ValueError("distribution targets require schemaVersion 1.0.0")
    raw_targets = document.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("distribution targets must not be empty")
    targets: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    for index, target in enumerate(raw_targets):
        if not isinstance(target, dict) or set(target) != {"platform", "architecture"}:
            raise ValueError(f"targets[{index}] must contain only platform and architecture")
        platform = require_text(target.get("platform"), f"targets[{index}].platform")
        architecture = require_text(
            target.get("architecture"), f"targets[{index}].architecture"
        )
        identity = (platform, architecture)
        if identity in identities:
            raise ValueError(f"duplicate distribution target: {platform}/{architecture}")
        if identity == ("source", "source"):
            raise ValueError("source/source is not an installable host target")
        identities.add(identity)
        targets.append({"platform": platform, "architecture": architecture})
    return targets


def build_version_content(
    manifest: Any, receipt: Any, targets_document: Any
) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ValueError("connector manifest must be a JSON object")
    if manifest.get("schemaVersion") != "3.0.0":
        raise ValueError("connector manifest must use schemaVersion 3.0.0")
    version = require_text(manifest.get("version"), "manifest.version")
    require_text(manifest.get("appId"), "manifest.appId")
    source = manifest.get("source")
    if not isinstance(source, dict) or source.get("revision") != f"v{version}":
        raise ValueError("manifest source revision must equal v<version>")
    artifact = artifact_from_receipt(receipt)
    expected_file_name = f"wechat-bridge-collector-{version}-source.zip"
    if artifact["fileName"] != expected_file_name:
        raise ValueError(
            f"artifact fileName must match the immutable release: {expected_file_name}"
        )
    artifacts = [
        {**artifact, "platform": target["platform"], "architecture": target["architecture"]}
        for target in distribution_targets(targets_document)
    ]
    return {
        "applicationType": "connector",
        "manifest": manifest,
        "sourceRevision": source["revision"],
        "artifacts": artifacts,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact-receipt", type=Path, required=True)
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    content = build_version_content(
        read_json(args.manifest),
        read_json(args.artifact_receipt),
        read_json(args.targets),
    )
    args.output.write_text(
        json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
