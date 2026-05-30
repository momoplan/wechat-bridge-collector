# WeChat Bridge Collector

跨平台微信本地消息采集器。它读取本机微信 4.x 本地数据库，依赖 `ylytdeng/wechat-decrypt` 产出的 `config.json` 和 `all_keys.json`，然后把新消息作为 bridge-agent 事件广播出去。

## 架构

```text
WeChat local DB/WAL
  -> ylytdeng/wechat-decrypt
  -> wechat-bridge-collector
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

3. 初始化 `vendor/wechat-decrypt`：

```bash
cd vendor/wechat-decrypt
pip install -r requirements.txt
python main.py decrypt
cd ../..
```

macOS 首次提取 key 可能需要按 `wechat-decrypt` 文档给 WeChat.app 增加调试访问权限并重启微信。Windows 通常需要管理员权限。Linux 通常需要 root 或 `CAP_SYS_PTRACE`。

## 本机运行

安装 collector：

```bash
pip install .
```

验证读取链路：

```bash
wechat-bridge-collector probe
```

如果 `wechat-decrypt` 不在 `./vendor/wechat-decrypt`、`~/dev/wechat-decrypt` 或相邻目录，显式指定：

```bash
export WECHAT_DECRYPT_DIR=/path/to/wechat-decrypt
```

注册 bridge-agent 事件声明：

```bash
wechat-bridge-collector register
```

启动采集器：

```bash
wechat-bridge-collector run --register
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

## 状态

collector 状态默认保存在：

```text
~/.wechat-bridge-collector/state.json
```

状态里只保存会话时间戳和消息表游标，不保存消息正文。
