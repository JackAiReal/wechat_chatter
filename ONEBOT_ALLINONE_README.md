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

默认端口：
- 外部触发端口：`127.0.0.1:3222`
- onebot内部端口：`127.0.0.1:3223`
- 回调端口：`127.0.0.1:18888`

## 便捷 API（推荐）

### 1) 发送文本

`POST /api/send_text`

```json
{
  "target_id": "wxid_xxx 或 123@chatroom",
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

## 原生 onebot 接口（仍可用）

- `/send_private_msg`
- `/send_group_msg`
- `/download_media`
- `/ws`

## 能力边界

当前发送侧稳定支持：
- text / at / image / video

文件/语音发送在该仓库当前实现中未直接暴露发送能力；
接收侧会对图/视频/文件/语音做解析并回调（带本地 URL 字段）。
