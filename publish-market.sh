#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: $0 <connector-version> <source-commit> <minimum-bridge-version>" >&2
  exit 2
fi

version="$1"
source_commit="$2"
minimum_bridge_version="$3"
case "$version" in *[!0-9.]*|'') exit 2 ;; esac
case "$minimum_bridge_version" in *[!0-9.]*|'') exit 2 ;; esac
[[ "$source_commit" =~ ^[0-9a-f]{40}$ ]]

capabilities='["wechat.messages.read","wechat.messages.search","wechat.events.messageReceived"]'
permissions='[{"id":"macos.fullDiskAccess","title":"完全磁盘访问","platforms":["macos"]}]'
manifest="$(jq -nc \
  --arg min_bridge "$minimum_bridge_version" \
  --argjson permissions "$permissions" \
  '{
    applicationType: "connector",
    runtime: "process",
    command: "wechat-bridge-collector-python",
    args: ["start"],
    startPolicy: "manual",
    minimumBridgeVersion: $min_bridge,
    permissions: $permissions
  }')"

nacos_content="$(timeout 30s aliyun mse GetNacosConfig \
  --profile baijimu \
  --RegionId cn-beijing \
  --InstanceId mse_regserverless_cn-cy74qcvrg01 \
  --NamespaceId 6ef6a8f2-8682-422b-9627-6fadf27f2b3e \
  --DataId lowcode \
  --Group DEFAULT_GROUP 2>/dev/null \
  | jq -r '.Configuration.Content // .Content // empty')"
db_password="$(printf '%s\n' "$nacos_content" | sed -n 's/^spring.datasource.password=//p' | head -1)"
if [ -z "$db_password" ]; then
  echo "failed to resolve production database password from MSE" >&2
  exit 1
fi

mysql_args=(
  --protocol=TCP
  --host=rm-2zen9i892pqpan6at.mysql.rds.aliyuncs.com
  --user=baijimu
  --database=local_app_market
  --connect-timeout=10
  --default-character-set=utf8mb4
  --batch
  --raw
)
backup_file="${WORKSPACE:-$PWD}/wechat-market-before-${BUILD_NUMBER:-manual}.tsv"
MYSQL_PWD="$db_password" mysql "${mysql_args[@]}" \
  -e "SELECT app.*, version.* FROM local_app app LEFT JOIN local_app_version version ON version.app_id=app.id WHERE app.id='wechat' ORDER BY version.id" \
  > "$backup_file"

b64() {
  printf '%s' "$1" | base64 | tr -d '\n'
}

name_b64="$(b64 '微信')"
description_b64="$(b64 '安装微信本地采集 Connector，把微信相关本地能力接入工作区。')"
risk_b64="$(b64 '需要读取本机微信数据库、联系人和消息记录目录，只在用户本机运行。')"
capability_b64="$(b64 '本地微信消息查询、搜索和消息事件采集。')"
platforms_b64="$(b64 '["macos","windows"]')"
source_b64="$(b64 'https://github.com/momoplan/wechat-bridge-collector.git')"
repo_b64="$(b64 'momoplan/wechat-bridge-collector')"
revision_b64="$(b64 "v${version}")"
capabilities_b64="$(b64 "$capabilities")"
manifest_b64="$(b64 "$manifest")"
published_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

MYSQL_PWD="$db_password" mysql "${mysql_args[@]}" <<SQL
START TRANSACTION;
INSERT INTO local_app (
  id, connector_id, name, status, publisher, description, risk, risk_level,
  capability, platforms_json, rank_order
) VALUES (
  'wechat', 'com.baijimu.connector.wechat',
  CONVERT(FROM_BASE64('${name_b64}') USING utf8mb4), 'PUBLISHED', 'Baijimu',
  CONVERT(FROM_BASE64('${description_b64}') USING utf8mb4),
  CONVERT(FROM_BASE64('${risk_b64}') USING utf8mb4), 'high',
  CONVERT(FROM_BASE64('${capability_b64}') USING utf8mb4),
  CONVERT(FROM_BASE64('${platforms_b64}') USING utf8mb4), 20
) ON DUPLICATE KEY UPDATE
  connector_id=VALUES(connector_id), name=VALUES(name), status='PUBLISHED',
  publisher=VALUES(publisher), description=VALUES(description), risk=VALUES(risk),
  risk_level=VALUES(risk_level), capability=VALUES(capability), platforms_json=VALUES(platforms_json);

INSERT INTO local_app_version (
  app_id, version, status, source_type, source, repo, revision, checksum,
  capabilities_json, manifest_json, rank_order, published_at
) VALUES (
  'wechat', '${version}', 'PUBLISHED', 'git',
  CONVERT(FROM_BASE64('${source_b64}') USING utf8mb4),
  CONVERT(FROM_BASE64('${repo_b64}') USING utf8mb4),
  CONVERT(FROM_BASE64('${revision_b64}') USING utf8mb4), NULL,
  CONVERT(FROM_BASE64('${capabilities_b64}') USING utf8mb4),
  CONVERT(FROM_BASE64('${manifest_b64}') USING utf8mb4),
  400, '${published_at}'
) ON DUPLICATE KEY UPDATE
  status='PUBLISHED', source_type=VALUES(source_type), source=VALUES(source),
  repo=VALUES(repo), revision=VALUES(revision), checksum=VALUES(checksum),
  capabilities_json=VALUES(capabilities_json), manifest_json=VALUES(manifest_json),
  rank_order=VALUES(rank_order), published_at=VALUES(published_at);
COMMIT;
SQL

echo "published WeChat Connector ${version} from ${source_commit}; backup=${backup_file}"
