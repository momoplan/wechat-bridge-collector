# WeChat Bridge Collector Setup Skill

这个技能用于帮助用户在自己的电脑上快速配置 `wechat-bridge-collector`，把本机微信 4.x 消息只读接入 `bridge-agent`。

## 文件

- `SKILL.md`：技能主指令。
- `scripts/setup_macos.sh`：macOS 源码与 key 初始化、旧 LaunchAgent 清理和权限设置入口。
- 后台生命周期由百积木管理；Connector 不创建 macOS LaunchAgent。

## 发布信息

- 建议技能名：`微信本机采集器配置`
- 建议版本：`2.0.0`
- 建议描述：`帮助用户安装 WeChat Bridge Collector，把完全磁盘访问授予百积木，并从签名后的百积木应用启动和验证。`

## 用户侧结果

配置成功后，本机应具备：

- `wechatLocal.messageReceived` 事件广播。
- `getRecentSessions`
- `getContacts`
- `getChatHistory`
- `searchMessages`
- `getMessageById`
- `getChatImages`
- `getVoiceMessages`

技能不会保存或展示微信数据库 key、bridge-agent token 或微信消息正文。
