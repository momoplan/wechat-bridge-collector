#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <cli-release-config> <destination-directory>" >&2
  exit 2
fi

config="$1"
destination="$2"

for command_name in curl jq unzip; do
  command -v "$command_name" >/dev/null
done

jq -e '
  .schemaVersion == 1 and
  (.version | type == "string" and test("^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$")) and
  .platform == "linux" and
  .arch == "x86_64" and
  (.archivePath | type == "string" and startswith("bin/")) and
  (.source | type == "string" and startswith("https://download.baijimu.com/managed-tool-artifacts/baijimu-cli/releases/")) and
  (.checksum | type == "string" and test("^[0-9a-f]{64}$"))
' "$config" >/dev/null

test "$(uname -s)" = "Linux"
case "$(uname -m)" in
  x86_64|amd64) ;;
  *) echo "configured Baijimu CLI artifact requires Linux x86_64" >&2; exit 1 ;;
esac

version="$(jq -er .version "$config")"
archive_path="$(jq -er .archivePath "$config")"
source_url="$(jq -er .source "$config")"
expected_checksum="$(jq -er .checksum "$config")"

mkdir -p "$destination"
archive="$destination/baijimu-cli.zip"
curl -fsSL --retry 6 --retry-all-errors --connect-timeout 15 --max-time 300 \
  "$source_url" -o "$archive"

if command -v sha256sum >/dev/null 2>&1; then
  actual_checksum="$(sha256sum "$archive" | awk '{print $1}')"
else
  actual_checksum="$(shasum -a 256 "$archive" | awk '{print $1}')"
fi
test "$actual_checksum" = "$expected_checksum"

unzip -q "$archive" -d "$destination/unpacked"
cli="$destination/unpacked/$archive_path"
test -f "$cli"
chmod 755 "$cli"
test "$($cli --version)" = "$version"
printf '%s\n' "$cli"
