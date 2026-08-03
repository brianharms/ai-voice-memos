# AGENTS.md — installing AI Voice Memos

These instructions are written for a coding agent (e.g. Claude Code) performing
the install on a user's Mac. Execute the steps in order, verify each one, and
stop to ask the human only where marked **HUMAN STEP** — those are macOS
permission grants that cannot be automated.

Conventions in this file:

- `$REPO` = the cloned repo root (any location works; `~/Desktop/Claude Projects/ai-voice-memos` is the default the optional menu-bar app assumes).
- `$STATE` = `~/.voice-memo-ai` (created by the installer; the daemon runs from a copy here, deliberately outside iCloud and TCC-protected paths).
- Never run the engine from the repo directly; `vma load` deploys it.

## 0. Preflight — verify the environment

Run each check; resolve failures before continuing.

```bash
# macOS + login user (launchd agents are per-user)
sw_vers -productVersion

# Claude Code CLI, logged in (the daemon calls it headless)
test -x "$HOME/.local/bin/claude" && "$HOME/.local/bin/claude" --version
# If claude lives elsewhere, edit CLAUDE at the top of engine/watcher.py.

# Apple's system python — NOT homebrew's. The daemon must run on a binary with
# a stable code identity; homebrew python changes identity on every upgrade.
/usr/bin/python3 --version

# ffmpeg + whisper.cpp
brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
brew list whisper-cpp >/dev/null 2>&1 || brew install whisper-cpp
test -x /opt/homebrew/bin/whisper-cli || echo "whisper-cli missing — check brew install"

# Xcode Command Line Tools (for clang; menubar app additionally needs swift)
xcode-select -p
```

**HUMAN STEP** — confirm with the user that **Voice Memos iCloud sync** is on
(iPhone: Settings → [name] → iCloud → Voice Memos; Mac: Voice Memos app must
show phone recordings). Without sync there is nothing to watch. The watched
folder is `~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings`
(override with `VMA_WATCH_DIR`).

## 1. Whisper model

```bash
mkdir -p "$HOME/Library/Application Support/whisper/models"
cd "$HOME/Library/Application Support/whisper/models"
test -f ggml-medium.en.bin || curl -L -o ggml-medium.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.en.bin
ls -lh ggml-medium.en.bin   # ~1.5 GB
```

A different model works too — edit `MODEL` at the top of `engine/watcher.py`.

## 2. Rules — what should run, and when

```bash
cp "$REPO/rules.example.txt" "$REPO/rules.txt"
```

Edit `rules.txt`: one line per skill, `/skill-name : plain-language condition`.
Every `/skill-name` must exist at `~/.claude/skills/<name>/SKILL.md` on this
machine. Start with `/note` and `/wordcount`-style low-stakes skills; the user
can arm more from the configurator later. (`vma load` seeds
`$STATE/rules.txt` from this file on first install; after that the state copy
is the live one.)

## 3. Install and start the daemon

```bash
cd "$REPO"
chmod +x daemon/vma daemon/watchdog.sh
./daemon/vma load
```

`vma load` copies the engine to `$STATE/engine/`, builds `$STATE/bin/vma-reader`
(one-time; ad-hoc signed), templates + installs two launchd agents (daemon +
external watchdog), and starts them.

**HUMAN STEP — Full Disk Access for vma-reader.** The helper binary needs a
one-time FDA grant to read the Voice Memos container. Ask the user to:

1. Open System Settings → Privacy & Security → **Full Disk Access**
2. Click **+**, press **⌘⇧G**, and enter: `~/.voice-memo-ai/bin/vma-reader`
3. Add it and toggle it **on**

Notes that save future pain:
- The grant is bound to the binary's code identity. **Never rebuild vma-reader
  casually** — `vma reader-rebuild` exists for deliberate updates, after which
  the grant must be re-done.
- Nothing else needs FDA. The daemon's python holds zero grants by design.

## 4. Verify

```bash
./daemon/vma doctor    # checks folder access, deps, claude, model, launchd state
./daemon/vma status    # running? heartbeat fresh?
tail -20 ~/.voice-memo-ai/log.txt   # expect: "folder access OK (N recording files visible)"
```

`vma doctor` failing on folder access means the FDA grant didn't take —
re-check the toggle, then `./daemon/vma restart`.

## 5. Configurator (web UI)

```bash
nohup /usr/bin/python3 "$REPO/configurator/server.py" >> ~/.voice-memo-ai/configurator.out 2>&1 &
open http://localhost:8787
```

Loopback-only; writes are token-gated per server start. From here the user can
arm/disarm automations, edit conditions, watch memos process live, read
transcripts, and play recordings.

Caveat: audio playback goes through an index the daemon publishes
(`$STATE/recordings-index.json`) plus `vma-reader` copies. If playback reports
"recordings are temporarily unavailable", the server process's TCC context is
blocked — relaunch it from a terminal the user has been using interactively.

## 6. Menu-bar app (optional)

```bash
cd "$REPO/menubar" && ./build-app.sh
```

If the repo is not at `~/Desktop/Claude Projects/ai-voice-memos`, set
`VMA_PROJECT_DIR=<repo path>` in the app's environment. The app shells out to
`daemon/vma` and reads `$STATE/status.json` for its status glyph.

## 7. End-to-end test

1. Ensure `/wordcount` (or another trivial skill) is armed in rules.
2. Ask the user to record a short voice memo on their iPhone: *"This is a test
   memo, please count these words."*
3. Watch: `./daemon/vma log` — within ~a minute of iCloud sync you should see
   `detected → transcript → route → running /wordcount → finished`, and the
   entry appears in the configurator with a title, transcript, and playback.

## Uninstall

```bash
./daemon/vma unload          # stops daemon + watchdog, removes launchd plists
rm -rf ~/.voice-memo-ai      # state, logs, transcripts, results (irreversible)
# then remove the FDA entry for vma-reader in System Settings
```

## Operational notes for the resident agent

- **Costs**: routing runs slim (~hundreds of tokens on a small model), but the
  skills themselves are real autonomous `claude -p` sessions and cost what a
  session costs. Arm accordingly.
- The daemon detects Claude quota/rate-limit walls, pauses detection, and
  retries memos when quota clears (memos are never consumed by a quota error).
- Every memo is at-most-once: if a run is interrupted mid-skill, it is NOT
  re-run automatically — check `~/.voice-memo-ai/log.txt` and re-run manually
  if appropriate.
- State worth knowing: `rules.txt` (live rules), `seen.json` (processed ids),
  `results/` (per-skill envelopes), `transcripts/` (per-memo text),
  `log.txt` (the activity feed's source of truth).
