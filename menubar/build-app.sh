#!/bin/bash
# Build the menu-bar executable and wrap it into a menu-bar-only .app bundle.
set -euo pipefail
cd "$(dirname "$0")"

SCRATCH="/tmp/vma-menubar-build"
APP="$HOME/Applications/Voice Memo AI.app"

echo "building (release)…"
swift build -c release --scratch-path "$SCRATCH"
BIN="$SCRATCH/release/VoiceMemoAI"

echo "assembling app bundle…"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/VoiceMemoAI"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Voice Memo AI</string>
  <key>CFBundleDisplayName</key><string>Voice Memo AI</string>
  <key>CFBundleIdentifier</key><string>industries.ritual.voice-memo-ai.menubar</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>VoiceMemoAI</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <!-- menu-bar only: no dock icon, no app switcher entry -->
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

# macOS 26 scar: unsigned binaries misbehave (mic zeros, TCC). Ad-hoc sign + strip xattrs.
xattr -cr "$APP"
codesign --force --deep --sign - "$APP"

echo "built: $APP"
