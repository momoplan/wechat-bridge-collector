# WeChat Bridge Collector Setup Skill

这个技能用于帮助用户在自己的电脑上快速配置 `wechat-bridge-collector`，把本机微信 4.x 消息只读接入 `bridge-agent`。

## 文件

- `SKILL.md`：技能主指令。
- `scripts/setup_macos.sh`：macOS 一键安装、配置、启动和验证脚本。
- Windows/macOS 后台启动入口由 `wechat-bridge-collector install-autostart/start` 提供，平台脚本随 collector 包分发。

## 发布信息

- 建议技能名：`微信本机采集器配置`
- 建议版本：`1.0.0`
- 建议描述：`帮助用户在电脑上安装和配置 WeChat Bridge Collector，完成 bridge-agent 注册、LaunchAgent 启动和本机健康检查。`

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
