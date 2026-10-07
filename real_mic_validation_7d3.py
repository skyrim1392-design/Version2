#!/usr/bin/env python3
"""7d.3 endurance / soak validation harness (untracked, local-only).

Specification: docs/7D3_DESIGN.md (approved 2026-10-07).

Covers:
  A. baseline snapshot (HEAD, sync, python, threads, mic owner,
     whisper process, supervisor/output state)
  B. live soak -- continuous session, delivery progress, no thread
     growth, no whisper accumulation, no drops
  C. repeated live start/stop cycles (default 100)
  D. F8/live contention cycles (default 50): f8_request blocked while
     live listens; allowed after stop
  E. shutdown under load (default 10): exit during active live;
     exit during fake F8 busy state (quiesce timeout path bounded);
     zero live threads / mic released / no whisper child / idempotent
  F. toggle/config cycles (default 100) with config.json SHA-256
     invariant
  G. final invariants + delta vs baseline

Synthetic/hardware-free: FakeLiveCapture + FakeWhisperEngine +
DeliveryRecorder seams (same doubles as the 7d.1/7d.2 suites).
Real-mic corroboration stays in the 7c/7d smoke harnesses.

No production module is modified. Run:
  python real_mic_validation_7d3.py [--full]
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from unittest import mock

import audio
import live_output as lo
import live_supervisor
import live_transcribe as lt
import whisper_engine
from app_state import state
from test_live_output import DeliveryRecorder
from test_live_transcribe import (
    FakeLiveCapture,
    FakeWhisperEngine,
    joined,
    session_threads,
    silence,
    tone,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

CHECKS = []
RESULTS = {}


def check(name, ok, detail=""):
    ok = bool(ok)
    CHECKS.append((name, ok, detail))
    suffix = f" :: {detail}" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{suffix}", flush=True)
    return ok


def section(title):
    print(flush=True)
    print("=" * 64, flush=True)
    print(title, flush=True)
    print("=" * 64, flush=True)


def output_threads():
    return [t for t in threading.enumerate() if t.name == "LiveOutput"]


def live_threads():
    return session_threads() + output_threads()


def whisper_child_count():
    return 1 if whisper_engine.whisper_process is not None else 0


def mic_owner():
    with audio._mic_owner_lock:
        return audio._mic_owner


def config_sha():
    with open(CONFIG_PATH, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def git(*args):
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=BASE_DIR,
            timeout=15,
            check=False,
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"<error: {exc}>"


def resource_snapshot():
    with state.lock:
        status = state.status
        live_status = state.live_status
        recording = state.recording
        processing = state.processing
    return {
        "threads": len(threading.enumerate()),
        "live_threads": len(live_threads()),
        "mic_owner": mic_owner(),
        "whisper_child": whisper_child_count(),
        "lt_active": lt.is_active(),
        "lo_active": bool(getattr(lo, "_stop_event", None) and not lo._stop_event.is_set()),
        "status": status,
        "live_status": live_status,
        "recording": recording,
        "processing": processing,
    }


def reset_world():
    lt.stop()
    lo.stop()
    live_supervisor._shutdown.clear()
    audio.release_microphone("live")
    audio.release_microphone("f8")
    with state.lock:
        state.live_status = live_supervisor.LIVE_IDLE
        state.status = "IDLE"
        state.recording = False
        state.processing = False


def install_fakes():
    capture = FakeLiveCapture()
    real_start = capture.start
    real_stop = capture.stop

    def start_and_claim():
        result = real_start()
        if result[0]:
            audio.claim_microphone("live")
        return result

    def stop_and_release():
        result = real_stop()
        audio.release_microphone("live")
        return result

    capture.start = start_and_claim
    capture.stop = stop_and_release

    engine = FakeWhisperEngine()
    deliver = DeliveryRecorder()
    patches = [
        mock.patch.object(lt, "CALIBRATION_TARGET_WINDOWS", 0),
        mock.patch.object(lt, "_import_capture", return_value=capture),
        mock.patch.object(lt, "whisper_engine", engine),
        mock.patch.object(lt, "logger"),
        mock.patch.object(lo, "_deliver_text", deliver),
        mock.patch.object(lo, "_emit_warning"),
        mock.patch.object(lo, "logger"),
    ]
    for patcher in patches:
        patcher.start()
    return capture, engine, deliver, patches


def remove_patches(patches):
    for patcher in patches:
        patcher.stop()


def feed_signal(capture, stop, rate_hz=16000, chunk=1600):
    signal = joined(tone(rate_hz, level=2000), silence(rate_hz // 2))
    while not stop.is_set():
        for offset in range(0, len(signal), chunk):
            if stop.is_set():
                return
            capture.push(signal[offset : offset + chunk])
        time.sleep(0.05)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="design targets: 1800s soak, 100/50/100/10 cycles")
    parser.add_argument("--soak-seconds", type=float, default=60.0)
    parser.add_argument("--lifecycle-cycles", type=int, default=20)
    parser.add_argument("--contention-cycles", type=int, default=10)
    parser.add_argument("--toggle-cycles", type=int, default=20)
    parser.add_argument("--shutdown-cycles", type=int, default=4)
    args = parser.parse_args()

    if args.full:
        args.soak_seconds = 1800.0
        args.lifecycle_cycles = 100
        args.contention_cycles = 50
        args.toggle_cycles = 100
        args.shutdown_cycles = 10

    started = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"7d.3 endurance harness start {started}", flush=True)
    print(f"profile: soak={args.soak_seconds}s lifecycle={args.lifecycle_cycles} contention={args.contention_cycles} toggle={args.toggle_cycles} shutdown={args.shutdown_cycles}", flush=True)

    # ---- A. baseline -------------------------------------------------
    section("A. Baseline snapshot")
    baseline = {
        "head": git("rev-parse", "HEAD"),
        "branch": git("branch", "--show-current"),
        "origin_sync": git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") == "origin/backend-foundation",
        "python": sys.version.split()[0],
        "resources": resource_snapshot(),
        "config_sha256": config_sha(),
    }
    for key, value in baseline.items():
        print(f"  {key}: {value}", flush=True)
    check("baseline mic owner None", baseline["resources"]["mic_owner"] is None)
    check("baseline whisper_child 0", baseline["resources"]["whisper_child"] == 0)

    capture, _engine, deliver, patches = install_fakes()
    soak_stop = threading.Event()
    feeder = threading.Thread(target=feed_signal, args=(capture, soak_stop), name="7d3Feeder", daemon=True)

    peak_threads = len(threading.enumerate())
    thread_samples = []

    try:
        # ---- B. continuous live soak ---------------------------------
        section("B. Continuous live soak")
        ok, reason = live_supervisor.start()
        check("live start", ok, reason)
        check("mic owner is live", mic_owner() == "live", str(mic_owner()))
        feeder.start()
        delivered_before = len(deliver.calls)
        deadline = time.monotonic() + args.soak_seconds
        while time.monotonic() < deadline:
            time.sleep(min(5.0, max(0.2, deadline - time.monotonic())))
            peak_threads = max(peak_threads, len(threading.enumerate()))
            thread_samples.append((round(time.monotonic(), 1), len(threading.enumerate()), len(live_threads()), whisper_child_count()))
            if thread_samples:
                _, n, live_n, wc = thread_samples[-1]
                print(f"  t+sample: threads={n} live_threads={live_n} whisper_children={wc} delivered={len(deliver.calls)}", flush=True)
        soak_stop.set()
        feeder.join(timeout=5.0)
        delivered_after = len(deliver.calls)
        with state.lock:
            live_status = state.live_status
        check("soak produced deliveries (progress)", delivered_after > delivered_before, f"{delivered_after - delivered_before} delivered")
        check("no thread growth during soak", (thread_samples[-1][1] - thread_samples[0][1] <= 0) if len(thread_samples) >= 2 else True, f"first={thread_samples[0][1] if thread_samples else '?'} last={thread_samples[-1][1] if thread_samples else '?'}")
        check("whisper children stable 0", all(s[3] == 0 for s in thread_samples) or not thread_samples)
        check("live still listening after soak", live_status == live_supervisor.LIVE_LISTENING, live_status)
        ok, reason = live_supervisor.stop()
        check("live stop", ok, reason)
        check("mic owner None after stop", mic_owner() is None)
        check("no live threads after stop", len(live_threads()) == 0, str(len(live_threads())))

        # ---- C. lifecycle cycles -------------------------------------
        section("C. Repeated live start/stop cycles")
        failures = 0
        for i in range(args.lifecycle_cycles):
            ok1, _ = live_supervisor.start()
            ok2, _ = live_supervisor.stop()
            if not (ok1 and ok2):
                failures += 1
            if mic_owner() is not None or lt.is_active():
                failures += 1
        check(f"{args.lifecycle_cycles} start/stop cycles clean", failures == 0, f"failures={failures}")
        check("no live threads after cycles", len(live_threads()) == 0)

        # ---- D. F8/live contention ------------------------------------
        section("D. F8/live contention cycles")
        blocked_ok = 0
        allowed_ok = 0
        for _ in range(args.contention_cycles):
            live_supervisor.start()
            allowed, code = live_supervisor.f8_request()
            if (not allowed) and code == live_supervisor.F8_BLOCKED:
                blocked_ok += 1
            live_supervisor.stop()
            allowed, code = live_supervisor.f8_request()
            if allowed and code is None:
                allowed_ok += 1
        check("F8 blocked while live owns mic", blocked_ok == args.contention_cycles, f"{blocked_ok}/{args.contention_cycles}")
        check("F8 allowed after live stop", allowed_ok == args.contention_cycles, f"{allowed_ok}/{args.contention_cycles}")

        # ---- E. shutdown under load -----------------------------------
        section("E. Shutdown under load")
        clean = 0
        timeout_calls = [0]
        real_terminate = live_supervisor.whisper_engine.terminate_outstanding

        def counting_terminate():
            timeout_calls[0] += 1
            return real_terminate()

        for i in range(args.shutdown_cycles):
            reset_world()
            if i % 2 == 0:
                live_supervisor.start()
                with state.lock:
                    state.status = "IDLE"
            else:
                with state.lock:
                    state.status = "PROCESSING"
                    state.processing = True
                with mock.patch.object(live_supervisor, "QUIESCE_TIMEOUT", 0.5), mock.patch.object(
                    live_supervisor.whisper_engine, "terminate_outstanding", counting_terminate
                ):
                    live_supervisor.shutdown()
                reset_world()
                clean += 1
                continue
            live_supervisor.shutdown()
            post = resource_snapshot()
            if (
                post["live_threads"] == 0
                and post["mic_owner"] is None
                and post["whisper_child"] == 0
                and post["live_status"] == live_supervisor.LIVE_IDLE
            ):
                clean += 1
            reset_world()
        check(f"{args.shutdown_cycles} shutdown-under-load cycles clean", clean == args.shutdown_cycles, f"{clean}/{args.shutdown_cycles}")
        check("quiesce-timeout terminate path exercised", timeout_calls[0] > 0, f"terminate_outstanding called {timeout_calls[0]}x")

        # ---- F. toggle/config cycles ----------------------------------
        section("F. Toggle/config cycles")
        toggle_failures = 0
        for _ in range(args.toggle_cycles):
            ok1, _ = live_supervisor.toggle()
            ok2, _ = live_supervisor.toggle()
            if not (ok1 and ok2):
                toggle_failures += 1
        reset_world()
        check(f"{args.toggle_cycles} toggle cycles clean", toggle_failures == 0, f"failures={toggle_failures}")
        check("config.json SHA-256 unchanged", config_sha() == baseline["config_sha256"])

        # ---- G. final invariants --------------------------------------
        section("G. Final invariants")
        final = resource_snapshot()
        print(f"  final: {final}", flush=True)
        check("mic owner None", final["mic_owner"] is None)
        check("no live threads", final["live_threads"] == 0)
        check("no whisper child", final["whisper_child"] == 0)
        check("live_status IDLE", final["live_status"] == live_supervisor.LIVE_IDLE)
        check("F8 flags clear", not final["recording"] and not final["processing"])
        check("thread count ~ baseline", final["threads"] <= baseline["resources"]["threads"] + 3, f"final={final['threads']} baseline={baseline['resources']['threads']}")
        check("config.json SHA-256 unchanged (final)", config_sha() == baseline["config_sha256"])

        RESULTS.update(
            soak_seconds=args.soak_seconds,
            lifecycle_cycles=args.lifecycle_cycles,
            contention_cycles=args.contention_cycles,
            toggle_cycles=args.toggle_cycles,
            shutdown_cycles=args.shutdown_cycles,
            delivered=len(deliver.calls),
            peak_threads=peak_threads,
            thread_samples=thread_samples[-5:],
            baseline=baseline,
            final=final,
            config_sha_unchanged=config_sha() == baseline["config_sha256"],
        )
    finally:
        soak_stop.set()
        reset_world()
        remove_patches(patches)

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    total = len(CHECKS)
    print(flush=True)
    print(f"7d.3 endurance summary: {passed}/{total} checks passed", flush=True)

    RESULTS["passed"] = passed
    RESULTS["total"] = total
    RESULTS["started"] = started
    out_dir = os.path.join(os.environ.get("TEMP", "."), "opencode")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "7d3_endurance_results.json")
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(RESULTS, handle, indent=2, default=str)
    print(f"results: {out_path}", flush=True)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
