#!/usr/bin/env python3
"""
OneBot all-in-one launcher (single terminal):
- starts a callback HTTP server (prints inbound callbacks)
- starts a trigger proxy HTTP server (prints outbound API calls)
- starts onebot process and wires send_url -> callback server

Usage:
  python3 onebot_allinone.py --config onebot_allinone.json
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
from typing import Dict, Tuple
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


def make_callback_handler() -> type[BaseHTTPRequestHandler]:
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8", errors="ignore")
            log("callback", f"{self.command} {self.path}")
            if body:
                print(body, flush=True)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true,"service":"callback"}')

        def log_message(self, fmt: str, *args):
            return

    return CallbackHandler


def make_trigger_proxy_handler(forward_base: str) -> type[BaseHTTPRequestHandler]:
    class TriggerProxyHandler(BaseHTTPRequestHandler):
        def _proxy(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length > 0 else b""

            log("trigger", f"{self.command} {self.path}")
            if body:
                print(body.decode("utf-8", errors="ignore"), flush=True)

            target = f"{forward_base}{self.path}"
            headers: Dict[str, str] = {}
            ctype = self.headers.get("Content-Type")
            if ctype:
                headers["Content-Type"] = ctype

            req = Request(target, data=body if self.command in ("POST", "PUT", "PATCH") else None,
                          method=self.command, headers=headers)

            try:
                with urlopen(req, timeout=20) as resp:
                    resp_body = resp.read()
                    self.send_response(resp.status)
                    self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                    self.end_headers()
                    self.wfile.write(resp_body)
                    log("trigger", f"-> {resp.status} {target}")
                    if resp_body:
                        print(resp_body.decode("utf-8", errors="ignore"), flush=True)
            except HTTPError as e:
                body_bytes = e.read() if hasattr(e, "read") else b""
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body_bytes or b'{"error":"upstream http error"}')
                log("trigger", f"-> HTTPError {e.code} {target}")
                if body_bytes:
                    print(body_bytes.decode("utf-8", errors="ignore"), flush=True)
            except URLError as e:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                msg = json.dumps({"error": f"upstream unavailable: {e}"}).encode("utf-8")
                self.wfile.write(msg)
                log("trigger", f"-> URLError {target} {e}")

        def do_POST(self):
            self._proxy()

        def do_GET(self):
            self._proxy()

        def do_PUT(self):
            self._proxy()

        def do_PATCH(self):
            self._proxy()

        def do_DELETE(self):
            self._proxy()

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
            print(f"[onebot] {line.rstrip()}" , flush=True)
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

    callback_server = start_server(cfg.callback_listen, make_callback_handler(), "callback")
    proxy_server = start_server(
        cfg.trigger_listen,
        make_trigger_proxy_handler(f"http://{cfg.onebot_internal_listen}"),
        "trigger",
    )

    log("main", f"send HTTP to: http://{cfg.trigger_listen}")
    log("main", f"onebot callback target: http://{cfg.callback_listen}{cfg.callback_path}")

    try:
        rc = run_onebot(cfg)
    finally:
        callback_server.shutdown()
        proxy_server.shutdown()

    sys.exit(rc)


if __name__ == "__main__":
    main()
