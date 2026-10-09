#!/usr/bin/env python3
"""AI Voice Memos — handoff queue CLI (runs on the Mac mini).

A second Mac's helper calls this over ssh. Prints one JSON object.

  handoff_cli.py waiting               every waiting job, oldest first; counts as a MacBook check-in
  handoff_cli.py take <job>            that waiting job -> claimed by the MacBook ({} if gone)
  handoff_cli.py claim                 RETIRED, always {}: it handed out the oldest job blindly; the
                                       parallel helper picks with waiting + take (one job per project)
  handoff_cli.py check <job>           {"cancel": bool}; also counts as a MacBook check-in
  handoff_cli.py finish <job>          stdin {"state", "exit", "envelope", "board"?} -> done
  handoff_cli.py board <job>           stdin: the run's task board -> {"cancel": bool}; MacBook check-in
  handoff_cli.py ship <job>            stdin {"service", "ok", "summary", "commit"}: auto-ship result
  handoff_cli.py orphans               jobs the MacBook claimed but no longer runs -> interrupted
  handoff_cli.py create                stdin job JSON (skill, transcript, title, source) -> {"job": id}
  handoff_cli.py list                  every job
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watcher as W  # noqa: E402


def save_board(job_id: str, board) -> bool:
    """A MacBook run's task board, filed under its job's own key (never the sender's)."""
    if not isinstance(board, dict) or not W._JOB_RE.fullmatch(job_id or ""):
        return False
    job = W._read_job(W._job_file("claimed", job_id)) or W._read_job(W._job_file("done", job_id))
    key, skill = str(job.get("result_key") or ""), str(job.get("skill") or "").lstrip("/")
    if not key or not skill:
        return False
    board["key"] = f"{key}--{skill}"
    return W.write_board(board["key"], board)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    arg = argv[2] if len(argv) > 2 else ""
    if cmd == "claim":
        return {}
    if cmd == "waiting":
        W.laptop_seen()
        jobs = [j for j in W.handoff_jobs() if j.get("state") == "waiting"]
        return {"jobs": sorted(jobs, key=lambda j: j.get("created") or "")}
    if cmd == "take":
        W.laptop_seen()
        return W.handoff_take(arg, worker="laptop") or {}
    if cmd == "check":
        W.laptop_seen()
        return {"cancel": W.handoff_cancel_requested(arg)}
    if cmd == "board":
        W.laptop_seen()
        body = json.loads(sys.stdin.read() or "{}")
        return {"ok": save_board(arg, body), "cancel": W.handoff_cancel_requested(arg)}
    if cmd == "finish":
        body = json.loads(sys.stdin.read() or "{}")
        if isinstance(body, dict) and isinstance(body.get("board"), dict):
            save_board(arg, body["board"])
        job = W.handoff_finish(arg, state=str(body.get("state") or "done"),
                               exit_code=body.get("exit"), envelope=body.get("envelope"))
        return job or {"error": "not a running job"}
    if cmd == "ship":
        body = json.loads(sys.stdin.read() or "{}")
        return W.handoff_note_ship(arg, body if isinstance(body, dict) else {})
    if cmd == "orphans":
        W.laptop_seen()
        return {"interrupted": W.handoff_interrupt_orphans("laptop")}
    if cmd == "create":
        body = json.loads(sys.stdin.read() or "{}")
        if not isinstance(body, dict):
            return {"error": "expected a JSON object"}
        return {"job": W.handoff_create(body)}
    if cmd == "list":
        return {"jobs": W.handoff_jobs(), "laptop": W.laptop_status()}
    return {"error": f"unknown command {cmd!r}"}


if __name__ == "__main__":
    try:
        out = main(sys.argv)
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out))
    sys.exit(1 if "error" in out else 0)
