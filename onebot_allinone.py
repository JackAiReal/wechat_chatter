#!/usr/bin/env python3
"""
OneBot all-in-one launcher (single terminal):
- starts a callback HTTP server (prints inbound callbacks)
- starts a trigger proxy HTTP server (prints outbound API calls)
- starts onebot process and wires send_url -> callback server

Extra convenience APIs (on trigger_listen):
- POST /api/send_text
- POST /api/send_image
- POST /api/send_video
- POST /api/send_at
- POST /api/download_media

All other routes are proxied as-is to onebot internal server.
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
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass
class Config:
    onebot_dir: str
    devkit_dir: str
    onebot_bin: str
    wechat_conf: str
    gadget_addr: str = "127.0.0.1:27042"
    token: str = "MuseBot"
    log_level: str = "info"
    send_interval: int = 1000
    conn_type: str = "http"

    # External API port user calls
    trigger_listen: str = "127.0.0.1:3222"
    # Internal onebot API bind (proxied by trigger server)
    onebot_internal_listen: str = "127.0.0.1:3223"

    # Callback server from onebot -> your local app
    callback_listen: str = "127.0.0.1:18888"
    callback_path: str = "/onebot"

    # Optional: save callback payloads as json lines
    callback_dump_jsonl: str = ""

    auto_build: bool = True


def load_config(path: Path) -> Config:
    data = json.loads(path.read_text(encoding="utf-8"))
    return Config(**data)


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


def build_send_payload(msg_type: str, target_id: str, text: str = "", file_value: str = "", at_user: str = "") -> Dict[str, Any]:
    payload: Dict[str, Any] = {**infer_target_fields(target_id), "message": []}

    if msg_type == "text":
        payload["message"].append({"type": "text", "data": {"text": text}})
    elif msg_type in ("image", "video"):
        payload["message"].append({"type": msg_type, "data": {"file": file_value}})
    elif msg_type == "at_text":
        if at_user:
            for one in [x.strip() for x in at_user.split(",") if x.strip()]:
                payload["message"].append({"type": "at", "data": {"qq": one}})
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


def forward_http(target_url: str, method: str, body: bytes, content_type: Optional[str] = None) -> Tuple[int, bytes, str]:
    headers: Dict[str, str] = {}
    if content_type:
        headers["Content-Type"] = content_type
    req = Request(
        target_url,
        data=body if method in ("POST", "PUT", "PATCH") else None,
        method=method,
        headers=headers,
    )
    try:
        with urlopen(req, timeout=20) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "application/json")
    except HTTPError as e:
        return e.code, e.read() if hasattr(e, "read") else b"", "application/json"
    except URLError as e:
        err = json.dumps({"error": f"upstream unavailable: {e}"}, ensure_ascii=False).encode("utf-8")
        return 502, err, "application/json"


def make_callback_handler(cfg: Config) -> type[BaseHTTPRequestHandler]:
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8", errors="ignore")

            log("callback", f"{self.command} {self.path}")
            if raw:
                try:
                    data = json.loads(raw)
                    mtype = data.get("message_type")
                    sender = data.get("user_id")
                    gid = data.get("group_id", "")
                    text = data.get("raw_message", "")
                    log("callback", f"message_type={mtype} sender={sender} group={gid}")
                    if text:
                        print(text, flush=True)

                    # 打印媒体 URL（落地文件路径通常在这里）
                    for seg in data.get("message", []) or []:
                        seg_type = seg.get("type")
                        seg_data = seg.get("data") or {}
                        url = seg_data.get("url")
                        if url:
                            log("callback", f"media[{seg_type}] url={url}")
                except Exception:
                    print(raw, flush=True)

            if cfg.callback_dump_jsonl:
                p = Path(cfg.callback_dump_jsonl)
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as f:
                    f.write(raw + "\n")

            write_json(self, 200, {"ok": True})

        def do_GET(self):
            write_json(self, 200, {"ok": True, "service": "callback"})

        def log_message(self, fmt: str, *args):
            return

    return CallbackHandler


def make_trigger_proxy_handler(forward_base: str) -> type[BaseHTTPRequestHandler]:
    class TriggerProxyHandler(BaseHTTPRequestHandler):
        def _forward_current(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length > 0 else b""
            content_type = self.headers.get("Content-Type", "application/json")

            log("trigger", f"{self.command} {self.path}")
            if body:
                print(body.decode("utf-8", errors="ignore"), flush=True)

            status, resp_body, resp_ct = forward_http(
                target_url=f"{forward_base}{self.path}",
                method=self.command,
                body=body,
                content_type=content_type,
            )
            self.send_response(status)
            self.send_header("Content-Type", resp_ct)
            self.end_headers()
            self.wfile.write(resp_body)

            log("trigger", f"-> {status} {forward_base}{self.path}")
            if resp_body:
                print(resp_body.decode("utf-8", errors="ignore"), flush=True)

        def _handle_api(self):
            body = read_json_body(self)
            try:
                if self.path == "/api/send_text":
                    target_id = body.get("target_id", "")
                    text = body.get("text", "")
                    payload = build_send_payload("text", target_id=target_id, text=text)
                    route = "/send_group_msg" if target_id.endswith("@chatroom") else "/send_private_msg"
                elif self.path == "/api/send_image":
                    target_id = body.get("target_id", "")
                    file_value = body.get("file", "")
                    payload = build_send_payload("image", target_id=target_id, file_value=file_value)
                    route = "/send_group_msg" if target_id.endswith("@chatroom") else "/send_private_msg"
                elif self.path == "/api/send_video":
                    target_id = body.get("target_id", "")
                    file_value = body.get("file", "")
                    payload = build_send_payload("video", target_id=target_id, file_value=file_value)
                    route = "/send_group_msg" if target_id.endswith("@chatroom") else "/send_private_msg"
                elif self.path == "/api/send_at":
                    target_id = body.get("target_id", "")
                    if not target_id.endswith("@chatroom"):
                        raise ValueError("/api/send_at 仅支持群聊 target_id (xxx@chatroom)")
                    text = body.get("text", "")
                    at_user = body.get("at_user", "")
                    payload = build_send_payload("at_text", target_id=target_id, text=text, at_user=at_user)
                    route = "/send_group_msg"
                elif self.path == "/api/download_media":
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

                log("trigger", f"API {self.path} -> {route}")
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                status, resp_body, _ = forward_http(
                    target_url=f"{forward_base}{route}",
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

        def do_POST(self):
            if self.path.startswith("/api/"):
                self._handle_api()
                return
            self._forward_current()

        def do_GET(self):
            if self.path == "/api/capabilities":
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
                        ],
                        "passthrough": [
                            "/send_private_msg",
                            "/send_group_msg",
                            "/download_media",
                            "/ws",
                        ],
                    },
                )
                return
            self._forward_current()

        def do_PUT(self):
            self._forward_current()

        def do_PATCH(self):
            self._forward_current()

        def do_DELETE(self):
            self._forward_current()

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


def run_onebot(cfg: Config) -> int:
    callback_url = f"http://{cfg.callback_listen}{cfg.callback_path}"
    cmd = [
        str((Path(cfg.onebot_dir) / cfg.onebot_bin).resolve()),
        "-type=gadget",
        f"-gadget_addr={cfg.gadget_addr}",
        f"-wechat_conf={cfg.wechat_conf}",
        f"-conn_type={cfg.conn_type}",
        f"-receive_host={cfg.onebot_internal_listen}",
        f"-send_url={callback_url}",
        f"-token={cfg.token}",
        f"-send_interval={cfg.send_interval}",
        f"-log_level={cfg.log_level}",
    ]

    env = os.environ.copy()
    env["CGO_CFLAGS"] = f"-I{cfg.devkit_dir}"
    env["CGO_LDFLAGS"] = f"-L{cfg.devkit_dir}"

    log("onebot", "starting:")
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

    stop_flag = {"stop": False}

    def handle_sig(_sig, _frame):
        if stop_flag["stop"]:
            return
        stop_flag["stop"] = True
        log("main", "stopping...")
        try:
            proc.terminate()
        except Exception:
            pass

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            print(f"[onebot] {line.rstrip()}", flush=True)
    finally:
        rc = proc.wait()
        log("onebot", f"exited with code {rc}")
    return rc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).with_name("onebot_allinone.json")))
    args = parser.parse_args()

    cfg = load_config(Path(args.config))

    ensure_onebot_binary(cfg)

    callback_server = start_server(cfg.callback_listen, make_callback_handler(cfg), "callback")
    proxy_server = start_server(
        cfg.trigger_listen,
        make_trigger_proxy_handler(f"http://{cfg.onebot_internal_listen}"),
        "trigger",
    )

    log("main", f"send HTTP to: http://{cfg.trigger_listen}")
    log("main", f"onebot callback target: http://{cfg.callback_listen}{cfg.callback_path}")
    log("main", "convenience APIs: /api/send_text /api/send_image /api/send_video /api/send_at /api/download_media")

    try:
        rc = run_onebot(cfg)
    finally:
        callback_server.shutdown()
        proxy_server.shutdown()

    sys.exit(rc)


if __name__ == "__main__":
    main()
