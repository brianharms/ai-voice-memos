#!/bin/bash
# Voice Memo AI — installer.
#
#   ./install.sh            set everything up, asking before each change
#   ./install.sh --dry-run  walk through every step, but only PRINT what it would do
#
# Safe to run again: every step checks what is already done and skips it.
# It never uses sudo. Anything that needs your password or a click in
# System Settings is shown to you to do yourself.
#
# Works with the bash that ships with macOS (3.2).

set -u

DRY=0
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n) DRY=1 ;;
        -h|--help)
            sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "unknown option: $arg (try --help)"; exit 1 ;;
    esac
done

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE="$HOME/.voice-memo-ai"
SKILLS_DIR="$HOME/.claude/skills"
LOCAL_BIN="$HOME/.local/bin"
MODEL_DIR="$HOME/Library/Application Support/whisper/models"
MODEL_FILE="$MODEL_DIR/ggml-medium.en.bin"
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.en.bin"
READER="$STATE/bin/vma-reader"
VMA="$REPO_DIR/daemon/vma"

STEP=0
PROBLEMS=""

# ---------------------------------------------------------------- helpers ----

say()  { printf '%s\n' "$*"; }
ok()   { printf '  ok: %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*"; }
note_problem() { PROBLEMS="${PROBLEMS}  - $*
"; }

step() {
    STEP=$((STEP + 1))
    printf '\n---- Step %d: %s ----\n' "$STEP" "$*"
}

# run a command, or just print it in dry-run mode
run() {
    if [ "$DRY" = 1 ]; then
        printf '  [dry run] would run:'
        for a in "$@"; do printf ' %q' "$a"; done
        printf '\n'
        return 0
    fi
    "$@"
}

# ask_yn "question" default(y|n)  -> returns 0 for yes
# End of input (e.g. run non-interactively) takes the default.
ask_yn() {
    local q="$1" def="$2" hint ans
    if [ "$def" = y ]; then hint="[Y/n]"; else hint="[y/N]"; fi
    printf '  %s %s ' "$q" "$hint"
    if ! read -r ans; then ans=""; echo; fi
    [ -z "$ans" ] && ans="$def"
    case "$ans" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

# ask_text "question" "default"  -> answer in $ANSWER
ask_text() {
    local q="$1" def="$2" ans
    if [ -n "$def" ]; then printf '  %s [%s] ' "$q" "$def"; else printf '  %s ' "$q"; fi
    if ! read -r ans; then ans=""; echo; fi
    [ -z "$ans" ] && ans="$def"
    ANSWER="$ans"
}

# wait for the person to do something by hand (skipped in dry-run mode)
pause() {
    if [ "$DRY" = 1 ]; then
        say "  [dry run] would wait here for you to press Return."
        return 0
    fi
    printf '  Press Return when you have done this... '
    read -r _ || true
}

# run a command with a time limit (macOS has no `timeout` by default)
with_timeout() {
    local secs="$1"; shift
    /usr/bin/perl -e 'alarm shift; exec @ARGV' "$secs" "$@"
}

json_get() {  # json_get file key
    /usr/bin/python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
except Exception: print("")' "$1" "$2" 2>/dev/null
}

# ------------------------------------------------------------------ intro ----

say "Voice Memo AI — installer"
say ""
say "Record a voice memo on your iPhone. This Mac transcribes it locally, Claude"
say "picks the right skill and does the work, and you get a text when it's done."
say ""
say "This sets it up for the person logged in to this Mac ($(id -un))."
say "It asks before every change. Nothing here uses sudo."
[ "$DRY" = 1 ] && say "" && say "DRY RUN: nothing will be installed or changed; every action is only printed."

# ------------------------------------------------------------ 1. this Mac ----

step "Check this Mac"
if [ "$(uname -s)" != "Darwin" ]; then
    say "  This only runs on macOS. Stopping."
    exit 1
fi
OSVER="$(sw_vers -productVersion 2>/dev/null || echo unknown)"
ARCH="$(uname -m)"
ok "macOS $OSVER on $ARCH"
case "$OSVER" in
    10.*|11.*|12.*|13.*) warn "macOS $OSVER is old; this has been used on macOS 14 and later."
                         note_problem "macOS $OSVER is older than what this was tested on." ;;
esac
if [ "$ARCH" = "arm64" ]; then
    BREW_PREFIX="/opt/homebrew"
    ok "Apple Silicon"
else
    BREW_PREFIX="/usr/local"
    warn "This is an Intel Mac. The daemon currently expects Homebrew tools in /opt/homebrew"
    warn "(Apple Silicon), so transcription will not work here without changes."
    note_problem "Intel Mac: the daemon looks for whisper-cli and ffmpeg in /opt/homebrew/bin."
    if ! ask_yn "Continue anyway?" n; then exit 1; fi
fi
if [ -x /usr/bin/sandbox-exec ]; then ok "macOS sandbox tool present"; else warn "sandbox-exec missing: skills would run without the safety sandbox."; fi

# --------------------------------------------------- 2. where the code is ----

step "Check where this folder is"
say "  This folder: $REPO_DIR"
# Any location works: vma finds its code by following its own symlink, the
# daemon runs from a copy in ~/.voice-memo-ai, and 'vma load' records this
# folder in ~/.voice-memo-ai/project-path for the menu-bar app.
if [ -f "$REPO_DIR/engine/watcher.py" ] && [ -x "$VMA" ]; then
    ok "location is fine"
else
    warn "This doesn't look like a complete copy (engine/watcher.py or daemon/vma missing)."
    note_problem "Incomplete copy of the code in $REPO_DIR (re-clone it)."
fi

# ------------------------------------------------ 3. Xcode command line tools ----

step "Apple's command line tools (needed to build a small helper)"
if xcode-select -p >/dev/null 2>&1 && [ -x /usr/bin/clang ] && /usr/bin/clang --version >/dev/null 2>&1; then
    ok "installed ($(xcode-select -p))"
else
    say "  Not installed. Apple's installer opens a window; it takes a few minutes."
    if ask_yn "Open Apple's installer now?" y; then
        run xcode-select --install
        say "  Finish that installer, then run ./install.sh again."
        exit 0
    fi
    note_problem "Command line tools missing (xcode-select --install)."
fi

# ------------------------------------------------------------ 4. Homebrew ----

step "Homebrew (installs the transcription tools)"
BREW=""
if [ -x "$BREW_PREFIX/bin/brew" ]; then BREW="$BREW_PREFIX/bin/brew"; fi
[ -z "$BREW" ] && BREW="$(command -v brew 2>/dev/null || true)"
if [ -n "$BREW" ]; then
    ok "found $BREW"
else
    say "  Homebrew is not installed. Install it with the official command from https://brew.sh :"
    say ""
    say '    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    say ""
    say "  (It asks for your Mac password itself.) Then run ./install.sh again."
    exit 0
fi

# ------------------------------------------------- 5. whisper-cpp + ffmpeg ----

step "Transcription tools: whisper-cpp and ffmpeg"
MISSING=""
[ -x "$BREW_PREFIX/bin/whisper-cli" ] || MISSING="$MISSING whisper-cpp"
[ -x "$BREW_PREFIX/bin/ffmpeg" ]      || MISSING="$MISSING ffmpeg"
if [ -z "$MISSING" ]; then
    ok "whisper-cli and ffmpeg are installed"
else
    say "  Missing:$MISSING"
    if ask_yn "Install with Homebrew now (brew install$MISSING)?" y; then
        # shellcheck disable=SC2086
        run "$BREW" install $MISSING || { warn "brew install failed"; note_problem "brew install$MISSING failed."; }
    else
        note_problem "Not installed:$MISSING (brew install$MISSING)."
    fi
fi

# ---------------------------------------------------------- 6. Claude Code ----

step "Claude Code (does the actual work)"
# The daemon runs from launchd with a minimal PATH (see daemon/vma), so it looks
# for claude at ~/.local/bin/claude first. If you already have Claude Code
# somewhere else (npm, Homebrew, nvm, an older ~/.claude/local install), it is
# linked there. Nothing is reinstalled; only sign-in is checked.
CLAUDE="$LOCAL_BIN/claude"
DAEMON_PATH="$LOCAL_BIN:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [ -L "$CLAUDE" ] && [ ! -e "$CLAUDE" ]; then
    warn "$CLAUDE is a broken link (points to $(readlink "$CLAUDE"))."
    if ask_yn "Remove it so the Claude Code you have now can be linked?" y; then
        run rm "$CLAUDE"
    fi
fi
if [ ! -x "$CLAUDE" ]; then
    OTHER="$(command -v claude 2>/dev/null || true)"
    # aliases are invisible to this script; older installs live behind one
    for c in "$HOME/.claude/local/claude" "$HOME/.npm-global/bin/claude" \
             "/opt/homebrew/bin/claude" "/usr/local/bin/claude"; do
        [ -n "$OTHER" ] && break
        [ -x "$c" ] && OTHER="$c"
    done
    if [ -n "$OTHER" ]; then
        say "  Found Claude Code at $OTHER"
        say "  The background daemon looks for it at $CLAUDE."
        # An npm install is a node script ("#!/usr/bin/env node"). If that node
        # (nvm, volta, fnm...) isn't on the daemon's PATH, a plain link would fail
        # in the background, so write a two-line launcher that names node exactly.
        NODE=""
        if head -c 120 "$OTHER" 2>/dev/null | head -1 | grep -q node; then
            if ! PATH="$DAEMON_PATH" command -v node >/dev/null 2>&1; then
                NODE="$(dirname "$OTHER")/node"
                [ -x "$NODE" ] || NODE="$(command -v node 2>/dev/null || true)"
            fi
        fi
        if [ -e "$CLAUDE" ] || [ -L "$CLAUDE" ]; then
            warn "$CLAUDE exists but isn't runnable; left it alone."
            note_problem "$CLAUDE exists but doesn't run. Remove it and run ./install.sh again."
        elif [ -n "$NODE" ]; then
            say "  It runs on node at $NODE, which the daemon can't see on its own."
            if ask_yn "Write a small launcher at $CLAUDE that runs it with that node?" y; then
                run mkdir -p "$LOCAL_BIN"
                if [ "$DRY" = 1 ]; then
                    say "  [dry run] would write $CLAUDE: exec \"$NODE\" \"$OTHER\" \"\$@\""
                else
                    printf '#!/bin/bash\n# Written by Voice Memo AI install.sh so the daemon can run Claude Code.\n# If you change node versions, delete this file and run ./install.sh again.\nexec "%s" "%s" "$@"\n' \
                        "$NODE" "$OTHER" > "$CLAUDE" && chmod +x "$CLAUDE" && ok "wrote $CLAUDE"
                fi
            fi
        elif ask_yn "Link it at $CLAUDE?" y; then
            run mkdir -p "$LOCAL_BIN"
            run ln -s "$OTHER" "$CLAUDE" && ok "linked"
        fi
    else
        say "  Claude Code is not installed. The official installer is:"
        say "    curl -fsSL https://claude.ai/install.sh | bash"
        if ask_yn "Run it now? (it prints nothing for about a minute while it downloads)" y; then
            if [ "$DRY" = 1 ]; then
                run "curl -fsSL https://claude.ai/install.sh | bash"
            else
                curl -fsSL https://claude.ai/install.sh | bash || warn "The Claude Code installer reported a problem."
            fi
        fi
        if [ ! -x "$CLAUDE" ] && [ "$DRY" != 1 ]; then
            say "  Then run '$CLAUDE' once, sign in, and run ./install.sh again."
            exit 0
        fi
        if [ "$DRY" != 1 ]; then
            say "  Now sign in: Claude Code opens below. Sign in, then type /exit to come back here."
            "$CLAUDE" || true
        fi
    fi
else
    ok "found $CLAUDE"
fi
if [ -x "$CLAUDE" ] || [ "$DRY" = 1 ]; then
    say "  Checking you are signed in, the way the daemon will run it (one tiny request)..."
    if [ -x "$CLAUDE" ] && OUT="$(PATH="$DAEMON_PATH" with_timeout 90 "$CLAUDE" -p "Reply with just: ok" 2>&1)" \
       && printf '%s' "$OUT" | grep -qi "ok"; then
        ok "Claude Code works and is signed in"
    else
        warn "Claude Code did not answer. Open a Terminal, run 'claude', and sign in"
        warn "(type /login if it doesn't ask). Then run ./install.sh again."
        [ -n "${OUT:-}" ] && printf '%s\n' "$OUT" | head -3 | sed 's/^/    > /'
        note_problem "Claude Code is not signed in (run: claude, then /login)."
    fi
fi
say "  Note: every memo runs Claude, and big jobs use a lot of it. A Max plan is recommended."

# --------------------------------------------------------- 7. whisper model ----

step "Speech model (runs on this Mac; about 1.5 GB)"
if [ -f "$MODEL_FILE" ] && [ "$(stat -f %z "$MODEL_FILE" 2>/dev/null || echo 0)" -gt 1000000000 ]; then
    ok "already downloaded: $MODEL_FILE"
else
    say "  The model ggml-medium.en.bin (~1.5 GB) comes from the official whisper.cpp page:"
    say "    $MODEL_URL"
    say "  It is saved to: $MODEL_FILE"
    if ask_yn "Download it now?" y; then
        run mkdir -p "$MODEL_DIR"
        if run curl -L --fail --progress-bar -o "$MODEL_FILE.part" "$MODEL_URL"; then
            run mv "$MODEL_FILE.part" "$MODEL_FILE"
            ok "downloaded"
        else
            warn "download failed; run ./install.sh again to retry"
            note_problem "Speech model not downloaded."
        fi
    else
        note_problem "Speech model not downloaded (transcription won't work without it)."
    fi
fi

# --------------------------------------------------------- 8. starter skills ----

step "Starter skills (copied into $SKILLS_DIR)"
run mkdir -p "$SKILLS_DIR"
for src in "$REPO_DIR"/starter/skills/*/; do
    name="$(basename "$src")"
    if [ -e "$SKILLS_DIR/$name" ]; then
        say "  skipped /$name: you already have a skill with that name (left untouched)"
    else
        run cp -R "${src%/}" "$SKILLS_DIR/$name"
        ok "added /$name"
    fi
done

# ---------------------------------------------------------- 9. rules.txt ----

step "Routing rules (which skill runs for which kind of memo)"
run mkdir -p "$STATE"
if [ -f "$STATE/rules.txt" ]; then
    ok "you already have rules at $STATE/rules.txt (left untouched)"
else
    run cp "$REPO_DIR/starter/rules.txt" "$STATE/rules.txt"
    ok "installed starter rules to $STATE/rules.txt"
fi

# ----------------------------------------------------------- 10. settings ----

step "Your name and projects folder"
SETTINGS="$STATE/settings.json"
CUR_NAME=""; CUR_DIR=""
if [ -f "$SETTINGS" ]; then
    CUR_NAME="$(json_get "$SETTINGS" name)"
    CUR_DIR="$(json_get "$SETTINGS" projects_dir)"
fi
WRITE_SETTINGS=1
if [ -n "$CUR_DIR" ]; then
    say "  Current settings: name \"$CUR_NAME\", projects folder $CUR_DIR"
    if ! ask_yn "Change them?" n; then WRITE_SETTINGS=0; fi
fi
if [ "$WRITE_SETTINGS" = 1 ]; then
    DEF_NAME="${CUR_NAME:-$(id -F 2>/dev/null || id -un)}"
    ask_text "Your name (skills use it in what they write):" "$DEF_NAME"
    NEW_NAME="$ANSWER"
    say "  Skills like /workOrder look for your projects in one folder, one project per subfolder."
    ask_text "Your projects folder:" "${CUR_DIR:-$HOME/Projects}"
    NEW_DIR="$ANSWER"
    case "$NEW_DIR" in "~"|"~/"*) NEW_DIR="$HOME${NEW_DIR#\~}" ;; esac
    if [ ! -d "$NEW_DIR" ]; then
        if ask_yn "$NEW_DIR doesn't exist. Create it?" y; then run mkdir -p "$NEW_DIR"; fi
    fi
    if [ "$DRY" = 1 ]; then
        say "  [dry run] would write $SETTINGS with name \"$NEW_NAME\" and projects_dir \"$NEW_DIR\""
    else
        /usr/bin/python3 -c 'import json,sys
json.dump({"projects_dir": sys.argv[2], "name": sys.argv[3]}, open(sys.argv[1], "w"), indent=2)' \
            "$SETTINGS" "$NEW_DIR" "$NEW_NAME" && ok "saved $SETTINGS"
    fi
fi

# --------------------------------------------------------- 11. vma command ----

step "The 'vma' command (start, stop, and check the daemon)"
run mkdir -p "$LOCAL_BIN"
if [ -L "$LOCAL_BIN/vma" ] && [ "$(readlink "$LOCAL_BIN/vma")" = "$VMA" ]; then
    ok "$LOCAL_BIN/vma already points here"
elif [ -e "$LOCAL_BIN/vma" ]; then
    warn "$LOCAL_BIN/vma already exists and isn't this one; left it alone."
    note_problem "$LOCAL_BIN/vma is something else; use $VMA directly."
else
    run ln -s "$VMA" "$LOCAL_BIN/vma"
    ok "linked $LOCAL_BIN/vma"
fi
case ":$PATH:" in
    *":$LOCAL_BIN:"*) ok "$LOCAL_BIN is on your PATH" ;;
    *) if grep -qs 'HOME/.local/bin' "$HOME/.zshrc"; then
           ok "~/.zshrc already adds $LOCAL_BIN (new Terminal windows will have it)"
       elif ask_yn "Add $LOCAL_BIN to your PATH in ~/.zshrc (so 'vma' and 'claude' work in new windows)?" y; then
           if [ "$DRY" = 1 ]; then
               run "append export PATH=\"\$HOME/.local/bin:\$PATH\" to ~/.zshrc"
           else
               printf '\n# Voice Memo AI + Claude Code\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$HOME/.zshrc"
               ok "added to ~/.zshrc (open a new Terminal window to use it)"
           fi
       else
           say "    To do it later: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
       fi ;;
esac

# ------------------------------------------------- 12. Voice Memos in iCloud ----

step "Turn on Voice Memos syncing (iPhone and Mac)"
say "  Memos you record on your iPhone reach this Mac through iCloud."
say "  On your iPhone: Settings > [your name] > iCloud > Saved to iCloud / See All > turn on Voice Memos."
say "  On this Mac:    System Settings > [your name] > iCloud > Saved to iCloud / See All > turn on Voice Memos."
say "  Then open the Voice Memos app on this Mac once so the folder exists."
pause

# --------------------------------------------------------- 13. start daemon ----

step "Start the daemon"
say "  This builds a tiny helper, installs a background job, and starts watching for memos."
say "  It starts automatically at login from now on. ('vma unload' turns it off.)"
if ask_yn "Start it now?" y; then
    run "$VMA" load || note_problem "vma load failed (see ~/.voice-memo-ai/daemon.err.log)."
else
    note_problem "Daemon not started (run: vma load)."
fi

# ------------------------------------------------------------ 14. texting ----

step "Where to text results"
TARGET_FILE="$STATE/imessage-target.txt"
CUR_T=""
[ -f "$TARGET_FILE" ] && CUR_T="$(cat "$TARGET_FILE")"
if [ -f "$TARGET_FILE" ] && ! ask_yn "Results currently go to \"${CUR_T:-nobody}\". Change that?" n; then
    ok "kept"
else
    say "  When a memo is done, this Mac sends an iMessage from the Messages app."
    say "  Enter your own Apple ID email or phone number (+15551234567), or leave blank for no texts."
    ask_text "Text results to:" ""
    if [ -n "$ANSWER" ]; then
        run "$VMA" notify "$ANSWER"
        say "  Messages must be signed in on this Mac. The first time, macOS asks to let the"
        say "  daemon control Messages: click Allow."
    else
        run "$VMA" notify off
    fi
fi

# ------------------------------------------------- 15. Full Disk Access ----

step "The one permission: Full Disk Access for the memo reader"
say "  macOS protects the Voice Memos folder. One small helper needs permission to read it:"
say "    $READER"
say "  1. System Settings opens to Privacy & Security > Full Disk Access."
say "  2. A Finder window opens with vma-reader selected."
say "  3. Drag vma-reader from Finder into the Full Disk Access list"
say "     (or click +, press Cmd+Shift+G, paste the path above, click Open)."
say "  4. Make sure its switch is on."
run open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
if [ -e "$READER" ] || [ "$DRY" = 1 ]; then
    run open -R "$READER"
else
    warn "The helper isn't built yet (the daemon didn't start). Run 'vma load', then this step again."
fi
pause

# ------------------------------------------------------------- 16. doctor ----

step "Check it can read your memos"
if [ "$DRY" = 1 ]; then
    run "$VMA" doctor
else
    TRIES=0
    while :; do
        TRIES=$((TRIES + 1))
        DOC="$("$VMA" doctor 2>&1)"; printf '%s\n' "$DOC" | sed 's/^/  /'
        if printf '%s' "$DOC" | grep -q "PASS"; then break; fi
        if [ "$TRIES" -ge 3 ] || ! ask_yn "Not yet. Fix the permission and try again?" y; then
            note_problem "vma doctor did not pass: grant Full Disk Access to $READER, then run: vma doctor"
            break
        fi
    done
fi

# ---------------------------------------------------------- 17. configurator ----

step "Open the configurator"
say "  A page on this Mac (http://localhost:8787) shows recent memos and lets you"
say "  switch skills on and off and edit when they run."
run "$VMA" config || true

# ----------------------------------------------------------------- done ----

printf '\n---- Done ----\n'
if [ -n "$PROBLEMS" ]; then
    say "Still to do:"
    printf '%s' "$PROBLEMS"
    say "Run ./install.sh again after fixing these; finished steps are skipped."
else
    say "All set. Record a voice memo on your iPhone, for example:"
    say "  \"Note to self: the spare key is in the blue drawer.\""
    say "A minute or two after it syncs, you'll get a text."
fi
say ""
say "Useful commands:  vma status   vma log   vma config   vma unload (turn it off)"
