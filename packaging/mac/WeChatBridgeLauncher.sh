#!/bin/bash
set -euo pipefail

APP_NAME="WeChatBridge"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_CONTENTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RES_DIR="$APP_CONTENTS_DIR/Resources"

APP_SUPPORT_DIR="$HOME/Library/Application Support/$APP_NAME"
USER_RUNTIME_DIR="$APP_SUPPORT_DIR/runtime"
USER_CONFIG="$APP_SUPPORT_DIR/config.json"
RUNTIME_CONFIG="$APP_SUPPORT_DIR/runtime.config.json"

LOG_DIR="$HOME/Library/Logs/$APP_NAME"
LOG_FILE="$LOG_DIR/bridge.log"

mkdir -p "$APP_SUPPORT_DIR" "$LOG_DIR"
touch "$LOG_FILE"
exec >>"$LOG_FILE" 2>&1

echo "==== [$APP_NAME] startup $(date '+%Y-%m-%d %H:%M:%S') ===="

notify() {
  local msg="$1"
  /usr/bin/osascript -e "display notification \"${msg//\"/\\\"}\" with title \"$APP_NAME\"" >/dev/null 2>&1 || true
}

fatal_dialog() {
  local msg="$1"
  /usr/bin/osascript -e "display dialog \"${msg//\"/\\\"}\" with title \"$APP_NAME\" buttons {\"OK\"} default button \"OK\"" >/dev/null 2>&1 || true
}

PYTHON_BIN="$(command -v python3 || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  fatal_dialog "未找到 python3，请先安装 Python 3（建议 Homebrew: brew install python）"
  exit 1
fi

APP_VERSION="$(cat "$RES_DIR/version.txt" 2>/dev/null || echo dev)"

sync_runtime_payload() {
  local current_version=""
  if [[ -f "$USER_RUNTIME_DIR/.payload-version" ]]; then
    current_version="$(cat "$USER_RUNTIME_DIR/.payload-version" || true)"
  fi

  if [[ ! -d "$USER_RUNTIME_DIR" || "$current_version" != "$APP_VERSION" ]]; then
    echo "sync runtime payload -> $USER_RUNTIME_DIR (version=$APP_VERSION)"
    rm -rf "$USER_RUNTIME_DIR"
    mkdir -p "$USER_RUNTIME_DIR"
    cp -R "$RES_DIR/runtime/." "$USER_RUNTIME_DIR/"
    echo "$APP_VERSION" > "$USER_RUNTIME_DIR/.payload-version"
  fi

  chmod +x "$USER_RUNTIME_DIR/onebot/onebot" >/dev/null 2>&1 || true
  mkdir -p "$USER_RUNTIME_DIR/onebot/log" "$USER_RUNTIME_DIR/onebot/image" "$USER_RUNTIME_DIR/onebot/file" "$USER_RUNTIME_DIR/onebot/upload_media"
}

bootstrap_user_config_if_missing() {
  if [[ -f "$USER_CONFIG" ]]; then
    return
  fi

  cp "$RES_DIR/config.example.json" "$USER_CONFIG"
  notify "首次启动：已生成配置文件，请先编辑后再启动。"
  open -a TextEdit "$USER_CONFIG" || true

  cat <<EOF
[$APP_NAME] 首次启动已生成配置：
  $USER_CONFIG
请先填写 callback_forward_url（如需）并确认端口后，再次启动 App。
EOF
  exit 0
}

render_runtime_config() {
  "$PYTHON_BIN" - "$USER_CONFIG" "$RUNTIME_CONFIG" "$USER_RUNTIME_DIR" <<'PY'
import json
import sys

src, dst, runtime = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src, "r", encoding="utf-8") as f:
    cfg = json.load(f)

def walk(v):
    if isinstance(v, str):
        return v.replace("__USER_RUNTIME__", runtime)
    if isinstance(v, list):
        return [walk(x) for x in v]
    if isinstance(v, dict):
        return {k: walk(vv) for k, vv in v.items()}
    return v

out = walk(cfg)
with open(dst, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
PY
}

sync_runtime_payload
bootstrap_user_config_if_missing
render_runtime_config

notify "服务启动中（HTTP: 127.0.0.1:3222）"

echo "python: $PYTHON_BIN"
echo "config: $RUNTIME_CONFIG"

auto_chdir="$USER_RUNTIME_DIR"
cd "$auto_chdir"
exec "$PYTHON_BIN" "$USER_RUNTIME_DIR/onebot_allinone.py" --config "$RUNTIME_CONFIG"
