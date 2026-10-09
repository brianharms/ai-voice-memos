#!/usr/bin/python3
"""Manually re-run ONE held memo through the daemon's own run_skill().

Why this exists: the pipeline is at-most-once — process_memo marks a memo `seen`
BEFORE running skills, so a memo whose skill run died (daemon kill, quota wall,
crash) is never retried automatically. Recovery has always been ad-hoc. This is
that recovery, made repeatable.

It deliberately reuses the LIVE engine (run_skill) rather than reimplementing it,
so a re-run is byte-for-byte the real thing: same prompt, same sandbox profile,
same untrusted-transcript framing, same result envelope, same log grammar (so the
configurator's activity feed parses it like any other run).

What it does NOT touch: seen.json and attempts.json (at-most-once still holds —
a re-run is an explicit human act), live.json and processing.json (the daemon
owns those), and the daemon's child.pgid (we track our own, so a daemon restart
never kills a manual re-run and our exit never clears the daemon's tracking).

Usage:
  rerun.py <memo-id> --skill /ideaDaemon [--model claude-fable-5-1 --effort max]
           [--title ...] [--summary ...] [--reason ...]
"""
import argparse
import importlib.util
import json
import os
import shutil
import sys
import traceback
from pathlib import Path

HOME = Path.home()
STATE = Path(os.environ.get("VMA_STATE_DIR") or HOME / ".voice-memo-ai")
RERUN_DIR = STATE / "rerun"


def load_engine():
    """Import the LIVE watcher module without running its main()."""
    path = STATE / "engine/watcher.py"
    spec = importlib.util.spec_from_file_location("vma_watcher", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vma_watcher"] = mod
    spec.loader.exec_module(mod)          # safe: watcher guards main() behind __main__
    return mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sid")
    ap.add_argument("--skill", required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--effort", default=None)
    ap.add_argument("--title", default="")
    ap.add_argument("--summary", default="")
    ap.add_argument("--reason", default="manual re-run")
    a = ap.parse_args()

    sid, skill = a.sid, a.skill
    RERUN_DIR.mkdir(parents=True, exist_ok=True)
    status_file = RERUN_DIR / f"{sid}.status.json"

    def status(**kw):
        try:
            cur = {}
            if status_file.exists():
                cur = json.loads(status_file.read_text())
            cur.update(kw)
            status_file.write_text(json.dumps(cur, indent=2))
        except Exception:
            pass

    w = load_engine()
    status(sid=sid, skill=skill, model=a.model, effort=a.effort,
           state="starting", pid=os.getpid(), started=w.now_iso())

    # --- locate the memo + its saved transcript --------------------------------
    name = w.read_recordings_index().get(sid)
    if not name:
        status(state="failed", error=f"{sid} not in recordings-index.json")
        return 2
    m4a = w.RECORDINGS / name
    tx = STATE / "transcripts" / f"{sid}.txt"
    if not tx.exists():
        status(state="failed", error=f"no saved transcript at {tx}")
        return 2
    transcript = tx.read_text()

    # API keys for children (wrangler deploys, fal renders) — launchd gives us a
    # bare env exactly like the daemon's, so do what the daemon does at startup.
    w.load_secrets()

    # --- isolate our child-process tracking from the daemon's -------------------
    # The daemon kills whatever pgid sits in child.pgid on restart (kill_stale_child)
    # and unlinks it when its own child exits. Sharing that file would mean a daemon
    # restart kills this re-run, and our exit blinds the daemon to its own child.
    w.CHILD_PGID_FILE = RERUN_DIR / f"child-{sid}.pgid"

    # --- private sandbox profile ------------------------------------------------
    # _sandbox_profile_path() rewrites a SHARED skill.sb then immediately execs it.
    # Concurrent re-runs would race (one truncating the file another is reading), so
    # each re-run gets its own frozen copy.
    trusted = skill in w.TRUSTED_SKILLS
    shared = w._sandbox_profile_path(trusted=trusted)
    private = RERUN_DIR / f"skill-{sid}.sb"
    private.write_text(shared.read_text())
    w._sandbox_profile_path = lambda trusted=False, _p=private: _p

    # --- model / effort ---------------------------------------------------------
    # run_skill takes these directly and allowlists them the same way the daemon's
    # own runs do; unset means "whatever model.json says".
    cfg_model, cfg_effort = w.read_model_config()
    model = w.normalize_model(a.model) or cfg_model
    effort = w.normalize_effort(a.effort) or cfg_effort
    if a.model and not w.normalize_model(a.model):
        status(state="failed", error=f"unknown model {a.model!r}")
        return 2
    if a.effort and not w.normalize_effort(a.effort):
        status(state="failed", error=f"unknown effort {a.effort!r}")
        return 2
    status(model=model, effort=effort)

    # --- log plumbing -----------------------------------------------------------
    # parse_activity() groups the log into feed cards by "NEW memo:" markers and
    # reads the lines that follow, so a re-run must speak the same grammar. Two
    # writes, each a single atomic append so concurrent runs can't interleave
    # inside a card:
    #   1. a header block now  -> the card appears in Activity immediately
    #   2. the full block at the end -> dedupe (newest per memo_id wins) replaces
    #      the header card with one carrying the finished result
    snippet = " ".join(transcript.split())[:120]
    ts = w.now()

    def base_block():
        out = [f"[{ts}] NEW memo: {name} (id {sid})",
               f"[{ts}]     transcript: {snippet}..."]
        if a.title:
            out.append(f"[{ts}]     memo title: {a.title}")
        if a.summary:
            out.append(f"[{ts}]     memo summary: {a.summary}")
        out.append(f"[{ts}]     route -> ['{skill}']  ({a.reason})")
        return out

    def emit(lines):
        try:
            with open(w.LOG_FILE, "a") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:
            pass

    emit(base_block() + [f"[{ts}]     -> running {skill} (manual re-run) ..."])

    captured = []
    real_log = w.log
    w.log = lambda msg: captured.append(f"[{w.now()}] {msg}")

    # --- the run ----------------------------------------------------------------
    # Move the dead envelope aside first: run_skill only overwrites it on success,
    # so leaving it would let a stale failure keep showing as this memo's result.
    old = w.RESULTS_DIR / f"{sid}--{skill.lstrip('/')}.json"
    if old.exists():
        shutil.move(str(old), str(RERUN_DIR / f"{old.name}.superseded"))

    status(state="running", log_header_at=ts)
    rc = 0
    try:
        allowed = {r["skill"] for r in w.read_rules()}
        w.run_skill(skill, transcript, m4a, allowed, model=model, effort=effort)
    except Exception:
        rc = 1
        captured.append(f"[{w.now()}]     !! re-run driver error: {traceback.format_exc(limit=3)}")
    finally:
        w.log = real_log
        emit(base_block() + captured)
        try:
            (RERUN_DIR / f"child-{sid}.pgid").unlink(missing_ok=True)
        except Exception:
            pass

    # --- completion push --------------------------------------------------------
    envs = w._envelopes(sid)
    urls = w.result_urls(sid)
    partials = w.result_partials(sid)
    status(state="done", finished=w.now_iso(), rc=rc,
           urls=urls, partials=partials, envelopes=len(envs))

    title = a.title or snippet[:60]
    lines = [("⚠️ Re-run finished but incomplete: " if partials and not urls
              else "🔁 Re-run done: ") + title]
    for e in envs:
        m = " ".join(str(e.get("message") or "").split())
        if m:
            lines.append(m[:2000])
    for e in envs:
        t = str(e.get("title") or "").strip()
        if t:
            lines.append(f"• {t}")
    lines += urls
    if partials:
        lines.append("⚠️ partial: " + " | ".join(partials))
    lines.append(f"({skill} on {model} at {effort} effort)")
    try:
        w.send_imessage("\n".join(lines))
    except Exception:
        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
