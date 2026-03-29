# WeChatBridge（macOS）3 步启动指南

## 第 1 步：安装并登录指定微信版本
1. 安装与你的 `wechat_conf` 匹配的微信版本（默认：`4_1_7_57_mac.json`）。
2. 打开微信并完成登录。
3. 确认微信可正常手动收发消息。

---

## 第 2 步：安装并配置 WeChatBridge
1. 双击挂载 `WeChatBridge-*.dmg`，把 `WeChatBridge.app` 拖到 `Applications`。
2. 双击启动一次 `WeChatBridge.app`（首次会自动生成配置并打开编辑器）。
3. 编辑配置文件（两种方式选一种）：
   - 方式 A：浏览器打开控制台（推荐）：`http://127.0.0.1:3222/bridge`
   - 方式 B：直接编辑文件：`~/Library/Application Support/WeChatBridge/config.json`
   - 重点字段：
     - `trigger_listen`（默认 `127.0.0.1:3222`）
     - `callback_forward_enabled`（是否转发回调）
     - `callback_forward_url`（你的回调接口 URL）
     - `callback_forward_headers`（如 Bearer Token）

> 配置里 `__USER_RUNTIME__` 占位符会在启动时自动替换为本机运行目录。

---

## 第 3 步：启动并验证 API
1. 再次双击启动 `WeChatBridge.app`。
   - 若后台已运行，再次双击不会重复注入微信，只会直接打开控制台。
2. 验证能力接口：

```bash
curl http://127.0.0.1:3222/api/capabilities
```

3. 发送文本测试：

```bash
curl -X POST http://127.0.0.1:3222/api/send_text \
  -H 'Content-Type: application/json' \
  -d '{"target_id":"filehelper","text":"hello from WeChatBridge"}'
```

4. 群 @all 测试：

```bash
curl -X POST http://127.0.0.1:3222/api/send_at \
  -H 'Content-Type: application/json' \
  -d '{"target_id":"123@chatroom","at_user":"notify@all","text":"请大家看一下"}'
```

---

## 回调转发说明
当 `callback_forward_enabled=true` 且 `callback_forward_url` 非空时：
- 本地回调仍会正常响应（不阻塞 onebot）
- 同一份回调 JSON 会 **额外 POST** 到你的 `callback_forward_url`
- 会附带 Header：
  - `X-OneBot-Instance`
  - `X-OneBot-Callback-Path`
  - 以及你在 `callback_forward_headers` 里定义的 Header

---

## 日志与运行目录
- 主日志：`~/Library/Logs/WeChatBridge/bridge.log`
- 运行时目录：`~/Library/Application Support/WeChatBridge/runtime`
- 回调事件落盘（若开启）：`callback_dump_jsonl` 指定路径

---

## 常见问题
1. `send_image/send_video` 报 `need_init_upload_context`
   - 先在微信客户端手动发一张图/视频，再重试 API。
2. API 可访问但发不出去
   - 检查微信版本与 `wechat_conf` 是否匹配。
3. 回调没到你的服务
   - 检查 `callback_forward_enabled`、URL、鉴权 Header、目标服务可达性。
