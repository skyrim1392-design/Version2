"""Runtime recovery for startup validation failures (Phase 6b).

Import-safe: registering hooks and running validation only happen when
main.py explicitly calls in. This module is the single recovery entry
point; later triggers (retry timer, tray menu action) reuse it without
importing main.py.

Guarantees:
  - F8 handlers are installed exactly once, never duplicated
  - configuration is never mutated by a recovery attempt
"""

import keyboard

import logger
import runtime_validation
import tray

f8_registered = False
_f8_handlers = None


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


def attempt_runtime_recovery():
    """Re-run startup validation while the startup error overlay is active.

    Success clears the overlay and registers F8 (once), restoring normal
    tray behavior. Failure leaves state exactly as it was so a later
    retry can succeed. Never mutates configuration.

    Returns True when the application is healthy after the attempt.
    """
    if not tray.startup_error:
        return True

    if not runtime_validation.run_startup_validation():
        logger.warning("Runtime recovery failed; startup error remains")
        return False

    tray.clear_startup_error()
    register_f8()
    return True
