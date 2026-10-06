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

    start(): CALIBRATION_DURATION_S ambient calibration window
        -> threshold = max(ABSOLUTE_FLOOR, ambient_rms * THRESHOLD_MULTIPLIER)
        -> UtteranceAssembler(energy_threshold=threshold)
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

4. Cancellation: setting whisper_engine.cancel_requested from the stop
   path is permitted only because test_whisper_cancel_lifecycle proves
   a fresh invocation resets the flag before spawning, so cancellation
   cannot leak into the next transcription. whisper_engine.py itself is
   never modified by 7b.

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

Step 3a content: adaptive energy gate. start() samples an ambient
window of CALIBRATION_DURATION_S through the capture after the capture
is acquired and before any thread is spawned, and derives the session
threshold as max(ABSOLUTE_FLOOR, ambient_rms * THRESHOLD_MULTIPLIER);
the UtteranceAssembler receives that value instead of a fixed
constant. ambient_rms is plain RMS over the collected int16 samples -
the same energy the assembler compares per 20 ms frame, and the window
is frame-aligned, so calibration and gating measure one quantity. The
calibration window is consumed here: it never reaches the assembler,
the commit queue, or the transcript. Diagnostics are count-only: the
log line "Live transcription calibration: ambient_rms=..., threshold="
plus stats()["energy_threshold"]; no raw audio, sample content, or
exception payload. Failure modes: the window cannot be filled before
the deadline ("calibration timeout") and a capture read error
("calibration failed"); both release the capture and leave no session
state. Deliberately absent from 7b.3a: quiet-window validation,
retry loops, spectral or neural VAD, automatic gain control, and
normalization. If the initial window contains speech the derived
threshold inflates toward the speech level; that accepted limitation
is pinned by a synthetic test and may gain validation or retry in a
later step. Unit tests: test_live_transcribe.py (synthetic windows
against a faked capture).

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
_transcript = []
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


def _worker_loop(stop_event, assembler, commits, capture):
    """Consume capture chunks into the assembler until stopped.

    Exit paths (worker never joins itself):
    - stop_event set: flush pending speech, terminate
    - unexpected failure: _active=False, stop_event set, log the
      exception TYPE only, flush, terminate; a later stop() performs
      the join and the capture release
    """
    global _active

    try:
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


def _transcribe_commit(commit):
    """Run one-shot Whisper over one committed window.

    The samples are written to a fresh WAV in TEMP_DIR (created on
    demand so a missing temp/ never fails a session) and the file is
    removed in finally, on success and on failure alike. Failures
    propagate to the transcription loop, which counts them; nothing
    about the audio or the text is logged here.
    """
    os.makedirs(TEMP_DIR, exist_ok=True)
    descriptor, wav_path = tempfile.mkstemp(suffix=".wav", dir=TEMP_DIR)
    os.close(descriptor)

    try:
        _write_wav(wav_path, commit.samples)
        return whisper_engine.whisper_transcribe(wav_path)
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
                text = _transcribe_commit(commit)
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


def _calibrate(capture):
    """Sample an ambient window and derive the session energy threshold.

    Reads whole capture chunks until CALIBRATION_DURATION_S of audio
    has been collected, then computes plain RMS over the collected
    int16 samples - the same energy the UtteranceAssembler compares
    per 20 ms frame (the window is frame-aligned, so both measure the
    same quantity). The session threshold is
    max(ABSOLUTE_FLOOR, ambient_rms * THRESHOLD_MULTIPLIER). The
    window is consumed here: it never reaches the assembler, the
    commit queue, or the transcript. Returns
    (ok, reason, threshold, ambient_rms); the only failure is a window
    that cannot be filled before the deadline. A CALIBRATION_DURATION_S
    of zero skips sampling and yields the floor threshold, which keeps
    synthetic session tests deterministic.
    """
    target = int(CALIBRATION_DURATION_S * SAMPLE_RATE)
    if target <= 0:
        return True, "calibrated", ABSOLUTE_FLOOR, 0.0

    collected = []
    total = 0
    deadline = (
        time.monotonic()
        + CALIBRATION_DURATION_S
        + CALIBRATION_TIMEOUT_SLACK_S
    )

    while total < target:
        if time.monotonic() > deadline:
            return False, "calibration timeout", 0, 0.0
        chunk = capture.read_chunk(timeout=WORKER_POLL_TIMEOUT)
        if chunk is None:
            continue
        collected.append(chunk)
        total += len(chunk)

    signal = np.concatenate(collected)
    ambient_rms = float(np.sqrt(np.mean(signal.astype(np.float64) ** 2)))
    threshold = int(max(ABSOLUTE_FLOOR, ambient_rms * THRESHOLD_MULTIPLIER))
    return True, "calibrated", threshold, ambient_rms


def start():
    """Start a live transcription session. Returns (ok, reason).

    Acquires the capture session, calibrates the ambient energy window
    (step 3a), resets the commit queue and the transcript buffer, and
    spawns LiveTranscribeWorker plus LiveTranscribeTranscription.
    Atomic under _lock: any calibration or spawn failure releases the
    capture, leaves no session state behind, and returns a plain
    reason.
    """
    global _worker, _transcription, _stop_event, _assembler, _commits
    global _capture, _active, _commits_dropped, _transcript
    global _previous_text, _consecutive_failures

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
            calibrated, reason, threshold, ambient_rms = _calibrate(capture)
        except Exception as error:  # noqa: BLE001 - release capture, keep (ok, reason)
            capture.stop()
            logger.error(
                f"Live transcription calibration failed: "
                f"{type(error).__name__}"
            )
            return False, "calibration failed"

        if not calibrated:
            capture.stop()
            logger.warning(f"Live transcription refused: {reason}")
            return False, reason

        logger.info(
            f"Live transcription calibration: ambient_rms={ambient_rms:.1f}, "
            f"threshold={threshold}"
        )

        if _commits is not None:
            remaining = _commits.qsize()
            if remaining:
                logger.warning(
                    f"Discarding {remaining} queued commits from "
                    f"previous session"
                )

        assembler = UtteranceAssembler(energy_threshold=threshold)
        commits = queue.Queue(maxsize=COMMIT_QUEUE_SIZE)
        stop_event = threading.Event()

        _capture = capture
        _assembler = assembler
        _commits = commits
        _stop_event = stop_event
        _commits_dropped = 0
        _transcript = []
        _previous_text = ""
        _consecutive_failures = 0

        worker = None
        transcription = None

        try:
            worker = threading.Thread(
                target=_worker_loop,
                args=(stop_event, assembler, commits, capture),
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
    -> release capture. While the transcription thread is alive its
    in-flight Whisper call is cancelled (whisper_engine resets the flag
    before the next spawn), so stop never waits out a full inference.
    The commits queue survives until the next start(), so audio
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
                whisper_engine.cancel_requested = True
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
    }
