# Windows Voice Assistant — Agent Handover

Last updated: 2026-10-07
Repository: `C:\VoiceAssistant\Version2`
Branch: `backend-foundation`
Current HEAD: `dd5d231` — Complete 7c live transcription delivery and serialization
Remote: `origin/backend-foundation` (in sync with HEAD; nothing new pushed)
Worktree: dirty — 7d.1 implemented, **uncommitted by instruction**

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
   ├── DECISIONS.md           → frozen decisions (D1–D12)
   ├── TEST_STATUS.md         → evidence (automated + manual checklist)
   ├── ROADMAP.md             → future phases
   └── project_state.json     → machine-readable state snapshot
```

Read order for a new session:

1. `docs/HANDOVER.md` (this file) — authoritative
2. `docs/TEST_STATUS.md` — the sole blocker and its checklist
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

**Phase 7d.1 — Production hardening (lifecycle + tray integration)**

Status: `IMPLEMENTATION COMPLETE — MANUAL UI VALIDATION PASSED`

- Code implemented: yes
- Automated tests: 126/126 pass (re-run after the smoke test, 11.35 s)
- Static checks: 7d.1-touched files pass `ruff check`
- Manual tray smoke test: **RUN — PASS (2026-10-07)**, 8/8 scenarios,
  every child process exited code 0 (see `docs/TEST_STATUS.md`)
- Commit: **NOT ALLOWED YET** (review verdict: validate first, then stop for approval)
- Push: not allowed without approval

Next action: the validation blocker is cleared. **Stop and request
approval for the 7d.1 commit.** Do not write new production code, do not
commit, do not push until approval is given.

---

## Git checkpoints

| Phase | Commit | Status |
|---|---|---|
| 7b.4 Calibration robustness | `2691031` | pushed |
| 7c Live transcription delivery | `dd5d231` | pushed (current HEAD) |
| 7d.0 Design gate | — | approved, no code |
| 7d.1 Lifecycle + tray | uncommitted | waiting manual validation |

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
| `live_supervisor.py` | live session lifecycle, mic ownership gate, shutdown coordination, `state.live_status` |
| `live_transcribe.py` | streaming capture + whisper streaming (`lt`) |
| `live_output.py` | transcript delivery / cursor (`lo`) |
| `whisper_engine.py` | serialized whisper invocation |
| `tray.py` | tray rendering, menu, Exit |
| `app_state.py` | shared state (`status`, `live_status`) |
| `config.py` | defaults + persistence (`live_mode_enabled` is the only 7d.1 addition) |

Key invariant: **one microphone owner at a time.** While live is
`LIVE_LISTENING`, F8 is refused with a warning; while F8 owns the mic,
live start is refused.

---

## Current uncommitted work (7d.1)

Modified:

- `app_state.py` — adds `state.live_status`
- `config.py` — adds `live_mode_enabled: False` default
- `main.py` — supervisor gate in `f8_down`, startup live restore,
  main loop waits on `shutdown_event` and calls `reconcile()`,
  `cleanup()` calls `live_supervisor.shutdown()`
- `tray.py` — Live Transcription menu item, live-aware status
  rendering, Exit calls `live_supervisor.shutdown()` before
  `icon.stop()`, tray loop stops on `stopping`
- `config.json` — **runtime dirt only, must NOT be committed**
- `whisper/whisper-vulkan` — **must NOT be committed**

New:

- `live_supervisor.py`
- `test_live_supervisor.py`
- `test_integration_7d.py`

---

## Current blocker

Manual tray validation has not been performed. Required checks:

1. Live ON/OFF menu semantics (checked state follows actual state)
2. Failed live start → `LIVE_ERROR`, menu not left checked, retry works
3. Tray Exit terminates the real Python process (not just the icon)
4. Exit during `LIVE_LISTENING`
5. Exit during F8 `PROCESSING`
6. F8 rejected while live owns the mic
7. Status rendering precedence when F8 owns the foreground state

Full procedure and expected observations: `docs/TEST_STATUS.md`.

---

## Exact commands

```powershell
# tests (126 expected)
python -m unittest discover -v

# single suites
python -m unittest test_live_supervisor -v
python -m unittest test_integration_7d -v

# lint (ruff lives in the user bin folder, not on PATH)
& "$env:USERPROFILE\.local\bin\ruff.exe" check <files>
```

Notes:

- There is no `pytest`; the project uses `unittest`.
- The project venv (`.venv`) contains only runtime deps
  (sounddevice, numpy, keyboard, pyperclip, pyautogui, PIL).
- Full-repo `ruff check .` currently reports 30 pre-existing findings in
  files untouched by 7d.1 (`live_capture.py`, `recovery.py`,
  `text_output.py`, `runtime_validation.py`, `real_mic_validation*.py`).
  All 7d.1-touched files are clean.

---

## Next agent instruction

> **Resume from repository handover.**
>
> First read `docs/HANDOVER.md`, then `docs/TEST_STATUS.md` and `docs/DECISIONS.md`.
>
> Current checkpoint is `dd5d231`. 7d.1 production implementation is complete but **not committed**. `126/126` automated tests pass and the manual tray smoke test passed (2026-10-07, 8/8 scenarios, all children exit code 0).
>
> **Do not modify production code. Do not commit. Do not push.**
>
> The validation blocker is cleared. Read `docs/TEST_STATUS.md` → "Manual tray smoke test" for the exact recorded observations.
>
> Next action: **request approval for the 7d.1 commit**. If approval is granted, commit only the 7d.1 code files — never `config.json`, `whisper/whisper-vulkan`, or `real_mic_validation*.py`. Do not push without approval.
>
> Known open gap recorded during validation: no unit test covers `tray.update_tray` status-rendering precedence (it was observed live, but it is not test-covered).
>
> Do not begin 7d.2.

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
