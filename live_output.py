"""Live transcription output integration (Phase 7c.2).

Scope: live_output.py is the only new production file in 7c.2.
Unchanged: live_transcribe.py (its 7c.1 cursor API is consumed, not
edited), text_output.py, main.py, tray.py, app_state.py, audio.py,
live_capture.py, whisper_engine.py, config.py, config.json.

Import-safe: importing this module starts no thread and touches no
transcript, clipboard, or tray. text_output and tray are resolved
lazily inside the two seams (_deliver_text, _emit_warning), so tests
inject fakes without loading the clipboard stack.

Pipeline:

    live_transcribe.create_cursor()
        -> single LiveOutput thread (STOPPED -> RUNNING)
        -> live_transcribe.read_cursor(): ordered, exactly-once,
           session-boundary realign (7c.1)
        -> _deliver_text() seam -> text_output.deliver(): the same
           clipboard transaction F8 uses; text_output.typing_lock
           serializes F8 and live deliveries end to end
        -> _record_result(): length-only result log plus deterministic
           warning suppression
        -> stop(): RUNNING -> DRAINING -> at most DRAIN_MAX_SEGMENTS
           further deliveries, remainder dropped, count-only log
        -> join -> STOPPED

Contract:

1. One worker thread named LiveOutput. start() refuses unless the
   state is STOPPED ("already active") and resets cursor/session
   tracking, last_failure, and the stop event before spawning exactly
   one thread.
2. stop() is idempotent: already STOPPED returns (True, "inactive").
   On success ((True, "stopped")) no worker thread remains, no
   delivery can begin afterwards, and any in-flight deliver() has
   completed - deliveries exist only on the worker thread, which is
   joined. A join timeout returns (False, "output timeout") and keeps
   RUNNING/DRAINING state and the thread, so stop() can be retried.
3. The drain is count-based only: DRAIN_MAX_SEGMENTS deliveries are
   allowed after the stop request, then every remaining pending
   segment is dropped and only the count is logged. No wall-clock
   drain limit exists.
4. Warning suppression is a state machine on result codes only:
   DELIVERED resets last_failure; a failure code equal to
   last_failure suppresses the tray warning (logging continues); any
   other failure code warns and becomes last_failure. No timers, no
   timestamps, no throttling - suppression affects tray warnings only.
5. Order and exactly-once delivery come from one worker consuming the
   7c.1 cursor. There is no second transcript store and no text
   queue.
6. 7c does not promise unbounded output buffering: _transcript in
   live_transcribe is the only store; stats()["pending_segments"]
   reports the cursor gap for diagnostics.

The result constants and OUTPUT_WARNING_MESSAGES mirror text_output
and main.py (a test proves message parity by parsing main.py without
importing it).

Privacy contract: no transcript text enters the logger. Logs carry
result codes, character counts, drop counts, and exception type names
only.
"""

import threading

import live_transcribe as lt
import logger

STATE_STOPPED = "STOPPED"
STATE_RUNNING = "RUNNING"
STATE_DRAINING = "DRAINING"

OUTPUT_JOIN_TIMEOUT = 10.0
WORKER_POLL_TIMEOUT = 0.1
DRAIN_MAX_SEGMENTS = 8
WARNING_DURATION = 3.0

DELIVERED = "DELIVERED"
SKIPPED = "SKIPPED"
NO_TARGET = "NO_TARGET"
CAPTURE_FAILED = "CAPTURE_FAILED"
WRITE_FAILED = "WRITE_FAILED"
HOTKEY_FAILED = "HOTKEY_FAILED"
RESTORE_FAILED = "RESTORE_FAILED"

FAILURE_CODES = frozenset(
    {
        NO_TARGET,
        CAPTURE_FAILED,
        WRITE_FAILED,
        HOTKEY_FAILED,
        RESTORE_FAILED,
    }
)

OUTPUT_WARNING_MESSAGES = {
    NO_TARGET: "No active window for the text",
    CAPTURE_FAILED: "Output failed: clipboard unavailable",
    WRITE_FAILED: "Output failed: clipboard unavailable",
    HOTKEY_FAILED: "Output failed: text kept in clipboard",
    RESTORE_FAILED: "Text pasted; clipboard not restored",
}

_lock = threading.Lock()
_state = STATE_STOPPED
_thread = None
_stop_event = None
_cursor = None
_last_failure = None


def _deliver_text(text):
    """Delivery seam: the existing F8 clipboard transaction.

    Lazy import keeps this module import-safe; tests replace this
    function with an injected fake instead of loading the clipboard
    stack.
    """
    import text_output

    return text_output.deliver(text)


def _emit_warning(message):
    """Warning seam: the only tray call path in this module."""
    import tray

    tray.show_warning(message, duration=WARNING_DURATION)


def _record_result(outcome, characters):
    """Log one delivery result (length-only) and manage warnings.

    Deterministic state machine on the result code alone: DELIVERED
    resets, a repeated failure code suppresses the tray warning while
    logging continues, a different failure code warns. Unknown
    outcomes are logged and ignored. The result line is logged LAST -
    after the suppression decision and any tray warning - so its
    arrival marks the record as complete for observers. Only the
    worker thread writes last_failure (start() resets it while
    STOPPED).
    """
    global _last_failure

    warning_message = None

    if outcome == DELIVERED:
        _last_failure = None

    elif outcome in FAILURE_CODES:
        if outcome == _last_failure:
            logger.info(
                f"Live output warning suppressed: result={outcome}"
            )
        else:
            warning_message = OUTPUT_WARNING_MESSAGES.get(outcome)
            _last_failure = outcome

            if warning_message is not None:
                _emit_warning(warning_message)

    logger.info(
        f"Live output result: result={outcome}, characters={characters}"
    )


def _pending_segments():
    """Cursor gap against the current transcript (diagnostics only)."""
    cursor = _cursor

    if cursor is None:
        return 0

    session, index = cursor
    segments = lt.stats()["transcript_segments"]

    if session != lt.create_cursor()[0]:
        return max(segments, 0)

    return max(segments - index, 0)


def _worker_loop(stop_event):
    """Consume the cursor into delivery until stopped.

    Exit paths (the worker never joins itself):
    - stop requested and no unread segment remains: exit (the drain
      finished or there was nothing to drain)
    - stop requested with pending segments: DRAINING - deliver up to
      DRAIN_MAX_SEGMENTS, then drop the remainder with a count-only
      log and exit
    - unexpected failure: the exception TYPE only is logged and the
      thread exits; stop() still performs the join
    """
    global _state, _cursor

    drain_count = 0

    try:
        while True:
            if stop_event.is_set():
                with _lock:
                    _state = STATE_DRAINING

            result = lt.read_cursor(_cursor)

            if result is None:
                if stop_event.is_set():
                    break
                stop_event.wait(WORKER_POLL_TIMEOUT)
                continue

            _cursor, text = result

            draining = stop_event.is_set()

            if draining:
                with _lock:
                    _state = STATE_DRAINING

                if drain_count >= DRAIN_MAX_SEGMENTS:
                    dropped = 1
                    while True:
                        more = lt.read_cursor(_cursor)
                        if more is None:
                            break
                        _cursor, _ = more
                        dropped += 1
                    logger.warning(
                        f"Live output dropped: dropped={dropped}"
                    )
                    break

                drain_count += 1

            outcome = _deliver_text(text)
            _record_result(outcome, len(text))

    except Exception as error:  # noqa: BLE001 - log type, thread exits
        logger.error(
            f"Live output worker failed: {type(error).__name__}"
        )


def start():
    """Start the delivery worker. Returns (ok, reason).

    Refuses unless STOPPED. Resets cursor/session tracking to the
    current transcript head, clears the warning-suppression state and
    the stop event, and spawns exactly one LiveOutput thread. Atomic
    under _lock.
    """
    global _state, _thread, _stop_event, _cursor, _last_failure

    with _lock:
        if _state != STATE_STOPPED:
            return False, "already active"

        _cursor = lt.create_cursor()
        _last_failure = None
        _stop_event = threading.Event()
        thread = threading.Thread(
            target=_worker_loop,
            args=(_stop_event,),
            name="LiveOutput",
            daemon=True,
        )
        _thread = thread
        _state = STATE_RUNNING
        thread.start()

    logger.info("Live output started")
    return True, "started"


def stop():
    """Stop the delivery worker (idempotent). Returns (ok, reason).

    Sets the stop event, then joins outside _lock (the worker only
    takes _lock for the DRAINING state transition - never during
    deliver() - and this function releases _lock before joining). On
    success the state
    is STOPPED, no LiveOutput thread remains, the in-flight delivery
    has completed, and nothing can begin afterwards. A timeout keeps
    the RUNNING/DRAINING state and the thread so stop() is retriable.
    """
    global _state, _thread, _stop_event

    with _lock:
        if _state == STATE_STOPPED:
            return True, "inactive"

        if _stop_event is not None:
            _stop_event.set()

        thread = _thread

    if thread is not None:
        thread.join(timeout=OUTPUT_JOIN_TIMEOUT)

        if thread.is_alive():
            logger.error("Live output did not stop in time")
            return False, "output timeout"

    with _lock:
        _thread = None
        _state = STATE_STOPPED
        _stop_event = None

    logger.info("Live output stopped")
    return True, "stopped"


def stats():
    return {
        "state": _state,
        "worker_alive": _thread is not None and _thread.is_alive(),
        "pending_segments": _pending_segments(),
    }
