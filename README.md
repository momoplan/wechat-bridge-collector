# WeChat Bridge Collector

跨平台微信本地消息采集器和只读查询服务。它读取本机微信 4.x 本地数据库，依赖 `ylytdeng/wechat-decrypt` 的 key 提取能力，然后把新消息作为 bridge-agent 事件广播出去，同时向 bridge-agent 注册本机 HTTP methods 用于查询最近会话、联系人、聊天记录和消息搜索。

## 架构

```text
WeChat local DB/WAL
  -> ~/.wechat-bridge-collector/all_keys.json
  -> wechat-bridge-collector
  -> http://127.0.0.1:18082/invoke/*
  -> POST http://127.0.0.1:18081/v1/events
  -> bridge-agent websocket
  -> relay subscribers
```

collector 不直接连接 relay，也不修改微信数据。

## 前置条件

1. 安装并运行 `bridge-agent`。
2. 克隆本仓库时带上 submodule：

```bash
git clone --recurse-submodules https://github.com/momoplan/wechat-bridge-collector.git
cd wechat-bridge-collector
```

如果已经普通 clone：

```bash
git submodule update --init --recursive
```

3. 初始化 collector 自己的配置和 key：

```bash
wechat-bridge-collector setup
```

collector 默认只读写自己的目录：

```text
~/.wechat-bridge-collector/config.json
~/.wechat-bridge-collector/all_keys.json
~/.wechat-bridge-collector/state.json
~/.wechat-bridge-collector/decrypted/
```

macOS 首次提取 key 可能需要管理员权限；如果系统拦截 `task_for_pid`，`setup` 会尝试按“保留 WeChat 原 entitlements + 添加 `com.apple.security.get-task-allow`”的方式重签微信，并提示重启微信后重试。Windows 通常需要管理员权限。Linux 通常需要 root 或 `CAP_SYS_PTRACE`。

## 本机运行

安装 collector：

```bash
pip install .
```

验证读取链路：

```bash
wechat-bridge-collector setup
wechat-bridge-collector probe
```

如果 `wechat-decrypt` 不在 `./vendor/wechat-decrypt`、`~/dev/wechat-decrypt` 或相邻目录，显式指定：

```bash
export WECHAT_DECRYPT_DIR=/path/to/wechat-decrypt
```

`--keys-file` 仍可用于高级场景，但默认不会读取其它工具的目录。

注册 bridge-agent 事件声明：

```bash
wechat-bridge-collector register
```

如果 Bridge Agent 使用默认本机地址，collector 会自动读取本机 Bridge Agent 配置里的
`runtime.service_registration_token` 和 `runtime.event_server_token`。需要覆盖时可使用：

```bash
export BRIDGE_AGENT_SERVICE_REGISTRATION_TOKEN=...
export BRIDGE_AGENT_EVENT_TOKEN=...
```

启动采集器：

```bash
wechat-bridge-collector run --register
```

`run` 会同时启动本机只读 method server，默认地址是 `http://127.0.0.1:18082`。需要换端口时：

```bash
wechat-bridge-collector --method-port 18083 run --register
```

首次启动默认只建立当前游标，不广播历史消息。需要回放最近历史时显式指定：

```bash
wechat-bridge-collector run --reset-state --backfill-seconds 300
```

## 事件

默认服务和事件名：

- service: `wechatLocal`
- event: `messageReceived`

payload 示例：

```json
{
  "messageId": "message/message_0.db:Msg_xxx:123",
  "conversationId": "xxx@chatroom",
  "conversationName": "群名",
  "isGroup": true,
  "senderId": "wxid_xxx",
  "senderName": "张三",
  "direction": "unknown",
  "messageType": "text",
  "messageTypeLabel": "文本",
  "text": "消息内容",
  "timestamp": 1780106113,
  "occurredAt": "2026-05-30T10:00:00+00:00",
  "source": "wechat-local-db",
  "platform": "darwin"
}
```

## 查询方法

`wechat-bridge-collector run --register` 会向 bridge-agent 注册以下 methods：

注册 payload 同时包含：

- `healthCheck`：`GET /health`，供 Bridge Agent 小客户端展示采集器是否可用。
- `startCommand`：macOS 下通过 LaunchAgent 触发 `com.baijimu.wechat-bridge-collector` 启动；Bridge Agent 只按注册字段执行，不内置 WeChat 采集器逻辑。

- `getRecentSessions`：查询最近会话。
- `getContacts`：搜索或列出联系人、群聊。
- `getChatHistory`：按会话分页查询消息历史。
- `searchMessages`：按关键词搜索消息，可限定会话和时间范围。
- `getMessageById`：按事件 payload 里的 `messageId` 精确查询单条消息。
- `getChatImages`：列出指定会话里的图片消息。
- `getVoiceMessages`：列出指定会话里的语音消息。

本机直连调试示例：

```bash
curl -s http://127.0.0.1:18082/invoke/getChatHistory \
  -H 'Content-Type: application/json' \
  -d '{"chat":"文件传输助手","limit":20}'
```

返回体统一为：

```json
{
  "success": true,
  "data": {
    "messages": []
  },
  "error": null
}
```

## 状态

collector 状态默认保存在：

```text
~/.wechat-bridge-collector/state.json
```

状态里只保存会话时间戳和消息表游标，不保存消息正文。
