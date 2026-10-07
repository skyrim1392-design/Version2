# Architecture

> Authority: `docs/HANDOVER.md` is the single source of truth. This file is supporting detail; if they disagree, HANDOVER.md wins.

Stable architecture decisions and module ownership.
Last updated: 2026-10-07 (as of 7d.2, uncommitted)

---

## Pipelines

```text
F8 (push-to-talk)
  microphone (audio.py)
   -> 16 kHz mono WAV
   -> whisper_engine (serialized whisper.cpp call)
   -> text_output (clipboard + paste)
   -> F8 unlock

Live mode
  microphone (live_capture.py, energy gate)
   -> live_transcribe (lt): streaming recognition
   -> live_output (lo): cursor-based delivery
   -> clipboard
```

## Ownership map

| Module | Responsibility | Must not do |
|---|---|---|
| `main.py` | F8 lifecycle, startup validation, main loop, cleanup | own live session state |
| `live_supervisor.py` | live start/stop/toggle, F8 gate, shutdown ordering, `live_status`, failure cleanup | capture audio, run whisper, render tray |
| `live_transcribe.py` | capture + streaming whisper for live | touch `app_state` |
| `live_output.py` | transcript delivery / cursor movement | touch `app_state` |
| `whisper_engine.py` | serialized whisper invocation for both callers, shutdown-path child termination | decide ownership |
| `tray.py` | menu, icon, `display_status()` rendering, Exit | make ownership decisions |
| `app_state.py` | shared state + lock | business logic |
| `config.py` | defaults and persistence | tuning constants for calibration/delivery |
| `audio.py` | F8 recording path | (frozen) |
| `live_capture.py` | microphone streaming + energy gate | (frozen) |

## State model

```text
state.status        F8 foreground:  IDLE | RECORDING | PROCESSING | OUTPUTTING | ERROR ...
state.live_status   live session:   LIVE_IDLE | LIVE_LISTENING | LIVE_ERROR
```

`live_status` was added in 7d.1. The two are independent; the tray
renders a single status through `tray.display_status(status,
live_status)` using the precedence in `DECISIONS.md` D6 (matrix plus
overlay chain, 7d.2 addendum).

## Live supervisor internals

```text
start()
  lock: guard _starting / already-active
  lt.start()        -> acquires "live" mic ownership
  lock: re-check shutdown -> lo.start()
  on any refusal    -> LIVE_ERROR (nothing left behind)

stop()
  lock: lt.stop(); lo.stop(); -> LIVE_IDLE

reconcile()   [main loop, every 1 s]
  LISTENING but lt dead -> lt.stop(); lo.stop(); -> LIVE_ERROR

shutdown()    [tray Exit / cleanup / KeyboardInterrupt]
  set _shutdown FIRST (idempotent; later callers wait, then return)
  quiesce F8 up to 5 s (status in RECORDING/PROCESSING/OUTPUTTING
                        or state.recording / state.processing)
  on quiesce timeout: whisper_engine.terminate_outstanding()
                      (kill + reap the outstanding F8 child)
  lt.stop(); lo.stop(); -> LIVE_IDLE
```

`f8_request()` returns `(allowed, code)` with
`code in {None, F8_BLOCKED, SHUTTING_DOWN}`.

## Tray integration

- `Live Transcription` menu item toggles via
  `live_supervisor.toggle()`; `checked=` reads `live_status`.
- `Exit` sets `stopping = True`, calls `live_supervisor.shutdown()`
  inside `try`/`finally` so `icon.stop()` is attempted even when
  teardown raises; the polling loop exits, the main loop's
  `shutdown_event.wait(1.0)` returns, `main` falls through and the
  process ends.
- Polling interval stays 0.5 s; no event infrastructure added.
- Icons: `LIVE_LISTENING -> recording.png`, `LIVE_ERROR -> error.png`.

## Threading / locking

```text
state.lock           app_state fields
live_supervisor._lock  session transitions and teardown
```

Rule: the shutdown flag is set before acquiring `_lock`, so late
callers fail fast instead of deadlocking behind teardown. Whisper calls
are serialized in `whisper_engine` (F8 and live never run concurrently).

## Tests

```text
python -m unittest discover -v      # 152
python -m unittest test_live_supervisor -v
python -m unittest test_tray -v
python -m unittest test_config_persistence -v
python -m unittest test_integration_7d -v
```
