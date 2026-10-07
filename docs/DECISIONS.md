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

## D5 — Failure is reported, not auto-restarted (7d.0, Decision 5)

If a listening session dies, `reconcile()` releases the microphone and
sets `LIVE_ERROR`. The user restarts live manually from the tray menu.
No automatic restart loop.

## D6 — Tray rendering precedence is explicit

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

## D7 — Live menu check state reflects real state, not user intent

`checked=` reads `state.live_status == LIVE_LISTENING`. A failed start
leaves the menu unchecked and `LIVE_ERROR` shown. A requested ON that
fails must **not** persist.

## D8 — Config persistence rule for live mode

- `live_mode_enabled=True` is written only after a **successful** start.
- A failed start never persists the requested state.
- `live_mode_enabled` is the only new config key from 7d.1; calibration
  and delivery tuning stay code-owned.

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
