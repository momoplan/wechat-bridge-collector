# WeChat Bridge Collector

跨平台微信本地消息采集器和只读查询应用。它读取本机微信 4.x 本地数据库，依赖 `ylytdeng/wechat-decrypt` 的 key 提取能力，然后把新消息作为设备上的本地应用事件交给 Bridge Agent；查询方法由 `connector.json` 直接声明。

从 `0.4.0` 起官方 Connector 固定使用 Python 入口 `wechat-bridge-collector-python`。仓库中保留的 Rust 实验代码和旧二进制不参与 Connector 启动解析，也不是官方市场运行路径。

从 `0.6.0` 起 Connector 在百积木应用详情中提供完整的首次运行引导：无密钥时服务仍会启动，用户可在界面中自动获取、导入已有 `all_keys.json` 或重新检测，成功后无需重启即可浏览最近会话、联系人和聊天记录。界面只通过清单声明的 management operation 调用本机服务；所有 `/management/v1/*` 请求都要求 Bridge Agent 管理的私有 Connector token。

从 `0.7.0` 起运行环境统一为客户安装的 Python 3.12.x。百积木只保存解释器绝对路径，并为本 Connector 创建独立的 `.bridge-agent-python` 虚拟环境；依赖严格按 `requirements.lock` 安装，不写入客户的全局 Python。

macOS 上由签名后的百积木桌面应用作为权限宿主启动 Python 子进程。Connector 不再安装或加载 LaunchAgent；这是为了让微信沙盒数据库的访问权限稳定归属到“百积木”，而不是归属到一个无稳定签名身份的独立 Python/launchd 进程。

## 架构

```text
WeChat local DB/WAL
  -> ~/.wechat-bridge-collector/all_keys.json
  -> wechat-bridge-collector
  -> http://127.0.0.1:18082/invoke/*
  -> POST http://127.0.0.1:18081/v1/local-app-events
  -> bridge-agent websocket
  -> relay subscribers
```

collector 不直接连接 relay，也不修改微信数据。

## 百积木 Local Connector 安装

推荐通过百积木 Local 安装本仓库提供的 Connector，而不是让 AI skill 逐条执行安装命令。

```bash
bridge-agent connector install /path/to/wechat-bridge-collector --replace
```

安装器读取 `schemaVersion: "2.0"` 的 `connector.json`，以
`connectorId=com.baijimu.connector.wechat` 创建一个本地应用。methods、events、
healthCheck 和启停命令都直接属于这个应用，不创建 runtime service，也不生成
businessId。安装后按下面顺序操作：

- 在 macOS“隐私与安全性 > 完全磁盘访问”中启用“百积木”并重启百积木
- `wechat-bridge-collector-python setup`
- `wechat-bridge-collector-python probe`
- 从百积木应用详情页手动启动 Connector
- `GET http://127.0.0.1:18082/health`

当前 README 保留下面的手工命令，主要用于调试、诊断和 legacy fallback。

## 前置条件

1. 安装并运行 `bridge-agent`。
2. 安装 Python 3.12.x，并在百积木“设置 > 运行环境”中检测、选择并保存 Python 3.12 的绝对路径。
3. 克隆本仓库时带上 submodule：

```bash
git clone --recurse-submodules https://github.com/momoplan/wechat-bridge-collector.git
cd wechat-bridge-collector
```

如果已经普通 clone：

```bash
git submodule update --init --recursive
```

4. 初始化 collector 自己的配置和 key：

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

从源码安装 Python collector：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
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

由 Bridge Agent 启动时，collector 通过
`BAIJIMU_CONNECTOR_EVENT_TOKEN_FILE` 获得该 Connector 独立的事件发布凭证，通过
`BAIJIMU_CONNECTOR_EVENT_ENDPOINT` 获得事件入口。调试时也可以使用
`--event-token` 显式提供凭证。

启动采集器：

```bash
wechat-bridge-collector run
```

`run` 会同时启动本机只读 method server，默认地址是 `http://127.0.0.1:18082`。需要换端口时：

```bash
wechat-bridge-collector --method-port 18083 run
```

首次启动默认只建立当前游标，不广播历史消息。需要回放最近历史时显式指定：

```bash
wechat-bridge-collector run --reset-state --backfill-seconds 300
```

## 后台启动

collector 提供统一 CLI：

```bash
wechat-bridge-collector-python start
wechat-bridge-collector-python status
wechat-bridge-collector-python stop
```

- macOS：必须由百积木应用详情页显式启动；不创建 LaunchAgent，不随登录自行拉起。
- Windows：渲染并执行 `wechat_bridge_collector/scripts/windows/start-collector.ps1`。
- Linux：当前未提供后台启动集成。

Connector 清单中的 `runtime` 启动命令会统一解析为：

```bash
python -m wechat_bridge_collector start
```

Connector 的 `runtime.startPolicy` 是 `manual`。Bridge Agent 构建运行时能力时不会自动执行它；只有用户完成授权并点击“启动应用”后，`startCommand` 才会触发后台进程并退出。

## 事件

默认本地应用和事件名：

- connectorId: `com.baijimu.connector.wechat`
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

`connector.json` 直接声明以下 methods：

同一份 Connector 清单同时包含：

- `healthCheck`：`GET /health`，供 Bridge Agent 小客户端展示采集器是否可用。
- `startCommand`：通过 Python 入口启动采集器。
- `stopCommand`：停止 PID 文件所指向且经过命令行校验的采集器进程。

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
  -d '{"conversationId":"filehelper","limit":20}'
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
