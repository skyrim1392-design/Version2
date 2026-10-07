"""Live transcription pipeline (Phase 7b).

Scope (locked): live_transcribe.py is the ONLY new production file in
7b. Unchanged: audio.py, live_capture.py, whisper_engine.py, main.py,
text_output.py, tray.py, app_state.py, recovery.py, config.json.

Import-safe: no stream, thread, queue, file, or config access happens at
import time, and live_capture is resolved only when a session starts, so
importing this module requires no audio stack. whisper_engine is
imported for its name only: no configuration is read and no Whisper
process is spawned until a commit is transcribed by the transcription
thread of an active session.

Pipeline:

    start(): robust ambient calibration (step 3b)
        -> up to CALIBRATION_TARGET_WINDOWS x CALIBRATION_DURATION_S
           windows, hard-capped at CALIBRATION_MAX_DURATION_S
        -> outlier windows (RMS > CALIBRATION_OUTLIER_RATIO x median)
           rejected, ambient = median of retained windows
        -> threshold = max(ABSOLUTE_FLOOR, ambient * THRESHOLD_MULTIPLIER)
        -> UtteranceAssembler(energy_threshold=threshold)
    calibration chunks (chronological, replayed exactly once)
        -> UtteranceAssembler -> commit queue (below)
    live_capture.read_chunk()
        -> LiveTranscribeWorker (step 2)
        -> UtteranceAssembler (below)
        -> bounded memory-only commit queue (audio only)
        -> LiveTranscribeTranscription (step 3)
        -> per-commit WAV written to temp/, deleted in finally
        -> one-shot whisper-cli invocation per committed window
        -> dedupe_prefix() against the previous segment (pre-roll only)
        -> memory-only transcript buffer (get_transcript())

Locked invariants:

1. Three-failure auto-stop (threading rule):

       worker detects 3rd consecutive failure
           |
           v
       set _active = False
       set stop_event
       exit worker loop
           |
           v
       worker terminates naturally

   stop() remains responsible for joining when called externally.
   No worker-thread self-join.

2. Forced-split pre-roll (8 s hard cap, 0.5 s pre-roll):

       window A = first 128000 samples
       window B = last 8000 samples of A + subsequent samples

   - the 8000 pre-roll samples are overlap, not new speech
   - had_pre_roll=True belongs to B; B is transcribed normally
   - B's text is deduplicated against the tail of the previously
     committed segment
   - the pre-roll does not count toward B's 8 s cap; a commit carrying
     no samples beyond its pre-roll is discarded, so pure overlap never
     reaches Whisper
   - the silence gate is evaluated before the cap on the frame where
     both would trigger, so an utterance that ends exactly at the cap
     is committed as silence-gated speech, never force-split

3. dedupe_prefix() is deterministic and conservative:

   longest leading substring of `new` that equals the tail of
   `previous`, only when that overlap is >= MIN_DEDUPE_OVERLAP (12)
   characters; otherwise `new` is returned unchanged. Exact character
   comparison only: case-sensitive, punctuation and whitespace
   significant, no normalization, no fuzzy matching.

4. Cancellation (7c.3, M1): there is no global cancellation flag. The
   transcription path forwards cancel_event=stop_event into
   whisper_engine.whisper_transcribe(), which serializes every
   invocation behind its internal lock and polls only the event it was
   given; a live stop therefore kills the live invocation alone and an
   F8 call (cancel_event=None) is never affected. The stop path only
   sets stop_event and joins. test_whisper_cancel_lifecycle proves the
   serialization and the per-invocation ownership.

5. Calibration replay (step 3b): every chunk read during calibration is
   buffered and replayed through the normal UtteranceAssembler path -
   exactly once, in chronological order, before the worker reads any
   live chunk. Ownership is unchanged: one assembler per session, fed
   only from the worker thread; _calibrate() never touches the
   assembler. Replay must not gate on stop_event mid-stream (the flush
   contract in the worker exit paths still applies afterwards).

Privacy contract: no transcript text enters the logger, directly or
indirectly. Logging is limited to reason/timing/length; exceptions are
reported by type name only, never message text. test_live_transcribe.py
statically audits this file as it grows.

Step 1 content: constants, Commit, dedupe_prefix(), and the pure
UtteranceAssembler (no I/O, no threads, no global state; single-thread
ownership by the capture worker).

Step 2 content: session lifecycle on top of live_capture (start, stop,
is_active, read_commit, stats), the LiveTranscribeWorker thread, and the
bounded memory-only commit queue. start() acquires the capture session;
stop() releases it. Commits are audio only: no text is produced, no
Whisper call exists yet, and no output path is touched. Unit tests:
test_live_transcribe.py (faked capture, no microphone, no audio stack).

Step 3 content: the LiveTranscribeTranscription thread, per-commit WAV
writing (tempfile.mkstemp in temp/, removed in finally on success and
on failure), the one-shot whisper_engine.whisper_transcribe() call per
window, dedupe_prefix() applied only to windows carrying pre-roll, the
memory-only transcript buffer behind get_transcript(), and the
three-consecutive-failure auto-stop. Nothing about the audio or the
text is logged; only reason, duration, character count, and exception
type names. Unit tests: test_live_transcribe.py (faked capture and a
faked whisper engine: no microphone, no model, no subprocess).

Step 3a/3b content: adaptive energy gate (3a, 7b.3a) made robust by the
locked calibration design (3b, 7b.4). 7b.4 work items: 1 = robust
median-of-retained estimator, 2 = bounded extension to the hard cap,
3 = calibration_contaminated flag, 4 = calibration replay buffer,
5 = replay ownership invariant (invariant 5). start() samples windows
of CALIBRATION_DURATION_S through the capture after the capture is
acquired and before any thread is spawned, targeting
CALIBRATION_TARGET_WINDOWS usable windows (about 3 s) within a hard
cap of CALIBRATION_MAX_DURATION_S (about 5 s). Windows are classified
against the median of the sampled set: an RMS above
CALIBRATION_OUTLIER_RATIO x median is a clear high-energy outlier and
is rejected (synthetic tests pin the boundary); ambient_rms is the
median of the retained windows and the threshold is
max(ABSOLUTE_FLOOR, ambient_rms * THRESHOLD_MULTIPLIER) - the
UtteranceAssembler receives that value. ambient_rms and frame RMS are
the same quantity (plain RMS over int16 samples, frame-aligned). At
the hard cap - or on a capture stall after at least one window was
sampled - a best-available estimate is used and
calibration_contaminated=True is recorded (diagnostic only: fewer
than CALIBRATION_TARGET_WINDOWS usable windows); a contaminated but
usable calibration still starts the session. Calibration audio is
never discarded: every chunk is replayed through the assembler by the
worker, in order, before live audio (invariant 5). Diagnostics are
count-only: the log line "Live transcription calibration:
windows=..., rejected=..., usable=..., ambient_rms=..., threshold="
plus stats()["energy_threshold"], stats()["calibration_windows"],
["calibration_rejected"], ["calibration_usable"],
["calibration_contaminated"]; no raw audio, sample content, or
exception payload. Failure modes: no window sampled before the
deadline ("calibration timeout") and a capture read error
("calibration failed"); both release the capture and leave no session
state. Deliberately absent from 7b.4: quiet-window validation, retry
loops, spectral or neural VAD, automatic gain control, and
normalization; THRESHOLD_MULTIPLIER stays 1.75 unless measurement
demands re-derivation (7b.4 D4). Unit tests: test_live_transcribe.py
(synthetic windows against a faked capture).

Step 7c.1 content: the ordered read-only cursor API over the memory
transcript - create_cursor() and read_cursor(cursor). Exactly-once
consumption happens only by cursor advancement (the reader stores the
returned cursor), session boundaries are detected by a session id that
increments on every successful start(), a stale cursor realigns to
(session, 0) and its unread old-session segments are dropped, and empty
strings are never exposed. One transcript store only: the cursor reads
the existing _transcript list, get_transcript() is unchanged, and no
output, delivery, text_output, tray, or app_state code exists here
(live_output.py arrives in 7c.2). Unit tests: test_live_transcribe.py
(direct segment appends plus a real session boundary).

Worker exit contract (no worker-thread self-join anywhere):
    - stop() sets stop_event; the worker flushes pending speech into
      the commit queue, then terminates naturally; stop() performs the
      join (WORKER_JOIN_TIMEOUT) and the capture release
    - the transcription thread follows the same shape: three
      consecutive transcription failures set _active=False and
      stop_event, log the count only, and exit; stop() joins it first
      (TRANSCRIPTION_JOIN_TIMEOUT, requesting Whisper cancellation
      while it is alive) and then the worker, so a cancelled or
      unfinished window never blocks the capture release
    - unexpected worker failure: _active=False, stop_event set, thread
      exits; a later stop() still performs the join and release
    - the commits queue survives stop() until the next start(), so
      audio committed at shutdown stays readable
"""

import os
import queue
import tempfile
import threading
import time
import wave
from dataclasses import dataclass

import numpy as np

import logger
import whisper_engine

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 16000

FRAME_SAMPLES = 320

SILENCE_GATE_MS = 700
MIN_UTTERANCE_S = 0.3
MAX_UTTERANCE_S = 8.0
PRE_ROLL_S = 0.5
MIN_DEDUPE_OVERLAP = 12
ABSOLUTE_FLOOR = 500
CALIBRATION_DURATION_S = 1.0
CALIBRATION_TIMEOUT_SLACK_S = 2.0
CALIBRATION_TARGET_WINDOWS = 3
CALIBRATION_MAX_DURATION_S = 5.0
CALIBRATION_OUTLIER_RATIO = 2.0
THRESHOLD_MULTIPLIER = 1.75

SILENCE_GATE_SAMPLES = SAMPLE_RATE * SILENCE_GATE_MS // 1000
MIN_UTTERANCE_SAMPLES = SAMPLE_RATE * 3 // 10
MAX_UTTERANCE_SAMPLES = SAMPLE_RATE * 8
PRE_ROLL_SAMPLES = SAMPLE_RATE // 2

REASON_SILENCE_GATE = "silence_gate"
REASON_FORCED_SPLIT = "forced_split"
REASON_FLUSH = "flush"

COMMIT_QUEUE_SIZE = 8
COMMIT_DROP_LOG_EVERY = 20
WORKER_POLL_TIMEOUT = 0.1
WORKER_JOIN_TIMEOUT = 5.0
TRANSCRIPTION_JOIN_TIMEOUT = 10.0
CANCEL_POLL_INTERVAL = 0.05
CONSECUTIVE_FAILURE_LIMIT = 3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, "temp")


@dataclass(frozen=True, eq=False)
class Commit:
    """One committed utterance window ready for transcription.

    samples: int16 mono audio at SAMPLE_RATE.
    had_pre_roll: True when the window begins with pre-roll overlap
        taken from the tail of the previously committed window (a
        forced-split continuation); such windows must have their text
        deduplicated against that previous segment.
    reason: why the window closed (REASON_* constant only).
    """

    samples: np.ndarray
    had_pre_roll: bool
    reason: str

    @property
    def length_samples(self):
        return len(self.samples)

    @property
    def duration_s(self):
        return self.length_samples / SAMPLE_RATE


@dataclass(frozen=True, eq=False)
class Calibration:
    """Result of the robust ambient calibration (step 3b).

    ok/reason: session-start verdict; only a capture stall before the
        first usable window fails ("calibration timeout").
    threshold: derived session gate, max(ABSOLUTE_FLOOR,
        ambient_rms * THRESHOLD_MULTIPLIER).
    ambient_rms: median RMS of the retained (non-outlier) windows.
    windows: total 1-second windows sampled.
    rejected: windows discarded as clear high-energy outliers.
    usable: retained windows after the final classification.
    contaminated: True when fewer than CALIBRATION_TARGET_WINDOWS
        usable windows existed at the hard cap (work item 3); a
        diagnostic only - a contaminated-but-usable calibration still
        starts the session.
    replay: every calibration chunk, chronological, int16 mono. The
        worker feeds these through the normal UtteranceAssembler before
        reading live audio: calibration audio is retained, never
        discarded. Count/diagnostic fields only here - no audio content
        is ever logged.
    """

    ok: bool
    reason: str
    threshold: int
    ambient_rms: float
    windows: int
    rejected: int
    usable: int
    contaminated: bool
    replay: tuple


def dedupe_prefix(previous, new):
    """Deterministic tail-overlap trim between two committed texts.

    Returns the longest leading substring of `new` removed such that
    the removed part equals the tail of `previous` AND is at least
    MIN_DEDUPE_OVERLAP characters. If no such overlap exists, `new` is
    returned unchanged.

    Exact character comparison: case-sensitive, punctuation and
    whitespace significant, no normalization, no fuzzy matching. A
    fully contained `new` reduces to the empty string.
    """
    if not previous or not new:
        return new

    max_overlap = min(len(previous), len(new))

    for length in range(max_overlap, MIN_DEDUPE_OVERLAP - 1, -1):
        if previous.endswith(new[:length]):
            return new[length:]

    return new


class UtteranceAssembler:
    """Pure sample-to-commit assembly with energy-only gating.

    feed() consumes int16 mono audio of any length and returns the
    commits it closes, in order. State machine:

    - leading silence before speech is dropped and never committed,
      so silence before speech produces no inference
    - an utterance starts at the first frame whose RMS exceeds
      energy_threshold (strictly greater)
    - mid-utterance pauses shorter than SILENCE_GATE_SAMPLES stay in
      the utterance; SILENCE_GATE_SAMPLES of consecutive silence closes
      it, trimming the trailing silence run from the commit
    - a closed window shorter than MIN_UTTERANCE_SAMPLES is discarded
    - at MAX_UTTERANCE_SAMPLES of captured (non pre-roll) audio the
      window force-splits: the full cap window commits as A, and the
      next window starts with the last PRE_ROLL_SAMPLES of A as
      pre-roll (had_pre_roll=True), which does not count toward the
      new window's cap
    - flush() closes pending speech with REASON_FLUSH under the same
      trim/minimum rules and drops the sub-frame carry

    Not thread-safe: one assembler per worker. No I/O, no threads, no
    global state.
    """

    def __init__(self, energy_threshold=ABSOLUTE_FLOOR):
        self.energy_threshold = int(energy_threshold)
        self._carry = np.empty(0, dtype=np.int16)
        self._speaking = False
        self._buffer = []
        self._buffer_len = 0
        self._pre_roll = 0
        self._trailing_silence = 0

    @property
    def is_speaking(self):
        return self._speaking

    @property
    def pending_samples(self):
        return self._buffer_len

    def feed(self, chunk):
        """Consume one chunk; returns the list of commits it closes."""
        data = np.asarray(chunk)

        if data.ndim != 1:
            raise ValueError("UtteranceAssembler.feed expects mono (1-D) audio")

        if data.dtype != np.int16:
            raise ValueError("UtteranceAssembler.feed expects int16 audio")

        data = data.copy()

        commits = []
        offset = 0
        total = len(data)

        while offset < total:
            take = min(FRAME_SAMPLES - len(self._carry), total - offset)
            segment = data[offset:offset + take]

            if len(self._carry) > 0:
                self._carry = np.concatenate([self._carry, segment])
            else:
                self._carry = segment

            offset += take

            if len(self._carry) < FRAME_SAMPLES:
                break

            commit = self._process_frame(self._carry)
            self._carry = np.empty(0, dtype=np.int16)

            if commit is not None:
                commits.append(commit)

        return commits

    def flush(self):
        """End of stream: close pending speech, drop the frame carry."""
        self._carry = np.empty(0, dtype=np.int16)

        if not self._speaking:
            return []

        commit = self._close(REASON_FLUSH)
        return [commit] if commit is not None else []

    def reset(self):
        """Drop all partial state (device loss, session restart)."""
        self._carry = np.empty(0, dtype=np.int16)
        self._speaking = False
        self._buffer = []
        self._buffer_len = 0
        self._pre_roll = 0
        self._trailing_silence = 0

    def _frame_voiced(self, frame):
        squared = frame.astype(np.float64) ** 2
        rms = float(np.sqrt(np.mean(squared)))
        return rms > self.energy_threshold

    def _process_frame(self, frame):
        voiced = self._frame_voiced(frame)

        if not self._speaking:
            if not voiced:
                return None

            self._speaking = True
            self._buffer = [frame]
            self._buffer_len = FRAME_SAMPLES
            self._pre_roll = 0
            self._trailing_silence = 0
            return None

        self._buffer.append(frame)
        self._buffer_len += FRAME_SAMPLES

        if voiced:
            self._trailing_silence = 0
        else:
            self._trailing_silence += FRAME_SAMPLES

        if self._trailing_silence >= SILENCE_GATE_SAMPLES:
            return self._close(REASON_SILENCE_GATE)

        if self._buffer_len - self._pre_roll >= MAX_UTTERANCE_SAMPLES:
            return self._close(REASON_FORCED_SPLIT)

        return None

    def _close(self, reason):
        pre_roll = self._pre_roll

        if reason == REASON_FORCED_SPLIT:
            window = np.concatenate(self._buffer)
            tail = window[-PRE_ROLL_SAMPLES:].copy()
            self._buffer = [tail]
            self._buffer_len = PRE_ROLL_SAMPLES
            self._pre_roll = PRE_ROLL_SAMPLES
            return Commit(
                samples=window,
                had_pre_roll=pre_roll > 0,
                reason=reason,
            )

        kept_len = self._buffer_len - min(
            self._trailing_silence, self._buffer_len
        )
        samples = np.concatenate(self._buffer)[:kept_len]
        self._reset_utterance()

        if kept_len < MIN_UTTERANCE_SAMPLES:
            return None

        if kept_len <= pre_roll:
            return None

        return Commit(samples=samples, had_pre_roll=pre_roll > 0, reason=reason)

    def _reset_utterance(self):
        self._speaking = False
        self._buffer = []
        self._buffer_len = 0
        self._pre_roll = 0
        self._trailing_silence = 0


_lock = threading.Lock()
_worker = None
_transcription = None
_stop_event = None
_assembler = None
_commits = None
_capture = None
_active = False
_commits_dropped = 0
_calibration_windows = 0
_calibration_rejected = 0
_calibration_usable = 0
_calibration_contaminated = False
_transcript = []
_transcript_session = 0
_previous_text = ""
_consecutive_failures = 0


def _import_capture():
    """Resolve live_capture lazily so importing this module never
    requires the audio stack (sounddevice/scipy). Sessions acquire it.
    """
    import live_capture

    return live_capture


def _offer_commits(commits, commit):
    """Atomically enqueue a commit with drop-oldest overflow.

    Same bounded discipline as live_capture: a stalled consumer loses
    the OLDEST committed window (fresh audio has priority), the drop
    count is logged, never any audio or text content.
    """
    global _commits_dropped

    try:
        commits.put_nowait(commit)
        return
    except queue.Full:
        pass

    try:
        commits.get_nowait()
    except queue.Empty:
        pass

    try:
        commits.put_nowait(commit)
    except queue.Full:
        pass

    _commits_dropped += 1
    dropped = _commits_dropped

    if dropped == 1 or dropped % COMMIT_DROP_LOG_EVERY == 0:
        logger.warning(
            f"Live transcription commit overflow: dropped={dropped}"
        )


def _worker_loop(stop_event, assembler, commits, capture, replay=()):
    """Consume capture chunks into the assembler until stopped.

    Step 3b replay contract (work item 5): `replay` holds every
    calibration chunk in capture order; the worker feeds them through
    the assembler exactly once, before reading any live chunk, so
    calibration audio is never discarded. Replay is not gated on
    stop_event (at most CALIBRATION_MAX_DURATION_S of audio, processed
    before the first read); the stop/flush exit paths below are
    unchanged.

    Exit paths (worker never joins itself):
    - stop_event set: flush pending speech, terminate
    - unexpected failure: _active=False, stop_event set, log the
      exception TYPE only, flush, terminate; a later stop() performs
      the join and the capture release
    """
    global _active

    try:
        for chunk in replay:
            for commit in assembler.feed(chunk):
                _offer_commits(commits, commit)

        while not stop_event.is_set():
            chunk = capture.read_chunk(timeout=WORKER_POLL_TIMEOUT)
            if chunk is None:
                continue

            for commit in assembler.feed(chunk):
                _offer_commits(commits, commit)

    except Exception as error:  # noqa: BLE001 - log type, auto-stop, flush
        _active = False
        stop_event.set()
        logger.error(
            f"Live transcription worker failed: {type(error).__name__}"
        )

    finally:
        for commit in assembler.flush():
            _offer_commits(commits, commit)


def _write_wav(path, samples):
    """Write one committed window as mono 16 kHz int16 PCM."""
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(samples.tobytes())


def _transcribe_commit(commit, stop_event):
    """Run one-shot Whisper over one committed window.

    The session stop event is forwarded as cancel_event (M1, 7c.3) so
    a live stop kills this invocation inside the engine without a
    global flag. The samples are written to a fresh WAV in TEMP_DIR
    (created on demand so a missing temp/ never fails a session) and
    the file is removed in finally, on success and on failure alike.
    Failures propagate to the transcription loop, which counts them;
    nothing about the audio or the text is logged here.
    """
    os.makedirs(TEMP_DIR, exist_ok=True)
    descriptor, wav_path = tempfile.mkstemp(suffix=".wav", dir=TEMP_DIR)
    os.close(descriptor)

    try:
        _write_wav(wav_path, commit.samples)
        return whisper_engine.whisper_transcribe(
            wav_path, cancel_event=stop_event
        )
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass


def _transcription_loop(stop_event, commits):
    """Consume committed windows into the memory-only transcript.

    Exit paths (the transcription thread never joins itself):
    - stop_event set before the next commit is taken: exit; a commit
      taken after the stop is offered back to the queue so it stays
      readable after stop()
    - three consecutive transcription failures: _active=False,
      stop_event set, the count only is logged, the thread exits;
      stop() performs the join later
    - unexpected failure: _active=False, stop_event set, the exception
      TYPE only is logged, the thread exits
    """
    global _active, _consecutive_failures, _previous_text

    try:
        while not stop_event.is_set():
            try:
                commit = commits.get(timeout=WORKER_POLL_TIMEOUT)
            except queue.Empty:
                continue

            if stop_event.is_set():
                _offer_commits(commits, commit)
                break

            try:
                text = _transcribe_commit(commit, stop_event)
            except Exception as error:  # noqa: BLE001 - count, log type only
                _consecutive_failures += 1
                failures = _consecutive_failures
                logger.error(
                    f"Live transcription failed: {type(error).__name__} "
                    f"consecutive={failures}"
                )

                if failures >= CONSECUTIVE_FAILURE_LIMIT:
                    _active = False
                    stop_event.set()
                    logger.error(
                        f"Live transcription auto-stopped: "
                        f"failures={failures}"
                    )
                continue

            _consecutive_failures = 0

            if commit.had_pre_roll:
                text = dedupe_prefix(_previous_text, text)

            characters = len(text)
            if characters:
                _transcript.append(text)
                _previous_text = text

            logger.info(
                f"Live transcription window: reason={commit.reason}, "
                f"duration={commit.duration_s:.2f}s, "
                f"characters={characters}"
            )

    except Exception as error:  # noqa: BLE001 - auto-stop, log type only
        _active = False
        stop_event.set()
        logger.error(
            f"Live transcription loop failed: {type(error).__name__}"
        )


def _classify_calibration(values):
    """Classify sampled calibration windows against the median center.

    Work item 1 (7b.4 D1): the robust center is the median of the
    windows sampled so far; a window whose RMS exceeds
    CALIBRATION_OUTLIER_RATIO x that center is a clear high-energy
    outlier and is rejected. The boundary is inclusive: a window
    exactly at the limit is retained. Returns (retained, rejected
    count). The ratio value itself is not derived here - synthetic
    tests establish it from representative cases (for example the
    measured [837, 958, 2899] triple retains [837, 958]).

    Pure: no capture, no clock, no logging.
    """
    center = float(np.median(values))
    limit = CALIBRATION_OUTLIER_RATIO * center
    retained = [value for value in values if value <= limit]
    return retained, len(values) - len(retained)


def _calibrate(capture):
    """Robust ambient calibration (step 3b, 7b.4 design D1/D2).

    Reads whole capture chunks into CALIBRATION_DURATION_S windows
    until CALIBRATION_TARGET_WINDOWS usable windows exist (about 3 s)
    or the hard cap CALIBRATION_MAX_DURATION_S is reached (about 5 s,
    best-available estimate + contaminated flag, D2, work items 2/3).
    Each sampled window is classified by _classify_calibration (work
    item 1): RMS above CALIBRATION_OUTLIER_RATIO x median is a clear
    high-energy outlier and is rejected (the boundary is pinned by
    synthetic tests, not asserted here). ambient is the median of the
    retained windows; threshold is
    max(ABSOLUTE_FLOOR, ambient * THRESHOLD_MULTIPLIER).

    Returns a Calibration. Every chunk read - including chunks holding
    rejected windows - is kept in chronological order in
    Calibration.replay for the worker to feed through the assembler
    before live audio (invariant 5, work item 4/5): calibration audio
    is retained, never discarded, and this function never touches the
    assembler (single worker ownership). The only failure is the
    deadline passing before a single window can be filled
    ("calibration timeout"); a deadline reached with one or more
    windows falls through to the best-available estimate and the
    contaminated flag (work items 2/3). CALIBRATION_TARGET_WINDOWS or
    CALIBRATION_DURATION_S of zero skips sampling and yields the floor
    threshold, which keeps synthetic session tests deterministic.
    """
    target = int(CALIBRATION_TARGET_WINDOWS)
    window_samples = int(CALIBRATION_DURATION_S * SAMPLE_RATE)
    if target <= 0 or window_samples <= 0:
        return Calibration(
            True, "calibrated", ABSOLUTE_FLOOR, 0.0, 0, 0, 0, False, ()
        )

    max_windows = max(
        target, round(CALIBRATION_MAX_DURATION_S / CALIBRATION_DURATION_S)
    )
    deadline = (
        time.monotonic()
        + CALIBRATION_MAX_DURATION_S
        + CALIBRATION_TIMEOUT_SLACK_S
    )
    replay = []
    rms_values = []
    pending = None

    while True:
        if rms_values:
            retained, _ = _classify_calibration(rms_values)
            if len(retained) >= target or len(rms_values) >= max_windows:
                break
        if time.monotonic() > deadline:
            if not rms_values:
                return Calibration(
                    False, "calibration timeout", 0, 0.0, 0, 0, 0, False, ()
                )
            break
        chunk = capture.read_chunk(timeout=WORKER_POLL_TIMEOUT)
        if chunk is None:
            continue
        replay.append(chunk)
        pending = chunk if pending is None else np.concatenate([pending, chunk])
        while len(pending) >= window_samples:
            window = pending[:window_samples]
            pending = pending[window_samples:]
            rms = float(np.sqrt(np.mean(window.astype(np.float64) ** 2)))
            rms_values.append(rms)
            if len(rms_values) >= max_windows:
                break

    retained, rejected = _classify_calibration(rms_values)
    ambient = float(np.median(retained))
    threshold = int(max(ABSOLUTE_FLOOR, ambient * THRESHOLD_MULTIPLIER))
    return Calibration(
        True,
        "calibrated",
        threshold,
        ambient,
        len(rms_values),
        rejected,
        len(retained),
        len(retained) < target,
        tuple(replay),
    )


def start():
    """Start a live transcription session. Returns (ok, reason).

    Acquires the capture session, runs the robust ambient calibration
    (step 3b), resets the commit queue and the transcript buffer, and
    spawns LiveTranscribeWorker plus LiveTranscribeTranscription. The
    calibration chunks are handed to the worker for replay through the
    assembler before any live audio. Atomic under _lock: any
    calibration or spawn failure releases the capture, leaves no
    session state behind, and returns a plain reason.
    """
    global _worker, _transcription, _stop_event, _assembler, _commits
    global _capture, _active, _commits_dropped, _transcript
    global _transcript_session, _previous_text, _consecutive_failures
    global _calibration_windows, _calibration_rejected
    global _calibration_usable, _calibration_contaminated

    with _lock:
        if _active:
            return False, "already active"

        if _worker is not None or _transcription is not None:
            return False, "stop required"

        try:
            capture = _import_capture()
        except Exception as error:  # noqa: BLE001 - degrade to a reason
            logger.error(
                f"Live transcription capture unavailable: "
                f"{type(error).__name__}"
            )
            return False, "capture unavailable"

        ok, reason = capture.start()
        if not ok:
            logger.warning(f"Live transcription refused: {reason}")
            return False, reason

        try:
            calibration = _calibrate(capture)
        except Exception as error:  # noqa: BLE001 - release capture, keep (ok, reason)
            capture.stop()
            logger.error(
                f"Live transcription calibration failed: "
                f"{type(error).__name__}"
            )
            return False, "calibration failed"

        if not calibration.ok:
            capture.stop()
            logger.warning(f"Live transcription refused: {calibration.reason}")
            return False, calibration.reason

        logger.info(
            f"Live transcription calibration: windows={calibration.windows}, "
            f"rejected={calibration.rejected}, usable={calibration.usable}, "
            f"ambient_rms={calibration.ambient_rms:.1f}, "
            f"threshold={calibration.threshold}"
        )

        if _commits is not None:
            remaining = _commits.qsize()
            if remaining:
                logger.warning(
                    f"Discarding {remaining} queued commits from "
                    f"previous session"
                )

        assembler = UtteranceAssembler(
            energy_threshold=calibration.threshold
        )
        commits = queue.Queue(maxsize=COMMIT_QUEUE_SIZE)
        stop_event = threading.Event()

        _capture = capture
        _assembler = assembler
        _commits = commits
        _stop_event = stop_event
        _commits_dropped = 0
        _calibration_windows = calibration.windows
        _calibration_rejected = calibration.rejected
        _calibration_usable = calibration.usable
        _calibration_contaminated = calibration.contaminated
        _transcript = []
        _transcript_session += 1
        _previous_text = ""
        _consecutive_failures = 0

        worker = None
        transcription = None

        try:
            worker = threading.Thread(
                target=_worker_loop,
                args=(
                    stop_event,
                    assembler,
                    commits,
                    capture,
                    calibration.replay,
                ),
                name="LiveTranscribeWorker",
                daemon=True,
            )
            transcription = threading.Thread(
                target=_transcription_loop,
                args=(stop_event, commits),
                name="LiveTranscribeTranscription",
                daemon=True,
            )
            worker.start()
            transcription.start()
        except Exception as error:  # noqa: BLE001 - atomic start rollback
            stop_event.set()
            for thread in (worker, transcription):
                if thread is not None and thread.ident is not None:
                    thread.join(timeout=WORKER_JOIN_TIMEOUT)
            _capture = None
            _assembler = None
            _commits = None
            _stop_event = None
            capture.stop()
            logger.error(
                f"Live transcription start failed: {type(error).__name__}"
            )
            return False, "start failed"

        _worker = worker
        _transcription = transcription
        _active = True

        logger.info("Live transcription started")
        return True, "started"


def stop():
    """Stop the session (idempotent). Returns (ok, reason).

    Order: set stop_event -> transcription thread cancels/finishes and
    exits -> worker flushes pending speech and exits -> external joins
    -> release capture. The stop event is passed into the engine as
    cancel_event, so while the transcription thread is alive its
    in-flight Whisper call is killed there (per-invocation
    cancellation, M1) and stop never waits out a full inference. The
    commits queue survives until the next start(), so audio
    committed at shutdown stays readable. If a join times out, session
    state is kept (stop() may be retried) rather than risking a second
    worker or a second transcription thread.
    """
    global _worker, _transcription, _stop_event, _assembler, _capture
    global _active

    with _lock:
        if not _active and _worker is None and _transcription is None:
            return True, "inactive"

        if _stop_event is not None:
            _stop_event.set()

        transcription = _transcription
        if transcription is not None:
            deadline = time.monotonic() + TRANSCRIPTION_JOIN_TIMEOUT
            while transcription.is_alive() and time.monotonic() < deadline:
                transcription.join(timeout=CANCEL_POLL_INTERVAL)
            if transcription.is_alive():
                logger.error(
                    "Live transcription transcription did not stop in time"
                )
                return False, "transcription timeout"
            _transcription = None

        worker = _worker
        if worker is not None:
            worker.join(timeout=WORKER_JOIN_TIMEOUT)
            if worker.is_alive():
                logger.error("Live transcription worker did not stop in time")
                return False, "worker timeout"
            _worker = None

        _stop_event = None
        _assembler = None
        _active = False

        capture = _capture
        _capture = None
        if capture is not None:
            capture.stop()

        logger.info("Live transcription stopped")
        return True, "stopped"


def is_active():
    return _active


def read_commit(timeout=1.0):
    """Consumer API: next committed window (audio), or None."""
    commits = _commits
    if commits is None:
        return None

    try:
        return commits.get(timeout=timeout)
    except queue.Empty:
        return None


def get_transcript():
    """Consumer API: copy of the memory-only transcript segments.

    Segments are the text appended so far in commit order (after
    pre-roll dedupe). The buffer itself is never exposed, and the
    transcript survives stop() until the next start() resets it.
    """
    return list(_transcript)


def create_cursor():
    """Consumer API: fresh cursor positioned at the transcript head.

    Returns a (session_id, index) cursor for read_cursor(): session_id
    identifies the session whose transcript is currently stored and
    index 0 is the oldest unread segment. A cursor created before
    start() still works - read_cursor() detects the stale session id
    and realigns it.
    """
    return (_transcript_session, 0)


def read_cursor(cursor):
    """Consumer API: read exactly one segment at the cursor position.

    Returns ((session_id, next_index), text) for the next non-empty
    segment, or None when no unread segment is available. Ordered and
    exactly-once: advancement happens only through the returned cursor,
    so re-reading with an unchanged cursor yields nothing new, and an
    empty string is skipped, never returned.

    Session boundary: when the cursor's session_id no longer matches
    the stored session (start() reset the transcript), the cursor
    realigns to (current_session, 0) and stale unread old-session
    segments are dropped - a segment is returned only if the session
    id stayed unchanged for the whole read, otherwise the read
    restarts on the current session. Text goes to the caller only;
    nothing about it is logged. Single-list reads: appends are atomic
    list operations and start() assigns fresh state as a unit, so a
    racing reader either finishes on the old session or restarts on
    the new one.
    """
    session, index = cursor

    while True:
        current_session = _transcript_session

        if session != current_session:
            session = current_session
            index = 0

        transcript = _transcript
        text = None

        while index < len(transcript):
            candidate = transcript[index]
            index += 1

            if candidate:
                text = candidate
                break

        if _transcript_session != session:
            continue

        if text is None:
            return None

        return (session, index), text


def stats():
    return {
        "active": _active,
        "worker_alive": _worker is not None and _worker.is_alive(),
        "transcription_alive": (
            _transcription is not None and _transcription.is_alive()
        ),
        "capture_active": _capture is not None and _capture.is_active(),
        "commits_queued": _commits.qsize() if _commits is not None else 0,
        "commits_dropped": _commits_dropped,
        "assembler_pending": (
            _assembler.pending_samples if _assembler is not None else 0
        ),
        "transcript_segments": len(_transcript),
        "consecutive_failures": _consecutive_failures,
        "energy_threshold": (
            _assembler.energy_threshold
            if _assembler is not None
            else ABSOLUTE_FLOOR
        ),
        "calibration_windows": _calibration_windows,
        "calibration_rejected": _calibration_rejected,
        "calibration_usable": _calibration_usable,
        "calibration_contaminated": _calibration_contaminated,
    }
