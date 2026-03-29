#!/usr/bin/env python3
"""
OneBot all-in-one launcher:
- starts a callback HTTP server (prints inbound callbacks; can optionally forward to your webhook)
- starts a trigger proxy HTTP server (prints outbound API calls)
- starts one or more onebot processes and wires send_url -> callback server

Convenience APIs (on trigger_listen):
- POST /api/send_text
- POST /api/send_image
- POST /api/send_video
- POST /api/send_at
- POST /api/download_media

Multi-instance notes:
- Configure `instances` in JSON to launch multiple onebot workers.
- For /api/* requests, pass `"instance": "<name>"` when multiple instances exist.
- Non-API passthrough can target an instance with /i/<name>/... prefix.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen


@dataclass
class InstanceConfig:
    name: str
    wechat_conf: str
    onebot_internal_listen: str
    callback_path: str

    frida_type: str = "gadget"
    gadget_addr: str = "127.0.0.1:27042"
    token: str = "MuseBot"
    log_level: str = "info"
    send_interval: int = 1000
    conn_type: str = "http"
    callback_dump_jsonl: str = ""
    wechat_pid: int = 0


@dataclass
class Config:
    onebot_dir: str
    devkit_dir: str
    onebot_bin: str

    # shared listener ports
    trigger_listen: str = "127.0.0.1:3222"
    callback_listen: str = "127.0.0.1:18888"

    # optional callback forwarding (best-effort)
    callback_forward_enabled: bool = False
    callback_forward_url: str = ""
    callback_forward_timeout_ms: int = 8000
    callback_forward_headers: Optional[Dict[str, str]] = None

    auto_build: bool = True

    # backward-compatible single-instance fields
    wechat_conf: str = "../wechat_version/4_1_7_57_mac.json"
    frida_type: str = "gadget"
    gadget_addr: str = "127.0.0.1:27042"
    token: str = "MuseBot"
    log_level: str = "info"
    send_interval: int = 1000
    conn_type: str = "http"
    onebot_internal_listen: str = "127.0.0.1:3223"
    callback_path: str = "/onebot"
    callback_dump_jsonl: str = ""
    wechat_pid: int = 0

    # optional multi-instance config
    instances: Optional[List[Dict[str, Any]]] = None


def load_config(path: Path) -> Config:
    data = json.loads(path.read_text(encoding="utf-8"))
    return Config(**data)


def build_instances(cfg: Config) -> List[InstanceConfig]:
    if cfg.instances:
        out: List[InstanceConfig] = []
        for idx, raw in enumerate(cfg.instances):
            if not isinstance(raw, dict):
                raise ValueError(f"instances[{idx}] must be object")

            name = str(raw.get("name", "")).strip() or f"inst{idx + 1}"
            wechat_conf = str(raw.get("wechat_conf", cfg.wechat_conf)).strip()
            onebot_internal_listen = str(raw.get("onebot_internal_listen", "")).strip()
            if not onebot_internal_listen:
                raise ValueError(f"instances[{idx}] missing onebot_internal_listen")

            callback_path = str(raw.get("callback_path", f"/onebot/{name}")).strip()
            if not callback_path.startswith("/"):
                callback_path = "/" + callback_path

            out.append(
                InstanceConfig(
                    name=name,
                    wechat_conf=wechat_conf,
                    onebot_internal_listen=onebot_internal_listen,
                    callback_path=callback_path,
                    frida_type=str(raw.get("frida_type", cfg.frida_type)).strip() or cfg.frida_type,
                    gadget_addr=str(raw.get("gadget_addr", cfg.gadget_addr)).strip() or cfg.gadget_addr,
                    token=str(raw.get("token", cfg.token)).strip() or cfg.token,
                    log_level=str(raw.get("log_level", cfg.log_level)).strip() or cfg.log_level,
                    send_interval=int(raw.get("send_interval", cfg.send_interval)),
                    conn_type=str(raw.get("conn_type", cfg.conn_type)).strip() or cfg.conn_type,
                    callback_dump_jsonl=str(raw.get("callback_dump_jsonl", cfg.callback_dump_jsonl)).strip(),
                    wechat_pid=int(raw.get("wechat_pid", cfg.wechat_pid or 0)),
                )
            )
    else:
        out = [
            InstanceConfig(
                name="default",
                wechat_conf=cfg.wechat_conf,
                onebot_internal_listen=cfg.onebot_internal_listen,
                callback_path=cfg.callback_path,
                frida_type=cfg.frida_type,
                gadget_addr=cfg.gadget_addr,
                token=cfg.token,
                log_level=cfg.log_level,
                send_interval=cfg.send_interval,
                conn_type=cfg.conn_type,
                callback_dump_jsonl=cfg.callback_dump_jsonl,
                wechat_pid=cfg.wechat_pid,
            )
        ]

    names = set()
    callbacks = set()
    listens = set()
    for inst in out:
        if inst.name in names:
            raise ValueError(f"duplicate instance name: {inst.name}")
        names.add(inst.name)

        if inst.callback_path in callbacks:
            raise ValueError(f"duplicate callback_path: {inst.callback_path}")
        callbacks.add(inst.callback_path)

        if inst.onebot_internal_listen in listens:
            raise ValueError(f"duplicate onebot_internal_listen: {inst.onebot_internal_listen}")
        listens.add(inst.onebot_internal_listen)

    return out


def split_host_port(addr: str) -> Tuple[str, int]:
    host, port = addr.rsplit(":", 1)
    return host, int(port)


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(prefix: str, msg: str):
    print(f"[{prefix}] {now()} {msg}", flush=True)


def infer_target_fields(target_id: str) -> Dict[str, str]:
    if target_id.endswith("@chatroom"):
        return {"group_id": target_id}
    return {"user_id": target_id}


def normalize_at_user(one: str) -> str:
    v = one.strip()
    if not v:
        return ""

    lowered = v.lower()
    if lowered in ("all", "@all", "notify@all", "所有人", "全体"):
        return "notify@all"

    return v


def build_send_payload(msg_type: str, target_id: str, text: str = "", file_value: str = "", at_user: str = "") -> Dict[str, Any]:
    payload: Dict[str, Any] = {**infer_target_fields(target_id), "message": []}

    if msg_type == "text":
        payload["message"].append({"type": "text", "data": {"text": text}})
    elif msg_type in ("image", "video"):
        payload["message"].append({"type": msg_type, "data": {"file": file_value}})
    elif msg_type == "at_text":
        if at_user:
            for one in [x.strip() for x in at_user.split(",") if x.strip()]:
                normalized = normalize_at_user(one)
                if normalized:
                    payload["message"].append({"type": "at", "data": {"qq": normalized}})
        if text:
            payload["message"].append({"type": "text", "data": {"text": text}})
    else:
        raise ValueError(f"unsupported msg_type: {msg_type}")

    return payload


def read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    body = handler.rfile.read(length) if length > 0 else b"{}"
    if not body:
        return {}
    return json.loads(body.decode("utf-8", errors="ignore"))


def write_json(handler: BaseHTTPRequestHandler, code: int, obj: Dict[str, Any]):
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def forward_http(
    target_url: str,
    method: str,
    body: bytes,
    content_type: Optional[str] = None,
    timeout: float = 20,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, bytes, str]:
    headers: Dict[str, str] = {}
    if content_type:
        headers["Content-Type"] = content_type
    if extra_headers:
        headers.update({str(k): str(v) for k, v in extra_headers.items()})

    req = Request(
        target_url,
        data=body if method in ("POST", "PUT", "PATCH") else None,
        method=method,
        headers=headers,
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "application/json")
    except HTTPError as e:
        return e.code, e.read() if hasattr(e, "read") else b"", "application/json"
    except URLError as e:
        err = json.dumps({"error": f"upstream unavailable: {e}"}, ensure_ascii=False).encode("utf-8")
        return 502, err, "application/json"


def parse_instance_hint(handler: BaseHTTPRequestHandler, body_instance: Optional[str] = None) -> Optional[str]:
    if body_instance:
        return body_instance

    parsed = urlsplit(handler.path)
    q = parse_qs(parsed.query)
    if q.get("instance"):
        return q["instance"][0]

    h = handler.headers.get("X-OneBot-Instance")
    if h:
        return h.strip()

    return None


def init_runtime_state(instances: List[InstanceConfig]) -> Dict[str, Any]:
    return {
        "_lock": threading.Lock(),
        "started_at": int(time.time()),
        "instances": {
            i.name: {
                "running": False,
                "pid": None,
                "frida_ready": False,
                "frida_ready_at": None,
                "last_log": "",
                "last_log_at": None,
                "exit_code": None,
            }
            for i in instances
        },
    }


def update_instance_state(runtime_state: Dict[str, Any], name: str, **kwargs: Any):
    lock = runtime_state.get("_lock")
    if lock is None:
        return
    with lock:
        inst = runtime_state.setdefault("instances", {}).setdefault(name, {})
        inst.update(kwargs)


def append_instance_log(runtime_state: Dict[str, Any], name: str, line: str):
    lock = runtime_state.get("_lock")
    if lock is None:
        return
    now_ts = int(time.time())
    with lock:
        inst = runtime_state.setdefault("instances", {}).setdefault(name, {})
        inst["last_log"] = line[-800:]
        inst["last_log_at"] = now_ts

        if "Frida 已就绪" in line:
            inst["frida_ready"] = True
            inst["frida_ready_at"] = now_ts


def runtime_snapshot(runtime_state: Dict[str, Any]) -> Dict[str, Any]:
    lock = runtime_state.get("_lock")
    if lock is None:
        return {"started_at": int(time.time()), "instances": {}}

    with lock:
        copied: Dict[str, Any] = {
            "started_at": runtime_state.get("started_at"),
            "instances": json.loads(json.dumps(runtime_state.get("instances", {}), ensure_ascii=False)),
        }
    return copied


def make_callback_handler(
    instances: List[InstanceConfig],
    callback_forward_enabled: bool = False,
    callback_forward_url: str = "",
    callback_forward_timeout_ms: int = 8000,
    callback_forward_headers: Optional[Dict[str, str]] = None,
) -> type[BaseHTTPRequestHandler]:
    inst_by_path = {i.callback_path: i for i in instances}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            parsed = urlsplit(self.path)
            path_only = parsed.path
            inst = inst_by_path.get(path_only)
            if not inst:
                write_json(self, 404, {"error": f"unknown callback path: {path_only}"})
                return

            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8", errors="ignore")

            prefix = f"callback:{inst.name}"
            log(prefix, f"{self.command} {self.path}")
            if raw:
                try:
                    data = json.loads(raw)
                    mtype = data.get("message_type")
                    sender = data.get("user_id")
                    gid = data.get("group_id", "")
                    text = data.get("raw_message", "")
                    log(prefix, f"message_type={mtype} sender={sender} group={gid}")
                    if text:
                        print(text, flush=True)

                    # 打印媒体 URL（落地文件路径通常在这里）
                    for seg in data.get("message", []) or []:
                        seg_type = seg.get("type")
                        seg_data = seg.get("data") or {}
                        url = seg_data.get("url")
                        if url:
                            log(prefix, f"media[{seg_type}] url={url}")
                except Exception:
                    print(raw, flush=True)

            if inst.callback_dump_jsonl:
                p = Path(inst.callback_dump_jsonl)
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as f:
                    f.write(raw + "\n")

            if callback_forward_enabled and callback_forward_url:
                merged_headers: Dict[str, str] = {
                    "X-OneBot-Instance": inst.name,
                    "X-OneBot-Callback-Path": path_only,
                }
                if callback_forward_headers and isinstance(callback_forward_headers, dict):
                    merged_headers.update({str(k): str(v) for k, v in callback_forward_headers.items()})

                status, resp_body, _ = forward_http(
                    target_url=callback_forward_url,
                    method="POST",
                    body=raw.encode("utf-8"),
                    content_type="application/json",
                    timeout=max(0.5, float(callback_forward_timeout_ms) / 1000.0),
                    extra_headers=merged_headers,
                )
                log(prefix, f"forward -> {callback_forward_url} status={status}")
                if resp_body:
                    print(resp_body.decode("utf-8", errors="ignore"), flush=True)

            write_json(self, 200, {"ok": True})

        def do_GET(self):
            if self.path == "/":
                write_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "service": "callback",
                        "paths": sorted(inst_by_path.keys()),
                    },
                )
                return
            write_json(self, 404, {"error": "not found"})

        def log_message(self, fmt: str, *args):
            return

    return CallbackHandler


def make_trigger_proxy_handler(
    instances: List[InstanceConfig],
    runtime_state: Optional[Dict[str, Any]] = None,
    managed_config_path: Optional[str] = None,
) -> type[BaseHTTPRequestHandler]:
    inst_by_name = {i.name: i for i in instances}
    default_inst = instances[0]
    multi_mode = len(instances) > 1

    def choose_instance(hint: Optional[str], require_explicit: bool = False) -> Tuple[Optional[InstanceConfig], Optional[str]]:
        if hint:
            inst = inst_by_name.get(hint)
            if not inst:
                return None, f"unknown instance: {hint}"
            return inst, None

        if require_explicit and multi_mode:
            return None, "multiple instances enabled; please specify instance"

        return default_inst, None

    cfg_path = (managed_config_path or "").strip()

    def read_managed_config() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        if not cfg_path:
            return None, "config path not provided"
        p = Path(cfg_path)
        if not p.exists():
            return None, f"config not found: {cfg_path}"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None, "config root must be object"
            return data, None
        except Exception as e:
            return None, f"invalid config json: {e}"

    def write_managed_config(data: Dict[str, Any]) -> Optional[str]:
        if not cfg_path:
            return "config path not provided"
        p = Path(cfg_path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return None
        except Exception as e:
            return str(e)

    dashboard_html = """<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <title>WeChatBridge 控制台</title>
  <style>
    body { font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; margin: 20px; color: #222; }
    h1 { font-size: 20px; margin-bottom: 8px; }
    .row { margin: 8px 0; }
    .ok { color: #0a7f2e; }
    .bad { color: #b42318; }
    textarea { width: 100%; min-height: 320px; font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
    button { margin-right: 8px; padding: 6px 12px; }
    code { background: #f2f4f7; padding: 2px 6px; border-radius: 4px; }
  </style>
</head>
<body>
  <h1>WeChatBridge 控制台</h1>
  <div class=\"row\">服务入口：<code id=\"entry\">http://127.0.0.1:3222</code></div>
  <div class=\"row\" id=\"status\">状态读取中...</div>
  <div class=\"row\">
    <button onclick=\"refreshAll()\">刷新状态</button>
    <button onclick=\"saveConfig()\">保存配置</button>
  </div>
  <div class=\"row\"><strong>当前配置（保存后重启生效）</strong></div>
  <textarea id=\"cfg\" spellcheck=\"false\"></textarea>
  <div class=\"row\" id=\"msg\"></div>

<script>
async function jget(url){ const r=await fetch(url); return [r.status, await r.json()]; }
async function refreshStatus(){
  const [code, data] = await jget('/api/bridge/status');
  const el = document.getElementById('status');
  if(code !== 200){ el.innerHTML = `<span class=\"bad\">状态读取失败: HTTP ${code}</span>`; return; }
  const d = data.instances?.default || Object.values(data.instances || {})[0] || {};
  const ready = d.frida_ready ? '已注入' : '未就绪';
  const cls = d.frida_ready ? 'ok' : 'bad';
  el.innerHTML = `进程状态：allinone=<b>${data.allinone_running ? '运行中' : '未运行'}</b> / onebot=<b>${d.running ? '运行中' : '未运行'}</b> / 注入状态：<b class=\"${cls}\">${ready}</b> ${d.pid ? `(pid=${d.pid})` : ''}`;
}
async function refreshConfig(){
  const [code, data] = await jget('/api/bridge/config');
  const msg = document.getElementById('msg');
  if(code !== 200){ msg.innerHTML = `<span class=\"bad\">读取配置失败: ${JSON.stringify(data)}</span>`; return; }
  document.getElementById('cfg').value = JSON.stringify(data.config, null, 2);
  msg.innerHTML = '';
}
async function saveConfig(){
  const msg = document.getElementById('msg');
  let parsed;
  try { parsed = JSON.parse(document.getElementById('cfg').value); }
  catch(e){ msg.innerHTML = `<span class=\"bad\">JSON 格式错误：${e.message}</span>`; return; }

  const r = await fetch('/api/bridge/config', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({config: parsed})
  });
  const data = await r.json();
  if(r.status !== 200){ msg.innerHTML = `<span class=\"bad\">保存失败：${JSON.stringify(data)}</span>`; return; }
  msg.innerHTML = `<span class=\"ok\">保存成功。${data.restart_required ? '请重启 WeChatBridge 生效。' : ''}</span>`;
}
async function refreshAll(){ await refreshStatus(); await refreshConfig(); }
refreshAll();
setInterval(refreshStatus, 3000);
</script>
</body>
</html>
"""

    class TriggerProxyHandler(BaseHTTPRequestHandler):
        def _forward_current(self, inst: InstanceConfig, path_override: Optional[str] = None):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length > 0 else b""
            content_type = self.headers.get("Content-Type", "application/json")

            forward_path = path_override if path_override is not None else self.path
            base = f"http://{inst.onebot_internal_listen}"

            log("trigger", f"[{inst.name}] {self.command} {forward_path}")
            if body:
                print(body.decode("utf-8", errors="ignore"), flush=True)

            status, resp_body, resp_ct = forward_http(
                target_url=f"{base}{forward_path}",
                method=self.command,
                body=body,
                content_type=content_type,
            )
            self.send_response(status)
            self.send_header("Content-Type", resp_ct)
            self.end_headers()
            self.wfile.write(resp_body)

            log("trigger", f"[{inst.name}] -> {status} {base}{forward_path}")
            if resp_body:
                print(resp_body.decode("utf-8", errors="ignore"), flush=True)

        def _write_html(self, html_text: str):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_text.encode("utf-8"))

        def _bridge_status(self):
            snap = runtime_snapshot(runtime_state or {"started_at": int(time.time()), "instances": {}})
            write_json(
                self,
                200,
                {
                    "ok": True,
                    "allinone_pid": os.getpid(),
                    "allinone_running": True,
                    "started_at": snap.get("started_at"),
                    "instances": snap.get("instances", {}),
                    "managed_config_path": cfg_path,
                },
            )

        def _bridge_get_config(self):
            data, err = read_managed_config()
            if err:
                write_json(self, 400, {"ok": False, "error": err})
                return
            write_json(self, 200, {"ok": True, "config": data, "path": cfg_path})

        def _bridge_set_config(self):
            body = read_json_body(self)
            cfg_obj = body.get("config") if isinstance(body, dict) and "config" in body else body
            if not isinstance(cfg_obj, dict):
                write_json(self, 400, {"ok": False, "error": "config must be object"})
                return

            current, _ = read_managed_config()
            if isinstance(current, dict):
                merged = dict(current)
                merged.update(cfg_obj)
                cfg_obj = merged

            err = write_managed_config(cfg_obj)
            if err:
                write_json(self, 500, {"ok": False, "error": err})
                return
            write_json(
                self,
                200,
                {
                    "ok": True,
                    "saved": cfg_path,
                    "restart_required": True,
                    "message": "配置已保存，重启 WeChatBridge 后生效",
                },
            )

        def _handle_api(self):
            body = read_json_body(self)
            body_instance = body.pop("instance", None)
            inst_hint = parse_instance_hint(self, body_instance=body_instance)
            inst, err = choose_instance(inst_hint, require_explicit=True)
            if err:
                write_json(self, 400, {"error": err})
                return
            assert inst is not None

            try:
                if self.path.startswith("/api/send_text"):
                    target_id = body.get("target_id", "")
                    text = body.get("text", "")
                    payload = build_send_payload("text", target_id=target_id, text=text)
                    route = "/send_group_msg" if target_id.endswith("@chatroom") else "/send_private_msg"
                elif self.path.startswith("/api/send_image"):
                    target_id = body.get("target_id", "")
                    file_value = body.get("file", "")
                    payload = build_send_payload("image", target_id=target_id, file_value=file_value)
                    route = "/send_group_msg" if target_id.endswith("@chatroom") else "/send_private_msg"
                elif self.path.startswith("/api/send_video"):
                    target_id = body.get("target_id", "")
                    file_value = body.get("file", "")
                    payload = build_send_payload("video", target_id=target_id, file_value=file_value)
                    route = "/send_group_msg" if target_id.endswith("@chatroom") else "/send_private_msg"
                elif self.path.startswith("/api/send_at"):
                    target_id = body.get("target_id", "")
                    if not target_id.endswith("@chatroom"):
                        raise ValueError("/api/send_at 仅支持群聊 target_id (xxx@chatroom)")
                    text = body.get("text", "")
                    at_user = body.get("at_user", "")
                    payload = build_send_payload("at_text", target_id=target_id, text=text, at_user=at_user)
                    route = "/send_group_msg"
                elif self.path.startswith("/api/download_media"):
                    payload = {
                        "target_id": body.get("target_id", ""),
                        "cdn_url": body.get("cdn_url", ""),
                        "aes_key": body.get("aes_key", ""),
                        "file_type": int(body.get("file_type", 0)),
                        "file_path": body.get("file_path", ""),
                        "md5": body.get("md5", ""),
                        "file_id": body.get("file_id", ""),
                    }
                    route = "/download_media"
                else:
                    write_json(self, 404, {"error": "unknown api route"})
                    return

                base = f"http://{inst.onebot_internal_listen}"
                log("trigger", f"[{inst.name}] API {self.path} -> {route}")
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                status, resp_body, _ = forward_http(
                    target_url=f"{base}{route}",
                    method="POST",
                    body=data,
                    content_type="application/json",
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(resp_body)
                if resp_body:
                    print(resp_body.decode("utf-8", errors="ignore"), flush=True)
            except Exception as e:
                write_json(self, 400, {"error": str(e)})

        def _resolve_passthrough_target(self) -> Tuple[Optional[InstanceConfig], Optional[str], Optional[str]]:
            parsed = urlsplit(self.path)
            path_only = parsed.path
            query = parsed.query

            # /i/<instance>/<route> passthrough
            if path_only.startswith("/i/"):
                parts = path_only.split("/", 3)
                if len(parts) < 4 or not parts[2]:
                    return None, None, "invalid instance passthrough path; expected /i/<name>/<route>"
                inst_name = parts[2]
                inst = inst_by_name.get(inst_name)
                if not inst:
                    return None, None, f"unknown instance: {inst_name}"
                route = "/" + parts[3]
                if query:
                    route = route + "?" + query
                return inst, route, None

            # fallback by query/header, else default
            hint = parse_instance_hint(self)
            inst, err = choose_instance(hint, require_explicit=False)
            if err:
                return None, None, err
            assert inst is not None
            return inst, self.path, None

        def do_POST(self):
            path_only = urlsplit(self.path).path

            if path_only == "/api/bridge/config":
                self._bridge_set_config()
                return

            if self.path.startswith("/api/"):
                self._handle_api()
                return

            inst, route, err = self._resolve_passthrough_target()
            if err:
                write_json(self, 400, {"error": err})
                return
            assert inst is not None and route is not None
            self._forward_current(inst, path_override=route)

        def do_GET(self):
            path_only = urlsplit(self.path).path

            if path_only in ("/bridge", "/bridge/", "/ui", "/ui/"):
                self._write_html(dashboard_html)
                return

            if path_only == "/api/bridge/status":
                self._bridge_status()
                return

            if path_only == "/api/bridge/config":
                self._bridge_get_config()
                return

            if self.path.startswith("/api/capabilities"):
                write_json(
                    self,
                    200,
                    {
                        "ok": True,
                        "routes": [
                            "/api/send_text",
                            "/api/send_image",
                            "/api/send_video",
                            "/api/send_at",
                            "/api/download_media",
                            "/api/bridge/status",
                            "/api/bridge/config",
                        ],
                        "ui": [
                            "/bridge",
                        ],
                        "passthrough": [
                            "/send_private_msg",
                            "/send_group_msg",
                            "/download_media",
                            "/ws",
                            "/i/<instance>/*",
                        ],
                        "instances": [
                            {
                                "name": i.name,
                                "onebot_internal_listen": i.onebot_internal_listen,
                                "callback_path": i.callback_path,
                                "wechat_pid": i.wechat_pid,
                            }
                            for i in instances
                        ],
                        "api_usage": {
                            "when_multi_instance": "POST /api/* body add {\"instance\":\"<name>\"}",
                            "passthrough": "prefix path with /i/<name>/...",
                        },
                    },
                )
                return

            inst, route, err = self._resolve_passthrough_target()
            if err:
                write_json(self, 400, {"error": err})
                return
            assert inst is not None and route is not None
            self._forward_current(inst, path_override=route)

        def do_PUT(self):
            inst, route, err = self._resolve_passthrough_target()
            if err:
                write_json(self, 400, {"error": err})
                return
            assert inst is not None and route is not None
            self._forward_current(inst, path_override=route)

        def do_PATCH(self):
            inst, route, err = self._resolve_passthrough_target()
            if err:
                write_json(self, 400, {"error": err})
                return
            assert inst is not None and route is not None
            self._forward_current(inst, path_override=route)

        def do_DELETE(self):
            inst, route, err = self._resolve_passthrough_target()
            if err:
                write_json(self, 400, {"error": err})
                return
            assert inst is not None and route is not None
            self._forward_current(inst, path_override=route)

        def log_message(self, fmt: str, *args):
            return

    return TriggerProxyHandler


def start_server(addr: str, handler_cls: type[BaseHTTPRequestHandler], name: str) -> ThreadingHTTPServer:
    host, port = split_host_port(addr)
    server = ThreadingHTTPServer((host, port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log(name, f"listening on http://{addr}")
    return server


def ensure_onebot_binary(cfg: Config):
    onebot_dir = Path(cfg.onebot_dir)
    bin_path = (onebot_dir / cfg.onebot_bin).resolve()
    if bin_path.exists() and not cfg.auto_build:
        return
    if bin_path.exists() and cfg.auto_build:
        log("build", "auto_build=true, rebuilding onebot...")
    elif not bin_path.exists():
        log("build", "onebot binary not found, building...")

    env = os.environ.copy()
    env["CGO_CFLAGS"] = f"-I{cfg.devkit_dir}"
    env["CGO_LDFLAGS"] = f"-L{cfg.devkit_dir}"

    cmd = ["go", "build", "-o", cfg.onebot_bin, "."]
    subprocess.run(cmd, cwd=str(onebot_dir), env=env, check=True)
    log("build", "onebot build done")


def build_onebot_cmd(cfg: Config, inst: InstanceConfig) -> List[str]:
    cmd = [
        str((Path(cfg.onebot_dir) / cfg.onebot_bin).resolve()),
        f"-type={inst.frida_type}",
        f"-gadget_addr={inst.gadget_addr}",
        f"-wechat_conf={inst.wechat_conf}",
        f"-conn_type={inst.conn_type}",
        f"-receive_host={inst.onebot_internal_listen}",
        f"-send_url=http://{cfg.callback_listen}{inst.callback_path}",
        f"-token={inst.token}",
        f"-send_interval={inst.send_interval}",
        f"-log_level={inst.log_level}",
    ]
    if inst.wechat_pid > 0:
        cmd.append(f"-wechat_pid={inst.wechat_pid}")
    return cmd


def run_onebots(cfg: Config, instances: List[InstanceConfig], runtime_state: Optional[Dict[str, Any]] = None) -> int:
    env = os.environ.copy()
    env["CGO_CFLAGS"] = f"-I{cfg.devkit_dir}"
    env["CGO_LDFLAGS"] = f"-L{cfg.devkit_dir}"

    procs: Dict[str, subprocess.Popen[str]] = {}
    rcs: Dict[str, int] = {}
    stopped = {"yes": False}

    def pump_stdout(name: str, proc: subprocess.Popen[str]):
        assert proc.stdout is not None
        for line in proc.stdout:
            clean = line.rstrip()
            print(f"[onebot:{name}] {clean}", flush=True)
            if runtime_state is not None:
                append_instance_log(runtime_state, name, clean)

    def stop_all():
        if stopped["yes"]:
            return
        stopped["yes"] = True
        log("main", "stopping all onebot instances...")
        for n, p in procs.items():
            if p.poll() is None:
                try:
                    p.terminate()
                    log("main", f"terminate sent -> {n}")
                except Exception:
                    pass

    def handle_sig(_sig, _frame):
        stop_all()

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    for inst in instances:
        cmd = build_onebot_cmd(cfg, inst)
        log("onebot", f"[{inst.name}] starting:")
        print(" ".join(cmd), flush=True)
        proc = subprocess.Popen(
            cmd,
            cwd=cfg.onebot_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        procs[inst.name] = proc
        if runtime_state is not None:
            update_instance_state(
                runtime_state,
                inst.name,
                running=True,
                pid=proc.pid,
                exit_code=None,
            )
        t = threading.Thread(target=pump_stdout, args=(inst.name, proc), daemon=True)
        t.start()

    try:
        while len(rcs) < len(procs):
            for name, proc in procs.items():
                if name in rcs:
                    continue
                rc = proc.poll()
                if rc is not None:
                    rcs[name] = rc
                    if runtime_state is not None:
                        update_instance_state(runtime_state, name, running=False, exit_code=rc)
                    log("onebot", f"[{name}] exited with code {rc}")

            if stopped["yes"]:
                # give children time to quit
                for name, proc in procs.items():
                    if name in rcs:
                        continue
                    rc = proc.poll()
                    if rc is None:
                        try:
                            proc.wait(timeout=1.5)
                        except Exception:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                    rc = proc.poll()
                    if rc is not None:
                        rcs[name] = rc
                        if runtime_state is not None:
                            update_instance_state(runtime_state, name, running=False, exit_code=rc)

            time.sleep(0.2)
    finally:
        stop_all()

    if not rcs:
        return 1
    return max(rcs.values())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).with_name("onebot_allinone.json")))
    parser.add_argument(
        "--managed-config",
        default="",
        help="可选：控制台读写的配置文件路径（默认跟 --config 一致）",
    )
    args = parser.parse_args()

    config_path = str(Path(args.config).resolve())
    managed_config_path = str(Path(args.managed_config).resolve()) if args.managed_config else config_path

    cfg = load_config(Path(config_path))
    instances = build_instances(cfg)
    state = init_runtime_state(instances)

    ensure_onebot_binary(cfg)

    callback_server = start_server(
        cfg.callback_listen,
        make_callback_handler(
            instances,
            callback_forward_enabled=cfg.callback_forward_enabled,
            callback_forward_url=cfg.callback_forward_url,
            callback_forward_timeout_ms=cfg.callback_forward_timeout_ms,
            callback_forward_headers=cfg.callback_forward_headers,
        ),
        "callback",
    )
    proxy_server = start_server(
        cfg.trigger_listen,
        make_trigger_proxy_handler(instances, runtime_state=state, managed_config_path=managed_config_path),
        "trigger",
    )

    log("main", f"send HTTP to: http://{cfg.trigger_listen}")
    log("main", f"callback listen: http://{cfg.callback_listen}")
    if cfg.callback_forward_enabled and cfg.callback_forward_url:
        log(
            "main",
            f"callback forwarding enabled -> {cfg.callback_forward_url} timeout={cfg.callback_forward_timeout_ms}ms",
        )
    for inst in instances:
        log(
            "main",
            f"instance={inst.name} onebot={inst.onebot_internal_listen} callback_path={inst.callback_path} wechat_pid={inst.wechat_pid}",
        )
    log("main", "convenience APIs: /api/send_text /api/send_image /api/send_video /api/send_at /api/download_media")
    log("main", f"bridge console: http://{cfg.trigger_listen}/bridge")

    try:
        rc = run_onebots(cfg, instances, runtime_state=state)
    finally:
        callback_server.shutdown()
        proxy_server.shutdown()

    sys.exit(rc)


if __name__ == "__main__":
    main()
