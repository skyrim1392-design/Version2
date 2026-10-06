"""Runtime recovery for startup validation failures (Phase 6b).

Import-safe: registering hooks and running validation only happen when
main.py explicitly calls in. This module is the single recovery entry
point; later triggers (retry timer, tray menu action) reuse it without
importing main.py.

Guarantees:
  - F8 handlers are installed exactly once, never duplicated
  - configuration is never mutated by a recovery attempt
  - the periodic retry is single-instance: at most one worker thread,
    started only after a boot failure, stopped on recovery or shutdown
  - retry-path failure logging is deduplicated by failure signature
"""

import threading

import keyboard

import logger
import runtime_validation
import tray

STARTUP_RECOVERY_INTERVAL = 10.0

f8_registered = False
_f8_handlers = None

_recovery_thread = None
_recovery_stop = threading.Event()
_last_retry_failure = None


def configure_f8_handlers(down_handler, up_handler):
    """Store the F8 handlers used for (re-)registration.

    Called once at startup, before validation, so a failed boot can
    still register the hotkey later during recovery.
    """
    global _f8_handlers

    _f8_handlers = (down_handler, up_handler)


def register_f8():
    """Register the F8 hotkey exactly once.

    Returns True when this call installed the hooks, False when they
    were already registered or the handlers were never configured.
    """
    global f8_registered

    if f8_registered:
        logger.info("F8 hotkey already registered; skipping duplicate")
        return False

    if _f8_handlers is None:
        logger.error("Cannot register F8: handlers not configured")
        return False

    down_handler, up_handler = _f8_handlers
    keyboard.on_press_key("f8", down_handler)
    keyboard.on_release_key("f8", up_handler)
    f8_registered = True
    logger.info("F8 hotkey registered")
    return True


def attempt_runtime_recovery(quiet=False):
    """Re-run startup validation while the startup error overlay is active.

    Success clears the overlay and registers F8 (once), restoring normal
    tray behavior, logs the recovery, resets the retry failure signature,
    and stops further periodic retries. Failure leaves state exactly as it
    was so a later retry can succeed. Never mutates configuration.

    quiet=True is the periodic retry path: validation runs without its
    per-attempt log lines and the failure is logged only when the failure
    signature differs from the previous one (first occurrence and changes
    are logged, repeats are suppressed). quiet=False (default) keeps the
    original direct-call logging.

    Returns True when the application is healthy after the attempt.
    """
    global _last_retry_failure

    if not tray.startup_error:
        _last_retry_failure = None
        return True

    if not runtime_validation.run_startup_validation(quiet=quiet):
        failure = str(runtime_validation.last_error)

        if quiet:
            if failure != _last_retry_failure:
                _last_retry_failure = failure
                logger.warning(f"Runtime recovery failed: {failure}")
        else:
            logger.warning("Runtime recovery failed; startup error remains")

        return False

    _last_retry_failure = None
    tray.clear_startup_error()
    register_f8()
    logger.info("Runtime recovery succeeded")
    _recovery_stop.set()
    return True


def start_startup_retry(interval=STARTUP_RECOVERY_INTERVAL):
    """Start the single periodic retry worker.

    Idempotent: a second call while the worker is alive does nothing.
    The first attempt happens one interval after start; the worker exits
    by itself once recovery succeeds (or the stop event is set).
    """
    global _recovery_thread

    thread = _recovery_thread

    if thread is not None and thread.is_alive():
        return False

    _recovery_stop.clear()
    _recovery_thread = threading.Thread(
        target=_retry_loop,
        args=(interval,),
        name="StartupRecovery",
        daemon=True,
    )
    _recovery_thread.start()
    logger.info("Startup recovery retry started")
    return True


def stop_startup_retry(timeout=2.0):
    """Signal the retry worker to exit and wait for it (idempotent)."""
    _recovery_stop.set()

    thread = _recovery_thread

    if (
        thread is not None
        and thread.is_alive()
        and thread is not threading.current_thread()
    ):
        thread.join(timeout=timeout)

    return thread is None or not thread.is_alive()


def _retry_loop(interval):
    while not _recovery_stop.is_set():
        if _recovery_stop.wait(interval):
            break

        try:
            if attempt_runtime_recovery(quiet=True):
                break

        except Exception as error:
            logger.error(
                f"Recovery retry error: {type(error).__name__}: {error}"
            )
