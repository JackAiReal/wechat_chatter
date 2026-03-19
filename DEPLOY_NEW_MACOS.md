# wechat_chatter 新 Mac 部署文档

> 适用场景：在一台全新的 macOS 机器上，把当前这套 `wechat_chatter + onebot_allinone` 部署并跑起来。
>
> 本文按**推荐方案：不关闭 SIP，使用 FridaGadget**来写；文末补充了**关闭 SIP、直接 attach** 的替代方案。

---

## 1. 部署目标

部署完成后，你将得到：

- 一个可运行的 WeChat hook 环境
- 一个可运行的 `onebot` 进程
- 一个可运行的 `onebot_allinone.py` 聚合服务
- 对外 HTTP 入口：`127.0.0.1:3222`
- OneBot 内部端口：`127.0.0.1:3223`
- 回调监听端口：`127.0.0.1:18888`

---

## 2. 关键前提

这个项目能不能成功部署，最关键看下面三件事：

1. **微信版本必须与配置文件匹配**
2. **Frida 相关组件版本尽量一致**
3. **配置文件里的绝对路径必须改成新机器自己的路径**

### 当前项目内已有的微信版本配置

位于：`wechat_version/`

- `4_1_6_12_mac.json`
- `4_1_6_46_mac.json`
- `4_1_6_47_mac.json`
- `4_1_7_31_mac.json`
- `4_1_7_55_mac.json`
- `4_1_7_57_mac.json`

### 当前默认配置使用的是

```json
"wechat_conf": "../wechat_version/4_1_7_57_mac.json"
```

**推荐：安装与 `4_1_7_57_mac.json` 对应的微信版本。**

否则常见现象是：

- hook 偏移不匹配
- 发消息异常
- attach 正常但功能不正常
- 启动后崩溃或无响应

---

## 3. 推荐方案说明

### 推荐方案：不关闭 SIP，使用 FridaGadget

优点：

- 不需要关闭系统 SIP
- 更适合新机器复现
- 对日常系统环境影响更小

缺点：

- 需要对 `WeChat.app` 注入 `FridaGadget.dylib`
- 需要重新签名

---

## 4. 基础环境准备

默认假设：

- macOS 为 Apple Silicon / arm64
- 项目路径为：`~/work/wechat_chatter`
- 微信安装在：`/Applications/WeChat.app`

### 4.1 安装 Xcode Command Line Tools

```bash
xcode-select --install
```

### 4.2 安装 Homebrew（如果未安装）

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### 4.3 安装基础依赖

```bash
brew install git go python wget xz
```

### 4.4 安装 Frida CLI（建议）

```bash
pip3 install --user frida-tools
```

> 用于排查 gadget 是否启动、脚本是否能挂上。

---

## 5. 获取项目代码

如果要复现当前这套环境，建议直接 clone 你自己的 fork：

```bash
mkdir -p ~/work
cd ~/work
git clone https://github.com/JackAiReal/wechat_chatter.git
cd wechat_chatter
```

如果你想固定到某个已知可用提交，可自行 checkout，例如：

```bash
git checkout 76793e0
```

> 如果你想直接跟随最新 `main`，那就不用 checkout。

---

## 6. 准备 Frida devkit

当前项目默认配置里要求：

- `devkit_dir` 指向 `wechat_chatter/.frida-devkit`

先检查：

```bash
ls -la .frida-devkit
```

理想情况下，这个目录里至少要有：

- `frida-core.h`
- `libfrida-core.a`

### 如果目录不存在或内容不完整

以 arm64 + 17.8.2 为例：

```bash
mkdir -p .frida-devkit
cd .frida-devkit
curl -L -o frida-core-devkit-17.8.2-macos-arm64.tar.xz \
  https://github.com/frida/frida/releases/download/17.8.2/frida-core-devkit-17.8.2-macos-arm64.tar.xz
tar -xf frida-core-devkit-17.8.2-macos-arm64.tar.xz
cd ..
```

> 如果是 Intel Mac，请改为 x86_64 对应包。
>
> 最好让 `frida-core-devkit`、`frida-gadget`、本机安装的 `frida-tools` 版本尽量同一代。

---

## 7. 安装 insert_dylib

SIP 不关闭时，需要用它给 WeChat 主程序注入 FridaGadget。

```bash
cd ~/work
git clone https://github.com/Tyilo/insert_dylib.git
cd insert_dylib
xcodebuild
sudo cp build/Release/insert_dylib /usr/local/bin/
```

验证：

```bash
which insert_dylib
```

---

## 8. 注入 FridaGadget 到 WeChat

### 8.1 下载 FridaGadget

建议使用与 devkit 同一代版本。以 17.8.2 为例：

```bash
cd ~/work/wechat_chatter
curl -L -o /tmp/frida-gadget-17.8.2-macos-universal.dylib.xz \
  https://github.com/frida/frida/releases/download/17.8.2/frida-gadget-17.8.2-macos-universal.dylib.xz

xz -dk /tmp/frida-gadget-17.8.2-macos-universal.dylib.xz
sudo cp /tmp/frida-gadget-17.8.2-macos-universal.dylib \
  /Applications/WeChat.app/Contents/Frameworks/FridaGadget.dylib

sudo chmod +x /Applications/WeChat.app/Contents/Frameworks/FridaGadget.dylib
```

### 8.2 注入 WeChat 主程序

```bash
sudo insert_dylib --inplace --strip-codesig \
  "@executable_path/../Frameworks/FridaGadget.dylib" \
  /Applications/WeChat.app/Contents/MacOS/WeChat
```

### 8.3 拷贝 Gadget 配置文件

```bash
sudo cp frida-gadget/FridaGadget.config /Applications/WeChat.app/Contents/Frameworks/
```

### 8.4 重新签名 WeChat

项目已自带签名脚本：

```bash
./frida-gadget/sign.sh
```

如果微信启动仍然异常，再补一遍深签名：

```bash
sudo codesign --force --deep --sign - /Applications/WeChat.app
```

如果系统拦截了 app，也可以尝试移除 quarantine：

```bash
sudo xattr -dr com.apple.quarantine /Applications/WeChat.app
```

---

## 9. 启动微信并登录

完成注入后，手动启动微信：

```bash
open /Applications/WeChat.app
```

要求：

- 微信能正常打开
- 能正常登录
- 能正常收发消息

> 这一步是基础。微信本体如果都没跑稳，后续 onebot 不会稳。

---

## 10. 修改 onebot_allinone 配置

当前仓库里的 `onebot_allinone.json` 使用的是旧机器上的绝对路径，必须改成新机器自己的路径。

### 10.1 当前配置参考

```json
{
  "onebot_dir": "/Users/jackgong/.openclaw/workspace-idreadl/wechat_chatter/onebot",
  "devkit_dir": "/Users/jackgong/.openclaw/workspace-idreadl/wechat_chatter/.frida-devkit",
  "onebot_bin": "onebot",
  "wechat_conf": "../wechat_version/4_1_7_57_mac.json",
  "gadget_addr": "127.0.0.1:27042",
  "token": "MuseBot",
  "log_level": "info",
  "send_interval": 1000,
  "conn_type": "http",
  "trigger_listen": "127.0.0.1:3222",
  "onebot_internal_listen": "127.0.0.1:3223",
  "callback_listen": "127.0.0.1:18888",
  "callback_path": "/onebot",
  "callback_dump_jsonl": "",
  "auto_build": true
}
```

### 10.2 新机器示例配置

假设你的用户名是 `yourname`，项目路径为 `~/work/wechat_chatter`：

```json
{
  "onebot_dir": "/Users/yourname/work/wechat_chatter/onebot",
  "devkit_dir": "/Users/yourname/work/wechat_chatter/.frida-devkit",
  "onebot_bin": "onebot",

  "wechat_conf": "../wechat_version/4_1_7_57_mac.json",

  "gadget_addr": "127.0.0.1:27042",
  "token": "MuseBot",
  "log_level": "info",
  "send_interval": 1000,
  "conn_type": "http",

  "trigger_listen": "127.0.0.1:3222",
  "onebot_internal_listen": "127.0.0.1:3223",

  "callback_listen": "127.0.0.1:18888",
  "callback_path": "/onebot",
  "callback_dump_jsonl": "",

  "auto_build": true
}
```

### 10.3 关键字段说明

- `onebot_dir`：onebot 源码目录
- `devkit_dir`：Frida devkit 目录
- `onebot_bin`：编译产物名称
- `wechat_conf`：必须与你安装的微信版本匹配
- `gadget_addr`：FridaGadget 默认监听地址
- `trigger_listen`：对外触发 API 端口
- `onebot_internal_listen`：内部 onebot 监听端口
- `callback_listen`：微信回调监听端口
- `auto_build=true`：首次启动时自动构建 onebot

---

## 11. 启动 all-in-one

在项目根目录执行：

```bash
cd ~/work/wechat_chatter
python3 onebot_allinone.py --config onebot_allinone.json
```

### 正常情况下你会看到

- callback 服务启动
- trigger 服务启动
- onebot 自动构建或启动
- Frida 脚本加载成功
- WeChat 控制通道打通

---

## 12. 最小验证流程

### 12.1 查询能力

```bash
curl http://127.0.0.1:3222/api/capabilities
```

正常应返回：

- `/api/send_text`
- `/api/send_image`
- `/api/send_video`
- `/api/send_at`
- `/api/download_media`
- passthrough：`/send_private_msg`、`/send_group_msg`、`/download_media`、`/ws`

### 12.2 发送文本测试

```bash
curl -X POST http://127.0.0.1:3222/api/send_text \
  -H "Content-Type: application/json" \
  -d '{
    "target_id": "wxid_xxx",
    "text": "hello from new mac"
  }'
```

如果微信成功发出这条消息，说明主链路已通。

---

## 13. 当前已兼容的主要接口

### 对外便捷 API（3222）

- `POST /api/send_text`
- `POST /api/send_image`
- `POST /api/send_video`
- `POST /api/send_at`
- `POST /api/download_media`
- `GET /api/capabilities`

### 透传 / 原生接口

- `POST /send_private_msg`
- `POST /send_group_msg`
- `POST /download_media`
- `WS /ws`

### WebSocket 已处理 action

- `get_login_info`
- `get_group_member_info`
- `send_private_msg`
- `send_group_msg`

### 当前稳定支持的发送能力

- 文本
- 群 @
- 图片
- 视频

### 接收回调已结构化处理的消息类型

- `text`
- `record`
- `image`
- `video`
- `file`
- `face`

### 文件下载通知类型

- `notice_type: download`
- `download_status: queued`
- `download_status: done`
- `download_status: failed`

---

## 14. 常见坑与处理办法

### 14.1 图片 / 视频发送报 `need_init_upload_context`

原因：微信客户端的上传上下文尚未初始化。

处理办法：

- 先在微信客户端里**手动发一张图片 / 视频**
- 再调用 `/api/send_image` 或 `/api/send_video`

---

### 14.2 下载媒体报 `need_init_download_context` 或超时

原因：下载上下文没准备好，或该消息的 CDN 信息不完整。

处理办法：

- 先在微信里手动点开一次图片 / 文件
- 再重试下载

---

### 14.3 文件消息自动下载失败

当前项目已做成“异步两阶段回调”，失败不会阻塞主消息回调。

常见原因：

- `cdnattachurl` 缺失
- `aeskey` 缺失
- 文件尚未初始化下载上下文

此时你通常会收到：

- 主消息回调正常返回
- 之后再收到一条 `download_status=failed`

---

### 14.4 微信能打开，但 onebot 不工作

优先检查：

1. 微信版本是否与 `wechat_conf` 匹配
2. `FridaGadget.dylib` 是否成功注入
3. 是否已完成签名
4. `gadget_addr` 是否可连
5. `.frida-devkit` 是否完整

你可以用 Frida 手动验证 Gadget 是否在线：

```bash
frida -H 127.0.0.1:27042 -n Gadget
```

如果连不上，通常就是 gadget 没起来。

---

## 15. 替代方案：关闭 SIP，直接 attach

> 不推荐日常主力机使用，仅适合研究 / 调试环境。

### 15.1 在 Recovery 中关闭 SIP

进入恢复模式后执行：

```bash
csrutil disable
```

重启回系统。

### 15.2 修改配置为 local 模式

把 `onebot_allinone.json` 改成：

```json
{
  "onebot_dir": "/Users/yourname/work/wechat_chatter/onebot",
  "devkit_dir": "/Users/yourname/work/wechat_chatter/.frida-devkit",
  "onebot_bin": "onebot",
  "wechat_conf": "../wechat_version/4_1_7_57_mac.json",
  "frida_type": "local",
  "token": "MuseBot",
  "log_level": "info",
  "send_interval": 1000,
  "conn_type": "http",
  "trigger_listen": "127.0.0.1:3222",
  "onebot_internal_listen": "127.0.0.1:3223",
  "callback_listen": "127.0.0.1:18888",
  "callback_path": "/onebot",
  "auto_build": true
}
```

如果要显式指定微信进程，也可以加：

```json
"wechat_pid": 12345
```

### 15.3 启动

```bash
cd ~/work/wechat_chatter
python3 onebot_allinone.py --config onebot_allinone.json
```

这种方式不需要给 WeChat 注入 gadget，但系统层面的风险更高。

---

## 16. 推荐的可重复部署策略

为了让新机器部署更稳定，建议你固定以下内容：

- 固定微信版本
- 固定项目提交
- 固定 Frida 版本
- 固定 `wechat_conf`
- 默认走 gadget 方案，不关闭 SIP

这样未来换机器时最容易复现。

---

## 17. 一份极简部署清单

如果只看最短 checklist，按下面做：

1. 安装 `git go python wget xz`
2. clone 项目
3. 安装与 `wechat_conf` 匹配的微信版本
4. 准备 `.frida-devkit`
5. 编译 `insert_dylib`
6. 注入 `FridaGadget.dylib` 到 WeChat
7. 重新签名 WeChat
8. 修改 `onebot_allinone.json` 的绝对路径
9. 登录微信
10. 启动：

```bash
python3 onebot_allinone.py --config onebot_allinone.json
```

11. 验证：

```bash
curl http://127.0.0.1:3222/api/capabilities
```

---

## 18. 后续可扩展项

如果你后续要把这份部署文档做得更完整，建议再补：

- 一键部署脚本 `deploy_new_mac.sh`
- 一键检测脚本 `doctor.sh`
- 微信版本 / 配置文件对应表
- 常见报错截图与处理流程
- 多实例部署说明

---

## 19. 当前启动命令（参考）

```bash
cd /Users/yourname/work/wechat_chatter
python3 onebot_allinone.py --config onebot_allinone.json
```

---

如果这份文档要进一步整理成**面向别人交付**的版本，建议把所有 `yourname`、微信版本、Frida 版本、仓库地址替换成你的最终标准值。