# WeChat Bridge Collector

跨平台微信本地消息采集器和只读查询应用。它读取本机微信 4.x 本地数据库，依赖 `ylytdeng/wechat-decrypt` 的 key 提取能力，然后把新消息作为设备上的本地应用事件交给 Bridge Agent；查询方法由 `connector.json` 直接声明。

从 `0.4.0` 起官方 Connector 固定使用 Python 入口 `wechat-bridge-collector-python`。仓库中保留的 Rust 实验代码不参与 Connector 启动解析；从 `2.0.4` 起，未签名且不参与运行的旧预编译二进制不再进入源码归档。

从 `0.6.0` 起 Connector 在百积木应用详情中提供完整的首次运行引导：无密钥时服务仍会启动，用户可在界面中自动获取、导入已有 `all_keys.json` 或重新检测，成功后无需重启即可浏览最近会话、联系人和聊天记录。界面只通过清单声明的 management operation 调用本机服务；所有 `/management/v1/*` 请求都要求 Bridge Agent 管理的私有 Connector token。

从 `0.7.0` 起，百积木只保存客户 Python 解释器的绝对路径，并为本 Connector 创建独立的 `.bridge-agent-python` 虚拟环境；依赖严格按 `requirements.lock` 安装，不写入客户的全局 Python。从 `3.1.2` 起，Connector 恢复支持 Python 3.10 及以上版本，不再把运行环境限定为 Python 3.12.x。

从 `1.0.1` 起，Connector 使用的 `wechat-decrypt` 固定源码作为普通文件随仓库和标签归档一起发布，不再依赖 Git submodule，也不要求用户额外克隆依赖仓库。

从 `2.0.0` 起，所有公开业务时间字段统一为 Unix epoch 毫秒整数。查询范围的 `startTime`、`endTime`，会话的 `lastTimestamp` 和消息的 `timestamp` 均使用毫秒；ISO 8601 字符串只允许在界面显示层生成，不再作为 Connector 数据契约返回。

从 `2.0.2` 起，本地应用界面的日期筛选也严格遵循该契约：开始日期转换为本地时区当天零点的 Unix epoch 毫秒，结束日期转换为次日零点减 1 毫秒；未填写的边界不会发送给查询接口。

从 `2.0.3` 起，`getRecentSessions` 会依据实际微信消息表返回 `historyAvailable`。没有独立消息表的微信系统聚合会话仍保留最近摘要，但本地应用不会再把它当作可读取聊天记录的普通会话。

从 `2.0.4` 起，采集器只在 `session.db` 出现新的会话时间戳时读取对应消息库；消息库快照按 SQLite WAL 的校验和与提交边界增量更新。完整 `quick_check` 只在首次建立或主库换代后的重建阶段执行，快照失败使用有上限的指数退避，并发查询与采集由每库锁串行化。

从 `2.0.5` 起，启动权限探测不再枚举受 macOS 隐私保护的微信 `db_storage` 根目录，而是只校验配置中已知的数据库文件并尝试建立只读解密快照，避免系统目录打开调用长期阻塞 Connector 初始化。

从 `3.0.0` 起，Connector 迁移到 Bridge Agent `3.0.0` 本地应用协议：应用身份改由平台注册的 `appId` 和宿主环境提供，事件请求使用 `appId`，私有数据、管理凭证和事件凭证统一使用 `BAIJIMU_LOCAL_APP_*` 契约。本版本要求 Bridge Agent `0.6.0` 或更高版本，不兼容旧宿主协议。

从 `3.1.3` 起，权威源码和标签发布到 Gitee，并同步 GitHub 镜像；百积木登记的安装归档使用 `download.baijimu.com` 国内公共 OSS 内容寻址地址，不再让客户安装器直接下载 GitHub/Gitee 归档。

从 `3.1.4` 起，Windows 后台启动不再经由 PowerShell 启动器转接，避免 Bridge Agent 捕获的标准输出句柄让生命周期命令超时；停止时会核验记录 PID 的命令行身份并回收完整 Python 进程树。

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
baijimu local-app install \
  36d35399-a0cd-11f1-8622-00163e3536cb \
  --version 3.1.4 \
  --replace
```

安装命令只使用平台注册的不可变版本，不接受调用方覆盖下载地址。安装器会校验 appId、版本、revision 和 SHA-256；审核状态由平台版本记录决定。

安装器读取 `schemaVersion: "3.0.0"` 的 `connector.json`，以
`appId=36d35399-a0cd-11f1-8622-00163e3536cb` 创建一个本地应用。methods、events、
healthCheck 和启停命令都直接属于这个应用，不创建 runtime service，也不生成
businessId。安装后按下面顺序操作：

- 在 macOS“隐私与安全性 > 完全磁盘访问”中启用“百积木”并重启百积木
- `wechat-bridge-collector-python setup`
- `wechat-bridge-collector-python probe`
- 从百积木应用详情页手动启动 Connector
- `GET http://127.0.0.1:18082/health`

当前 README 保留下面的手工命令，主要用于调试和诊断。

## 前置条件

1. 安装并运行 `bridge-agent`。
2. 安装 Python 3.10 或更高版本，并在百积木“设置 > 运行环境”中检测、选择并保存兼容解释器的绝对路径。
3. 克隆本仓库：

```bash
git clone https://gitee.com/zxflimit_admin/wechat-bridge-collector.git
cd wechat-bridge-collector
```

4. 初始化 collector 自己的配置和 key：

```bash
wechat-bridge-collector setup
```

由 Bridge Agent 启动时，collector 只读写宿主通过 `BAIJIMU_LOCAL_APP_DATA_DIR` 分配的应用私有目录。独立调试时默认目录为：

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
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
```

验证读取链路：

```bash
wechat-bridge-collector setup
wechat-bridge-collector probe
```

发布包默认使用内置的 `./vendor/wechat-decrypt`。只有调试其它版本时才需要显式覆盖：

```bash
export WECHAT_DECRYPT_DIR=/path/to/wechat-decrypt
```

`--keys-file` 仍可用于高级场景，但默认不会读取其它工具的目录。

由 Bridge Agent 启动时，collector 通过
`BAIJIMU_LOCAL_APP_EVENT_TOKEN_FILE` 获得该应用独立的事件发布凭证，通过
`BAIJIMU_LOCAL_APP_EVENT_ENDPOINT` 获得事件入口，通过 `BAIJIMU_LOCAL_APP_ID` 获得平台注册身份，并从 `BAIJIMU_LOCAL_APP_TOKEN_FILE` 读取管理凭证。调试时也可以使用
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
- Windows：由启动命令直接创建脱离控制台的 Python 后台进程并记录 PID；停止时校验命令行身份并回收完整进程树。
- Linux：当前未提供后台启动集成。

Connector 清单中的 `runtime` 启动命令会统一解析为：

```bash
python -m wechat_bridge_collector start
```

Connector 的 `runtime.startPolicy` 是 `manual`。Bridge Agent 构建运行时能力时不会自动执行它；只有用户完成授权并点击“启动应用”后，`startCommand` 才会触发后台进程并退出。

## 事件

默认本地应用和事件名：

- appId: `36d35399-a0cd-11f1-8622-00163e3536cb`
- event: `messageReceived`，携带归属工作微信 `accountId`，用于按工作手机/销售归属处理消息
- event: `contactSnapshotChanged`，按 `started/contact/completed` 顺序同步直接好友快照

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
  "timestamp": 1780106113000,
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
- `getAccountProfile`：返回当前授权工作微信账号的稳定账号标识。
- `getContactSnapshot`：分页读取带检查点的直接好友快照；默认排除群聊。
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
  "errorCode": "0",
  "value": "成功",
  "data": {
    "messages": []
  },
  "systemCurrentTime": 1780106113000
}
```

## 状态

collector 状态默认保存在：

```text
~/.wechat-bridge-collector/state.json
```

状态里只保存会话时间戳、消息表游标和联系人快照检查点，不保存消息正文。
