# Frozen decisions

> Authority: `docs/HANDOVER.md` is the single source of truth. This file is supporting detail; if they disagree, HANDOVER.md wins.

Decisions below are frozen. Revisit only with explicit approval, in a
new phase, with a written rationale.

Last updated: 2026-10-07

---

## D1 — F8 and Live are mutually exclusive (7d.0, Decision 1)

Exactly one microphone owner at a time.

- Enforcement point: `live_supervisor.f8_request()` called at the top of
  `main.f8_down()`.
- Live start refuses while F8 owns the mic (refused by
  `live_transcribe` ownership acquisition).
- Refusal message: "Microphone is currently used by Live Transcription".
- Implemented as a **supervisory gate only** — the frozen capture and
  ownership machinery in `audio.py` / `live_capture.py` is unchanged.

## D2 — `main.py` remains the F8 execution owner

The supervisor gates F8, it does not own F8. Recording, whisper
invocation, clipboard delivery and F8 unlock stay in `main.py`.

## D3 — Only stable live state is published (7d.0, Decision 3)

`app_state.state.live_status` carries exactly:

`LIVE_IDLE | LIVE_LISTENING | LIVE_ERROR`

Internal `lt`/`lo` counters, stats and transient values never reach
`app_state` or the tray. `lt`/`lo` remain independent of `app_state`
(7c separation preserved).

## D4 — Shutdown flag is set before teardown (7d.1)

`live_supervisor.shutdown()` sets `_shutdown` **first**, then quiesces F8
(bounded by `QUIESCE_TIMEOUT = 5.0 s`) and stops `lt` then `lo`.
Every request arriving after the flag is set is rejected immediately,
never blocked behind the teardown lock. Validated by the race test.

**First-caller ownership (documented in 7d.2, behaviour unchanged):**
only the caller that observed the flag *unset* performs the teardown.
A second caller — for example `atexit` cleanup after tray Exit already
shut down — blocks on the teardown lock and returns as soon as that
teardown finishes; it never repeats `lt.stop()` / `lo.stop()` and never
runs the quiesce again. No hardening applied: no evidence has shown it
necessary.

## D5 — Failure is reported, not auto-restarted (7d.0, Decision 5)

If a listening session dies, `reconcile()` releases the microphone and
sets `LIVE_ERROR`. The user restarts live manually from the tray menu.
No automatic restart loop.

## D6 — Tray rendering precedence is explicit (7d.1, addendum 7d.2)

```text
state.status in {RECORDING, PROCESSING, OUTPUTTING}  -> F8 wins
state.status == IDLE and live_status == LIVE_LISTENING -> LIVE_LISTENING
state.status == IDLE and live_status == LIVE_ERROR    -> LIVE_ERROR
otherwise                                             -> state.status
```

F8's active states always win, so the tray can never claim live is the
foreground operation while F8 owns it. Live rendering is applied only in
`IDLE`. (Supervisor gate should make the overlap impossible; precedence
makes it explicit — a targeted test is required.)

**7d.2 addendum (approved as clarification/extension, no semantic
change).** The matrix above is the pure function
`tray.display_status(status, live_status)`. It is surrounded by the
overlay chain that `tray.update_tray` applies in this order:

```text
startup_error set                                 -> STARTUP_ERROR title + error icon (wins outright)
microphone_error set                              -> MICROPHONE_ERROR title + error icon
                                                     (warnings are still logged, not rendered)
status in {RECORDING, PROCESSING, OUTPUTTING}
  while a temporary warning is rendered           -> warning superseded, F8 state rendered
temporary warning still live
  and state.status == IDLE                        -> warning title (outranks live rendering while IDLE)
otherwise                                         -> display_status(state.status, state.live_status)
```

Covered by `test_tray.py`: the D6 matrix (including the overlap case),
the overlay chain, and a thin `update_tray` loop run.

## D7 — Live menu check state reflects real state, not user intent

`checked=` reads `state.live_status == LIVE_LISTENING`. A failed start
leaves the menu unchecked and `LIVE_ERROR` shown. A requested ON that
fails must **not** persist.

## D8 — Config persistence rule for live mode

- `live_mode_enabled=True` is written only after a **successful** start.
- A failed start never persists the requested state.
- `live_mode_enabled` is the only new config key from 7d.1; calibration
  and delivery tuning stay code-owned.
- Startup restore (documented in 7d.2, behaviour unchanged): a persisted
  `True` makes `main.py` attempt `live_supervisor.start()` on **every**
  launch. A failed startup start is only logged — it never rewrites
  `live_mode_enabled`, so the next launch attempts again. There is no
  retry loop inside a session.

## D9 — `config.json` is never committed

The file is intentionally dirty (user runtime tuning: language, task,
backend, GPU). It must not enter any checkpoint. Validate with
`git diff --stat` before committing.

## D10 — No new event infrastructure

The existing 0.5 s tray polling loop is retained. `live_supervisor`
adds no timer threads beyond the main-loop `reconcile()` tick.

## D11 — Never committed files

`real_mic_validation.py`, `real_mic_validation_7c.py` are local
validation scripts and stay untracked.

## D12 — No VAD / AGC / normalization / new hotkeys / config tuning

Frozen scope. Any such change requires a new approved phase.

## D13 — Shutdown quiescence covers every F8 state (7d.2)

Teardown does not proceed until F8 has returned to `IDLE`, i.e. while
`state.status in {RECORDING, PROCESSING, OUTPUTTING}` **or** either F8
flag (`state.recording`, `state.processing`) is set. `OUTPUTTING` was
the gap: it runs with `processing == False`, so the earlier flag-only
wait could start teardown mid-delivery. The wait stays bounded by the
existing `QUIESCE_TIMEOUT = 5.0 s`; the F8 pipeline in `main.py` is
unchanged.

## D14 — Outstanding Whisper child is terminated on quiesce timeout (7d.2)

If the quiesce window expires, `whisper_engine.terminate_outstanding()`
kills and reaps `whisper_engine.whisper_process` before the `lt` / `lo`
teardown. The F8 path passes `cancel_event=None` and owns no
cancellation of its own, so without this call that child would outlive
the process. The helper deliberately does not touch `_invoke_lock` (it
has to interrupt the invocation currently holding it); normal-path M1
serialization is untouched, and no new cancellation architecture or
event infrastructure is introduced. Scope is the shutdown/lifecycle
path only.

## D15 — Tray Exit always stops the icon (7d.2)

`tray.exit_application()` sets `stopping = True` **first**, then runs
`live_supervisor.shutdown()` inside `try` / `finally` so `icon.stop()`
is attempted even when teardown raises — otherwise the tray thread
would outlive a failed teardown and the process would hang. The
exception still propagates to the pystray callback, as before.
