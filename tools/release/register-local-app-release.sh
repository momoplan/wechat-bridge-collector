#!/usr/bin/env bash
set -euo pipefail
set +x

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <release-request-json>" >&2
  exit 2
fi

request_path="$1"
for name in LOCAL_APP_MARKET_PUBLISH_TOKEN LOCAL_APP_OWNER_WORKSPACE_ID BAIJIMU_CLI; do
  test -n "${!name:-}" || { echo "$name is required" >&2; exit 2; }
done
test -x "$BAIJIMU_CLI"
[[ "$LOCAL_APP_MARKET_PUBLISH_TOKEN" == lc_pat_* ]]
[[ "$LOCAL_APP_OWNER_WORKSPACE_ID" =~ ^[1-9][0-9]*$ ]]

request="$(jq -ce '
  select(.version | type == "string" and test("^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$"))
  | select(.sourceType == "archive")
  | select(.source | type == "string" and startswith("https://github.com/"))
  | select(.repo | type == "string" and test("^https://github\\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\\.git$"))
  | select(.revision == ("v" + .version))
  | select(.checksum | type == "string" and test("^[0-9a-f]{64}$"))
  | select(.capabilities | type == "array")
  | select(.manifest.appId | type == "string" and length > 0)
  | select(.manifest.version == .version)
  | select(.manifest.source.repo == .repo)
  | select(.manifest.source.revision == .revision)
' "$request_path")"

app_id="$(printf '%s' "$request" | jq -er .manifest.appId)"
version="$(printf '%s' "$request" | jq -er .version)"

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/wechat-local-app-register.XXXXXX")"
trap 'rm -rf -- "$work_dir"' EXIT
auth_file="$work_dir/baijimu-auth.json"

BAIJIMU_AUTH_FILE="$auth_file" "$BAIJIMU_CLI" auth login \
  --token "$LOCAL_APP_MARKET_PUBLISH_TOKEN" \
  --workspace-id "$LOCAL_APP_OWNER_WORKSPACE_ID" \
  --no-browser --json >/dev/null

app_json="$(BAIJIMU_AUTH_FILE="$auth_file" "$BAIJIMU_CLI" local-app get "$app_id" --json)"
printf '%s' "$app_json" | jq -e \
  --arg appId "$app_id" \
  --argjson ownerWorkspaceId "$LOCAL_APP_OWNER_WORKSPACE_ID" '
  (.data // .).appId == $appId and
  (.data // .).ownerWorkspaceId == $ownerWorkspaceId and
  (.data // .).registrationStatus == "ACTIVE"
' >/dev/null

existing="$(printf '%s' "$app_json" | jq -c --arg version "$version" '
  first((.data // .).versions[]? | select(.version == $version)) // empty
')"
if [ -n "$existing" ]; then
  printf '%s' "$existing" | jq -e --argjson expected "$request" '
    .version == $expected.version and
    .sourceType == $expected.sourceType and
    .source == $expected.source and
    .repo == $expected.repo and
    .revision == $expected.revision and
    .checksum == $expected.checksum and
    .capabilities == $expected.capabilities and
    .manifest == $expected.manifest
  ' >/dev/null
else
  BAIJIMU_AUTH_FILE="$auth_file" "$BAIJIMU_CLI" local-app publish "$app_id" \
    --data "@$request_path" --json | jq -e '.errorCode == "0"' >/dev/null
fi

read_publication() {
  BAIJIMU_AUTH_FILE="$auth_file" "$BAIJIMU_CLI" local-app publications --json \
    | jq -c --arg appId "$app_id" --arg version "$version" '
      first((.data // .)[]? | select(.appId == $appId and .version == $version)) // empty
    '
}

publication="$(read_publication)"
status="$(printf '%s' "$publication" | jq -r '.status // empty')"
case "$status" in
  PENDING_REVIEW|PUBLISHED) ;;
  "")
    BAIJIMU_AUTH_FILE="$auth_file" "$BAIJIMU_CLI" local-app submit "$app_id" "$version" --json \
      | jq -e '.errorCode == "0"' >/dev/null
    ;;
  *) echo "local app publication is not retryable: $status" >&2; exit 1 ;;
esac

for _ in $(seq 1 20); do
  publication="$(read_publication)"
  status="$(printf '%s' "$publication" | jq -r '.status // empty')"
  case "$status" in
    PENDING_REVIEW|PUBLISHED)
      jq -n --arg appId "$app_id" --arg version "$version" --arg status "$status" \
        '{appId:$appId,version:$version,status:$status}'
      exit 0
      ;;
  esac
  sleep 3
done

echo "local app publication did not become visible before timeout" >&2
exit 1
