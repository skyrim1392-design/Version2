# Development progress

> Authority: `docs/HANDOVER.md` is the single source of truth. This file is supporting detail; if they disagree, HANDOVER.md wins.

Last updated: 2026-10-07 (7d.2 implemented, uncommitted)

```mermaid
flowchart LR
A[1 F8 Control Flow] --> B[2 Backend Foundation]
B --> C[3 Whisper Pipeline]
C --> D[7b Calibration Robustness]
D --> E[7c Live Transcription]
E --> F[7d Production Hardening]
F --> G[8 Agent Layer]

D --> D1[Calibration estimator]
D --> D2[Replay buffer]

E --> E1[Cursor API]
E --> E2[Live Output]
E --> E3[Whisper Serialization]

F --> F1[7d.0 Design gate]
F --> F2[7d.1 Lifecycle + Tray]
F --> F3[7d.2 Hardening]
F --> F4[7d.3 Endurance]
```

---

## Timeline

| Phase | Description | Commit | Status |
|---|---|---|---|
| 1 | F8 control flow | (history) | done |
| 2 | Backend foundation | (history) | done |
| 3 | Whisper pipeline | (history) | done |
| 65dc0af | Tray state precedence + processing failure feedback | `65dc0af` | pushed |
| 7327b12 | Runtime recovery foundation | `7327b12` | pushed |
| a396a38 | Runtime recovery + model validation boundary | `a396a38` | pushed |
| 7a | Streaming capture foundation | `f2207f7` | pushed |
| 7b | Live transcription pipeline | `5cc87fe` | pushed |
| 7b.4 | Calibration robustness | `2691031` | pushed |
| 7c | Live transcription delivery + serialization | `dd5d231` | pushed |
| 7d.0 | Design gate (F8/live exclusion, state model) | — | approved, no code |
| 7d.1 | Supervisor lifecycle + tray integration | `dc5c4aa` | committed 2026-10-07, **local only — not pushed** |
| 7d.2 | Tray rendering coverage, shutdown quiescence, Whisper orphan handling | — | implemented 2026-10-07, **uncommitted — at test-review gate** |
| 8 | Agent layer | — | not started |

---

## 7d.1 detail

Implemented:

- `live_supervisor.py` (new): session lifecycle, `f8_request()` gate,
  `reconcile()` failure cleanup, `shutdown()` with flag-first ordering.
- `main.py`: gate in `f8_down`, supervisor shutdown in `cleanup`,
  startup restore of `live_mode_enabled`, main loop waits on
  `shutdown_event` + runs `reconcile()`.
- `tray.py`: Live Transcription menu item, live-aware status rendering,
  Exit performs real shutdown before `icon.stop()`.
- `app_state.py`: `live_status`.
- `config.py`: `live_mode_enabled` default.
- `test_live_supervisor.py`, `test_integration_7d.py`.

Evidence: 126/126 tests (re-run after validation), 7d.1 files lint-clean,
manual tray smoke test **PASS** — 8/8 scenarios on 2026-10-07, every
child process exited code 0, no leftover Whisper process, `git status`
unchanged (observations in `TEST_STATUS.md`).

Outstanding (7d.1): `dc5c4aa` is committed but **not pushed** — hold
for a separate push approval. Its open gap (no unit test covering
`tray.update_tray` rendering precedence) is **closed by 7d.2** — see
below.

---

## 7d.2 detail

Approved scope (all implemented, decisions in `DECISIONS.md`
D13–D15, deferred items recorded in `ROADMAP.md`):

- **Tray rendering coverage (D13):** extracted
  `tray.display_status(status, live_status)` from `update_tray`;
  D6 precedence matrix + overlay-priority tests, thin fake-icon
  `update_tray` loop test. Closes the 7d.1 open gap above.
- **Shutdown quiescence (D14):** `live_supervisor.shutdown()` waits
  out every F8 state — `RECORDING`, `PROCESSING`, `OUTPUTTING`
  (`F8_BUSY_STATUSES`) and both F8 flags — bounded by the existing
  5 s `QUIESCE_TIMEOUT`; F8 pipeline in `main.py` untouched.
- **Whisper orphan handling (D14):** on quiesce timeout the shutdown
  path logs and calls `whisper_engine.terminate_outstanding()`
  (kill + `wait(timeout=2.0)`, reap-confirmed handle clear, no
  `_invoke_lock`, no pipe access); M1 single-invocation serialization
  preserved.
- **`exit_application` teardown fix (D4/D15):** `stopping` asserted
  first, teardown wrapped in `try`/`finally` so `icon.stop()` always
  runs; first caller owns shutdown, later callers no-op (D15).
- **Tests:** `test_tray.py` (10), `test_config_persistence.py` (8),
  `test_live_supervisor.py` +8 (quiescence, timeout fault injection
  with a fake process handle, `LIVE_ERROR` F8/reconcile/toggle).
- **Docs:** `DECISIONS.md` D13–D15 + D4/D6/D8 notes, `ROADMAP.md`
  deferrals, `ARCHITECTURE.md` ownership/state/shutdown, this file,
  `TEST_STATUS.md`, `project_state.json`, `HANDOVER.md`.

Deferred by decision: stale tray title after Exit, startup restore
retry on next launch (persisted `true` stays `true`), second-caller
shutdown semantics hardening.

Evidence: 152/152 tests, 7d.2 files lint-clean, full-repo ruff
unchanged at 30 pre-existing findings; targeted 7d.1 smoke re-run
7/8 scenarios PASS (`physical_exit` blocked by this session's denied
`GetCursorPos`, key injection unavailable — environment, recorded in
`TEST_STATUS.md`) plus a supplementary out-of-repo driver proving Exit
during `PROCESSING` quiesces in 2.423 s and exits 0 with no orphan
Whisper child.

Status: stopped at the implementation/test-review gate. `dc5c4aa`
still holds the 7d.1 checkpoint; 7d.2 commit requires explicit
approval and a separate push decision.
