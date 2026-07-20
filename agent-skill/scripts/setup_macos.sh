#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${WECHAT_BRIDGE_COLLECTOR_REPO:-https://github.com/momoplan/wechat-bridge-collector.git}"
BASE_DIR="${WECHAT_BRIDGE_BASE_DIR:-$HOME/baijimu-wechat-bridge}"
PROJECT_DIR="$BASE_DIR/wechat-bridge-collector"
BRIDGE_URL="${BRIDGE_AGENT_URL:-http://127.0.0.1:18081}"

log() {
  printf '[wechat-bridge-collector] %s\n' "$*"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    log "Missing command: $1"
    exit 1
  fi
}

if [[ "$(uname -s)" != "Darwin" ]]; then
  log "This script is for macOS. Use SKILL.md manual flow on other systems."
  exit 1
fi

require_cmd git
require_cmd python3
require_cmd curl

log "Checking bridge-agent at $BRIDGE_URL"
if ! curl -fsS "$BRIDGE_URL/health" >/dev/null; then
  log "bridge-agent is not reachable. Start bridge-agent first, then rerun this script."
  exit 2
fi

mkdir -p "$BASE_DIR"
if [[ ! -d "$PROJECT_DIR/.git" ]]; then
  log "Cloning collector repo into $PROJECT_DIR"
  git clone --recurse-submodules "$REPO_URL" "$PROJECT_DIR"
else
  log "Updating collector repo in $PROJECT_DIR"
  git -C "$PROJECT_DIR" pull --ff-only
  git -C "$PROJECT_DIR" submodule update --init --recursive
fi

cd "$PROJECT_DIR"

log "Preparing Python virtualenv"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install .

COLLECTOR="$PROJECT_DIR/.venv/bin/wechat-bridge-collector-python"

log "Running collector setup"
if ! "$COLLECTOR" setup; then
  log "Setup failed. If macOS blocked task_for_pid, run:"
  log "  cd \"$PROJECT_DIR\""
  log "  sudo \"$COLLECTOR\" setup --force"
  log "Then fully quit and reopen WeChat if prompted, and rerun this script."
  exit 3
fi

log "Removing the legacy LaunchAgent"
launchctl bootout "gui/$(id -u)/com.baijimu.wechat-bridge-collector" >/dev/null 2>&1 || true
rm -f "$HOME/Library/LaunchAgents/com.baijimu.wechat-bridge-collector.plist"

log "Opening Full Disk Access settings"
open 'x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles' || true

log "Source and key setup completed. Install WeChat Connector in Baijimu, grant Full Disk Access to Baijimu, restart Baijimu, then click Start App."
log "Do not create a LaunchAgent or run the collector persistently from Terminal."
log "Install dir: $PROJECT_DIR"
