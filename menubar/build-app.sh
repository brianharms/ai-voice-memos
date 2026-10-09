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
# assemble in a staging dir and swap it in only after signing succeeds, so a
# failed build never leaves the user without their working app
STAGE="$SCRATCH/stage/Voice Memo AI.app"
rm -rf "$SCRATCH/stage"
mkdir -p "$STAGE/Contents/MacOS" "$STAGE/Contents/Resources"
cp "$BIN" "$STAGE/Contents/MacOS/VoiceMemoAI"

cat > "$STAGE/Contents/Info.plist" <<'PLIST'
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
  <!-- the model/effort menu talks to the Configurator on http://127.0.0.1:8787 -->
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST

# macOS 26 scar: unsigned binaries misbehave (mic zeros, TCC). Strip xattrs, then sign.
xattr -cr "$STAGE"
# Prefer a real certificate over ad-hoc. TCC keys an ad-hoc app's grants on its cdhash, so every
# rebuild silently orphans its grants (2026-10-02: "Open Configurator" stopped working after a
# rebuild while System Settings still showed it allowed). With a team id TCC keys on team + bundle id.
# Set VMA_SIGN_IDENTITY (a codesigning identity hash or name from `security find-identity -v -p codesigning`)
# and optionally VMA_TEAM_ID to verify it; otherwise the app is signed ad-hoc.
IDS="$(security find-identity -v -p codesigning 2>/dev/null || true)"
if [ -n "${VMA_SIGN_IDENTITY:-}" ]; then
    IDENTITY="$VMA_SIGN_IDENTITY"
    [[ "$IDS" == *"$IDENTITY"* ]] || { echo "signing identity $IDENTITY not found in the keychain"; exit 1; }
else
    IDENTITY="-"
    echo "WARNING: no signing certificate found — signing ad-hoc. macOS permissions given to the"
    echo "         menu bar app will need re-granting after each rebuild (set VMA_SIGN_IDENTITY to avoid)."
fi
codesign --force --deep --sign "$IDENTITY" --identifier industries.ritual.voice-memo-ai.menubar "$STAGE"
if [ -n "${VMA_TEAM_ID:-}" ]; then
    SIG="$(codesign -dv "$STAGE" 2>&1)"   # captured first: grep -q closing the pipe early trips pipefail
    [[ "$SIG" == *"TeamIdentifier=$VMA_TEAM_ID"* ]] || { echo "signature lacks TeamIdentifier=$VMA_TEAM_ID — refusing"; exit 1; }
fi

mkdir -p "$(dirname "$APP")"
rm -rf "$APP"
mv "$STAGE" "$APP"
echo "built: $APP"
