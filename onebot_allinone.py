#!/usr/bin/env python3
"""
OneBot all-in-one launcher:
- starts a callback HTTP server (prints inbound callbacks)
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


def make_callback_handler(instances: List[InstanceConfig]) -> type[BaseHTTPRequestHandler]:
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


def make_trigger_proxy_handler(instances: List[InstanceConfig]) -> type[BaseHTTPRequestHandler]:
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


def run_onebots(cfg: Config, instances: List[InstanceConfig]) -> int:
    env = os.environ.copy()
    env["CGO_CFLAGS"] = f"-I{cfg.devkit_dir}"
    env["CGO_LDFLAGS"] = f"-L{cfg.devkit_dir}"

    procs: Dict[str, subprocess.Popen[str]] = {}
    rcs: Dict[str, int] = {}
    stopped = {"yes": False}

    def pump_stdout(name: str, proc: subprocess.Popen[str]):
        assert proc.stdout is not None
        for line in proc.stdout:
            print(f"[onebot:{name}] {line.rstrip()}", flush=True)

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

            time.sleep(0.2)
    finally:
        stop_all()

    if not rcs:
        return 1
    return max(rcs.values())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).with_name("onebot_allinone.json")))
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    instances = build_instances(cfg)

    ensure_onebot_binary(cfg)

    callback_server = start_server(cfg.callback_listen, make_callback_handler(instances), "callback")
    proxy_server = start_server(cfg.trigger_listen, make_trigger_proxy_handler(instances), "trigger")

    log("main", f"send HTTP to: http://{cfg.trigger_listen}")
    log("main", f"callback listen: http://{cfg.callback_listen}")
    for inst in instances:
        log(
            "main",
            f"instance={inst.name} onebot={inst.onebot_internal_listen} callback_path={inst.callback_path} wechat_pid={inst.wechat_pid}",
        )
    log("main", "convenience APIs: /api/send_text /api/send_image /api/send_video /api/send_at /api/download_media")

    try:
        rc = run_onebots(cfg, instances)
    finally:
        callback_server.shutdown()
        proxy_server.shutdown()

    sys.exit(rc)


if __name__ == "__main__":
    main()
