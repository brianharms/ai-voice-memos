#!/usr/bin/env python3
"""
Voice Memo AI — Configurator backend.

A tiny stdlib HTTP server that is a WINDOW onto the always-on watcher daemon.
It does NOT control the daemon's lifecycle (launchd owns that). It reads shared
state files and reads/writes rules.txt — the same files the watcher uses.

Run:  python3 configurator/server.py   (then open http://localhost:8787)

Endpoints (the design's HTML binds to these):
  GET  /status    -> { running, since, last_poll, cutoff, claude_ok }
  GET  /rules     -> [ { skill, condition, enabled } ]
  POST /rules     <- [ { skill, condition, enabled } ]   (rewrites rules.txt)
  GET  /activity  -> [ { time, memo_name, snippet, outcome, skill, result, reason } ]
  GET  /skills    -> [ "/idea", "/note", ... ]           (available on disk)
  GET  /model     -> { model, effort, models:[{id,label}], efforts:[{id,label}] }
  GET  /board?key=<memo>--<skill> -> a run's task board (steps, workers), 404 if none
  POST /model     <- { model?, effort? }   default for skill runs (also the menu bar's)
  POST /summarize <- { id }   fills in the "what was done" summary on a memo's older results
  GET  /           -> the configurator page (index.html if present, else a stub)
"""
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# import the shared helpers from the engine
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "engine"))
import watcher as W  # noqa: E402

import os
PORT = int(os.environ.get("VMA_CONFIGURATOR_PORT") or 8787)   # override only for test harnesses
WEB_DIR = Path(__file__).resolve().parent / "web"   # where index.html will live


def _phone_available() -> bool:
    """The Phone tab needs the switchboard service (same root phone.py reads)."""
    return Path(os.environ.get("SB_ROOT", str(Path.home() / "services" / "switchboard"))).exists()


# A per-run secret. The server serves the page (same-origin) and injects this token
# into it; a cross-origin attacker page cannot read our response bodies (no CORS),
# so it cannot learn the token and cannot forge a write. Written to a 0600 file so
# only this user can read it; regenerated each server start.
SESSION_TOKEN = None

def _new_token() -> str:
    import secrets
    return secrets.token_urlsafe(24)

# Host header allowlist — defeats DNS-rebinding (a remote name resolving to
# 127.0.0.1 would carry an attacker Host, which we reject).
_ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}", "localhost", "127.0.0.1"}


class Handler(BaseHTTPRequestHandler):
    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in _ALLOWED_HOSTS

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # NO wildcard CORS. Same-origin only: the page is served by this server, so
        # our own fetch() works; other sites' fetch() to these endpoints is blocked
        # by the browser's same-origin policy (which is exactly what we want).
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        p = self.path.split("?")[0]
        try:
            if p == "/status":
                st = W.read_status()
                st["multi_machine"] = W.ROLE_FILE.exists()
                st["phone"] = _phone_available()
                self._send(200, st)
            elif p == "/live":
                self._send(200, W.read_live())
            elif p == "/rules":
                self._send(200, W.read_all_rules())
            elif p == "/activity":
                self._send(200, W.parse_activity(limit=100))
            elif p == "/skills":
                self._send(200, W.list_available_skills())
            elif p == "/model":
                self._send(200, W.model_settings())
            elif p == "/board":
                # a run's task board for the Activity panel (key = envelope stem,
                # validated in board_path — no traversal)
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                b = W.read_board((q.get("key") or [""])[0])
                self._send(200 if b else 404, b or {"error": "no board"})
            elif p == "/skill":
                # the SKILL.md content for the docs viewer (name validated
                # against skills on disk inside read_skill_doc — no traversal)
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                name = (q.get("name") or [""])[0]
                self._send(200, W.read_skill_doc(name))
            elif p == "/skill-icons":
                self._send(200, W.read_skill_icons())
            elif p == "/transcript":
                # the persisted transcript for a memo id (saved by the daemon
                # at transcription time; older memos won't have one)
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                mid = (q.get("id") or [""])[0]
                doc = W.read_transcript(mid)
                self._send(404 if "error" in doc else 200, doc)
            elif p == "/audio":
                # serve the recording for a given memo id (for inline playback).
                # Resolved via the daemon-published index — this process's own
                # vma-reader listing is TCC-denied (different responsible
                # process), so it must not try to list the folder itself.
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                mid = (q.get("id") or [""])[0]
                name = W.read_recordings_index().get(mid) if mid else None
                if not name:
                    return self._send(404, {"error": "recording not found"})
                # goes through the FDA-granted helper if this process can't
                # read the protected container directly
                try:
                    data = W.read_recording_bytes(W.RECORDINGS / name)
                except OSError:
                    # distinct from 404: the file exists but THIS process is
                    # blocked from reading it — the UI words these differently
                    return self._send(503, {"error": "recordings unavailable (access blocked)"})
                self.send_response(200)
                self.send_header("Content-Type", "audio/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(data)
            elif p.startswith("/phone/"):
                # the Phone tab (switchboard): see configurator/phone.py
                import phone
                code, body = phone.get(p)
                self._send(code, body)
            elif p == "/phone.js":
                js = WEB_DIR / "phone.js"
                self._send(200, js.read_text(), "text/javascript; charset=utf-8") if js.exists() \
                    else self._send(404, {"error": "not found"})
            elif p == "/" or p == "/index.html":
                idx = WEB_DIR / "index.html"
                html = idx.read_text() if idx.exists() else _STUB
                # inject the write-token so the served page (same-origin) can send it
                # on POSTs. A cross-origin page can't read this HTML, so can't get it.
                html = html.replace("__VMA_TOKEN__", SESSION_TOKEN or "")
                if not _phone_available():
                    # no switchboard on this Mac: no Phone tab (and no tab bar for one tab)
                    html = re.sub(r"<!--PHONE-->.*?<!--/PHONE-->", "", html, flags=re.S)
                if "__VMA_TOKEN__" not in (idx.read_text() if idx.exists() else _STUB):
                    # also expose via meta tag if the page didn't use the placeholder
                    html = html.replace("<head>", f'<head><meta name="vma-token" content="{SESSION_TOKEN}">', 1)
                self._send(200, html, "text/html; charset=utf-8")
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        # write auth: require the session token (defeats CSRF from other origins,
        # which can't read the token). Accept it via header or JSON field.
        if self.headers.get("X-VMA-Token", "") != SESSION_TOKEN:
            return self._send(403, {"error": "missing or bad token"})
        p = self.path.split("?")[0]
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"[]")
            if p == "/rules":
                if not isinstance(payload, list):
                    return self._send(400, {"error": "expected a list of rules"})
                W.write_all_rules(payload)
                self._send(200, {"ok": True, "saved": len(payload)})
            elif p == "/skill-icon":
                # design (or fetch cached) a tile for a skill — one slim model
                # call per skill ever; glyph vocabulary comes from the page
                name = str(payload.get("name", ""))
                glyphs = payload.get("glyphs")
                if not isinstance(glyphs, list) or not all(isinstance(g, str) for g in glyphs):
                    return self._send(400, {"error": "expected glyphs: [names]"})
                icon = W.generate_skill_icon(name, glyphs[:64])
                self._send(200 if icon else 502, icon or {"error": "icon generation failed"})
            elif p == "/control":
                # The user's controls: {"action": "pause"|"resume"|"cancel"|"later"|"release"
                #   |"runhere"|"handoff_cancel", "id": optional (memo id, or handoff job id)}
                action = str(payload.get("action", "")).strip()
                sid = str(payload.get("id", "") or "").strip()
                if action == "release":
                    if not re.fullmatch(r"[\w .\-]{1,80}", sid) or sid.startswith("."):
                        return self._send(400, {"error": "release needs a valid memo id"})
                    W.RELEASE_DIR.mkdir(parents=True, exist_ok=True)
                    (W.RELEASE_DIR / sid).write_text("")
                    return self._send(200, {"ok": True, "action": "release"})
                res = W.request_control(action, sid)
                self._send(200 if res.get("ok") else 400, res)
            elif p == "/model":
                if not isinstance(payload, dict):
                    return self._send(400, {"error": "expected {model?, effort?}"})
                res = W.write_model_config(payload.get("model"), payload.get("effort"))
                self._send(200 if res.get("ok") else 400, res)
            elif p == "/hosts":
                # which machine runs an automation: {"skill": "/workOrder", "host": "mini"|"laptop"}
                res = W.write_host(str(payload.get("skill", "")), str(payload.get("host", "")))
                self._send(200 if res.get("ok") else 400, res)
            elif p == "/summarize":
                # "what was done" for a finished memo whose result predates summaries:
                # one slim model call per envelope, cached into the envelope itself
                res = W.summarize_memo_results(str(payload.get("id", "")).strip())
                self._send(400 if "error" in res else 200, res)
            elif p == "/activity-hide":
                # delete one entry from the activity feed (tombstone — the
                # daemon log is never rewritten). Key comes from del_key on
                # the /activity entries.
                key = str(payload.get("key", "")).strip()
                if not key:
                    return self._send(400, {"error": "expected key"})
                W.hide_activity_entry(key)
                self._send(200, {"ok": True})
            elif p.startswith("/phone/"):
                # the Phone tab (switchboard): see configurator/phone.py
                import phone
                code, body = phone.post(p, payload)
                self._send(code, body)
            elif p == "/skill-open":
                # open the SKILL.md in the user's default editor (local action;
                # path comes from read_skill_doc's validated lookup, never the client)
                doc = W.read_skill_doc(str(payload.get("name", "")))
                if "path" not in doc:
                    return self._send(404, doc)
                import subprocess as _sp
                _sp.run(["open", doc["path"]], timeout=10)
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def log_message(self, *a):  # quiet
        pass


# minimal placeholder page until the real design is dropped into web/index.html.
# The token is injected server-side into __VMA_TOKEN__; the real design page should
# read it from <meta name="vma-token"> and send it as the X-VMA-Token header on POST.
_STUB = """<!doctype html><html><head><meta charset=utf-8><title>Voice Memo AI</title>
<meta name="vma-token" content="__VMA_TOKEN__"></head>
<body style="font-family:ui-monospace,monospace;background:#111;color:#ddd;padding:2rem">
<h2>Voice Memo AI — Configurator (backend running)</h2>
<p>API is live. Drop the real design into <code>configurator/web/index.html</code>.</p>
<p>Endpoints: <a href=/status style=color:#8bd>/status</a> ·
<a href=/rules style=color:#8bd>/rules</a> ·
<a href=/activity style=color:#8bd>/activity</a> ·
<a href=/skills style=color:#8bd>/skills</a></p>
<p style="color:#666">Writes (POST /rules) require the X-VMA-Token header; the token is
in the meta tag above and is only readable same-origin.</p>
</body></html>"""


def main():
    global SESSION_TOKEN
    SESSION_TOKEN = _new_token()
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    # bind to loopback only (never 0.0.0.0) so it's unreachable from the network
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"configurator backend on http://localhost:{PORT} (loopback only, token-gated writes)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
