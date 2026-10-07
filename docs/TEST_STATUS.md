# Test status

> Authority: `docs/HANDOVER.md` is the single source of truth. This file is supporting detail; if they disagree, HANDOVER.md wins.

Last updated: 2026-10-07 — Phase 7d.1 (uncommitted)

---

## Automated

Command:

```powershell
python -m unittest discover -v
```

Result: **126 / 126 pass** (11.4 s), 2026-10-07.

| Suite | Tests |
|---|---|
| `test_live_transcribe` | 91 |
| `test_live_output` | 18 |
| `test_live_supervisor` | 6 |
| `test_integration_7d` | 6 |
| `test_whisper_cancel_lifecycle` | 5 |
| **total** | **126** |

Supervisor coverage highlights:

- shutdown flag is set before teardown (race test)
- F8/live mutual exclusion both directions
- start failure leaves no ownership and reports `LIVE_ERROR`
- failure cleanup path (`reconcile`) releases the microphone
- idempotent shutdown, rejection after shutdown

### Lint

```powershell
& "$env:USERPROFILE\.local\bin\ruff.exe" check <files>
```

- 7d.1-touched files (`app_state.py config.py main.py tray.py
  live_supervisor.py test_live_supervisor.py test_integration_7d.py`):
  **All checks passed.**
- Full repo `ruff check .`: 30 findings, all pre-existing and outside
  7d.1 (`live_capture.py` 4, `recovery.py` 1, `runtime_validation.py` 1,
  `text_output.py` 7, `real_mic_validation.py` 2, `real_mic_validation_7c.py` 14,
  plus `main.py` `# noqa: BLE001` comments already annotated).
- No `pytest` — the project uses `unittest`.

---

## Manual tray smoke test - REQUIRED BEFORE COMMIT

**Status: RUN - PASS, 2026-10-07.** Every row below was observed and
recorded. No production code was modified while testing.

Method: `smoke_7d1.py`, a validation harness kept **outside the repo**
(`%TEMP%\opencode\smoke_7d1.py`), so `git status` stays byte-identical to
the handover snapshot. Each scenario launches the real `main.py` as a
child process, drives the real pystray `MenuItem` callbacks
(byte-equivalent to `pystray._win32._on_notify` ->
`descriptors[index - 1](self)`) or the real Win32 menu, and records
`[SMOKE]` markers plus a `logs/assistant.log` slice. Machine-readable
output: `%TEMP%\opencode\smoke_7d1_results.json`.

One run, all 8 scenarios green. Every child process exited with **code 0**,
none timed out, none left a `whisper-cli`/`whisper` process behind.

### 1. Tray Exit must terminate the process

Defect being verified: previously `icon.stop()` stopped the tray thread
but `main.py` slept forever.

Expected path: `Tray Exit -> supervisor.shutdown() -> shutdown_event set
-> icon.stop() -> main loop wakes -> process exits`.

Process disappearance was verified from the parent harness with
`Popen.poll()` (an exited PID is gone from `Get-Process`), not by eye.

| Exit performed during | Process gone | mic owner `None` | no live/output threads | no Whisper child | tray stopped |
|---|---|---|---|---|---|
| `LIVE_IDLE` | yes, exit 0, 0.74 s after `exit_invoke`, 1.80 s after loop return | `post.mic_owner = None` | `post.live_threads = none`, `post.lo_state = STOPPED` | `post.whisper_child = None`, no leftover process | `shutdown_set=True`, `icon_running_after_exit=False`, `tray_stopping=True` |
| `LIVE_LISTENING` | yes, exit 0, 0.15 s after `exit_invoke`, 1.79 s after loop return | `post.mic_owner = None` | `post.live_threads = none`, `post.lt_active=False`, `post.lo_state = STOPPED` (was `LiveCaptureWorker,LiveOutput,LiveTranscribeTranscription,LiveTranscribeWorker` before Exit) | `post.whisper_child = None` | `shutdown_set=True`, `icon_running_after_exit=False`, `tray_stopping=True` |
| F8 `PROCESSING` | yes, exit 0, quiesce 1.36 s (< 5 s limit), 1.78 s after loop return | `post.mic_owner = None` | `post.live_threads = none` | `post.whisper_child = None`, no leftover process | `shutdown_set=True`, `icon_running_after_exit=False`, `tray_stopping=True` |

### 2. Live ON/OFF menu semantics

Scenarios `toggle_semantics` and `error_retry`:

| Step | Expected | Observed |
|---|---|---|
| Start with OFF | menu item unchecked | `start.checkmark=False`, `start.config=False`, `start.tray_title="Voice Assistant - IDLE"` |
| Click ON | live starts, menu checked | `on.live_status=LIVE_LISTENING`, `on.checkmark=True`, `on.config=True` |
| While active | tray shows `LIVE_LISTENING` | `live.tray_title="Voice Assistant - LIVE_LISTENING"`, `mic_owner=live`, `lt_active=True`, `lo_state=RUNNING` |
| Click OFF | live stops, returns to `LIVE_IDLE`, unchecked | `off.live_status=LIVE_IDLE`, `off.checkmark=False`, `off.config=False`, `off.tray_title="Voice Assistant - IDLE"` |
| Force a start failure | menu **not** checked, tray `LIVE_ERROR` | `failed.live_status=LIVE_ERROR`, `failed.checkmark=False`, `failed.config=False`, `failed.tray_title="Voice Assistant - LIVE_ERROR"`, `failed.lt_active=False`, `failed.mic_owner=f8` |
| Retry from menu after failure | successful start -> `LIVE_LISTENING`, checked | `retry.live_status=LIVE_LISTENING`, `retry.checkmark=True`, `retry.config=True`, `retry.mic_owner=live` |
| Restart persistence after success | `config.json` has `live_mode_enabled: true` | `config_on=True`, and the **next** scenario started with `config_normalized.before=true` (the value survived a process restart); startup restored it and launched Live |
| Persistence after failed start | `live_mode_enabled` **not** set to true | `failed.config=False`; after the later OFF, `stop.config=False` |
| Existing F8 menu/status behavior | unchanged | F8 hotkey still registered and functional: press -> `audio.is_recording=True` -> `PROCESSING` -> `OUTPUTTING`; the blocked case keeps `state.status=IDLE` |

Note: `config.json` is intentionally dirty. Observing writes is fine;
**it must not be committed**. The harness re-set `live_mode_enabled` to
`false` before each scenario (`config_normalized` in the results JSON) so
scenarios start from a known state; the file reads `false` at the end of
the run.

### 3. Status rendering precedence

Scenario `precedence`:

| `state.status` | `state.live_status` | tray must show | Observed |
|---|---|---|---|
| `IDLE` | `LIVE_LISTENING` | `LIVE_LISTENING` | `row1.tray_title = "Voice Assistant - LIVE_LISTENING"` |
| `RECORDING` / `PROCESSING` / `OUTPUTTING` | `LIVE_LISTENING` | the F8 state, **not** live | 8 consecutive samples while F8 held the mic with `state.live_status=LIVE_ERROR` still set: `["PROCESSING","LIVE_ERROR","Voice Assistant - PROCESSING"]` - the F8 state won every time; after completion the title became `... - OUTPUTTING` |

Extra row observed: `IDLE` + `LIVE_ERROR` -> `Voice Assistant - LIVE_ERROR`.

(The supervisor gate should make the second row impossible; verify the
rendering precedence is explicit and test-covered.)

**Finding:** the precedence is explicit in `tray.update_tray` (the live
override is applied only while `state.status == "IDLE"`), but **no unit
test in `test_*.py` exercises `tray.update_tray` status rendering**. The
row above is satisfied by observation only, not by test coverage - record
it as an open gap for the reviewer.

### 4. F8 rejection while live owns the mic

Scenario `f8_blocked`, key injected with `keybd_event` (the real global
hook path - `keyboard.send()` tags its payloads and is invisible to
`keyboard`'s own hook):

- warning shown: `after.warning = "Microphone is currently used by Live
  Transcription"`, and `after.tray_title = "Voice Assistant - Microphone
  is currently used by Live Transcription"`
- no recording starts, no Whisper child spawned:
  `after.recording=False`, `after.is_recording=False`,
  `after.whisper_child=False`
- `state.status` unchanged: `before.f8_status=IDLE` -> `after.status=IDLE`
- bonus: `after.f8_active=False` (the press did not arm F8) and
  `after.mic_owner` still `live`
- the release was also injected and observed
  (`f8_release_hook_seen=True`, `f8_release_app_handled=False`)

### 5. Targeted shutdown tests

| Scenario | Expected | Observed |
|---|---|---|
| Exit during `LIVE_LISTENING` | mic released, no live/output threads, no Whisper child, tray stopped, process terminated | `exit_live`: exit 0; `post.mic_owner=None`, `post.live_threads=none`, `post.lo_state=STOPPED`, `post.whisper_child=None`, `tray_stopping=True`, `icon_running_after_exit=False` |
| Exit during F8 `PROCESSING` | quiesce (<= 5 s) then same clean teardown | `exit_f8`: `f8_state_at_exit=PROCESSING`, quiesce 1.36 s, exit 0; `post.mic_owner=None`, `post.live_threads=none`, `post.whisper_child=None`, no leftover Whisper process |

### Bonus: physical tray menu

Scenario `physical_exit` exercised the real Win32 menu end to end:
`WM_RBUTTONUP` posted to the pystray icon window -> pystray
`SetForegroundWindow` + `TrackPopupMenuEx(TPM_RETURNCMD)` -> popup
`#32768` found with foreground owned by the app process. pystray
registers no accelerator characters, so `VK_UP` changed nothing,
`VK_END` highlighted the last item ("Exit") and `VK_RETURN` committed it.
`shutdown_after_menu=True`, process exit **0**, 1.73 s after loop return.

### Recording results

```text
Date: 2026-10-07
Tester: opencode agent (automated harness smoke_7d1.py, outside the repo)
Outcome: PASS
Notes:
- 8/8 scenarios green in one run: exit_idle, exit_live, exit_f8,
  toggle_semantics, error_retry, f8_blocked, precedence, physical_exit.
  All children exit code 0, no timeouts, no leftover whisper process,
  config.json.live_mode_enabled=false afterwards.
- F8 down/up driven by keybd_event; both were accepted by the app
  (f8_press_driver=injected, f8_release_driver=injected). The harness
  observer hook failed to record the key-up in 2 of 8 cases even though
  the app handled it (f8_release_app_handled=True) - observer artefact,
  not an application defect.
- Cosmetic: after Exit the tray title is no longer re-rendered, so a
  stale value (for example "... - LIVE_LISTENING") can still be read off
  the stopped icon object. The icon itself is already removed
  (icon_running_after_exit=False).
- Observation: after Exit during F8 PROCESSING the completion callback
  still ran and state.status advanced to OUTPUTTING during teardown.
  Mic, live threads and the Whisper child were all released and the
  process exited 0. The transcript paste landed in the harness scratch
  window, which was foreground at the time.
- Post-run verification: git status --porcelain identical to the
  handover snapshot; python -m unittest discover -> 126/126 OK (11.35 s);
  ruff check on 7d.1 files -> All checks passed. No commit, no push.
- GAP (see section 3): no unit test covers tray.update_tray rendering
  precedence.
```

---

## Checkpoint rule

If all manual checks pass: stop and request approval for the 7d.1
commit. Commit only 7d.1 code files — never `config.json`,
`whisper/whisper-vulkan`, `real_mic_validation*.py`. Do not push without
approval. Do not start 7d.2 before the checkpoint is committed and
verified.
