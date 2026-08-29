import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_baijimu_cli_release_is_pinned_to_registered_linux_artifact():
    release = json.loads(
        (ROOT / "tools/release/baijimu-cli-linux.json").read_text(encoding="utf-8")
    )
    assert release == {
        "schemaVersion": 1,
        "version": "0.27.0",
        "platform": "linux",
        "arch": "x86_64",
        "archivePath": "bin/baijimu",
        "source": "https://download.baijimu.com/managed-tool-artifacts/baijimu-cli/releases/v0.27.0/282f3b643d5e7a5d4923597d0bfa1e15c826ab72ab8e439eb48c65c0a4d063f7/baijimu-cli-0.27.0-linux-x64.zip",
        "checksum": "282f3b643d5e7a5d4923597d0bfa1e15c826ab72ab8e439eb48c65c0a4d063f7",
    }


def test_pipeline_uses_existing_manifest_identity_and_dedicated_credentials():
    pipeline = (ROOT / "Jenkinsfile.wechat-release").read_text(encoding="utf-8")
    assert "app_id=\"$(jq -er '.appId' connector.json)\"" in pipeline
    assert "wechat-local-app-market-publish-token" in pipeline
    assert "wechat-local-app-owner-workspace-id" in pipeline
    assert "register-local-app-release.sh" in pipeline
    assert "local-app create" not in pipeline


def test_dry_run_never_enters_platform_registration_stage():
    pipeline = (ROOT / "Jenkinsfile.wechat-release").read_text(encoding="utf-8")
    registration_stage = pipeline.split("stage('Register existing local app version')", 1)[1]
    assert "return !params.DRY_RUN" in registration_stage


def test_all_pipeline_shell_blocks_parse_as_bash():
    pipeline = (ROOT / "Jenkinsfile.wechat-release").read_text(encoding="utf-8")
    shell_blocks = re.findall(r"sh '''(.*?)'''", pipeline, flags=re.DOTALL)
    assert len(shell_blocks) == 7
    for shell_block in shell_blocks:
        subprocess.run(
            ["bash", "-n"], input=shell_block, text=True, check=True, cwd=ROOT
        )
