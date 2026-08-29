---
name: wechat-bridge-collector-setup
description: 帮用户在本机配置 WeChat Bridge Collector，把电脑上的微信 4.x 本地消息接入 bridge-agent，并完成安装、权限、启动、注册和验证。
allowed-tools: "Bash,Read,Grep,Glob"
version: "2.0.0"
---

# WeChat Bridge Collector 本机配置助手

## 什么时候使用
当用户想在自己的电脑上配置 `wechat-bridge-collector`，让本机微信 4.x 消息通过 `bridge-agent` 进入工作区，并希望 AI 帮他完成安装、权限检查、服务注册和验证时，使用本技能。

## 目标
把用户电脑配置成可被 bridge-agent 调用的本地微信采集节点：

1. `bridge-agent` 在本机运行，默认地址 `http://127.0.0.1:18081`。
2. `wechat-bridge-collector` 安装成功。
3. `wechat-bridge-collector-python setup` 能生成配置和 key 文件。
4. macOS 完全磁盘访问权限授予签名后的“百积木”应用，而不是 Python、Terminal 或独立 LaunchAgent。
5. Connector 从百积木应用详情页由用户显式启动，并完成权限预检。
6. 本机方法服务可用，默认 `http://127.0.0.1:18082/health` 返回成功。

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
- collector 事件名：`messageReceived`；payload 的 `accountId` 标识归属工作微信账号
- collector method server：`http://127.0.0.1:18082`
- bridge-agent：`http://127.0.0.1:18081`
- 旧版 macOS LaunchAgent（必须迁移删除）：`~/Library/LaunchAgents/com.baijimu.wechat-bridge-collector.plist`

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
- macOS：优先使用 `scripts/setup_macos.sh` 完成源码和 key 初始化；系统权限与启动必须回到百积木应用中完成。
- Windows：按 README 安装 Connector，再从百积木应用中启动。
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
- 删除旧版遗留的 LaunchAgent。
- 打开 macOS 完全磁盘访问设置。
- 提示用户把“百积木”加入并开启，重启百积木后从应用详情页启动。

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
git clone https://github.com/momoplan/wechat-bridge-collector.git
cd wechat-bridge-collector
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install .
.venv/bin/wechat-bridge-collector-python setup
```

随后在百积木中安装 WeChat Connector，打开“完全磁盘访问设置”，给“百积木”授权并重启百积木，最后在应用详情页点击“启动应用”。不要创建 LaunchAgent，也不要从 Terminal 直接长期运行 collector。

### 4. 验证
配置后必须验证：

```bash
curl -sS http://127.0.0.1:18082/health
.venv/bin/wechat-bridge-collector-python status
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
- `getRecentSessions` 返回 `success: true`。
- bridge-agent 小客户端或工作区能看到 `wechatLocal`。
- `launchctl print gui/$(id -u)/com.baijimu.wechat-bridge-collector` 返回找不到服务。

### 5. 常见问题处理

#### bridge-agent 不通
先让用户启动 bridge-agent。不要继续配置 collector，因为注册和事件转发一定失败。

#### 找不到微信数据库
确认用户安装并登录的是微信 4.x，且微信至少打开过一次。再运行：

```bash
.venv/bin/wechat-bridge-collector-python setup --force
```

#### macOS 权限拦截
如果看到 `task_for_pid`，需要管理员权限执行 setup。执行后可能需要重启微信再重跑。读取微信沙盒数据库的完全磁盘访问权限必须授予“百积木”；不要给 Connector 私有 Python 单独授权。

#### 端口冲突
如果 `18082` 被占用，在百积木的 Connector 配置中修改 `methodPort`，保存后重新启动应用。不要绕过百积木从 Terminal 长期运行。

#### 注册失败
重新同步或重新安装 Connector，并检查百积木本机服务注册状态。不要输出 bridge-agent token，也不要用 Terminal 启动另一份 collector 作为补偿。

#### 启动后没有历史消息
这是正常行为。首次启动默认只建立游标，不广播历史消息。生产 Connector 不从 Terminal 直接回放；确需历史回放时先停止应用实例，再按维护流程显式执行并在结束后恢复应用实例。

## 输出给用户
完成后简洁返回：
- 安装目录
- 完全磁盘访问授权步骤与旧 LaunchAgent 清理状态
- collector health 状态
- bridge-agent 本机服务状态
- `wechatLocal` 已注册的方法列表
- 如有失败，明确失败步骤和下一条命令

不要输出密钥文件内容、token 或微信本地消息正文。
