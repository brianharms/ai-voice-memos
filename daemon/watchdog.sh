#!/bin/bash
# AI Voice Memos — EXTERNAL watchdog (a RESIDENT loop under its own LaunchAgent).
#
# Why a resident loop and not StartInterval: on modern macOS, launchd defers
# "nondemand" spawns (RunAtLoad / StartInterval / KeepAlive respawns) with
# efficiency penalties — observed live: a StartInterval watchdog with runs=0
# ("pended nondemand spawn = speculative") and a KeepAlive daemon respawn
# delayed >5 minutes ("inefficient"). DEMAND spawns (launchctl kickstart) are
# never deferred. So: this script is spawned ONCE (vma load kickstarts it
# explicitly) and stays resident; every restart it performs is a demand spawn.
#
# Each minute it checks the watcher daemon and kickstarts it if:
#   - the process is missing entirely (covers launchd pending a KeepAlive
#     respawn after the daemon's internal watchdog force-exits), or
#   - the heartbeat file is stale >120s (process frozen solid — kernel/GIL
#     wedge that not even the daemon's internal watchdog thread can escape).
# A restart of a healthy-but-slow daemon is harmless by design (memos are
# marked seen before their skill runs — at-most-once, no double-deploy).
#
# NOTE: installed as a COPY at ~/.voice-memo-ai/watchdog.sh by `vma load` — launchd
# must never exec a script under the iCloud-synced project path (exit-127 scar).

LABEL="industries.ritual.voice-memo-ai"
STATE="$HOME/.voice-memo-ai"
HB="$STATE/heartbeat"
STALE_LIMIT=120   # seconds without a heartbeat = daemon is frozen
WLOG="$STATE/watchdog.log"

uid=$(id -u)

wlog() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$WLOG" 2>/dev/null
}

wlog "watchdog resident loop started (pid $$)"

while true; do
    sleep 60

    # If the main agent isn't loaded, the user disabled it (vma unload boots us
    # out too, but never resurrect a deliberately-disabled daemon regardless).
    launchctl print "gui/$uid/$LABEL" >/dev/null 2>&1 || continue

    now=$(date +%s)

    # Never fire in the first minutes after boot or wake-from-sleep: the
    # heartbeat's mtime is legitimately stale while the machine was asleep.
    skip=0
    for key in kern.boottime kern.waketime; do
        t=$(sysctl -n "$key" 2>/dev/null | sed -nE 's/.*sec = ([0-9]+).*/\1/p')
        if [ -n "$t" ] && [ $(( now - t )) -lt 180 ]; then
            skip=1
        fi
    done
    [ "$skip" = "1" ] && continue

    # Case 1: process missing entirely — launchd's KeepAlive respawn is pended.
    # A plain (demand) kickstart starts it immediately. Match ONLY the installed
    # daemon path — a dev/test watcher elsewhere must not mask a dead daemon.
    if ! pgrep -f "$STATE/engine/watcher.py" >/dev/null 2>&1; then
        wlog "watcher process missing — kickstart $LABEL"
        launchctl kickstart "gui/$uid/$LABEL"
        continue
    fi

    # Case 2: process alive but heartbeat frozen — kill and restart it.
    hb_mtime=0
    if [ -f "$HB" ]; then
        hb_mtime=$(stat -f %m "$HB" 2>/dev/null || echo 0)
    fi
    age=$(( now - hb_mtime ))
    if [ "$age" -gt "$STALE_LIMIT" ]; then
        wlog "heartbeat stale ${age}s — kickstart -k $LABEL"
        launchctl kickstart -k "gui/$uid/$LABEL"
    fi
done
