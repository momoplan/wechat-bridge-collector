#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 5 ]; then
  echo "usage: $0 <version> <connector-manifest> <archive> <archive-url> <output-json>" >&2
  exit 2
fi

version="$1"
manifest_path="$2"
archive="$3"
archive_url="$4"
output_json="$5"

for command_name in jq unzip od find; do
  command -v "$command_name" >/dev/null
done

[[ "$version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]
test -f "$manifest_path"
test -s "$archive"

manifest="$(jq -ce --arg version "$version" '
  select(.schemaVersion == "3.0.0")
  | select(.version == $version)
  | select(.appId | type == "string" and test("^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"))
  | select(has("id") | not)
  | select(.source.type == "git")
  | select(.source.repo | type == "string" and test("^https://github\\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\\.git$"))
  | select(.source.revision == ("v" + $version))
  | select(.runtime.type == "process")
  | select(.runtime.startPolicy == "manual")
  | select(.hostRequirements.minimumVersion | type == "string")
  | select((.hostRequirements.capabilities // []) | type == "array")
' "$manifest_path")"

repo_url="$(printf '%s' "$manifest" | jq -er .source.repo)"
repo_web_url="${repo_url%.git}"
expected_archive_url="$repo_web_url/archive/v${version}.zip"
test "$archive_url" = "$expected_archive_url"

listing="$(unzip -Z1 "$archive")"
test -n "$listing"
if printf '%s\n' "$listing" | grep -Eq '(^/|(^|/)\.\.(/|$)|\\)'; then
  echo "archive contains an unsafe path" >&2
  exit 1
fi

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/wechat-local-app-archive.XXXXXX")"
trap 'rm -rf -- "$work_dir"' EXIT
unzip -q "$archive" -d "$work_dir/unpacked"
test -z "$(find "$work_dir/unpacked" -type l -print -quit)"

archived_manifest_count="$(find "$work_dir/unpacked" -type f -name connector.json -print | wc -l | tr -d '[:space:]')"
test "$archived_manifest_count" -eq 1
archived_manifest="$(find "$work_dir/unpacked" -type f -name connector.json -print -quit)"
jq -e --argjson expected "$manifest" '. == $expected' "$archived_manifest" >/dev/null

archive_root="$(dirname "$archived_manifest")"
for required_path in \
  pyproject.toml \
  requirements.lock \
  wechat_bridge_collector/app.py \
  vendor/wechat-decrypt/key_utils.py \
  vendor/wechat-decrypt/find_all_keys.py \
  vendor/wechat-decrypt/find_all_keys_windows.py \
  vendor/wechat-decrypt/find_all_keys_macos.c \
  ui/index.html; do
  test -f "$archive_root/$required_path"
done

while IFS= read -r -d '' archived_file; do
  magic="$(od -An -tx1 -N4 "$archived_file" | tr -d '[:space:]')"
  case "$magic" in
    7f454c46|cafebabe|feedface|cefaedfe|feedfacf|cffaedfe|4d5a*)
      echo "archive contains a native executable: ${archived_file#"$archive_root/"}" >&2
      exit 1
      ;;
  esac
done < <(find "$archive_root" -type f -print0)

if command -v sha256sum >/dev/null 2>&1; then
  checksum="$(sha256sum "$archive" | awk '{print $1}')"
else
  checksum="$(shasum -a 256 "$archive" | awk '{print $1}')"
fi
[[ "$checksum" =~ ^[0-9a-f]{64}$ ]]

capabilities="$(printf '%s' "$manifest" | jq -c '.hostRequirements.capabilities // []')"
jq -n \
  --arg version "$version" \
  --arg source "$archive_url" \
  --arg repo "$repo_url" \
  --arg revision "v$version" \
  --arg checksum "$checksum" \
  --argjson capabilities "$capabilities" \
  --argjson manifest "$manifest" '
  {
    version: $version,
    sourceType: "archive",
    source: $source,
    repo: $repo,
    revision: $revision,
    checksum: $checksum,
    capabilities: $capabilities,
    manifest: $manifest
  }
' > "$output_json"

jq -e --arg version "$version" --arg source "$archive_url" '
  .version == $version and
  .sourceType == "archive" and
  .source == $source and
  .revision == ("v" + $version) and
  (.checksum | test("^[0-9a-f]{64}$")) and
  .manifest.version == $version and
  .manifest.source.revision == ("v" + $version)
' "$output_json" >/dev/null
