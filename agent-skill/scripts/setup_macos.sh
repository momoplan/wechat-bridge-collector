#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${WECHAT_BRIDGE_COLLECTOR_REPO:-https://github.com/momoplan/wechat-bridge-collector.git}"
BASE_DIR="${WECHAT_BRIDGE_BASE_DIR:-$HOME/baijimu-wechat-bridge}"
PROJECT_DIR="$BASE_DIR/wechat-bridge-collector"
BRIDGE_URL="${BRIDGE_AGENT_URL:-http://127.0.0.1:18081}"
METHOD_URL="${WECHAT_COLLECTOR_METHOD_URL:-http://127.0.0.1:18082}"

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

COLLECTOR="$PROJECT_DIR/.venv/bin/wechat-bridge-collector"

log "Running collector setup"
if ! "$COLLECTOR" setup; then
  log "Setup failed. If macOS blocked task_for_pid, run:"
  log "  cd \"$PROJECT_DIR\""
  log "  sudo \"$COLLECTOR\" setup --force"
  log "Then fully quit and reopen WeChat if prompted, and rerun this script."
  exit 3
fi

log "Running collector probe"
"$COLLECTOR" probe

log "Installing platform autostart"
"$COLLECTOR" install-autostart

log "Starting collector"
"$COLLECTOR" start

log "Waiting for method server"
for _ in $(seq 1 20); do
  if curl -fsS "$METHOD_URL/health" >/dev/null; then
    break
  fi
  sleep 1
done

log "Health check"
curl -fsS "$METHOD_URL/health"
printf '\n'

log "Registering service with bridge-agent"
"$COLLECTOR" register

log "Checking recent sessions endpoint"
curl -fsS "$METHOD_URL/invoke/getRecentSessions" \
  -H 'Content-Type: application/json' \
  -d '{"limit":5}'
printf '\n'

log "Done. Install dir: $PROJECT_DIR"
