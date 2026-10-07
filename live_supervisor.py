"""Live transcription supervisor: the application lifecycle boundary
for live mode (7d.1).

Responsibilities:
    - own the live session lifecycle (start/stop/toggle) by
      coordinating live_transcribe (capture + whisper) and
      live_output (delivery);
    - own the F8/live mode decision via f8_request(): exactly one
      microphone owner at a time (Decision 1, mutually exclusive);
    - publish the stable user-facing live state to
      app_state.state.live_status (LIVE_IDLE / LIVE_LISTENING /
      LIVE_ERROR) - never internal lt/lo stats (Decision 3);
    - handle failure cleanup: a LISTENING session whose
      live_transcribe died is stopped cleanly, the microphone is
      released and the state becomes LIVE_ERROR; the user restarts
      live manually (Decision 5, wait-for-user, no auto-restart);
    - enforce the shutdown invariant: shutdown() sets the shutdown
      flag FIRST, so no new work (F8 press, start, stop, tray toggle)
      can enter while teardown runs.

Not responsible for: capturing audio, running whisper, delivering
text, or rendering the tray - those remain in their subsystems.

Run: python -m unittest test_live_supervisor -v
"""

import threading
import time

import live_output as lo
import live_transcribe as lt
import logger
from app_state import state

LIVE_IDLE = "LIVE_IDLE"
LIVE_LISTENING = "LIVE_LISTENING"
LIVE_ERROR = "LIVE_ERROR"

F8_BLOCKED = "live_active"
SHUTTING_DOWN = "shutting_down"

F8_BLOCKED_MESSAGE = "Microphone is currently used by Live Transcription"

QUIESCE_TIMEOUT = 5.0

_lock = threading.Lock()
_shutdown = threading.Event()
_starting = False


def shutdown_event():
    return _shutdown


def is_shutting_down():
    return _shutdown.is_set()


def _set_live_status(value):
    with state.lock:
        changed = state.live_status != value
        state.live_status = value

    if changed:
        logger.info(f"Live status: {value}")


def f8_request():
    """Gate for main.f8_down: (allowed, code).

    code is SHUTTING_DOWN, F8_BLOCKED, or None when allowed.
    """
    if _shutdown.is_set():
        return False, SHUTTING_DOWN

    if state.live_status == LIVE_LISTENING:
        return False, F8_BLOCKED

    return True, None


def start():
    """Start the live session. Returns (ok, reason).

    lt.start() acquires the "live" microphone ownership; on refusal it
    guarantees no session state and no ownership are left behind, so
    the supervisor only reports LIVE_ERROR. lo.start() runs under the
    supervisor lock with a shutdown re-check so nothing can enter
    after shutdown begins.
    """
    global _starting

    if _shutdown.is_set():
        return False, SHUTTING_DOWN

    with _lock:
        if _starting:
            return False, "start in progress"

        if state.live_status == LIVE_LISTENING or lt.is_active():
            return False, "already active"

        _starting = True

    try:
        ok, reason = lt.start()

        if not ok:
            _set_live_status(LIVE_ERROR)
            logger.warning(f"Live transcription start refused: {reason}")
            return False, reason

        with _lock:
            if _shutdown.is_set():
                lt.stop()
                return False, SHUTTING_DOWN

            ok, reason = lo.start()

            if not ok:
                lt.stop()
                _set_live_status(LIVE_ERROR)
                logger.warning(f"Live output start refused: {reason}")
                return False, reason

            _set_live_status(LIVE_LISTENING)

        logger.info("Live transcription started")
        return True, "started"

    finally:
        with _lock:
            _starting = False


def stop():
    """Stop the live session and release ownership. Returns (ok, reason)."""
    if _shutdown.is_set():
        return False, SHUTTING_DOWN

    with _lock:
        if _starting:
            return False, "start in progress"

        lt.stop()
        lo.stop()
        _set_live_status(LIVE_IDLE)

    logger.info("Live transcription stopped")
    return True, "stopped"


def toggle():
    """Tray entry point: stop when listening, otherwise start."""
    if _shutdown.is_set():
        return False, SHUTTING_DOWN

    if state.live_status == LIVE_LISTENING or lt.is_active():
        return stop()

    return start()


def reconcile():
    """Failure cleanup tick: detect a LISTENING session whose
    live_transcribe died (e.g. consecutive-failure auto-stop), stop
    the leftovers cleanly and report LIVE_ERROR. Called from the main
    loop; never restarts anything."""
    if _shutdown.is_set():
        return

    with _lock:
        if _starting:
            return

        if state.live_status != LIVE_LISTENING:
            return

        if lt.is_active():
            return

        logger.warning(
            "Live transcription session lost; releasing microphone"
        )
        lt.stop()
        lo.stop()
        _set_live_status(LIVE_ERROR)


def shutdown():
    """Application teardown entry point. Idempotent.

    The shutdown flag is set FIRST, before any lock or teardown work,
    so every request that arrives from this moment on is rejected
    immediately (never blocked). The first caller then quiesces F8
    processing (bounded) and stops lt/lo in order.
    """
    first = not _shutdown.is_set()
    _shutdown.set()

    with _lock:
        if not first:
            return

        deadline = time.monotonic() + QUIESCE_TIMEOUT

        while time.monotonic() < deadline:
            with state.lock:
                busy = state.recording or state.processing

            if not busy:
                break

            time.sleep(0.05)
        else:
            logger.warning(
                "Shutdown quiesce timed out waiting for F8 processing"
            )

        lt.stop()
        lo.stop()
        _set_live_status(LIVE_IDLE)

    logger.info("Live supervisor shutdown complete")
