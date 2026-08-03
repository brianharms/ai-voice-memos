#!/usr/bin/env python3
"""device_deploy — gated build/test/device-push for VMA daemon skill runs.

Usage (from inside a sandboxed skill run):
    /usr/bin/python3 ~/.voice-memo-ai/engine/device_deploy.py <project-dir> [--tests-only]

Contract:
  - The project must contain .vma-device-deploy.json:
      { "deviceId": "...", "scheme": "...", "project": "....xcodeproj",
        "unitTestTarget": "...", "uiTestTarget": "...", "bundleId": "..." }
    No config file -> {"status": "skipped"} and NOTHING is built or pushed.
    A device is only ever pushed to if it is named in the project's own config.
  - Gate: the device install happens ONLY if the unit suite AND the UI suite
    both pass in THIS run. --tests-only stops after the gate (no build/push).
  - Every xcodebuild gets OTHER_SWIFT_FLAGS += "-Xfrontend -disable-sandbox":
    the Swift frontend sandboxes its macro plugin server, and a seatbelt
    profile cannot nest inside the daemon's sandbox (sandbox_apply EPERM ->
    "swift-plugin-server produced malformed response"). The outer VMA sandbox
    still contains the whole build. Derived data always goes under /tmp —
    project trees are iCloud-synced and fileproviderd hangs builds.
  - Install retries + reachability watcher: the phone drops off Wi-Fi
    constantly. "unavailable" CONTAINS "available", so state checks must
    exclude it explicitly, never bare-match.
  - Before replacing the cached app, the previously-installed .app is rotated
    to last-good.app (one-command rollback:
      xcrun devicectl device install app --device <id> <last-good.app>).

Prints a single JSON result line as the LAST line of stdout; also appends
progress to the VMA log so the configurator activity feed shows the steps.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
STATE = Path(os.environ.get("VMA_STATE") or HOME / ".voice-memo-ai")
LOG_FILE = STATE / "log.txt"
BUILD_CACHE = STATE / "device-builds"
CONFIG_NAME = ".vma-device-deploy.json"
SWIFT_FLAGS = "$(inherited) -Xfrontend -disable-sandbox"
SIM_DEST = os.environ.get("VMA_SIM_DEST", "platform=iOS Simulator,name=iPhone 17")

UNIT_TIMEOUT = int(os.environ.get("VMA_UNIT_TIMEOUT", "900"))
UI_TIMEOUT = int(os.environ.get("VMA_UI_TIMEOUT", "1800"))
DEVICE_BUILD_TIMEOUT = int(os.environ.get("VMA_DEVICE_BUILD_TIMEOUT", "900"))
REACH_POLL = int(os.environ.get("VMA_REACH_POLL", "5"))
REACH_CAP = int(os.environ.get("VMA_REACH_CAP", "1800"))     # 30 min
INSTALL_RETRIES = int(os.environ.get("VMA_INSTALL_RETRIES", "5"))
INSTALL_RETRY_GAP = int(os.environ.get("VMA_INSTALL_RETRY_GAP", "8"))


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]     device-deploy: {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run(cmd, timeout):
    """Run, capture combined output, never raise on timeout."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return -9, out + f"\n[timed out after {timeout}s]"


def finish(result: dict) -> None:
    log(f"result: {result['status']}" + (f" — {result.get('reason','')}" if result.get("reason") else ""))
    print(json.dumps(result))
    sys.exit(0 if result["status"] in ("deployed", "tests_green") else 1)


def xcodebuild_test(proj_dir: Path, cfg: dict, target: str, label: str, timeout: int, derived: str):
    cmd = ["xcodebuild", "test",
           "-project", cfg["project"], "-scheme", cfg["scheme"],
           "-only-testing:" + target,
           "-destination", SIM_DEST,
           "-derivedDataPath", derived,
           f"OTHER_SWIFT_FLAGS={SWIFT_FLAGS}"]
    log(f"{label} tests: {target} …")
    t0 = time.monotonic()
    rc, out = run(cmd, timeout)
    ok = rc == 0 and "** TEST SUCCEEDED **" in out
    log(f"{label} tests {'PASSED' if ok else 'FAILED'} ({time.monotonic()-t0:.0f}s)")
    return ok, out


def device_reachable(device_id: str) -> bool:
    rc, out = run(["xcrun", "devicectl", "list", "devices"], 60)
    if rc != 0:
        return False
    for line in out.splitlines():
        if device_id in line:
            # "unavailable" contains "available" — exclude it explicitly
            return "unavailable" not in line and "available" in line
    return False


def wait_reachable(device_id: str) -> bool:
    t0 = time.monotonic()
    logged = False
    while time.monotonic() - t0 < REACH_CAP:
        if device_reachable(device_id):
            if logged:
                log("device is reachable again")
            return True
        if not logged:
            log(f"device {device_id[:8]}… unreachable — waiting (cap {REACH_CAP//60} min)")
            logged = True
        time.sleep(REACH_POLL)
    return False


def install_app(device_id: str, app: Path) -> tuple:
    """Install with retry; returns (ok, classification)."""
    for attempt in range(1, INSTALL_RETRIES + 1):
        rc, out = run(["xcrun", "devicectl", "device", "install", "app",
                       "--device", device_id, str(app)], 300)
        if "App installed" in out:
            log(f"App installed (attempt {attempt})")
            return True, "installed"
        if "kAMDMobileImageMounterDeviceLocked" in out or "DeviceLocked" in out:
            log(f"attempt {attempt}: phone is locked — waiting 30s")
            time.sleep(30)
            continue
        if "1011" in out:
            log(f"attempt {attempt}: device unreachable (1011) — back to reachability watcher")
            if not wait_reachable(device_id):
                return False, "unreachable"
            continue
        if "3002" in out or "4000" in out:
            log(f"attempt {attempt}: transient connection error — retrying in {INSTALL_RETRY_GAP}s")
            time.sleep(INSTALL_RETRY_GAP)
            continue
        log(f"attempt {attempt}: install failed: {out.strip()[-200:]}")
        time.sleep(INSTALL_RETRY_GAP)
    return False, "retries_exhausted"


def main():
    if len(sys.argv) < 2:
        finish({"status": "error", "reason": "usage: device_deploy.py <project-dir> [--tests-only]"})
    proj_dir = Path(sys.argv[1]).expanduser().resolve()
    tests_only = "--tests-only" in sys.argv[2:]
    cfg_path = proj_dir / CONFIG_NAME
    if not cfg_path.exists():
        finish({"status": "skipped", "reason": f"no {CONFIG_NAME} in {proj_dir} — device deploy is opt-in per project"})
    try:
        cfg = json.loads(cfg_path.read_text())
    except Exception as e:
        finish({"status": "error", "reason": f"bad {CONFIG_NAME}: {e}"})
    for key in ("deviceId", "scheme", "project", "unitTestTarget", "bundleId"):
        if not cfg.get(key):
            finish({"status": "error", "reason": f"{CONFIG_NAME} missing required key: {key}"})

    slug = proj_dir.name
    os.chdir(proj_dir)
    log(f"start: {slug} (scheme {cfg['scheme']}, device {cfg['deviceId'][:8]}…)")

    # ---- gate: unit suite AND UI suite must pass in THIS run --------------
    sim_derived = f"/tmp/{slug}-sim-build"
    ok, out = xcodebuild_test(proj_dir, cfg, cfg["unitTestTarget"], "unit", UNIT_TIMEOUT, sim_derived)
    if not ok:
        finish({"status": "tests_failed", "suite": "unit", "tail": out.strip()[-400:]})
    if cfg.get("uiTestTarget"):
        ok, out = xcodebuild_test(proj_dir, cfg, cfg["uiTestTarget"], "UI", UI_TIMEOUT, sim_derived)
        if not ok:
            finish({"status": "tests_failed", "suite": "ui", "tail": out.strip()[-400:]})
    if tests_only:
        finish({"status": "tests_green", "note": "device push not requested (--tests-only)"})

    # ---- build for device -------------------------------------------------
    dev_derived = f"/tmp/{slug}-device-build"
    log("building for device …")
    t0 = time.monotonic()
    rc, out = run(["xcodebuild", "build",
                   "-project", cfg["project"], "-scheme", cfg["scheme"],
                   "-destination", "generic/platform=iOS",
                   "-allowProvisioningUpdates",
                   "-derivedDataPath", dev_derived,
                   f"OTHER_SWIFT_FLAGS={SWIFT_FLAGS}"], DEVICE_BUILD_TIMEOUT)
    if rc != 0 or "** BUILD SUCCEEDED **" not in out:
        finish({"status": "build_failed", "tail": out.strip()[-400:]})
    log(f"device build succeeded ({time.monotonic()-t0:.0f}s)")
    apps = sorted(Path(dev_derived, "Build/Products").glob("*-iphoneos/*.app"))
    if not apps:
        finish({"status": "build_failed", "reason": "no .app in Build/Products/*-iphoneos"})
    app = apps[-1]

    # ---- reachability, rollback cache, install ---------------------------
    if not wait_reachable(cfg["deviceId"]):
        finish({"status": "unreachable_timeout",
                "reason": f"device not reachable within {REACH_CAP//60} min; app built at {app}"})

    cache = BUILD_CACHE / slug
    cache.mkdir(parents=True, exist_ok=True)
    current = cache / "current.app"
    last_good = cache / "last-good.app"
    if current.exists():
        # the previously-installed build becomes the rollback target
        if last_good.exists():
            shutil.rmtree(last_good)
        shutil.move(str(current), str(last_good))

    ok, why = install_app(cfg["deviceId"], app)
    if not ok:
        finish({"status": "install_failed", "reason": why, "app": str(app),
                "rollback": str(last_good) if last_good.exists() else None})

    shutil.copytree(app, current)
    finish({"status": "deployed", "app": app.name, "bundleId": cfg["bundleId"],
            "device": cfg["deviceId"][:8] + "…",
            "rollback": str(last_good) if last_good.exists() else None})


if __name__ == "__main__":
    main()
