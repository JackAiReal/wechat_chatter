#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

APP_NAME="${APP_NAME:-WeChatBridge}"
BUNDLE_ID="${BUNDLE_ID:-ai.openclaw.wechatbridge}"
VERSION="${VERSION:-$(date +%Y.%m.%d)-$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || echo dev)}"

OUT_DIR="$ROOT_DIR/release/mac"
BUILD_DIR="$OUT_DIR/build"
APP_DIR="$BUILD_DIR/${APP_NAME}.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RES_DIR="$CONTENTS_DIR/Resources"
RUNTIME_DIR="$RES_DIR/runtime"

DMG_STAGING="$BUILD_DIR/dmg_staging"
DMG_PATH="$OUT_DIR/${APP_NAME}-${VERSION}.dmg"

mkdir -p "$OUT_DIR" "$BUILD_DIR"

echo "[1/6] prepare onebot binary"
if [[ ! -x "$ROOT_DIR/onebot/onebot" ]]; then
  echo "onebot binary missing, building..."
  (cd "$ROOT_DIR/onebot" && go build -o onebot .)
fi

echo "[2/6] create app bundle structure"
rm -rf "$APP_DIR"
mkdir -p "$MACOS_DIR" "$RES_DIR" "$RUNTIME_DIR/onebot" "$RUNTIME_DIR/wechat_version" "$RUNTIME_DIR/.frida-devkit"

cp "$ROOT_DIR/packaging/mac/WeChatBridgeLauncher.sh" "$MACOS_DIR/$APP_NAME"
chmod +x "$MACOS_DIR/$APP_NAME"

echo "[3/6] copy runtime payload"
cp "$ROOT_DIR/onebot_allinone.py" "$RUNTIME_DIR/"
cp "$ROOT_DIR/onebot/onebot" "$RUNTIME_DIR/onebot/"
cp "$ROOT_DIR/onebot/script.js" "$RUNTIME_DIR/onebot/"
cp -R "$ROOT_DIR/wechat_version/." "$RUNTIME_DIR/wechat_version/"

cp "$ROOT_DIR/packaging/mac/config.example.json" "$RES_DIR/config.example.json"
cp "$ROOT_DIR/packaging/mac/README_3STEP.md" "$RES_DIR/README_3STEP.md"
printf '%s\n' "$VERSION" > "$RES_DIR/version.txt"

cat > "$CONTENTS_DIR/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>zh_CN</string>
  <key>CFBundleExecutable</key>
  <string>$APP_NAME</string>
  <key>CFBundleIdentifier</key>
  <string>$BUNDLE_ID</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>$APP_NAME</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>$VERSION</string>
  <key>CFBundleVersion</key>
  <string>$VERSION</string>
  <key>LSMinimumSystemVersion</key>
  <string>12.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
PLIST

echo "[4/6] ad-hoc sign app bundle"
codesign --force --deep --sign - "$APP_DIR" >/dev/null

echo "[5/6] create dmg"
rm -rf "$DMG_STAGING"
mkdir -p "$DMG_STAGING"
cp -R "$APP_DIR" "$DMG_STAGING/"
cp "$ROOT_DIR/packaging/mac/README_3STEP.md" "$DMG_STAGING/README_3STEP.md"
cp "$ROOT_DIR/packaging/mac/config.example.json" "$DMG_STAGING/config.example.json"

rm -f "$DMG_PATH"
hdiutil create -volname "$APP_NAME" -srcfolder "$DMG_STAGING" -ov -format UDZO "$DMG_PATH" >/dev/null

echo "[6/6] done"
echo "APP: $APP_DIR"
echo "DMG: $DMG_PATH"
echo ""
echo "Run local app for test:"
echo "  open '$APP_DIR'"
echo ""
echo "First-run config will be generated at:"
echo "  ~/Library/Application Support/$APP_NAME/config.json"
