# Test status

> Authority: `docs/HANDOVER.md` is the single source of truth. This file is supporting detail; if they disagree, HANDOVER.md wins.

Last updated: 2026-10-07 — Phase 7d.2 (implemented, uncommitted)

---

## Automated

Command:

```powershell
python -m unittest discover -v
```

Result: **152 / 152 pass** (16.8 s), 2026-10-07 (7d.1 baseline was
126 / 126).

| Suite | Tests | of which 7d.2 |
|---|---|---|
| `test_live_transcribe` | 91 | — |
| `test_live_output` | 18 | — |
| `test_live_supervisor` | 14 | +8 |
| `test_tray` (new) | 10 | +10 |
| `test_config_persistence` (new) | 8 | +8 |
| `test_integration_7d` | 6 | — |
| `test_whisper_cancel_lifecycle` | 5 | — |
| **total** | **152** | **+26** |

Supervisor coverage highlights:

- shutdown flag is set before teardown (race test)
- F8/live mutual exclusion both directions
- start failure leaves no ownership and reports `LIVE_ERROR`
- failure cleanup path (`reconcile`) releases the microphone
- idempotent shutdown, rejection after shutdown
- **7d.2:** quiescence waits for every F8 state (RECORDING,
  PROCESSING, OUTPUTTING) and both F8 flags; idle F8 returns
  immediately; a quiesce timeout terminates and reaps an injected
  Whisper child (fake handle — no real whisper-cli, no timing on a real
  invocation); a clean quiesce never touches the child handle
- **7d.2:** `LIVE_ERROR` keeps F8 allowed, `toggle()` recovers to
  `LIVE_LISTENING`, `reconcile()` never auto-restarts (D5 unchanged)

Tray coverage (closes the 7d.1 open gap):

- D6 matrix in isolation, including the F8-vs-live overlap rows
- overlay priority: startup error > microphone error > F8 state
  (supersedes a rendered warning) > temporary warning (beats live
  rendering while IDLE) > normal status
- thin `update_tray` loop run against a fake icon (title/icon/stop)
- `exit_application`: `stopping` asserted before teardown, and
  `icon.stop()` still attempted when teardown raises

Config coverage (7d.2, temporary config file only — `config.json` is
byte-compared before/after each test):

- round trip, defaults on missing file, fallback on unreadable file,
  no leftover `config_*.tmp`
- D8 through the real tray callback: refused start writes nothing,
  successful start persists `true`, stop persists `false`
- `main.py` holds no config writer and still reads
  `live_mode_enabled` (structural check of decision 6)

### Lint

```powershell
& "$env:USERPROFILE\.local\bin\ruff.exe" check <files>
```

- 7d.2-touched files (`tray.py live_supervisor.py whisper_engine.py
  test_tray.py test_live_supervisor.py test_config_persistence.py`)
  plus 7d.1 files (`app_state.py config.py main.py
  test_integration_7d.py`): **All checks passed.**
- Full repo `ruff check .`: 30 findings, all pre-existing and in files
  untouched by 7d.1/7d.2 (`live_capture.py` 5, `text_output.py` 7,
  `real_mic_validation_7c.py` 14, `real_mic_validation.py` 2,
  `recovery.py` 1, `runtime_validation.py` 1). (The 7d.1 note listed
  `live_capture.py` as 4 and one finding as a `main.py` annotation;
  the total of 30 is unchanged and `main.py` is clean.)
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

## 7d.2 targeted smoke re-run — 2026-10-07

Method: the same out-of-repo harness (`%TEMP%\opencode\smoke_7d1.py`),
full 8-scenario run after the 7d.2 implementation. Transcript:
`%TEMP%\opencode\smoke_7d2_rerun.txt`; machine-readable output:
`%TEMP%\opencode\smoke_7d1_results.json` (overwritten by this run).
`config.json` SHA-256 identical before and after.

| Scenario | Result |
|---|---|
| `exit_idle` | **PASS**, exit 0, `tray_stopping=True`, `shutdown_set=True`, `post.mic_owner=None`, `post.lo_state=STOPPED`, `post.whisper_child=None` |
| `exit_live` | **PASS**, exit 0 from `LIVE_LISTENING`, same clean postconditions |
| `exit_f8` | **PASS**, exit 0, clean postconditions — but this session's Exit landed while status was already `IDLE` (see environment note below) |
| `toggle_semantics` | **PASS**, checkmarks/`live_mode_enabled` on/off as specified |
| `error_retry` | **PASS**, `failed.live_status=LIVE_ERROR`, `failed.config=False`, tray `LIVE_ERROR` |
| `f8_blocked` | **PASS**, refusal warning, `after.status=IDLE`, no recording, no whisper child |
| `precedence` | **PASS**, `IDLE+LIVE_LISTENING → LIVE_LISTENING`, `IDLE+LIVE_ERROR → LIVE_ERROR`, `row2_after.tray_title = LIVE_ERROR` (`row2.samples=[]`, see note) |
| `physical_exit` | **BLOCKED BY ENVIRONMENT** — see below |

Environment notes (this session, reproducible from an unrelated
`python -c` process):

1. **Global key injection is not delivered.** `f8_press_hook_seen` and
   `f8_release_hook_seen` were `False` in every scenario, so the
   harness used its documented direct fallback (calling
   `main.f8_down()` / `main.f8_up()`, the same functions the hotkey
   handler calls). Consequences: `exit_f8` completed the F8 cycle
   before Exit ran (`f8_state_at_exit = <not observed, status=IDLE>`),
   and `precedence` `row2.samples` is empty because the synchronous
   `f8_up()` fallback returns only after processing finished. Both
   scenarios still exited 0 with correct final titles.
2. **`GetCursorPos` is denied.** `physical_exit` fails inside pystray
   `_win32._on_notify` with `PermissionError: [WinError 5]` before any
   application code runs; the menu never opens and the harness kills
   the child (`exit_code=1`, `timed_out=True`). The same call returns
   error 5 from a plain `python -c` in this session, so this is
   session/desktop access, not a 7d.2 regression (the identical
   harness passed this scenario on 7d.1).

**Supplementary check for the path this session could not drive**
(`%TEMP%\opencode\smoke_7d2_exit_f8_direct.py`, out of repo, results in
`smoke_7d2_exit_f8_direct_results.json`): calls `main.f8_down()`,
holds the recording 3 s, then `main.f8_up()` on its own thread and
invokes the real pystray `Exit` callback while
`state.status == PROCESSING`.

| Marker | Value |
|---|---|
| `f8_state_at_exit` | `PROCESSING` |
| `releaser_alive_at_exit` | `True` |
| `exit_seconds` (teardown incl. quiesce) | **2.423 s** (< 5 s `QUIESCE_TIMEOUT`) |
| `post.status` / `post.recording` / `post.processing` | `IDLE` / `False` / `False` |
| `post.mic_owner` / `post.lo_state` / `post.live_threads` | `None` / `STOPPED` / `none` |
| `post.whisper_child` / `whisper_leftover` | `None` / none |
| `process.exit_code` / `process.timed_out` | `0` / `False` |

Outcome: **PASS.** The quiescence wait observed F8 finishing and
teardown completed cleanly with no orphaned Whisper child.

---

## 7d.3 endurance harness run — 2026-10-07

Harness: `real_mic_validation_7d3.py` (untracked, local-only per
D11), synthetic/hardware-free seams (`FakeLiveCapture`,
`FakeWhisperEngine`, `DeliveryRecorder`), spec in
`docs/7D3_DESIGN.md`.

Profile run:

- soak: 60 s continuous live session
- lifecycle: 20 start/stop cycles
- contention: 10 F8/live cycles
- toggle: 20 ON/OFF cycles
- shutdown under load: 4 cycles (2 live-active, 2 fake-F8-busy with
  `QUIESCE_TIMEOUT=0.5`, `terminate_outstanding` observed 2x)

Results: **26/26 PASS.**

### Full soak — 2026-10-08 (`--full`)

- soak: **1800 s** (30 min) continuous live session
- lifecycle: **100** start/stop cycles
- contention: **50** F8/live cycles (F8 blocked 50/50 while live
  active, allowed 50/50 after stop)
- toggle: **100** ON/OFF cycles
- shutdown under load: **10/10** clean (`terminate_outstanding`
  observed on the fake-F8-busy timeout path)
- Checks: **26/26 PASS**
- Threads: flat 5 during soak (3 live workers), 1 at finish —
  no monotonic growth
- Whisper children: 0 throughout, 0 after final teardown
- Mic ownership: `"live"` while active, `None` after every stop
- Deliveries: 1 (synthetic tone segment; rate not a pass/fail
  criterion per design)
- `config.json` SHA-256: unchanged across the full run
- Final teardown equivalent to baseline:
  `live_status=LIVE_IDLE`, `mic_owner=None`, `whisper_child=0`,
  0 live threads, F8 flags clear
- Regression: full suite **152/152 OK** (17.1 s); ruff clean on the
  harness
- Artifacts: `%TEMP%\opencode\7d3_endurance_results.json`,
  `%TEMP%\opencode\7d3_full_out.txt`

Condensed-profile details retained from the earlier run below.

Environment notes: the harness is synthetic; lack of automated
microphone injection is a documented validation limitation per the
design, not a failed test. Real-mic corroboration remains in the
7c/7d smoke harnesses.

Status: **7d.3 production endurance evidence: PASS — synthetic
profile, 30-minute soak + lifecycle/contention/toggle/shutdown
stress. Physical unplug/sleep-resume endurance remains unproven.**

Qualification: this is a synthetic endurance profile. It does **not**
replace a physical 30-minute test involving actual microphone
unplug/replug, Windows sleep/resume, audio-device
disappearance/reappearance, long-duration real speech, or real
clipboard/application delivery over the entire soak. The 7d.1
physical smoke plus this 7d.3 synthetic endurance run together
provide good evidence, but those environmental cases remain outside
the demonstrated 30-minute soak.

- Checks: **26/26 PASS**
- Deliveries during soak: 1 (single synthetic tone segment — rate not
  a pass/fail criterion per design)
- Threads: flat plateau (5 live-session threads during soak, 1 after)
- Mic ownership: `"live"` while active (claimed/released by the
  harness-wrapped capture seam), `None` after every stop
- Whisper children: 0 throughout, 0 after teardown
- `config.json` SHA-256: unchanged across the whole run
- Final teardown: `live_status=LIVE_IDLE`, 0 live threads,
  `mic_owner=None`, `whisper_child=0`, F8 flags clear
- Regression: full suite **152/152 OK** (17.1 s); ruff clean on the
  harness
- Artifacts: `%TEMP%\opencode\7d3_endurance_results.json`,
  `%TEMP%\opencode\7d3_run2.txt`

---

## Checkpoint rule

**7d.1:** satisfied — tests green, manual smoke PASS, `dc5c4aa`
committed (not pushed).

**7d.2:** implementation and synthetic tests complete; targeted 7d.1
smoke re-run executed as recorded above. Stop here and request review.
Commit the 7d.2 checkpoint only after explicit approval, staging
nothing but 7d.2 code/tests/docs — never `config.json`,
`whisper/whisper-vulkan`, `real_mic_validation*.py`. Push only on a
separate, explicit decision.
