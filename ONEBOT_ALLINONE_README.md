# OneBot All-in-One (Python)

单进程启动：
- onebot 主程序
- 回调监听服务（打印微信回调）
- 触发代理服务（打印你发起的HTTP请求）

## 启动

```bash
cd /Users/jackgong/.openclaw/workspace-idreadl/wechat_chatter
python3 onebot_allinone.py --config onebot_allinone.json
```

默认端口（单实例配置）：
- 外部触发端口：`127.0.0.1:3222`
- onebot内部端口：`127.0.0.1:3223`
- 回调端口：`127.0.0.1:18888`

> 现已支持多实例：在配置里使用 `instances` 数组可同时拉起多个 onebot（每个实例指定不同 `onebot_internal_listen`，建议指定 `wechat_pid`）。

## 便捷 API（推荐）

多实例时，请在请求体里增加：`"instance": "wx1"`（实例名由配置决定）。

### 1) 发送文本

`POST /api/send_text`

```json
{
  "target_id": "wxid_xxx 或 123@chatroom",
  "text": "你好"
}
```

多实例示例：

```json
{
  "instance": "wx1",
  "target_id": "wxid_xxx",
  "text": "你好"
}
```

### 2) 发送图片

`POST /api/send_image`

```json
{
  "target_id": "wxid_xxx 或 123@chatroom",
  "file": "base64://... 或 file:///... 或 http(s)://..."
}
```

### 3) 发送视频

`POST /api/send_video`

```json
{
  "target_id": "wxid_xxx 或 123@chatroom",
  "file": "base64://... 或 file:///... 或 http(s)://..."
}
```

### 4) 群@文本

`POST /api/send_at`

```json
{
  "target_id": "123@chatroom",
  "at_user": "wxid_a,wxid_b",
  "text": "请看一下"
}
```

支持 `@所有人`：

```json
{
  "target_id": "123@chatroom",
  "at_user": "notify@all",
  "text": "请大家看公告"
}
```

其中 `at_user` 也兼容写法：`all` / `@all` / `所有人` / `全体`（会自动归一化为 `notify@all`）。

## 多开微信（多实例）

1) 复制 `onebot_allinone.multi.example.json` 为你自己的配置文件并填写每个微信实例对应的 `wechat_pid`。
2) 每个实例必须使用不同的 `onebot_internal_listen` 和 `callback_path`。
3) 启动命令：

```bash
python3 onebot_allinone.py --config onebot_allinone.multi.example.json
```

### 5) 手动下载媒体并落地

`POST /api/download_media`

```json
{
  "target_id": "wxid_xxx 或 123@chatroom",
  "cdn_url": "https://...",
  "aes_key": "xxxxxxxx",
  "file_type": 2,
  "file_path": "/tmp/demo.jpg"
}
```

`file_type`：
- `1` HdImage
- `2` Image
- `3` ThumbImage
- `4` Video
- `5` File

不传 `file_path` 会自动写到 onebot 目录下 `file/manual_download/`。

> 若日志出现 `need_init_download_context` / `expected a pointer`，请先在微信客户端里手动点开一次图片或文件，初始化下载上下文后再重试。

## 原生 onebot 接口（仍可用）

- `/send_private_msg`
- `/send_group_msg`
- `/download_media`
- `/ws`

多实例直通（HTTP）可用前缀：
- `/i/<instance>/send_private_msg`
- `/i/<instance>/send_group_msg`
- `/i/<instance>/download_media`

查询能力与实例列表：
- `GET /api/capabilities`

## 回调转发（可选）

在 `onebot_allinone.json` 里可配置：

```json
{
  "callback_forward_enabled": true,
  "callback_forward_url": "https://example.com/wechat/callback",
  "callback_forward_timeout_ms": 8000,
  "callback_forward_headers": {
    "Authorization": "Bearer YOUR_TOKEN"
  }
}
```

开启后，onebot 的本地回调会继续保留，同时会额外 POST 一份同样的 JSON 到 `callback_forward_url`（best-effort，不阻塞本地回调返回）。

## 能力边界

当前发送侧稳定支持：
- text / at / image / video

文件/语音发送在该仓库当前实现中未直接暴露发送能力；
接收侧会对图/视频/文件/语音做解析并回调（带本地 URL 字段）。
