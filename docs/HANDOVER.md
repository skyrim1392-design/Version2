# Windows Voice Assistant — Agent Handover

Last updated: 2026-10-07 (docs sync pushed; repository fully checkpointed)
Repository: `C:\VoiceAssistant\Version2`
Branch: `backend-foundation`
Current HEAD: `dec546b` — Validate 7d.3 endurance: synthetic soak, cycles, shutdown stress
7d.2 checkpoint: `4de4bfd` — Harden live transcription shutdown and tray lifecycle
Remote: `origin/backend-foundation` at `663d461` — local **ahead 1** (7d.3 checkpoint), **push HOLD**
Remote: `origin/backend-foundation` — **synced with HEAD (`28b3eaf`, pushed 2026-10-07)**
Worktree: **7d.2 is committed and pushed.** Working tree contains only
the expected exclusions (`config.json`, `whisper/whisper-vulkan`,
`real_mic_validation*.py`) and the local icon PNG experiments
(`error.png`, `idle.png`, `processing.png`, `recording.png`, `icons/*.png`),
which remain local and are never committed

## Authority

**This file is the single authoritative session-entry document.**
If any other document disagrees with `HANDOVER.md`, this file wins.
Do not treat the seven documents as seven competing sources of truth.

```text
HANDOVER.md                    ← read first, always; authoritative state
   │
   ├── current state / next action
   ├── PROGRESS.md            → history (phases, commits, timeline)
   ├── ARCHITECTURE.md        → architecture (ownership, state model)
   ├── DECISIONS.md           → frozen decisions (D1–D15)
   ├── TEST_STATUS.md         → evidence (automated + manual checklist)
   ├── ROADMAP.md             → future phases
   └── project_state.json     → machine-readable state snapshot
```

Read order for a new session:

1. `docs/HANDOVER.md` (this file) — authoritative
2. `docs/TEST_STATUS.md` — evidence and the current gate checklist
3. `docs/DECISIONS.md` — frozen rules before touching anything
4. `git status` and `git log -5` — confirm reality matches this file
5. `docs/project_state.json` — machine-readable cross-check
6. Supporting docs (`ARCHITECTURE.md`, `PROGRESS.md`, `ROADMAP.md`)
   only as needed

Do not modify code until the current checkpoint state is confirmed.

---

## Forbidden assumptions

Do not assume:

- a clean git tree means safe to commit
- modified files are accidental
- untracked harnesses are disposable
- passing tests allow committing without approval
- documentation updates imply production changes are complete
- the handover is accurate without checking it against git

Always verify:

- `git status`
- `git log -1`
- test command output
- requested commit/push approval

`config.json`, `whisper/whisper-vulkan`, and `real_mic_validation*.py`
being dirty/untracked is **expected**, not a defect to clean up.

---

## Mission

Windows local dictation assistant, fully offline.

```text
F8 (push-to-talk)
  Microphone
   -> 16 kHz WAV
   -> whisper.cpp (Vulkan/CPU)
   -> clipboard output

Live mode
  Microphone
   -> live_capture
   -> energy gate
   -> whisper (serialized)
   -> live_output cursor
   -> clipboard
```

---

## Current phase

**Phase 7d.2 — Production hardening (tray coverage, shutdown
quiescence, Whisper orphan handling)**

Status: `7d.2 COMPLETE — committed as 4de4bfd and pushed to origin`

- Code implemented: yes (see "7d.2 contents"), **committed `4de4bfd`, pushed**
- Automated tests: 152/152 pass (`python -m unittest discover -v`,
  16.8 s) — 126 baseline + 26 new
- Static checks: all 7d.2-touched files pass `ruff check`; full repo
  unchanged at 30 pre-existing findings in frozen/untouched files
- Targeted 7d.1 smoke re-run: **7/8 scenarios PASS** (2026-10-07);
  `physical_exit` blocked by this session's denied `GetCursorPos`
  (environment, reproducible outside the app) and key injection was
  unavailable — both recorded in `docs/TEST_STATUS.md`
- Supplementary out-of-repo driver: Exit invoked during F8
  `PROCESSING` quiesced in 2.423 s, child exit 0, no orphaned Whisper
  process
- 7d.2 checkpoint: **`4de4bfd`**, pushed; `origin/backend-foundation`
  synced

Next action: **7d.3 design gate.** The next session enters a
**read-only 7d.3 design review** starting from the clean shared
checkpoint `4de4bfd`. No 7d.3 implementation until that design gate
concludes.

---

## Git checkpoints

| Phase | Commit | Status |
|---|---|---|
| 7b.4 Calibration robustness | `2691031` | pushed |
| 7c Live transcription delivery | `dd5d231` | pushed |
| 7d.0 Design gate | — | approved, no code |
| 7d.1 Lifecycle + tray | `dc5c4aa` | committed 2026-10-07, pushed |
| 7d.2 Hardening | `4de4bfd` | committed + pushed 2026-10-07 (current HEAD) |

Remote is synced with local: `backend-foundation...origin/backend-foundation` (no ahead/behind).

Never commit (development/validation scripts only):

- `real_mic_validation.py`
- `real_mic_validation_7c.py`

Never commit (intentionally dirty runtime state):

- `config.json`
- `whisper/whisper-vulkan` (working-tree content shows as modified)

---

## Frozen rules

Do not modify:

- `config.json` (runtime file; user-tuned, always dirty)
- `whisper/whisper-vulkan` binaries and DLLs
- `audio.py`
- `live_capture.py`
- the F8 pipeline inside `main.py` beyond the supervisor gate
- `whisper-cli` binaries

No new features of these kinds: VAD, AGC, normalization, new hotkeys,
tuned config constants. Calibration and delivery tuning stay code-owned.

---

## Current architecture (summary)

See `docs/ARCHITECTURE.md` for the full picture.

| Module | Owns |
|---|---|
| `main.py` | F8 lifecycle, startup, main loop, cleanup |
| `live_supervisor.py` | live session lifecycle, mic ownership gate, shutdown coordination + F8 quiescence, `state.live_status` |
| `live_transcribe.py` | streaming capture + whisper streaming (`lt`) |
| `live_output.py` | transcript delivery / cursor (`lo`) |
| `whisper_engine.py` | serialized whisper invocation; `terminate_outstanding()` (shutdown-path kill/reap) |
| `tray.py` | tray rendering (`display_status`), menu, Exit (teardown in `try`/`finally` around `icon.stop()`) |
| `app_state.py` | shared state (`status`, `live_status`) |
| `config.py` | defaults + persistence (`live_mode_enabled` is the only 7d.1 addition) |

Key invariant: **one microphone owner at a time.** While live is
`LIVE_LISTENING`, F8 is refused with a warning; while F8 owns the mic,
live start is refused.

---

## 7d.1 contents (committed in `dc5c4aa`)

Modified:

- `app_state.py` — adds `state.live_status`
- `config.py` — adds `live_mode_enabled: False` default
- `main.py` — supervisor gate in `f8_down`, startup live restore,
  main loop waits on `shutdown_event` and calls `reconcile()`,
  `cleanup()` calls `live_supervisor.shutdown()`
- `tray.py` — Live Transcription menu item, live-aware status
  rendering, Exit calls `live_supervisor.shutdown()` before
  `icon.stop()`, tray loop stops on `stopping`

New:

- `live_supervisor.py`
- `test_live_supervisor.py`
- `test_integration_7d.py`
- `docs/` (handover layer: `HANDOVER.md`, `TEST_STATUS.md`,
  `DECISIONS.md`, `ARCHITECTURE.md`, `PROGRESS.md`, `ROADMAP.md`,
  `project_state.json`)

Deliberately excluded from the commit (still dirty/untracked):

- `config.json` — runtime dirt only, must NOT be committed
- `whisper/whisper-vulkan` — must NOT be committed
- `real_mic_validation.py`, `real_mic_validation_7c.py` — validation
  harnesses, must NOT be committed

---

## 7d.2 contents (committed in `4de4bfd`, pushed)

Modified:

- `tray.py` — extracted `display_status(status, live_status)` from
  `update_tray` (local variable renamed to `display`); `exit_application`
  asserts `stopping` first and wraps teardown in `try`/`finally` so
  `icon.stop()` always runs (D15)
- `live_supervisor.py` — `F8_BUSY_STATUSES =
  ("RECORDING", "PROCESSING", "OUTPUTTING")`; `shutdown()` quiesce now
  waits for those statuses *and* both F8 flags (bounded by the existing
  `QUIESCE_TIMEOUT = 5.0`); on timeout it logs and calls
  `whisper_engine.terminate_outstanding()` (D13/D14)
- `whisper_engine.py` — `TERMINATE_TIMEOUT = 2.0` and
  `terminate_outstanding()`: kill + `wait(timeout)`, clears
  `whisper_process` only after a confirmed reap and only if the handle
  is unchanged; does not take `_invoke_lock`, never touches pipes (M1
  serialization preserved)
- `test_live_supervisor.py` — +8 tests: quiescence across every F8
  state and both flags, immediate return when idle, timeout
  fault-injection with a fake process handle (kill + reap, no real
  whisper-cli, no wall-clock timing), clean quiesce never touches the
  child handle, `LIVE_ERROR` keeps F8 allowed / `toggle()` recovers /
  `reconcile()` never auto-restarts

New:

- `test_tray.py` (10 tests) — D6 matrix, overlay priority, thin
  `update_tray` loop against a fake icon, `exit_application`
  ordering + `finally` behavior
- `test_config_persistence.py` (8 tests) — temp-config round trip,
  defaults/unreadable fallback, no temp leftovers, D8 through the real
  tray callback, structural check that `main.py` holds no config writer

Documentation (all eight `docs/` files updated): `DECISIONS.md`
D13–D15 + D4/D6/D8 notes, `ARCHITECTURE.md`, `PROGRESS.md`,
`TEST_STATUS.md`, `ROADMAP.md` (deferrals), `HANDOVER.md`,
`project_state.json`.

Verification performed this session: 152/152 tests, ruff clean on all
touched files, full repo still 30 pre-existing findings, `config.json`
SHA-256 identical before/after the smoke run, `audio.py` /
`live_capture.py` / `main.py` diff-clean vs HEAD.

---

## Current blocker

**None.** 7d.2 is committed (`4de4bfd`) and pushed; the working tree
matches `origin/backend-foundation`. The only permitted next
transition is the **read-only 7d.3 design gate**.

Environment limitations encountered (both reproducible outside the
application; recorded with evidence in `docs/TEST_STATUS.md`):

1. **`GetCursorPos` denied** (`WinError 5`) in this session — pystray
   `_on_notify` therefore raises `PermissionError` before application
   code runs, so the `physical_exit` smoke scenario cannot open a menu
   here. The same call fails from a plain `python -c`; the identical
   harness passed this scenario on 7d.1.
2. **Global key injection is not delivered** — `keyboard`/`keybd_event`
   presses never reach the F8 hook (`f8_press_hook_seen=False`), so the
   harness used its documented direct fallback (`main.f8_down()` /
   `main.f8_up()`). Mid-`PROCESSING` Exit coverage was recovered with a
   supplementary out-of-repo driver (quiesce 2.423 s, exit 0, no
   orphaned Whisper child).

Remaining (non-blocking) items:

1. **Next session:** enter the read-only 7d.3 design review from the
   shared checkpoint `4de4bfd`. Do not implement 7d.3 before the
   design gate concludes.
2. **Deferred by decision (D13–D15, listed in `ROADMAP.md`):** stale
   tray title on the stopped icon object after Exit; startup restore
   retry on next launch (persisted `live_mode_enabled=true` simply
   stays `true`); hardening of second-caller shutdown semantics.
3. **D6 addendum wording** was reconstructed from the approved decision
   summary in this session — confirm it matches the original proposal
   during review.

---

## Exact commands

```powershell
# tests (152 expected)
python -m unittest discover -v

# single suites
python -m unittest test_live_supervisor -v
python -m unittest test_tray -v
python -m unittest test_config_persistence -v
python -m unittest test_integration_7d -v

# lint (ruff lives in the user bin folder, not on PATH)
& "$env:USERPROFILE\.local\bin\ruff.exe" check <files>
```

Notes:

- There is no `pytest`; the project uses `unittest`.
- The project venv (`.venv`) contains only runtime deps
  (sounddevice, numpy, keyboard, pyperclip, pyautogui, PIL).
- Full-repo `ruff check .` currently reports 30 pre-existing findings in
  files untouched by 7d.1/7d.2 (`live_capture.py` 5, `text_output.py` 7,
  `real_mic_validation_7c.py` 14, `real_mic_validation.py` 2,
  `recovery.py` 1, `runtime_validation.py` 1). All 7d.1/7d.2-touched
  files are clean.

---

## Next agent instruction

> **Resume from repository handover.**
>
> First read `docs/HANDOVER.md`, then `docs/TEST_STATUS.md` and `docs/DECISIONS.md`.
>
> HEAD is **`28b3eaf` "Sync handover docs to pushed 7d.2 checkpoint"** on top of the 7d.2 checkpoint **`4de4bfd` "Harden live transcription shutdown and tray lifecycle"**, both committed locally and **pushed** — `backend-foundation` matches `origin/backend-foundation` (no ahead/behind).
>
> `152/152` automated tests pass and ruff is clean on every touched file. The targeted 7d.1 smoke re-run passed 7/8 scenarios (the eighth, `physical_exit`, is blocked by this session's denied `GetCursorPos` — environment, see `docs/TEST_STATUS.md`), and a supplementary driver proved Exit during `PROCESSING` quiesces in 2.423 s and exits 0 with no orphaned Whisper child.
>
> **Do not start 7d.3 implementation.**
>
> Next action: **read-only 7d.3 design review.** Work from the clean shared checkpoint. No commits, no implementation work until the 7d.3 design gate concludes.
>
> Working tree contains only the known exclusions: `config.json`, `whisper/whisper-vulkan`, `real_mic_validation.py`, `real_mic_validation_7c.py`, and the local icon PNG experiments (`error.png`, `idle.png`, `processing.png`, `recording.png`, `icons/*.png` — local only, never commit).
>
> Items deferred inside 7d.2 by decision: stale tray title, startup restore retry, second-caller shutdown semantics (`ROADMAP.md` → "Known deferred items").

---

## Mandatory session-end update

**Every agent must update `docs/HANDOVER.md` before ending a work
session**, covering all of:

- current HEAD
- worktree state
- test result
- completed work
- blocker
- exact next action
- one new row in the session transaction log (bottom of this file)

Then synchronize the supporting files: `docs/PROGRESS.md`,
`docs/TEST_STATUS.md`, `docs/ROADMAP.md` (if the plan changed), and
`docs/project_state.json`.

Never update the handover to claim a commit or push that was not
actually performed. Verify with `git status` / `git log -1` before
writing it down. Never leave a session without updating this file.

---

## Session transaction log

Audit trail only — one row per session, no diary. Append a row at every
session end (see the rule above).

| Date | Agent/session | Action | Result | Verification |
|---|---|---|---|---|
| 2026-10-07 | 7c checkpoint | Live transcription delivery + serialization | committed `dd5d231`, pushed | test suite green |
| 2026-10-07 | 7d.1 implementation | Added `live_supervisor` lifecycle boundary, F8/live gate, tray Exit + live menu | complete, awaiting smoke test | 126/126 tests, 7d.1 files lint-clean |
| 2026-10-07 | Documentation | Created `docs/` handover layer, authority model, forbidden assumptions | complete | `project_state.json` validated |
| 2026-10-07 | 7d.1 manual smoke test | Ran the `docs/TEST_STATUS.md` tray checklist with an out-of-repo harness (8 scenarios: tray Exit from `LIVE_IDLE`/`LIVE_LISTENING`/F8 `PROCESSING`, Live ON/OFF, forced `LIVE_ERROR` + retry, config persistence, F8 rejection, rendering precedence, physical Win32 menu Exit) | **PASS** — all 8 children exited code 0, no leftover Whisper process, `config.json.live_mode_enabled=false`, `git status` unchanged, no commit | 126/126 re-run OK (11.35 s), `ruff check` on 7d.1 files clean, results in `%TEMP%\opencode\smoke_7d1_results.json` |
| 2026-10-07 | 7d.1 checkpoint commit | Approved commit: staged only `app_state.py config.py main.py tray.py live_supervisor.py test_integration_7d.py test_live_supervisor.py docs/`; excluded `config.json`, `whisper/whisper-vulkan`, `real_mic_validation*.py` | committed **`dc5c4aa` "Integrate live transcription lifecycle"** (14 files, +1826/−11), **not pushed**, branch `[ahead 1]` | 126/126 tests OK (11.45 s) and `ruff check` clean immediately before commit; `git log -1 --stat` verified; `git status` shows only the four exclusions + this doc sync |
| 2026-10-07 | 7d.2 implementation (this session) | Approved scope: extracted `tray.display_status` + D6/overlay/thin-loop/Exit tests, extended `shutdown()` quiescence to every F8 state with `whisper_engine.terminate_outstanding()` on timeout, `exit_application` `try`/`finally`, `LIVE_ERROR` + config-persistence suites, docs D13–D15 | **complete, stopped at the implementation/test-review gate as instructed** | 152/152 tests OK (17.1 s), ruff clean on all touched files, smoke re-run 7/8 PASS + supplementary `PROCESSING` Exit driver PASS (2.423 s quiesce, exit 0) |
| 2026-10-07 | 7d.2 checkpoint + push | Approved: commit `4de4bfd` "Harden live transcription shutdown and tray lifecycle" (13 files, +1424/−120, 7d.2 code/tests/docs only), pushed `backend-foundation` to origin; handover + `project_state.json` synced to pushed state, HEAD `4de4bfd`, 7d.2 complete, next transition = read-only 7d.3 design gate; icon PNG experiments remain local/excluded | **pushed, remote synced with HEAD `4de4bfd`** | `git status` shows only expected exclusions + local icon PNG dirt; `git branch -vv` confirms no ahead/behind; one transient schannel revocation error on push retry confirmed the ref was already at `4de4bfd` |
