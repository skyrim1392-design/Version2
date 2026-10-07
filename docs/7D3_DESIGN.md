# 7d.3 Design — Production Endurance / Soak Validation

> Authority: `docs/HANDOVER.md` is the single source of truth. This
> file is supporting detail; if they disagree, HANDOVER.md wins.

Last updated: 2026-10-07
Status: **design approved, harness implementation next**

---

## Objective

Prove production endurance of the 7c live-transcription pipeline and
the 7d lifecycle/UI boundary under repetition and load. 7d.3 changes
**no production code**; it only validates and documents.

## In scope

- Long-running live-transcription stability
- Repeated live start/stop/toggle cycles
- F8/live ownership contention and switching
- Whisper serialization/cancellation behavior under repetition
- Delivery exactly-once / ordering over long runs
- Shutdown/Exit under active recording, inference, and output
- Repeated config toggle/persistence cycles
- Detection of: leaked threads, leaked microphone ownership, orphaned
  `whisper-cli`, delivery drops, transcript duplication, stale session
  data, unreleased resources
- Documentation of measured endurance evidence

## Explicitly out of scope

Stale tray title, startup silent-retry, second-caller shutdown
hardening, tray redesign, new event infrastructure, VAD/AGC/audio
normalization, new hotkeys, agent/LLM layer, architectural changes.
Deferred unless a soak exposes them as actual correctness failures —
then: stop at the evidence, classify, return to a design gate.

## Acceptance criteria

Primary soak duration: **30 minutes** (roadmap direction).

| Test | Acceptance |
|---|---|
| Continuous live session | 30 min without unexpected stop |
| Live transcription | No uncaught worker exception |
| Delivery | 0 unexpected drops; exactly-once/in-order |
| Threads | No monotonically increasing thread count |
| Mic ownership | `"live"` while active, `None` after stop |
| Whisper children | No accumulation; 0 remaining after shutdown |
| F8 contention | F8 remains uncancelled; no cross-cancellation |
| Live cancellation | Live invocation cancelled cleanly |
| Repeated lifecycle | 100 start/stop cycles |
| Live/F8 contention | 50+ contention cycles |
| Toggle/config | 100 ON/OFF cycles, persistence correct |
| Shutdown under load | 10/10 clean shutdowns, no hang |
| Final teardown | 0 live workers, mic owner `None`, no Whisper child |
| Test regression | Full automated suite remains green |

Individual inference timing is **not** a pass/fail threshold; the
invariants are resource stability, serialization, correctness, clean
teardown.

## Environment strategy

1. Synthetic/hardware-free endurance for deterministic repetition and
   fault injection.
2. Real-mic soak where practical, shorter corroborating run.
3. Existing 7c/7d smoke harnesses remain the hardware evidence layer.

Lack of automated microphone injection is a documented **validation
limitation**, not a failed test.

## Evidence format

**Automated harness**
- `real_mic_validation_7d3.py` (or equivalent), untracked/local per
  D11.
- Machine-readable/parseable console summary.
- Exercises counters and resource invariants.

**Documentation** (`docs/TEST_STATUS.md` entry): date/time, commit
SHA, Windows/Python/environment, duration, cycle counts, results,
peak thread count, Whisper process observations, mic ownership
observations, failures/recoveries, final teardown state, known
limitations. Raw temp logs/artifacts are not committed unless
deliberate.

## Baseline snapshot (before run)

```text
HEAD
branch / origin sync
working-tree exclusions
Python version
test count
Ruff result
thread count
mic owner
Whisper process state
live supervisor state
live output state
```

Post-run record of the identical resource state; the comparison is the
**delta**, not merely "no crash".

## Pass condition

> 7d.3 passes only if the required endurance cycles complete and the
> final resource state is equivalent to the baseline, with zero
> unexplained delivery/cancellation/ownership failures and no
> orphaned Whisper processes.

A genuine production defect stops the phase: classify, return to a
design/implementation gate, rerun the affected endurance criteria.

## Commit boundary

- No production code changes merely to conduct 7d.3.
- Validation harnesses remain untracked.
- Documentation may carry the evidence.
- **No checkpoint commit until endurance results are reviewed.**
- Push remains independent.
