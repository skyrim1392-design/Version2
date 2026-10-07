"""Unit tests for live_transcribe (Phase 7b, steps 1-3, 3a, 3b).

Covers the amended 7b contract:

- amendment 2: UtteranceAssembler forced-split pre-roll invariants
  (window B ownership, had_pre_roll, pre-roll excluded from the cap,
  pure-overlap commits discarded, gate-before-cap ordering)
- amendment 3: dedupe_prefix() deterministic semantics (exact overlap,
  12-character minimum, longest wins, case/punctuation/whitespace exact)
- privacy: static audit that no transcript text can reach the logger
  or exception messages in live_transcribe.py, plus a runtime scan of
  every logger call against a transcript marker
- step 2: session lifecycle against a faked live_capture - start/stop
  atomicity, worker residency and residue, bounded commit queue with
  drop-oldest overflow, stop-time flush, worker-failure auto-stop shape
- step 3: transcription thread against a faked whisper engine - temp
  WAV written/validated/deleted, pre-roll dedupe through the real
  assembler split, three-consecutive-failure auto-stop, cancel during
  stop, commits surviving stop, memory-only get_transcript()
- step 3a: adaptive calibration - the threshold derived from a
  synthetic ambient, speech above/below that derived threshold,
  silence gating against it, timeout and read-error failures that
  release the capture, threshold inflation when every window is
  speech, and stats()["energy_threshold"]
- step 3b (7b.4): calibration robustness - the locked D1-D5 design:
  median-of-retained estimator with a boundary established by
  synthetic tests from representative measured cases, including
  [837, 958, 2899] -> retained [837, 958] (D1, work item 1), bounded
  extension up to the hard cap (D2, work item 2) with the cap marking
  calibration_contaminated while the session still starts (D3, work
  item 3),   M=1.75 unchanged with its evidence recorded below (D4),
  and calibration audio replayed exactly once through the normal
  assembler before live audio (work items 4-5); acceptance invariants:
  threshold >= ABSOLUTE_FLOOR for every calibration input (zero-ambient
  floor clamp, all-elevated stability) and replay ordering (calibration
  speech commits before live speech, exactly once); session tests patch
  CALIBRATION_TARGET_WINDOWS to 0, so their timing and floor
  threshold are unchanged
- scope: live_transcribe may import live_capture, logger, and
  whisper_engine, and nothing from the forbidden production set (no
  audio, no config, no output, no main)

No Whisper subprocess and no microphone use: audio is synthesized,
live_capture is faked, and whisper_engine.whisper_transcribe is
replaced by FakeWhisperEngine, so no audio stack and no model are
needed.
"""

import ast
import dataclasses
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import unittest
import wave
from unittest import mock

import numpy as np

import live_transcribe as lt

TEXT_IDENTIFIERS = {
    "text",
    "new",
    "previous",
    "transcript",
    "segment",
    "stdout",
    "stderr",
    "output",
    "reply",
}

LOGGER_ARGUMENT_IDENTIFIERS = TEXT_IDENTIFIERS | {
    "error",
    "err",
    "exception",
    "exc",
}

FORBIDDEN_IMPORTS = {
    "main",
    "audio",
    "tray",
    "text_output",
    "app_state",
    "recovery",
    "config",
}


def silence(samples):
    return np.zeros(samples, dtype=np.int16)


def tone(samples, level=4000):
    return np.full(samples, level, dtype=np.int16)


def joined(*parts):
    return np.concatenate(parts)


def feed_chunked(assembler, signal, step):
    commits = []
    for offset in range(0, len(signal), step):
        commits.extend(assembler.feed(signal[offset:offset + step]))
    return commits


def snapshots(commits):
    return [
        (commit.reason, commit.had_pre_roll, commit.samples.tobytes())
        for commit in commits
    ]


def identifiers_outside_type_name(node):
    found = set()

    class Collector(ast.NodeVisitor):
        def visit_Name(self, current):
            found.add(current.id)

        def visit_Attribute(self, current):
            if current.attr == "__name__":
                value = current.value
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "type"
                ):
                    return
            self.generic_visit(current)

    Collector().visit(node)
    return found


def call_arguments(call_node):
    return list(call_node.args) + [
        keyword.value for keyword in call_node.keywords
    ]


class DedupePrefixTests(unittest.TestCase):
    """Amendment 3: deterministic, conservative text-boundary operation."""

    def test_exact_twelve_character_overlap_removed(self):
        overlap = "abcdefghijkl"
        previous = "earlier context " + overlap
        new = overlap + "tail"
        self.assertEqual(lt.dedupe_prefix(previous, new), "tail")

    def test_eleven_character_overlap_not_deduped(self):
        overlap = "abcdefghijk"
        previous = "earlier context " + overlap
        new = overlap + "tail"
        self.assertEqual(lt.dedupe_prefix(previous, new), new)

    def test_longest_overlap_wins(self):
        previous = "pad " + "a" * 13
        new = "a" * 13 + "tail"
        self.assertEqual(lt.dedupe_prefix(previous, new), "tail")

    def test_no_overlap_returns_new_unchanged(self):
        previous = "alpha beta gamma"
        new = "brown fox jumps"
        self.assertEqual(lt.dedupe_prefix(previous, new), new)

    def test_case_sensitive(self):
        previous = "end of abcdefghijkl"
        new = "ABCDEFGHIJKL tail"
        self.assertEqual(lt.dedupe_prefix(previous, new), new)

    def test_punctuation_must_match_exactly(self):
        previous = "she repeated hello, world daily"
        new = "hello, world-class daily reset"
        self.assertEqual(lt.dedupe_prefix(previous, new), new)

    def test_near_miss_is_not_fuzzy_matched(self):
        previous = "look at the colour of the sky"
        new = "color of the sky is nice"
        self.assertEqual(lt.dedupe_prefix(previous, new), new)

    def test_punctuation_overlap_removed_exactly(self):
        previous = "next: wait, then stop."
        new = "wait, then stop. then leave"
        self.assertEqual(lt.dedupe_prefix(previous, new), " then leave")

    def test_whitespace_runs_match_exactly_and_are_preserved(self):
        overlap = "alpha  beta   gamma delta"
        previous = "context " + overlap
        new = overlap + " extra"
        self.assertEqual(lt.dedupe_prefix(previous, new), " extra")

    def test_review_example_overlap_below_minimum_unchanged(self):
        previous = "...the quick brown fox"
        new = "brown fox jumps over..."
        self.assertEqual(lt.dedupe_prefix(previous, new), new)

    def test_full_containment_reduces_to_empty(self):
        previous = "the sentence we already committed"
        new = "already committed"
        self.assertEqual(lt.dedupe_prefix(previous, new), "")

    def test_new_shorter_than_minimum_unchanged(self):
        previous = "abcdefghijklmno"
        new = "abcdefghijk"
        self.assertEqual(lt.dedupe_prefix(previous, new), new)

    def test_empty_inputs(self):
        self.assertEqual(lt.dedupe_prefix("", "abc"), "abc")
        self.assertEqual(lt.dedupe_prefix("abc", ""), "")
        self.assertEqual(lt.dedupe_prefix("", ""), "")


class ThresholdAlignmentTests(unittest.TestCase):
    """Every timing threshold divides the 20 ms analysis frame exactly."""

    def test_thresholds_align_to_frames(self):
        aligned = (
            lt.SILENCE_GATE_SAMPLES,
            lt.MIN_UTTERANCE_SAMPLES,
            lt.MAX_UTTERANCE_SAMPLES,
            lt.PRE_ROLL_SAMPLES,
            lt.CHUNK_SAMPLES,
        )
        for value in aligned:
            self.assertEqual(value % lt.FRAME_SAMPLES, 0)

    def test_threshold_values_locked(self):
        self.assertEqual(lt.SILENCE_GATE_SAMPLES, 11200)
        self.assertEqual(lt.MIN_UTTERANCE_SAMPLES, 4800)
        self.assertEqual(lt.MAX_UTTERANCE_SAMPLES, 128000)
        self.assertEqual(lt.PRE_ROLL_SAMPLES, 8000)
        self.assertEqual(lt.MIN_DEDUPE_OVERLAP, 12)
        self.assertEqual(lt.SAMPLE_RATE, 16000)

    def test_calibration_duration_aligns_to_frames(self):
        target = int(lt.CALIBRATION_DURATION_S * lt.SAMPLE_RATE)
        self.assertEqual(target % lt.FRAME_SAMPLES, 0)
        self.assertEqual(target, lt.CHUNK_SAMPLES)

    def test_calibration_constants_locked(self):
        self.assertEqual(lt.ABSOLUTE_FLOOR, 500)
        self.assertEqual(lt.THRESHOLD_MULTIPLIER, 1.75)
        self.assertEqual(lt.CALIBRATION_DURATION_S, 1.0)
        self.assertEqual(lt.CALIBRATION_TIMEOUT_SLACK_S, 2.0)
        self.assertEqual(lt.CALIBRATION_TARGET_WINDOWS, 3)
        self.assertEqual(lt.CALIBRATION_MAX_DURATION_S, 5.0)
        self.assertEqual(lt.CALIBRATION_OUTLIER_RATIO, 2.0)


class UtteranceAssemblerTests(unittest.TestCase):

    def setUp(self):
        self.assembler = lt.UtteranceAssembler()

    def test_silence_before_speech_produces_no_inference(self):
        commits = feed_chunked(self.assembler, silence(32000), 16000)
        self.assertEqual(commits, [])
        self.assertEqual(self.assembler.flush(), [])
        self.assertFalse(self.assembler.is_speaking)

    def test_leading_silence_excluded_from_commit(self):
        signal = joined(silence(16000), tone(16000), silence(11200))
        commits = feed_chunked(self.assembler, signal, 16000)
        self.assertEqual(len(commits), 1)
        commit = commits[0]
        self.assertEqual(commit.length_samples, 16000)
        self.assertEqual(commit.duration_s, 1.0)
        self.assertEqual(commit.reason, lt.REASON_SILENCE_GATE)
        self.assertFalse(commit.had_pre_roll)

    def test_silence_gate_commits_and_trims_trailing_silence(self):
        signal = joined(tone(16000), silence(11200))
        commits = feed_chunked(self.assembler, signal, 16000)
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].length_samples, 16000)
        self.assertEqual(commits[0].reason, lt.REASON_SILENCE_GATE)
        self.assertFalse(self.assembler.is_speaking)
        self.assertEqual(self.assembler.pending_samples, 0)

    def test_pause_below_gate_keeps_single_utterance(self):
        signal = joined(
            tone(8000), silence(9600), tone(8000), silence(11200)
        )
        commits = feed_chunked(self.assembler, signal, 16000)
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].length_samples, 25600)
        self.assertEqual(commits[0].reason, lt.REASON_SILENCE_GATE)

    def test_short_utterance_below_minimum_discarded(self):
        signal = joined(tone(3200), silence(11200))
        commits = feed_chunked(self.assembler, signal, 16000)
        self.assertEqual(commits, [])
        self.assertFalse(self.assembler.is_speaking)

    def test_utterance_at_exact_minimum_commits(self):
        signal = joined(tone(4800), silence(11200))
        commits = feed_chunked(self.assembler, signal, 16000)
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].length_samples, 4800)

    def test_energy_threshold_is_strict(self):
        below = joined(np.full(4800, 500, dtype=np.int16), silence(11200))
        commits = feed_chunked(self.assembler, below, 16000)
        self.assertEqual(commits, [])
        self.assertFalse(self.assembler.is_speaking)

        assembler = lt.UtteranceAssembler()
        above = joined(np.full(4800, 501, dtype=np.int16), silence(11200))
        commits = feed_chunked(assembler, above, 16000)
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].length_samples, 4800)

    def test_energy_threshold_constructor_override(self):
        assembler = lt.UtteranceAssembler(energy_threshold=10000)
        signal = joined(tone(4800, level=4000), silence(11200))
        commits = feed_chunked(assembler, signal, 16000)
        self.assertEqual(commits, [])

    def test_forced_split_pre_roll_invariants(self):
        signal = joined(
            tone(128000 + 128000 + 16000), silence(11200)
        )
        commits = feed_chunked(self.assembler, signal, 16000)
        commits.extend(self.assembler.flush())

        self.assertEqual(len(commits), 3)
        first, second, third = commits

        self.assertEqual(first.reason, lt.REASON_FORCED_SPLIT)
        self.assertFalse(first.had_pre_roll)
        self.assertEqual(first.length_samples, lt.MAX_UTTERANCE_SAMPLES)

        self.assertEqual(second.reason, lt.REASON_FORCED_SPLIT)
        self.assertTrue(second.had_pre_roll)
        self.assertEqual(
            second.length_samples,
            lt.PRE_ROLL_SAMPLES + lt.MAX_UTTERANCE_SAMPLES,
        )
        self.assertEqual(
            second.length_samples - lt.PRE_ROLL_SAMPLES,
            lt.MAX_UTTERANCE_SAMPLES,
        )
        self.assertTrue(
            np.array_equal(
                second.samples[:lt.PRE_ROLL_SAMPLES],
                first.samples[-lt.PRE_ROLL_SAMPLES:],
            )
        )

        self.assertEqual(third.reason, lt.REASON_SILENCE_GATE)
        self.assertTrue(third.had_pre_roll)
        self.assertEqual(third.length_samples, lt.PRE_ROLL_SAMPLES + 16000)
        self.assertTrue(
            np.array_equal(
                third.samples[:lt.PRE_ROLL_SAMPLES],
                second.samples[-lt.PRE_ROLL_SAMPLES:],
            )
        )
        self.assertEqual(self.assembler.flush(), [])

    def test_continuation_window_gate_commit_carries_pre_roll(self):
        commits = feed_chunked(self.assembler, tone(128000), 16000)
        self.assertEqual(len(commits), 1)

        signal = joined(tone(16000), silence(11200))
        commits.extend(feed_chunked(self.assembler, signal, 16000))
        self.assertEqual(len(commits), 2)

        continuation = commits[1]
        self.assertEqual(continuation.reason, lt.REASON_SILENCE_GATE)
        self.assertTrue(continuation.had_pre_roll)
        self.assertEqual(
            continuation.length_samples, lt.PRE_ROLL_SAMPLES + 16000
        )
        self.assertTrue(
            np.array_equal(
                continuation.samples[:lt.PRE_ROLL_SAMPLES],
                commits[0].samples[-lt.PRE_ROLL_SAMPLES:],
            )
        )

    def test_pure_pre_roll_commit_discarded(self):
        commits = feed_chunked(self.assembler, tone(128000), 16000)
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].reason, lt.REASON_FORCED_SPLIT)

        after_split = feed_chunked(self.assembler, silence(11200), 16000)
        self.assertEqual(after_split, [])
        self.assertEqual(self.assembler.flush(), [])
        self.assertEqual(len(commits), 1)

    def test_flush_discards_pre_roll_only_pending(self):
        commits = feed_chunked(self.assembler, tone(128000), 16000)
        self.assertEqual(len(commits), 1)
        self.assertEqual(self.assembler.flush(), [])
        self.assertFalse(self.assembler.is_speaking)

    def test_silence_gate_wins_over_cap_on_shared_frame(self):
        signal = joined(tone(116800), silence(11200))
        commits = feed_chunked(self.assembler, signal, 16000)
        commits.extend(self.assembler.flush())

        self.assertEqual(len(commits), 1)
        commit = commits[0]
        self.assertEqual(commit.reason, lt.REASON_SILENCE_GATE)
        self.assertEqual(commit.length_samples, 116800)
        self.assertFalse(commit.had_pre_roll)
        self.assertFalse(self.assembler.is_speaking)

    def test_flush_commits_pending_speech(self):
        commits = feed_chunked(self.assembler, tone(16000), 16000)
        self.assertEqual(commits, [])
        self.assertEqual(self.assembler.pending_samples, 16000)

        commits = self.assembler.flush()
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].reason, lt.REASON_FLUSH)
        self.assertEqual(commits[0].length_samples, 16000)
        self.assertFalse(commits[0].had_pre_roll)
        self.assertEqual(self.assembler.flush(), [])

    def test_reset_clears_partial_state(self):
        feed_chunked(self.assembler, tone(8000), 16000)
        self.assertTrue(self.assembler.is_speaking)

        self.assembler.reset()
        self.assertFalse(self.assembler.is_speaking)
        self.assertEqual(self.assembler.pending_samples, 0)
        self.assertEqual(self.assembler.flush(), [])

    def test_feed_rejects_non_mono_input(self):
        with self.assertRaises(ValueError):
            self.assembler.feed(np.zeros((16000, 1), dtype=np.int16))

    def test_feed_rejects_non_int16_input(self):
        with self.assertRaises(ValueError):
            self.assembler.feed(np.zeros(16000, dtype=np.float32))

    def test_feed_split_pattern_does_not_change_output(self):
        signal = joined(
            silence(4000),
            tone(8000),
            silence(5000),
            tone(10000),
            silence(9600),
            tone(116800),
            silence(11200),
        )

        reference = None
        for step in (len(signal), 16000, 1117, 7919):
            assembler = lt.UtteranceAssembler()
            commits = feed_chunked(assembler, signal, step)
            commits.extend(assembler.flush())

            if reference is None:
                reference = commits
                self.assertGreater(len(commits), 0)
            else:
                self.assertEqual(snapshots(commits), snapshots(reference))


class PrivacyStaticAuditTests(unittest.TestCase):
    """No transcript text may enter the logger indirectly.

    Static rules for live_transcribe.py: logger call arguments and
    raised messages must not reference text-bearing identifiers, and
    print() is forbidden. Logging by reason/timing/length identifiers
    stays allowed. LiveTranscribeSessionTests adds the runtime
    transcript-marker scan over the same file.
    """

    @classmethod
    def setUpClass(cls):
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "live_transcribe.py"
        )
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        cls.tree = ast.parse(source)

    def test_no_print_calls(self):
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "print":
                self.fail("print() is forbidden in live_transcribe.py")

    def test_logger_arguments_cannot_carry_text(self):
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue

            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if not isinstance(func.value, ast.Name):
                continue
            if func.value.id != "logger":
                continue

            for argument in call_arguments(node):
                leaked = identifiers_outside_type_name(argument) & (
                    LOGGER_ARGUMENT_IDENTIFIERS
                )
                self.assertFalse(
                    leaked,
                    f"logger.{func.attr} references text-bearing "
                    f"identifiers: {sorted(leaked)}",
                )

    def test_exception_messages_cannot_carry_text(self):
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Raise):
                continue
            if node.exc is None:
                continue

            leaked = identifiers_outside_type_name(node.exc) & TEXT_IDENTIFIERS
            self.assertFalse(
                leaked,
                f"raise references text-bearing identifiers: "
                f"{sorted(leaked)}",
            )


class SessionScopeAuditTests(unittest.TestCase):
    """Step-3 scope: live_capture, logger, and whisper_engine only."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "live_transcribe.py"
        )
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        cls.tree = ast.parse(source)

    def test_scope_imports_only(self):
        found = set()

        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])

        forbidden = found & FORBIDDEN_IMPORTS
        self.assertFalse(
            forbidden, f"forbidden imports in scope step: {sorted(forbidden)}"
        )
        self.assertIn("live_capture", found)
        self.assertIn("logger", found)
        self.assertIn("whisper_engine", found)

    def test_commits_are_audio_only(self):
        field_names = {
            field.name for field in dataclasses.fields(lt.Commit)
        }
        self.assertEqual(field_names, {"samples", "had_pre_roll", "reason"})


class FakeLiveCapture:
    """live_capture contract double: no audio stack, no device."""

    def __init__(self):
        self.chunks = queue.Queue()
        self.active = False
        self.start_calls = 0
        self.stop_calls = 0
        self.start_result = (True, "started")
        self.read_error = None

    def start(self):
        self.start_calls += 1
        if self.start_result[0]:
            self.active = True
        return self.start_result

    def stop(self):
        self.stop_calls += 1
        self.active = False
        return True, "stopped"

    def is_active(self):
        return self.active

    def read_chunk(self, timeout=1.0):
        if self.read_error is not None:
            error, self.read_error = self.read_error, None
            raise error

        try:
            return self.chunks.get(timeout=timeout)
        except queue.Empty:
            return None

    def push(self, chunk):
        self.chunks.put(chunk)


def session_threads():
    return [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("LiveTranscribe")
    ]


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakeWhisperEngine:
    """whisper_engine contract double: one-shot call, no subprocess.

    Mimics the engine's cancellation lifecycle: cancel_requested is
    reset at the entry of every call and observed while a gated call is
    blocked, exactly where the real engine observes it between reset
    and process completion. It records what the real engine would have
    seen (WAV existence, header fields, first sample, path) so tests
    can prove the file is valid while it runs and gone afterwards.
    """

    def __init__(self):
        self.cancel_requested = False
        self.default_result = "committed transcript"
        self.results = queue.Queue()
        self.calls = []
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()

    def whisper_transcribe(self, wav_path):
        self.cancel_requested = False

        record = {
            "wav_path": wav_path,
            "wav_exists": os.path.exists(wav_path),
            "cancelled": False,
        }

        if record["wav_exists"]:
            with wave.open(wav_path, "rb") as handle:
                frames = handle.readframes(1)
                record["channels"] = handle.getnchannels()
                record["sampwidth"] = handle.getsampwidth()
                record["framerate"] = handle.getframerate()
                record["nframes"] = handle.getnframes()
                record["first_sample"] = (
                    struct.unpack("<h", frames)[0] if frames else None
                )

        self.calls.append(record)

        if not self.gate.is_set():
            self.entered.set()
            while not self.gate.wait(0.02):
                if self.cancel_requested:
                    record["cancelled"] = True
                    return ""

        if not self.results.empty():
            outcome = self.results.get_nowait()
        else:
            outcome = self.default_result

        if isinstance(outcome, BaseException):
            raise outcome

        return outcome


class LiveTranscribeSessionTests(unittest.TestCase):

    def setUp(self):
        lt.stop()

        self.capture = FakeLiveCapture()
        self.engine = FakeWhisperEngine()

        calibration_patcher = mock.patch.object(
            lt, "CALIBRATION_TARGET_WINDOWS", 0
        )
        calibration_patcher.start()
        self.addCleanup(calibration_patcher.stop)

        capture_patcher = mock.patch.object(
            lt, "_import_capture", return_value=self.capture
        )
        capture_patcher.start()
        self.addCleanup(capture_patcher.stop)

        engine_patcher = mock.patch.object(lt, "whisper_engine", self.engine)
        engine_patcher.start()
        self.addCleanup(engine_patcher.stop)

        logger_patcher = mock.patch.object(lt, "logger")
        self.logger_mock = logger_patcher.start()
        self.addCleanup(logger_patcher.stop)

        self.addCleanup(lt.stop)

    def park_transcriber(self):
        """Block the transcription thread inside one Whisper call."""
        self.engine.gate.clear()
        self.engine.entered.clear()
        self.capture.push(joined(tone(4800, level=2000), silence(11200)))
        self.assertTrue(
            wait_until(lambda: self.engine.entered.is_set()),
            "transcription thread never reached the Whisper call",
        )

    def logger_calls(self):
        calls = []
        for name in ("debug", "info", "warning", "error"):
            method = getattr(self.logger_mock, name)
            calls.extend(str(call) for call in method.call_args_list)
        return calls

    def test_start_stop_leaves_no_worker_residue(self):
        ok, reason = lt.start()
        self.assertTrue(ok, reason)
        self.assertTrue(lt.is_active())
        self.assertEqual(len(session_threads()), 2)
        self.assertTrue(lt.stats()["worker_alive"])
        self.assertTrue(lt.stats()["transcription_alive"])
        self.assertTrue(lt.stats()["capture_active"])

        ok, reason = lt.stop()
        self.assertEqual((ok, reason), (True, "stopped"))

        self.assertFalse(lt.is_active())
        self.assertEqual(session_threads(), [])
        stats = lt.stats()
        self.assertFalse(stats["worker_alive"])
        self.assertFalse(stats["transcription_alive"])
        self.assertFalse(stats["capture_active"])
        self.assertEqual(stats["assembler_pending"], 0)
        self.assertEqual(stats["transcript_segments"], 0)
        self.assertEqual(stats["consecutive_failures"], 0)
        self.assertEqual(self.capture.stop_calls, 1)

    def test_worker_feeds_assembler_and_commit_flows(self):
        ok, reason = lt.start()
        self.assertTrue(ok, reason)

        self.park_transcriber()

        self.capture.push(tone(16000))
        self.capture.push(silence(16000))

        self.assertTrue(
            wait_until(lambda: lt.stats()["commits_queued"] == 1)
        )

        commit = lt.read_commit(timeout=5.0)
        self.assertIsNotNone(commit)
        self.assertEqual(commit.reason, lt.REASON_SILENCE_GATE)
        self.assertEqual(commit.length_samples, 16000)
        self.assertFalse(commit.had_pre_roll)

        lt.stop()
        self.assertEqual(self.capture.chunks.qsize(), 0)

    def test_stop_flushes_pending_speech(self):
        ok, reason = lt.start()
        self.assertTrue(ok, reason)

        self.capture.push(tone(16000))
        self.assertTrue(
            wait_until(lambda: lt.stats()["assembler_pending"] == 16000)
        )

        lt.stop()

        commit = lt.read_commit(timeout=5.0)
        self.assertIsNotNone(commit)
        self.assertEqual(commit.reason, lt.REASON_FLUSH)
        self.assertEqual(commit.length_samples, 16000)
        self.assertFalse(commit.had_pre_roll)
        self.assertEqual(lt.stats()["assembler_pending"], 0)

    def test_start_twice_rejected(self):
        ok, reason = lt.start()
        self.assertTrue(ok, reason)

        ok, reason = lt.start()
        self.assertEqual((ok, reason), (False, "already active"))
        self.assertEqual(len(session_threads()), 2)

        lt.stop()

    def test_start_refuses_when_capture_refuses(self):
        self.capture.start_result = (False, "microphone busy (f8)")

        ok, reason = lt.start()
        self.assertEqual((ok, reason), (False, "microphone busy (f8)"))
        self.assertFalse(lt.is_active())
        self.assertEqual(session_threads(), [])
        self.assertEqual(self.capture.stop_calls, 0)
        self.assertEqual(lt.stop(), (True, "inactive"))

    def test_stop_is_idempotent(self):
        self.assertEqual(lt.stop(), (True, "inactive"))
        self.assertTrue(lt.start()[0])
        self.assertTrue(lt.stop()[0])
        self.assertEqual(lt.stop(), (True, "inactive"))

    def test_read_commit_returns_none_when_empty(self):
        self.assertIsNone(lt.read_commit(timeout=0.05))
        self.assertTrue(lt.start()[0])
        self.assertIsNone(lt.read_commit(timeout=0.05))
        lt.stop()

    def test_commit_overflow_drops_oldest(self):
        ok, reason = lt.start()
        self.assertTrue(ok, reason)

        self.park_transcriber()

        total = lt.COMMIT_QUEUE_SIZE + 4
        for index in range(total):
            level = 1000 + index * 100
            self.capture.push(joined(tone(4800, level=level), silence(11200)))

        self.assertTrue(
            wait_until(lambda: lt.stats()["commits_dropped"] == 4)
        )
        stats = lt.stats()
        self.assertEqual(stats["commits_queued"], lt.COMMIT_QUEUE_SIZE)

        first = lt.read_commit(timeout=1.0)
        self.assertIsNotNone(first)
        self.assertEqual(first.samples[0], 1000 + 4 * 100)

        drained = 1
        while lt.read_commit(timeout=0.05) is not None:
            drained += 1
        self.assertEqual(drained, lt.COMMIT_QUEUE_SIZE)

        lt.stop()

    def test_new_session_discards_unread_commits(self):
        self.assertTrue(lt.start()[0])
        self.park_transcriber()
        self.capture.push(joined(tone(4800), silence(11200)))
        self.assertTrue(
            wait_until(lambda: lt.stats()["commits_queued"] == 1)
        )
        lt.stop()

        self.assertTrue(lt.start()[0])
        self.assertEqual(lt.stats()["commits_queued"], 0)
        discarded = [
            call
            for call in self.logger_mock.warning.call_args_list
            if "Discarding 1 queued commits" in str(call)
        ]
        self.assertEqual(len(discarded), 1)
        lt.stop()

    def test_worker_failure_auto_stops_and_external_stop_joins(self):
        self.assertTrue(lt.start()[0])

        self.capture.read_error = RuntimeError("internal detail")
        self.assertTrue(
            wait_until(lambda: not lt.is_active())
        )
        self.assertTrue(
            wait_until(lambda: not lt.stats()["worker_alive"])
        )
        self.assertEqual(self.capture.stop_calls, 0)

        self.assertEqual(lt.start(), (False, "stop required"))
        self.assertEqual(lt.stop(), (True, "stopped"))
        self.assertEqual(self.capture.stop_calls, 1)
        self.assertEqual(session_threads(), [])

        error_calls = [
            str(call) for call in self.logger_mock.error.call_args_list
        ]
        self.assertTrue(
            any("RuntimeError" in message for message in error_calls)
        )
        self.assertFalse(
            any("internal detail" in message for message in error_calls)
        )

        self.assertTrue(lt.start()[0])
        lt.stop()

    def test_rapid_start_stop_cycles_leave_no_residue(self):
        cycles = 10
        for _ in range(cycles):
            ok, reason = lt.start()
            self.assertTrue(ok, reason)
            ok, reason = lt.stop()
            self.assertEqual((ok, reason), (True, "stopped"))

        self.assertEqual(self.capture.start_calls, cycles)
        self.assertEqual(self.capture.stop_calls, cycles)
        self.assertEqual(session_threads(), [])

    def test_transcription_writes_validates_and_deletes_wav(self):
        self.assertTrue(lt.start()[0])

        self.park_transcriber()
        call = self.engine.calls[0]

        self.assertTrue(call["wav_exists"])
        self.assertTrue(os.path.exists(call["wav_path"]))
        self.assertTrue(call["wav_path"].endswith(".wav"))
        self.assertEqual(call["channels"], 1)
        self.assertEqual(call["sampwidth"], 2)
        self.assertEqual(call["framerate"], lt.SAMPLE_RATE)
        self.assertEqual(call["nframes"], 4800)
        self.assertEqual(call["first_sample"], 2000)
        self.assertFalse(call["cancelled"])

        self.engine.gate.set()
        self.assertTrue(
            wait_until(lambda: lt.stats()["transcript_segments"] == 1)
        )
        self.assertFalse(os.path.exists(call["wav_path"]))
        self.assertEqual(lt.get_transcript(), [self.engine.default_result])
        self.assertEqual(lt.stats()["consecutive_failures"], 0)

        lt.stop()

    def test_pre_roll_window_is_deduped_against_previous_segment(self):
        self.assertTrue(lt.start()[0])

        first_text = "the quick brown fox jumps"
        second_raw = "brown fox jumps twice"
        third_raw = "no further overlap here"
        for outcome in (first_text, second_raw, third_raw):
            self.engine.results.put(outcome)

        self.capture.push(joined(tone(128000 + 128000 + 16000), silence(11200)))

        self.assertTrue(
            wait_until(lambda: lt.stats()["transcript_segments"] == 3)
        )
        self.assertEqual(
            lt.get_transcript(),
            [first_text, " twice", third_raw],
        )
        self.assertEqual(lt.stats()["consecutive_failures"], 0)

        lt.stop()

    def test_normal_commits_are_not_deduped(self):
        self.assertTrue(lt.start()[0])

        first_text = "we always end here"
        second_text = "we always end here plus more words"
        for outcome in (first_text, second_text):
            self.engine.results.put(outcome)

        for _ in range(2):
            self.capture.push(joined(tone(4800), silence(11200)))

        self.assertTrue(
            wait_until(lambda: lt.stats()["transcript_segments"] == 2)
        )
        self.assertEqual(lt.get_transcript(), [first_text, second_text])

        lt.stop()

    def test_three_consecutive_failures_auto_stop(self):
        self.assertTrue(lt.start()[0])

        for index in range(lt.CONSECUTIVE_FAILURE_LIMIT):
            self.engine.results.put(RuntimeError(f"whisper failure {index}"))
            self.capture.push(joined(tone(4800), silence(11200)))

        self.assertTrue(wait_until(lambda: not lt.is_active()))
        self.assertTrue(
            wait_until(lambda: not lt.stats()["transcription_alive"])
        )
        self.assertTrue(wait_until(lambda: not lt.stats()["worker_alive"]))

        stats = lt.stats()
        self.assertEqual(
            stats["consecutive_failures"], lt.CONSECUTIVE_FAILURE_LIMIT
        )
        self.assertEqual(stats["transcript_segments"], 0)
        self.assertEqual(self.capture.stop_calls, 0)

        self.assertEqual(lt.start(), (False, "stop required"))
        self.assertEqual(lt.stop(), (True, "stopped"))
        self.assertEqual(self.capture.stop_calls, 1)
        self.assertEqual(session_threads(), [])

        error_calls = [
            str(call) for call in self.logger_mock.error.call_args_list
        ]
        self.assertTrue(
            any("RuntimeError" in message for message in error_calls)
        )
        self.assertTrue(
            any("consecutive=3" in message for message in error_calls)
        )
        self.assertFalse(
            any("whisper failure" in message for message in error_calls)
        )

        self.assertTrue(lt.start()[0])
        lt.stop()

    def test_failure_counter_resets_on_success(self):
        self.assertTrue(lt.start()[0])

        self.engine.results.put(RuntimeError("first failure"))
        self.engine.results.put("recovered text")
        self.engine.results.put(RuntimeError("second failure"))

        for _ in range(3):
            self.capture.push(joined(tone(4800), silence(11200)))

        self.assertTrue(
            wait_until(
                lambda: (
                    lt.stats()["transcript_segments"] == 1
                    and lt.stats()["consecutive_failures"] == 1
                )
            )
        )
        self.assertTrue(lt.is_active())
        self.assertEqual(lt.get_transcript(), ["recovered text"])

        lt.stop()

    def test_stop_cancels_inflight_transcription(self):
        self.assertTrue(lt.start()[0])

        self.engine.gate.clear()
        self.engine.entered.clear()
        self.capture.push(joined(tone(4800), silence(11200)))
        self.assertTrue(wait_until(lambda: self.engine.entered.is_set()))

        started = time.monotonic()
        result = lt.stop()
        elapsed = time.monotonic() - started

        self.assertEqual(result, (True, "stopped"))
        self.assertLess(elapsed, 3.0)
        self.assertTrue(self.engine.calls[0]["cancelled"])
        self.assertEqual(lt.stats()["transcript_segments"], 0)
        self.assertEqual(lt.stats()["consecutive_failures"], 0)
        self.assertEqual(session_threads(), [])

    def test_commits_survive_stop_while_transcriber_busy(self):
        self.assertTrue(lt.start()[0])

        self.park_transcriber()

        self.capture.push(joined(tone(4800, level=6500), silence(11200)))
        self.assertTrue(
            wait_until(lambda: lt.stats()["commits_queued"] == 1)
        )

        self.assertEqual(lt.stop(), (True, "stopped"))

        commit = lt.read_commit(timeout=1.0)
        self.assertIsNotNone(commit)
        self.assertEqual(commit.length_samples, 4800)
        self.assertEqual(commit.samples[0], 6500)
        self.assertEqual(lt.stats()["transcript_segments"], 0)

    def test_wav_deleted_when_transcription_fails(self):
        self.assertTrue(lt.start()[0])

        self.engine.results.put(RuntimeError("model exploded"))
        self.capture.push(joined(tone(4800), silence(11200)))
        self.assertTrue(
            wait_until(lambda: lt.stats()["consecutive_failures"] == 1)
        )

        call = self.engine.calls[0]
        self.assertTrue(call["wav_exists"])
        self.assertFalse(os.path.exists(call["wav_path"]))
        self.assertEqual(lt.stats()["transcript_segments"], 0)

        lt.stop()

    def test_transcript_marker_never_reaches_logger(self):
        marker = "PRIVACY_MARKER_LIVE_TRANSCRIPT"
        self.engine.default_result = f"{marker} alpha beta gamma"

        self.assertTrue(lt.start()[0])
        self.capture.push(joined(tone(4800), silence(11200)))
        self.assertTrue(
            wait_until(lambda: lt.stats()["transcript_segments"] == 1)
        )

        self.assertEqual(lt.get_transcript(), [self.engine.default_result])

        calls = self.logger_calls()
        self.assertTrue(any("characters=" in message for message in calls))
        self.assertFalse(any(marker in message for message in calls))
        self.assertFalse(
            any(marker.lower() in message.lower() for message in calls)
        )

        lt.stop()

    def test_get_transcript_returns_a_copy(self):
        self.assertTrue(lt.start()[0])
        self.capture.push(joined(tone(4800), silence(11200)))
        self.assertTrue(
            wait_until(lambda: lt.stats()["transcript_segments"] == 1)
        )

        snapshot = lt.get_transcript()
        snapshot.append("tampered")

        self.assertEqual(lt.get_transcript(), [self.engine.default_result])

        lt.stop()

    def test_import_creates_no_worker_thread(self):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        script = (
            "import sys\n"
            "import threading\n"
            "import live_transcribe\n"
            "assert 'whisper_engine' in sys.modules\n"
            "assert 'sounddevice' not in sys.modules\n"
            "names = [thread.name for thread in threading.enumerate()]\n"
            "assert not any(name.startswith('LiveTranscribe') "
            "for name in names), names\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=base_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class _CalibrationSessionBase(unittest.TestCase):
    """Shared fixture for the calibration classes (steps 3a and 3b).

    Faked capture, faked whisper engine, mocked logger; calibration
    constants are NOT patched here, so start() really samples the
    faked capture through the production estimator.
    """

    def setUp(self):
        lt.stop()

        self.capture = FakeLiveCapture()
        self.engine = FakeWhisperEngine()

        capture_patcher = mock.patch.object(
            lt, "_import_capture", return_value=self.capture
        )
        capture_patcher.start()
        self.addCleanup(capture_patcher.stop)

        engine_patcher = mock.patch.object(lt, "whisper_engine", self.engine)
        engine_patcher.start()
        self.addCleanup(engine_patcher.stop)

        logger_patcher = mock.patch.object(lt, "logger")
        self.logger_mock = logger_patcher.start()
        self.addCleanup(logger_patcher.stop)

        self.addCleanup(lt.stop)

    def push_ambient(self, level, windows=None):
        """Queue CALIBRATION_TARGET_WINDOWS clean windows of `level`.

        Override `windows` to drive the extension/cap paths (3b); the
        default stops the estimator at the target with no extension.
        """
        count = (
            lt.CALIBRATION_TARGET_WINDOWS if windows is None else windows
        )
        samples = int(lt.CALIBRATION_DURATION_S * lt.SAMPLE_RATE)
        for _ in range(count):
            self.capture.push(tone(samples, level=level))

    def park_transcriber_above_threshold(self):
        """Block the transcription thread inside one Whisper call.

        Session tests park with level=2000 material, which stays below
        the derived calibration threshold here; this park utterance is
        voiced above it (2100 with a 1200-level ambient), so the parked
        commit is offered, consumed, and held inside the gated call,
        leaving the queue free for the commit under test.
        """
        self.engine.gate.clear()
        self.engine.entered.clear()
        self.capture.push(joined(tone(4800, level=4000), silence(11200)))
        self.assertTrue(
            wait_until(lambda: self.engine.entered.is_set()),
            "transcription thread never reached the Whisper call",
        )

    def calibration_log(self):
        info = [str(call) for call in self.logger_mock.info.call_args_list]
        return [
            message
            for message in info
            if "Live transcription calibration:" in message
        ]


class AdaptiveCalibrationTests(_CalibrationSessionBase):
    """Step 3a: calibration runs against synthetic ambient windows."""

    def test_quiet_calibration_derives_threshold_from_ambient(self):
        self.push_ambient(1200)

        ok, reason = lt.start()
        self.assertTrue(ok, reason)

        expected = int(max(lt.ABSOLUTE_FLOOR, 1200 * lt.THRESHOLD_MULTIPLIER))
        stats = lt.stats()
        self.assertEqual(stats["energy_threshold"], expected)
        self.assertEqual(stats["assembler_pending"], 0)
        self.assertEqual(self.capture.chunks.qsize(), 0)

        info = [str(call) for call in self.logger_mock.info.call_args_list]
        self.assertTrue(
            any("Live transcription calibration:" in message for message in info)
        )
        self.assertTrue(
            any(f"threshold={expected}" in message for message in info)
        )
        self.assertTrue(
            any("ambient_rms=1200.0" in message for message in info)
        )

        self.assertEqual(lt.stop(), (True, "stopped"))

    def test_speech_above_derived_threshold_is_detected(self):
        self.push_ambient(1200)
        self.assertTrue(lt.start()[0])
        self.assertEqual(lt.stats()["energy_threshold"], 2100)

        self.capture.push(tone(16000, level=4000))
        self.assertTrue(
            wait_until(lambda: lt.stats()["assembler_pending"] == 16000)
        )

        lt.stop()

        commit = lt.read_commit(timeout=1.0)
        self.assertIsNotNone(commit)
        self.assertEqual(commit.reason, lt.REASON_FLUSH)
        self.assertEqual(commit.length_samples, 16000)
        self.assertFalse(commit.had_pre_roll)

    def test_speech_close_to_baseline_is_not_detected(self):
        self.push_ambient(1200)
        self.assertTrue(lt.start()[0])

        self.capture.push(tone(32000, level=2000))
        self.assertTrue(
            wait_until(lambda: self.capture.chunks.qsize() == 0)
        )

        self.assertEqual(lt.stats()["assembler_pending"], 0)
        self.assertIsNone(lt.read_commit(timeout=0.05))

        lt.stop()

        self.assertIsNone(lt.read_commit(timeout=0.05))
        self.assertEqual(lt.stats()["transcript_segments"], 0)

    def test_silence_below_derived_threshold_commits_via_gate(self):
        self.push_ambient(1200)
        self.assertTrue(lt.start()[0])

        self.park_transcriber_above_threshold()

        self.capture.push(joined(tone(8000, level=4000), silence(11200)))
        self.assertTrue(
            wait_until(lambda: lt.stats()["commits_queued"] == 1)
        )

        commit = lt.read_commit(timeout=1.0)
        self.assertIsNotNone(commit)
        self.assertEqual(commit.reason, lt.REASON_SILENCE_GATE)
        self.assertEqual(commit.length_samples, 8000)

        lt.stop()

    def test_calibration_timeout_releases_capture(self):
        # Stall before the FIRST window: the only deadline failure
        # (work item 2 - with one or more windows the deadline instead
        # yields the best-available estimate, pinned by
        # test_stall_with_partial_windows_starts_best_available).
        with (
            mock.patch.object(lt, "CALIBRATION_MAX_DURATION_S", 0.05),
            mock.patch.object(lt, "CALIBRATION_TIMEOUT_SLACK_S", 0.2),
        ):
            started = time.monotonic()
            ok, reason = lt.start()
            elapsed = time.monotonic() - started

        self.assertEqual((ok, reason), (False, "calibration timeout"))
        self.assertLess(elapsed, 1.5)
        self.assertEqual(self.capture.stop_calls, 1)
        self.assertEqual(session_threads(), [])
        self.assertFalse(lt.is_active())
        self.assertEqual(lt.stop(), (True, "inactive"))

    def test_calibration_read_error_releases_capture(self):
        self.capture.read_error = RuntimeError("internal detail")

        ok, reason = lt.start()

        self.assertEqual((ok, reason), (False, "calibration failed"))
        self.assertEqual(self.capture.stop_calls, 1)
        self.assertEqual(session_threads(), [])
        self.assertFalse(lt.is_active())
        self.assertEqual(lt.stop(), (True, "inactive"))

        error_calls = [
            str(call) for call in self.logger_mock.error.call_args_list
        ]
        self.assertTrue(
            any(
                "calibration failed: RuntimeError" in message
                for message in error_calls
            )
        )
        self.assertFalse(
            any("internal detail" in message for message in error_calls)
        )

    def test_speech_in_calibration_window_inflates_threshold(self):
        self.push_ambient(4000)
        self.assertTrue(lt.start()[0])

        expected = int(4000 * lt.THRESHOLD_MULTIPLIER)
        self.assertEqual(lt.stats()["energy_threshold"], expected)

        self.capture.push(joined(tone(16000, level=4000), silence(11200)))
        self.assertTrue(
            wait_until(lambda: self.capture.chunks.qsize() == 0)
        )

        self.assertEqual(lt.stats()["assembler_pending"], 0)

        lt.stop()

        self.assertIsNone(lt.read_commit(timeout=0.05))
        self.assertEqual(lt.stats()["transcript_segments"], 0)


class CalibrationRobustnessTests(_CalibrationSessionBase):
    """Step 3b (7b.4): robust estimator, cap, flag, and replay.

    Each test pins one locked design decision against the production
    estimator (constants are NOT patched unless the test says so):
    D1 outlier rejection with a test-established boundary and the
    median-of-retained center (work item 1), D2 bounded extension up
    to the hard cap (work item 2), D3 the contaminated-but-starts
    contract (work item 3), and work items 4-5: calibration audio
    replayed exactly once, in chronological order, through the normal
    UtteranceAssembler path.

    D4 evidence - THRESHOLD_MULTIPLIER stays 1.75 (7b.3a Decision B,
    real-mic rounds 1-2 of the 7b.3a validation harness):
    - M = 2.25 put the derived threshold above the measured speech
      p50, so sentences were lost during live transcription
    - M = 1.75 on the same recordings produced 0 idle commits and
      silence_gate windows containing full sentences
    - the measured 1-s calibration ambient RMS spanned 837-2899
      (single-window variance, the contamination case D1 now
      rejects), yielding thresholds of 1465-5074 at M = 1.75
    M = 1.75 is therefore the locked starting point; it is re-derived
    only if a later measurement demands it, and these tests re-derive
    nothing.
    """

    def push_windows(self, *levels):
        samples = int(lt.CALIBRATION_DURATION_S * lt.SAMPLE_RATE)
        for level in levels:
            self.capture.push(tone(samples, level=level))

    def test_representative_measured_triple_retains_quiet_windows(self):
        # The 7b.3a measured ambient windows [837, 958, 2899]: 2899 is
        # contamination, 837 and 958 are quiet. This case - not a
        # hard-coded constant - establishes the D1 outlier boundary
        # (work item 1).
        retained, rejected = lt._classify_calibration([837.0, 958.0, 2899.0])

        self.assertEqual(retained, [837.0, 958.0])
        self.assertEqual(rejected, 1)

    def test_representative_case_completes_target_by_extension(self):
        # The same measured triple plus one more quiet window: the
        # contaminated window is rejected, bounded extension supplies
        # the third usable window, and the threshold is derived from
        # quiet material only (work items 1-2).
        self.push_windows(837, 958, 2899, 900)

        ok, reason = lt.start()
        self.assertTrue(ok, reason)

        stats = lt.stats()
        self.assertEqual(stats["calibration_windows"], 4)
        self.assertEqual(stats["calibration_rejected"], 1)
        self.assertEqual(stats["calibration_usable"], 3)
        self.assertFalse(stats["calibration_contaminated"])
        # median([837, 900, 958]) = 900 -> 900 x 1.75 = 1575
        self.assertEqual(stats["energy_threshold"], 1575)

        lt.stop()

    def test_three_clean_windows_stop_without_extension(self):
        self.push_windows(1200, 1200, 1200)

        ok, reason = lt.start()
        self.assertTrue(ok, reason)

        # A fourth read would block on the empty fake capture until the
        # deadline and fail start(): ok=True proves the estimator
        # stopped at the target (D1).
        stats = lt.stats()
        self.assertEqual(stats["energy_threshold"], 2100)
        self.assertEqual(stats["calibration_windows"], 3)
        self.assertEqual(stats["calibration_rejected"], 0)
        self.assertEqual(stats["calibration_usable"], 3)
        self.assertFalse(stats["calibration_contaminated"])
        self.assertEqual(self.capture.chunks.qsize(), 0)

        lt.stop()

    def test_outlier_boundary_is_inclusive(self):
        # 2000 == CALIBRATION_OUTLIER_RATIO x median: retained.
        self.push_windows(1000, 1000, 2000)

        ok, reason = lt.start()
        self.assertTrue(ok, reason)

        stats = lt.stats()
        self.assertEqual(stats["calibration_windows"], 3)
        self.assertEqual(stats["calibration_rejected"], 0)
        self.assertEqual(stats["calibration_usable"], 3)
        self.assertEqual(stats["energy_threshold"], 1750)

        lt.stop()

    def test_rms_just_above_ratio_is_rejected_and_extended_for(self):
        # 2001 > 2 x median: rejected; the fourth window completes the
        # target instead of failing the session (D1 + D2 extension).
        self.push_windows(1000, 1000, 2001, 1000)

        ok, reason = lt.start()
        self.assertTrue(ok, reason)

        stats = lt.stats()
        self.assertEqual(stats["calibration_windows"], 4)
        self.assertEqual(stats["calibration_rejected"], 1)
        self.assertEqual(stats["calibration_usable"], 3)
        self.assertFalse(stats["calibration_contaminated"])
        self.assertEqual(stats["energy_threshold"], 1750)
        self.assertTrue(
            any("rejected=1" in message for message in self.calibration_log())
        )

        lt.stop()

    def test_high_energy_window_outlier_does_not_inflate_threshold(self):
        # The 7b.3a inflation case, now rejected: a single speech-like
        # window among quiet ones must not move the median center.
        self.push_windows(900, 1000, 2900, 1100)

        ok, reason = lt.start()
        self.assertTrue(ok, reason)

        stats = lt.stats()
        self.assertEqual(stats["calibration_windows"], 4)
        self.assertEqual(stats["calibration_rejected"], 1)
        self.assertEqual(stats["calibration_usable"], 3)
        self.assertEqual(stats["energy_threshold"], 1750)
        self.assertEqual(self.capture.chunks.qsize(), 0)

        lt.stop()

    def test_zero_windows_clamp_threshold_to_absolute_floor(self):
        # Acceptance invariant: a partial calibration must never
        # produce an uninitialized or unsafe threshold - the degenerate
        # all-zero ambient lands exactly on ABSOLUTE_FLOOR, never below.
        self.push_windows(0, 0, 0)

        ok, reason = lt.start()
        self.assertTrue(ok, reason)

        stats = lt.stats()
        self.assertEqual(stats["calibration_windows"], 3)
        self.assertEqual(stats["calibration_usable"], 3)
        self.assertEqual(stats["energy_threshold"], lt.ABSOLUTE_FLOOR)
        self.assertGreaterEqual(
            stats["energy_threshold"], lt.ABSOLUTE_FLOOR
        )
        self.assertFalse(stats["calibration_contaminated"])

        lt.stop()

    def test_all_elevated_windows_stay_stable_and_valid(self):
        # [4000, 4200, 4500]: every window is elevated but mutually
        # consistent, so the D1 median rule finds no outlier - an
        # all-elevated set is statistically indistinguishable from an
        # all-quiet one (quiet-window validation is deliberately absent
        # from 7b.4, pinned by test_speech_in_calibration_window_
        # inflates_threshold). The estimator must not crash, must
        # report the counts honestly, and must yield a floor-safe
        # threshold: contaminated stays False because all 3 windows are
        # usable - the flag means "fewer than K usable windows", not
        # "windows contain speech".
        self.push_windows(4000, 4200, 4500)

        ok, reason = lt.start()
        self.assertTrue(ok, reason)

        stats = lt.stats()
        self.assertEqual(stats["calibration_windows"], 3)
        self.assertEqual(stats["calibration_rejected"], 0)
        self.assertEqual(stats["calibration_usable"], 3)
        self.assertFalse(stats["calibration_contaminated"])
        # median 4200 -> 4200 x 1.75 = 7350 (the pinned 7b.3a
        # inflation case, accepted and unchanged)
        self.assertEqual(stats["energy_threshold"], 7350)
        self.assertGreaterEqual(
            stats["energy_threshold"], lt.ABSOLUTE_FLOOR
        )

        lt.stop()

    def test_hard_cap_returns_best_available_and_flags_contaminated(self):
        with mock.patch.object(lt, "CALIBRATION_TARGET_WINDOWS", 4):
            self.push_windows(900, 1000, 1100, 2900, 3000)

            ok, reason = lt.start()
            # D2/D3: best-available estimate, session still starts.
            self.assertTrue(ok, reason)

            stats = lt.stats()
            self.assertEqual(stats["calibration_windows"], 5)
            self.assertEqual(stats["calibration_rejected"], 2)
            self.assertEqual(stats["calibration_usable"], 3)
            self.assertTrue(stats["calibration_contaminated"])
            self.assertEqual(stats["energy_threshold"], 1750)
            self.assertEqual(self.capture.chunks.qsize(), 0)

        lt.stop()

    def test_stall_with_partial_windows_starts_best_available(self):
        # Capture stall (empty reads) after two windows: the deadline
        # only fails a calibration that produced no window at all
        # (that path is pinned by test_calibration_timeout_releases_
        # capture); with one or more windows D2 falls through to the
        # best-available estimate and calibration_contaminated=True
        # (work items 2/3), and the session starts.
        with (
            mock.patch.object(lt, "CALIBRATION_MAX_DURATION_S", 0.05),
            mock.patch.object(lt, "CALIBRATION_TIMEOUT_SLACK_S", 0.2),
        ):
            self.push_windows(1000, 1000)

            ok, reason = lt.start()
            self.assertTrue(ok, reason)

            stats = lt.stats()
            self.assertEqual(stats["calibration_windows"], 2)
            self.assertEqual(stats["calibration_rejected"], 0)
            self.assertEqual(stats["calibration_usable"], 2)
            self.assertTrue(stats["calibration_contaminated"])
            self.assertEqual(stats["energy_threshold"], 1750)
            self.assertEqual(self.capture.chunks.qsize(), 0)

        lt.stop()
        self.assertEqual(self.capture.stop_calls, 1)

    def test_calibration_audio_replayed_through_normal_assembler(self):
        # quiet, speech, quiet, quiet: the speech window happened DURING
        # calibration; the old pipeline consumed it, the new one replays
        # it (work items 4-5).
        self.engine.gate.clear()
        self.engine.entered.clear()
        self.push_windows(400, 3000, 400, 400)

        ok, reason = lt.start()
        self.assertTrue(ok, reason)

        stats = lt.stats()
        self.assertEqual(stats["energy_threshold"], 700)
        self.assertEqual(stats["calibration_windows"], 4)
        self.assertEqual(stats["calibration_rejected"], 1)

        self.assertTrue(
            wait_until(lambda: self.engine.entered.is_set()),
            "replayed calibration speech never reached the transcription "
            "thread",
        )
        call = self.engine.calls[0]
        # Exactly one commit: the speech window (chronological position
        # 2), silence-gated at 16000 samples; the quiet calibration
        # windows commit nothing above the 700 derived threshold.
        self.assertEqual(call["nframes"], 16000)
        self.assertEqual(call["first_sample"], 3000)

        self.engine.gate.set()
        self.assertTrue(
            wait_until(lambda: lt.stats()["transcript_segments"] == 1)
        )
        self.assertEqual(len(self.engine.calls), 1)

        lt.stop()
        self.assertEqual(lt.stats()["transcript_segments"], 1)

    def test_replay_orders_calibration_speech_before_live_speech(self):
        # Calibration audio: quiet, speech, quiet (exactly K=3 windows,
        # work items 4-5); live audio afterwards: a second, distinct
        # utterance. The replayed calibration speech must commit FIRST,
        # in chronological order, exactly once - no inversion (replay
        # completes before any live chunk is read) and no duplicate
        # replay (a second pass would produce a third commit).
        # quiet=1000 keeps the speech window inside the D1 boundary
        # (1850 <= 2 x median) while staying above the derived gate
        # (1850 > 1000 x 1.75 = 1750), so the calibration window is
        # both retained by the estimator and detectable on replay.
        self.engine.results.put("calibration speech")
        self.engine.results.put("live speech")
        self.push_windows(1000, 1850, 1000)

        ok, reason = lt.start()
        self.assertTrue(ok, reason)

        stats = lt.stats()
        self.assertEqual(stats["energy_threshold"], 1750)
        self.assertEqual(stats["calibration_rejected"], 0)
        self.assertEqual(stats["calibration_usable"], 3)
        self.assertFalse(stats["calibration_contaminated"])

        self.assertTrue(
            wait_until(lambda: lt.stats()["transcript_segments"] == 1),
            "replayed calibration speech never reached the transcript",
        )

        self.capture.push(tone(16000, level=4000))
        self.capture.push(silence(16000))
        self.assertTrue(
            wait_until(lambda: lt.stats()["transcript_segments"] == 2),
            "live speech never reached the transcript",
        )

        self.assertEqual(
            lt.get_transcript(), ["calibration speech", "live speech"]
        )
        self.assertEqual(self.engine.calls[0]["nframes"], 16000)
        self.assertEqual(self.engine.calls[0]["first_sample"], 1850)
        self.assertEqual(self.engine.calls[1]["nframes"], 16000)
        self.assertEqual(self.engine.calls[1]["first_sample"], 4000)

        lt.stop()
        self.assertEqual(lt.stats()["transcript_segments"], 2)
        self.assertEqual(len(self.engine.calls), 2)

    def test_zero_target_skips_sampling_yields_floor(self):
        with mock.patch.object(lt, "CALIBRATION_TARGET_WINDOWS", 0):
            ok, reason = lt.start()
            self.assertTrue(ok, reason)

            stats = lt.stats()
            self.assertEqual(stats["energy_threshold"], lt.ABSOLUTE_FLOOR)
            self.assertEqual(stats["calibration_windows"], 0)
            self.assertFalse(stats["calibration_contaminated"])

        lt.stop()

    def test_floor_protection_zero_rms_windows(self):
        # A: Floor protection - all zero RMS windows must yield
        # threshold = ABSOLUTE_FLOOR, never uninitialized or unsafe.
        self.push_windows(0, 0, 0)

        ok, reason = lt.start()
        self.assertTrue(ok, reason)

        stats = lt.stats()
        self.assertEqual(stats["calibration_windows"], 3)
        self.assertEqual(stats["calibration_rejected"], 0)
        self.assertEqual(stats["calibration_usable"], 3)
        self.assertEqual(stats["energy_threshold"], lt.ABSOLUTE_FLOOR)
        self.assertFalse(stats["calibration_contaminated"])

        lt.stop()

    def test_all_high_energy_windows_contaminated_but_valid(self):
        # B: All contaminated windows - when every sampled window is
        # high-energy (e.g., continuous speech during calibration), the
        # estimator must not crash and must produce a valid threshold.
        # Four consistent windows reach the target, so none are rejected
        # as outliers and the result is usable, not contaminated (the
        # contaminated-but-valid path at the cap is pinned by
        # test_cap_with_all_contaminated_flags_and_valid_threshold).
        with mock.patch.object(lt, "CALIBRATION_TARGET_WINDOWS", 4):
            self.push_windows(4000, 4200, 4500, 4300)

            ok, reason = lt.start()
            self.assertTrue(ok, reason)

            stats = lt.stats()
            self.assertEqual(stats["calibration_windows"], 4)
            self.assertEqual(stats["calibration_rejected"], 0)
            self.assertEqual(stats["calibration_usable"], 4)
            self.assertFalse(stats["calibration_contaminated"])
            self.assertGreaterEqual(
                stats["energy_threshold"], lt.ABSOLUTE_FLOOR
            )

        lt.stop()

    def test_cap_with_all_contaminated_flags_and_valid_threshold(self):
        # B variant: target=5, but the capture only ever delivers three
        # windows, so the deadline falls through to the best-available
        # estimate (work items 2/3) with 3 usable < 5 target windows:
        # calibration_contaminated=True and a valid high threshold.
        with (
            mock.patch.object(lt, "CALIBRATION_TARGET_WINDOWS", 5),
            mock.patch.object(lt, "CALIBRATION_MAX_DURATION_S", 0.05),
            mock.patch.object(lt, "CALIBRATION_TIMEOUT_SLACK_S", 0.2),
        ):
            self.push_windows(4000, 4200, 4500)

            ok, reason = lt.start()
            self.assertTrue(ok, reason)

            stats = lt.stats()
            self.assertEqual(stats["calibration_windows"], 3)
            self.assertEqual(stats["calibration_rejected"], 0)
            self.assertEqual(stats["calibration_usable"], 3)
            self.assertTrue(stats["calibration_contaminated"])
            self.assertGreaterEqual(
                stats["energy_threshold"], lt.ABSOLUTE_FLOOR
            )

        lt.stop()

    def test_replay_ordering_no_inversion_no_duplicate(self):
        # C: Replay ordering invariant - calibration audio (silence,
        # speech, silence) then new live audio (speech2) must produce
        # segments in order: speech (from calibration replay), then
        # speech2 (from live). No inversion, no duplicate replay.
        # Calibration: quiet (400), speech (3000), quiet (400), quiet (400)
        # Live: speech2 (4000)
        self.engine.gate.clear()
        self.engine.entered.clear()
        self.push_windows(400, 3000, 400, 400)

        ok, reason = lt.start()
        self.assertTrue(ok, reason)

        stats = lt.stats()
        self.assertEqual(stats["energy_threshold"], 700)
        self.assertEqual(stats["calibration_windows"], 4)
        self.assertEqual(stats["calibration_rejected"], 1)

        # Wait for calibration replay to reach transcription thread
        self.assertTrue(
            wait_until(lambda: self.engine.entered.is_set()),
            "calibration replay never reached transcription thread",
        )

        # First call should be the calibration speech window
        call1 = self.engine.calls[0]
        self.assertEqual(call1["nframes"], 16000)
        self.assertEqual(call1["first_sample"], 3000)

        # Let calibration commit finish (gets default_result)
        self.engine.gate.set()
        self.assertTrue(
            wait_until(lambda: lt.stats()["transcript_segments"] == 1),
            "calibration speech never committed",
        )

        # Now feed live speech2 and provide its expected result
        self.engine.results.put("speech2")
        self.capture.push(joined(tone(4800, level=4000), silence(11200)))

        self.assertTrue(
            wait_until(lambda: lt.stats()["transcript_segments"] == 2),
            "live speech2 never committed",
        )

        # Verify order: calibration speech first, then live speech2
        transcript = lt.get_transcript()
        self.assertEqual(len(transcript), 2)
        self.assertEqual(transcript[0], "committed transcript")  # calibration speech
        self.assertEqual(transcript[1], "speech2")  # live speech2

        # Exactly 2 Whisper calls: one for calibration speech, one for speech2
        self.assertEqual(len(self.engine.calls), 2)

        lt.stop()
        self.assertEqual(lt.stats()["transcript_segments"], 2)


if __name__ == "__main__":
    unittest.main()
