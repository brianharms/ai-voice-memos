#!/usr/bin/env python3
"""device_deploy — gated build/test/device-push for VMA daemon skill runs.

Usage (from inside a sandboxed skill run):
    /usr/bin/python3 ~/.voice-memo-ai/engine/device_deploy.py <project-dir> [--tests-only]

Contract:
  - The project must contain .vma-device-deploy.json:
      { "deviceIds": ["...", "..."], "scheme": "...", "project": "....xcodeproj",
        "unitTestTarget": "...", "uiTestTarget": "...", "bundleId": "...",
        "configuration": "Release" }
    "configuration" is optional and defaults to Debug. Set it to Release for
    anything numerically heavy: Swift's Debug builds skip optimisation and keep
    bounds checking, which measured 2x the CPU on a particle simulation.
    "deviceId" (singular string) is still accepted for older configs.
    "optionalDeviceIds": devices installed only if reachable within
    OPTIONAL_GRACE s (never waited for; never fail the deploy).
    Every named device gets the SAME build: one gate, one compile, N installs.
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
  - HEARTBEAT: the watcher kills a skill run after VMA_SKILL_STALL (default
    1800 s) of SILENT stdout. Waiting for a device is silent by nature and
    REACH_CAP is the same 1800 s, so a device that is simply switched off
    would have taken the whole run down with it — after a green gate and a
    good build. Every wait therefore prints a heartbeat well inside that
    window. Keep REACH_HEARTBEAT comfortably below the stall threshold.
  - Install retries + reachability watcher: the phone drops off Wi-Fi
    constantly. "unavailable" CONTAINS "available", so state checks must
    exclude it explicitly, never bare-match. The reachability budget is
    SHARED across devices, not per-device: two devices each waiting the full
    cap would outlive the watcher's 30-minute STALL kill and lose the run.
  - With several devices, a device that never shows up does not cost the
    others their install — the result reports per-device outcomes and
    "partially_deployed" when some landed.
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
# Must stay well under the watcher's stall kill (VMA_SKILL_STALL, default 1800 s)
# — a silent wait is indistinguishable from a wedged process.
REACH_HEARTBEAT = int(os.environ.get("VMA_REACH_HEARTBEAT", "120"))
# how long an OPTIONAL device ("optionalDeviceIds") gets to show up before it is skipped
OPTIONAL_GRACE = int(os.environ.get("VMA_OPTIONAL_GRACE", "45"))


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
    sys.exit(0 if result["status"] in ("deployed", "partially_deployed", "tests_green") else 1)


# Simulator failures that are infrastructure, not code. Retried ONCE — never
# to paper over a red suite, only because a cold-booting simulator loses the
# race with its own test host and the daemon has no human to re-run it.
SIM_FLAKES = ("Application failed preflight checks",
              "Unable to boot the Simulator",
              "Simulator device failed to launch",
              "failed to background test runner")


def boot_sim() -> None:
    """Boot + settle the destination simulator before testing.

    xcodebuild will boot it on demand, but an on-demand cold boot is exactly
    when the host app hits "failed preflight checks". bootstatus blocks until
    the device is actually usable.
    """
    name = ""
    for part in SIM_DEST.split(","):
        if part.strip().startswith("name="):
            name = part.split("=", 1)[1].strip()
    if not name:
        return
    run(["xcrun", "simctl", "boot", name], 120)          # noop if already booted
    rc, _ = run(["xcrun", "simctl", "bootstatus", name, "-b"], 300)
    log(f"simulator '{name}' ready" if rc == 0 else f"simulator '{name}' boot status rc={rc}")


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
    if not ok and any(f in out for f in SIM_FLAKES):
        log(f"{label} tests hit a simulator flake — booting and retrying once")
        boot_sim()
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


def wait_reachable(device_id: str, deadline=None) -> bool:
    """Wait for a device, bounded by a budget SHARED across all devices."""
    t0 = time.monotonic()
    logged = False
    last_beat = time.monotonic()
    while time.monotonic() - t0 < REACH_CAP and (deadline is None or time.monotonic() < deadline):
        if device_reachable(device_id):
            if logged:
                log("device is reachable again")
            return True
        if not logged:
            log(f"device {device_id[:8]}… unreachable — waiting (cap {REACH_CAP//60} min)")
            logged = True
        elif time.monotonic() - last_beat >= REACH_HEARTBEAT:
            # Proof of life for the watcher's stall detector, not chatter.
            log(f"still waiting for {device_id[:8]}… "
                f"({(time.monotonic()-t0)/60:.0f} min)")
            last_beat = time.monotonic()
        time.sleep(REACH_POLL)
    return False


def install_app(device_id: str, app: Path, deadline=None) -> tuple:
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
            if not wait_reachable(device_id, deadline):
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
    for key in ("scheme", "project", "unitTestTarget", "bundleId"):
        if not cfg.get(key):
            finish({"status": "error", "reason": f"{CONFIG_NAME} missing required key: {key}"})
    # deviceIds (list) is the current shape; deviceId (string) still works.
    raw = cfg.get("deviceIds") or cfg.get("deviceId")
    device_ids = [raw] if isinstance(raw, str) else list(raw or [])
    device_ids = [d for d in device_ids if isinstance(d, str) and d.strip()]
    if not device_ids:
        finish({"status": "error",
                "reason": f"{CONFIG_NAME} needs deviceIds (list) or deviceId (string)"})

    slug = proj_dir.name
    os.chdir(proj_dir)
    log(f"start: {slug} (scheme {cfg['scheme']}, {len(device_ids)} device(s): "
        + ", ".join(d[:8] + "…" for d in device_ids) + ")")

    # ---- gate: unit suite AND UI suite must pass in THIS run --------------
    boot_sim()
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
    conf = cfg.get("configuration") or "Debug"
    log(f"configuration: {conf}")
    rc, out = run(["xcodebuild", "build",
                   "-project", cfg["project"], "-scheme", cfg["scheme"],
                   "-configuration", conf,
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

    # ---- rollback cache ---------------------------------------------------
    cache = BUILD_CACHE / slug
    cache.mkdir(parents=True, exist_ok=True)
    current = cache / "current.app"
    last_good = cache / "last-good.app"
    if current.exists():
        # the previously-installed build becomes the rollback target
        if last_good.exists():
            shutil.rmtree(last_good)
        shutil.move(str(current), str(last_good))

    # ---- install on every configured device -------------------------------
    # ONE budget for the whole run: two devices each allowed the full cap
    # would outlive the daemon's 60-minute kill and lose the result entirely.
    deadline = time.monotonic() + REACH_CAP
    results = {}
    for dev in device_ids:
        short = dev[:8] + "…"
        if not wait_reachable(dev, deadline):
            log(f"{short}: never became reachable within the shared budget")
            results[dev] = "unreachable"
            continue
        ok, why = install_app(dev, app, deadline)
        results[dev] = "installed" if ok else why
        log(f"{short}: {results[dev]}")

    # Optional devices ("optionalDeviceIds"): installed only if they're reachable
    # right now (a short look, OPTIONAL_GRACE s), never waited for, and never
    # able to turn a good deploy into a failure. "also target
    # my iPad if it is available".
    optional = cfg.get("optionalDeviceIds") or []
    optional = [d for d in optional if isinstance(d, str) and d.strip() and d not in device_ids]
    opt_results = {}
    for dev in optional:
        short = dev[:8] + "…"
        if not wait_reachable(dev, time.monotonic() + OPTIONAL_GRACE):
            log(f"{short} (optional): not reachable now — skipped")
            opt_results[dev] = "skipped (not reachable)"
            continue
        ok, why = install_app(dev, app, time.monotonic() + OPTIONAL_GRACE)
        opt_results[dev] = "installed" if ok else why
        log(f"{short} (optional): {opt_results[dev]}")

    landed = [d for d, r in results.items() if r == "installed"]
    if landed or "installed" in opt_results.values():
        # Cache only when something actually shipped, so the rollback slot
        # keeps pointing at the last build a device really ran.
        shutil.copytree(app, current)
    detail = {d[:8] + "…": r for d, r in results.items()}
    detail.update({d[:8] + "… (optional)": r for d, r in opt_results.items()})
    if len(landed) == len(device_ids):
        status = "deployed"
    elif landed:
        status = "partially_deployed"
    elif all(r == "unreachable" for r in results.values()):
        status = "unreachable_timeout"
    else:
        status = "install_failed"
    finish({"status": status, "app": app.name, "bundleId": cfg["bundleId"],
            "devices": detail,
            "installed": len(landed), "of": len(device_ids),
            "rollback": str(last_good) if last_good.exists() else None})


if __name__ == "__main__":
    main()
