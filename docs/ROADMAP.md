# Roadmap

> Authority: `docs/HANDOVER.md` is the single source of truth. This file is supporting detail; if they disagree, HANDOVER.md wins.

Context: `release_roadmap.md` (repository root) holds the long-term
release vision (backend abstraction, device discovery, model
management, packaging, release matrix). This file holds the near-term
phase plan for the current branch.

Last updated: 2026-10-07

---

## Near term

| Phase | Scope | Entry condition |
|---|---|---|
| **7d.1** | Supervisor lifecycle, F8/live exclusion, tray Exit fix, live menu, `live_status` rendering | committed `dc5c4aa`, verified (tests + manual smoke) |
| **7d.2** | Further production hardening (recovery paths, error surfacing) | 7d.1 committed **and** verified |
| **7d.3** | Endurance / soak validation | 7d.2 approved |
| **8** | Agent layer | 7d series complete |

**7d.2 must not start until the 7d.1 checkpoint is committed and
verified.**

**7d.2 status:** approved scope implemented (tray rendering coverage,
shutdown quiescence + Whisper orphan handling, `exit_application`
teardown, plus tests and documentation); awaiting the implementation /
test-review gate. Deferred within 7d.2 by decision: stale tray title,
startup restore retry, second-caller shutdown hardening (listed under
"Known deferred items").

---

## Later (from `release_roadmap.md`)

- Backend abstraction: `AUTO / CPU / VULKAN / CUDA` selection
- GPU/device discovery surfaced in the tray
- Model management: model / backend / device / language / task as
  separate concepts
- Text output polish (clipboard + Ctrl+V, Persian/Unicode correctness)
- Tray as the real UI (disabled items instead of errors)
- Diagnostics panel from existing microphone measurements
- Configuration safety (bad config -> safe defaults, never a traceback)
- Packaging (`VoiceAssistant.exe` + runtime + model download)
- Release test matrix (CPU / AMD / NVIDIA, failure-injection list)

---

## Known deferred items

- Pre-existing lint findings outside 7d.1 (see `TEST_STATUS.md`)
- Dedicated tray icon for `SUSPECT` state (currently reuses `error.png`)
- `.gitmodules` mapping for `whisper/whisper-vulkan` is missing
  (`git submodule status` errors); resolve before any submodule-based
  workflow is assumed
- **Stale tray title (7d.2, deferred):** after Exit the status loop has
  stopped, so the already-removed icon object can still report its last
  title (for example `... - LIVE_LISTENING`). Cosmetic only — the icon
  itself is gone from the tray. No production change.
- **Startup restore retry (7d.2, deferred):** a persisted
  `live_mode_enabled=true` causes a startup start attempt on **every**
  launch; a failed attempt is not rewritten and is not retried within
  the session. Current behaviour kept; documented in `DECISIONS.md` D8.
- **Second-caller shutdown semantics (7d.2, deferred):** the first
  caller owns teardown and later callers wait for it, then return
  without repeating anything. Documented in `DECISIONS.md` D4; no
  hardening unless implementation uncovers evidence that it is
  necessary.
