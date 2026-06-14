---
name: wechat-bridge-collector-setup
description: 帮用户在本机配置 WeChat Bridge Collector，把电脑上的微信 4.x 本地消息接入 bridge-agent，并完成安装、权限、启动、注册和验证。
allowed-tools: "Bash,Read,Grep,Glob"
version: "1.0.0"
---

# WeChat Bridge Collector 本机配置助手

## 什么时候使用
当用户想在自己的电脑上配置 `wechat-bridge-collector`，让本机微信 4.x 消息通过 `bridge-agent` 进入工作区，并希望 AI 帮他完成安装、权限检查、服务注册和验证时，使用本技能。

## 目标
把用户电脑配置成可被 bridge-agent 调用的本地微信采集节点：

1. `bridge-agent` 在本机运行，默认地址 `http://127.0.0.1:18081`。
2. `wechat-bridge-collector` 安装成功。
3. `wechat-bridge-collector setup` 能生成配置和 key 文件。
4. `wechat-bridge-collector probe` 能读取本机微信数据库。
5. `wechat-bridge-collector install-autostart` 能安装平台自启入口。
6. `wechat-bridge-collector start` 能触发后台启动并返回。
7. `wechat-bridge-collector register` 能向 bridge-agent 注册 `wechatLocal` 服务。
8. 本机方法服务可用，默认 `http://127.0.0.1:18082/health` 返回成功。

## 安全边界
- 不索要、不保存用户微信账号密码。
- 不读取或输出 `~/.wechat-bridge-collector/all_keys.json` 的内容。
- 不读取或输出 bridge-agent token。
- 只在用户明确同意时执行需要管理员权限的命令。
- 不修改微信数据库，只做只读读取和本地事件转发。
- 只把必要状态返回给用户：安装位置、服务状态、健康检查结果、下一步动作。

## 默认信息
- collector 仓库：`https://github.com/momoplan/wechat-bridge-collector.git`
- collector 默认目录：`~/baijimu-wechat-bridge/wechat-bridge-collector`
- collector 状态目录：`~/.wechat-bridge-collector`
- collector 服务名：`wechatLocal`
- collector 事件名：`messageReceived`
- collector method server：`http://127.0.0.1:18082`
- bridge-agent：`http://127.0.0.1:18081`
- macOS LaunchAgent：`~/Library/LaunchAgents/com.baijimu.wechat-bridge-collector.plist`
- Windows Startup：`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\BaijimuWeChatCollector.cmd`

## 执行流程

### 1. 判断系统和环境
先运行：

```bash
uname -a
python3 --version
git --version
curl -sS http://127.0.0.1:18081/health
```

判断：
- macOS：优先使用 `scripts/setup_macos.sh`，或直接使用 collector 内置 `install-autostart/start`。
- Windows：按 README 手动安装 collector 后，使用 collector 内置 `install-autostart/start`。
- Linux：先确认 Python、Git、WeChat 4.x、bridge-agent；当前没有官方自启集成。
- bridge-agent 不通：先让用户安装并启动 bridge-agent，再继续 collector 配置。

### 2. macOS 推荐一键配置
如果当前机器是 macOS，优先从技能目录执行：

```bash
bash {baseDir}/scripts/setup_macos.sh
```

脚本会做：
- 检查 Python/Git/curl。
- 检查 bridge-agent 本机健康状态。
- clone 或更新 `wechat-bridge-collector`。
- 初始化 venv 并 `pip install .`。
- 执行 `wechat-bridge-collector setup`。
- 执行 `wechat-bridge-collector probe`。
- 写入 LaunchAgent。
- 启动 collector。
- 调用 `/health` 和 `register` 验证。

如果 setup 因 macOS `task_for_pid` 被拦截失败，按错误提示处理：

```bash
cd ~/baijimu-wechat-bridge/wechat-bridge-collector
sudo .venv/bin/wechat-bridge-collector setup --force
```

如果命令提示已重签 WeChat，需要让用户完全退出并重新打开微信，再重跑：

```bash
bash {baseDir}/scripts/setup_macos.sh
```

### 3. 手动配置流程
当脚本不可用或用户不想使用脚本时，按下面步骤执行：

```bash
mkdir -p ~/baijimu-wechat-bridge
cd ~/baijimu-wechat-bridge
git clone --recurse-submodules https://github.com/momoplan/wechat-bridge-collector.git
cd wechat-bridge-collector
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install .
.venv/bin/wechat-bridge-collector setup
.venv/bin/wechat-bridge-collector probe
.venv/bin/wechat-bridge-collector install-autostart
.venv/bin/wechat-bridge-collector start
.venv/bin/wechat-bridge-collector register
```

### 4. 验证
配置后必须验证：

```bash
curl -sS http://127.0.0.1:18082/health
.venv/bin/wechat-bridge-collector status
.venv/bin/wechat-bridge-collector register
```

再用本机方法查询：

```bash
curl -sS http://127.0.0.1:18082/invoke/getRecentSessions \
  -H 'Content-Type: application/json' \
  -d '{"limit":5}'
```

成功标准：
- `/health` 返回成功。
- `status` 返回 `running`。
- `register` 返回成功。
- `getRecentSessions` 返回 `success: true`。
- bridge-agent 小客户端或工作区能看到 `wechatLocal`。

### 5. 常见问题处理

#### bridge-agent 不通
先让用户启动 bridge-agent。不要继续配置 collector，因为注册和事件转发一定失败。

#### 找不到微信数据库
确认用户安装并登录的是微信 4.x，且微信至少打开过一次。再运行：

```bash
.venv/bin/wechat-bridge-collector setup --force
```

#### macOS 权限拦截
如果看到 `task_for_pid`，需要管理员权限执行 setup。执行后可能需要重启微信再重跑。

#### 端口冲突
如果 `18082` 被占用，改用其他端口：

```bash
.venv/bin/wechat-bridge-collector --method-port 18083 run --register
```

同时更新 LaunchAgent 的 `--method-port`。

#### 注册失败
检查 bridge-agent token 是否可从本机配置自动读取。如果不能，要求用户提供运行环境变量，不要打印 token：

```bash
export BRIDGE_AGENT_SERVICE_REGISTRATION_TOKEN='...'
export BRIDGE_AGENT_EVENT_TOKEN='...'
.venv/bin/wechat-bridge-collector register
```

#### 启动后没有历史消息
这是正常行为。首次启动默认只建立游标，不广播历史消息。需要临时回放最近 5 分钟时才执行：

```bash
.venv/bin/wechat-bridge-collector run --register --reset-state --backfill-seconds 300
```

## 输出给用户
完成后简洁返回：
- 安装目录
- LaunchAgent 状态
- collector health 状态
- bridge-agent 注册状态
- `wechatLocal` 已注册的方法列表
- 如有失败，明确失败步骤和下一条命令

不要输出密钥文件内容、token 或微信本地消息正文。
