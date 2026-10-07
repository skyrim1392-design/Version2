"""7d.0 integration boundary: microphone ownership, mutual exclusion,
shutdown contract. Gate tests for the future live supervisor.

Read-only design of the live supervisor / lifecycle boundary
(7d.1+ will implement; no production changes in 7d.0):

Ownership rule (Decision 1 - mutually exclusive modes):
    audio._mic_owner is exactly one of None / "f8" / "live".
    Re-claim by the same owner succeeds, by any other owner fails.
    The F8 recording pipeline and live capture both claim BEFORE any
    state mutation or InputStream creation, so contention is detected
    fail-fast and never opens hardware on the losing path.

Supervisor boundary (planned live_supervisor.py):
    start_live()  -> lt.start() (captures "live" ownership inside
                     live_capture.start), then lo.start(). On refusal:
                     no threads, ownership untouched, reason surfaced
                     to tray (Live: OFF).
    stop_live()   -> lt.stop() (releases "live"), then lo.stop()
                     (drain, STOPPED).
    on_f8_pressed -> if owner == "live": reject with notification
                     "Microphone is currently used by Live Transcription"
                     (main.f8_down gate point; audio layer already
                     returns MICROPHONE_BUSY as the underlying signal).
    shutdown()    -> ordered: quiesce F8 -> lt.stop() -> lo.stop() ->
                     recovery stop -> tray stop -> exit main loop.
                     Post-condition asserted here: mic owner None,
                     zero LiveTranscribe*/LiveOutput threads,
                     whisper_engine.whisper_process None.
    failures      -> stop clean, release mic, tray ERROR, wait for
                     user restart (Decision 5, no auto-restart).

State writers (Decision 3 - hybrid): supervisors and main push
app_state.state.status (OFF/LISTENING/TRANSCRIBING/DELIVERING/ERROR
reconciled onto the existing status field); the existing tray refresh
loop renders. No new Condition/Event architecture.

Tests here exercise only fail-fast paths and faked sessions, so they
need no microphone hardware: real claim/release bookkeeping, real
live_capture busy refusal (returns before config load or stream),
real audio busy refusal (returns before stream creation), and a
full shutdown sequence against ownership-faithful fakes.

Run: python -m unittest test_integration_7d -v
"""

import ast
import os
import threading
import unittest
from unittest import mock

import audio
import live_output as lo
import live_transcribe as lt
import whisper_engine
from test_live_output import DeliveryRecorder
from test_live_transcribe import (
    FakeLiveCapture,
    FakeWhisperEngine,
    session_threads,
)


def output_threads():
    return [
        thread
        for thread in threading.enumerate()
        if thread.name == "LiveOutput"
    ]


def all_live_threads():
    return session_threads() + output_threads()


class OwnedFakeLiveCapture(FakeLiveCapture):
    """FakeLiveCapture plus the real ownership contract of
    live_capture: claim on start, release on stop."""

    def start(self):
        ok, reason = super().start()
        if ok and not audio.claim_microphone("live"):
            self.active = False
            return False, "microphone busy (f8)"
        return ok, reason

    def stop(self):
        audio.release_microphone("live")
        return super().stop()


class MicrophoneOwnershipPrimitivesTests(unittest.TestCase):
    """The claim/release invariant the whole gate rests on."""

    def setUp(self):
        audio.release_microphone("f8")
        audio.release_microphone("live")

    def tearDown(self):
        audio.release_microphone("f8")
        audio.release_microphone("live")

    def test_claim_is_exclusive_between_f8_and_live(self):
        self.assertTrue(audio.claim_microphone("f8"))
        self.assertEqual(audio._mic_owner, "f8")

        self.assertFalse(audio.claim_microphone("live"))
        self.assertEqual(audio._mic_owner, "f8")

        self.assertTrue(audio.claim_microphone("f8"))
        self.assertEqual(audio._mic_owner, "f8")

        self.assertTrue(audio.release_microphone("f8"))
        self.assertIsNone(audio._mic_owner)

        self.assertTrue(audio.claim_microphone("live"))
        self.assertEqual(audio._mic_owner, "live")

        self.assertFalse(audio.claim_microphone("f8"))
        self.assertEqual(audio._mic_owner, "live")

    def test_release_is_owner_scoped(self):
        self.assertTrue(audio.claim_microphone("live"))

        self.assertFalse(audio.release_microphone("f8"))
        self.assertEqual(audio._mic_owner, "live")

        self.assertTrue(audio.release_microphone("live"))
        self.assertIsNone(audio._mic_owner)

        self.assertFalse(audio.release_microphone("live"))
        self.assertIsNone(audio._mic_owner)


class MutualExclusionTests(unittest.TestCase):
    """F8 active -> live start rejected. Live active -> F8 blocked.

    Both directions run the real production refusal paths, which are
    fail-fast: they return before any InputStream is created.
    """

    def setUp(self):
        lt.stop()
        audio.release_microphone("f8")
        audio.release_microphone("live")

    def tearDown(self):
        lt.stop()
        audio.release_microphone("f8")
        audio.release_microphone("live")

    def test_live_start_rejected_while_f8_owns_microphone(self):
        self.assertTrue(audio.claim_microphone("f8"))

        ok, reason = lt.start()

        self.assertFalse(ok)
        self.assertIn("busy", reason)
        self.assertFalse(lt.is_active())
        self.assertFalse(lt.stats()["worker_alive"])
        self.assertFalse(lt.stats()["transcription_alive"])
        self.assertFalse(lt.stats()["capture_active"])
        self.assertEqual(session_threads(), [])
        self.assertEqual(audio._mic_owner, "f8")

        self.assertTrue(audio.release_microphone("f8"))
        self.assertIsNone(audio._mic_owner)

    def test_f8_recording_rejected_while_live_owns_microphone(self):
        self.assertTrue(audio.claim_microphone("live"))
        status_before = audio.state.status
        recording_before = audio.is_recording

        with mock.patch.object(
            audio, "find_microphone", return_value=0
        ), mock.patch.object(
            audio.config,
            "load_config",
            return_value={
                "audio": {"input_sample_rate": 44100, "channels": 1},
                "microphone": "auto",
            },
        ):
            started = audio.start_recording()

        self.assertIs(started, audio.MICROPHONE_BUSY)
        self.assertFalse(started)
        self.assertEqual(audio._mic_owner, "live")
        self.assertEqual(audio.state.status, status_before)
        self.assertEqual(audio.is_recording, recording_before)

    def test_claim_precedes_stream_creation_in_capture_paths(self):
        checks = [
            (os.path.join(os.path.dirname(__file__), "audio.py"),
             "start_recording"),
            (os.path.join(os.path.dirname(__file__), "live_capture.py"),
             "start"),
        ]

        for path, function_name in checks:
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())

            function = next(
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef)
                and node.name == function_name
            )

            claim_lines = [
                node.lineno
                for node in ast.walk(function)
                if isinstance(node, ast.Call)
                and (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "claim_microphone"
                    or isinstance(node.func, ast.Attribute)
                    and node.func.attr == "claim_microphone"
                )
            ]
            stream_lines = [
                node.lineno
                for node in ast.walk(function)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "InputStream"
            ]

            self.assertTrue(
                claim_lines,
                f"no claim_microphone call in {function_name}",
            )
            self.assertTrue(
                stream_lines,
                f"no InputStream creation in {function_name}",
            )
            self.assertLess(
                min(claim_lines),
                min(stream_lines),
                f"{function_name}: claim must precede stream creation",
            )


class ShutdownContractTests(unittest.TestCase):
    """Live active -> ordered shutdown -> mic released, threads zero,
    whisper process none. Runs real lt/lo lifecycle against the
    ownership-faithful capture fake and the delivery seam."""

    def setUp(self):
        lt.stop()
        lo.stop()
        audio.release_microphone("f8")
        audio.release_microphone("live")

        self.capture = OwnedFakeLiveCapture()
        self.engine = FakeWhisperEngine()
        self.deliver = DeliveryRecorder()

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

        engine_patcher = mock.patch.object(
            lt, "whisper_engine", self.engine
        )
        engine_patcher.start()
        self.addCleanup(engine_patcher.stop)

        logger_patcher = mock.patch.object(lt, "logger")
        logger_patcher.start()
        self.addCleanup(logger_patcher.stop)

        deliver_patcher = mock.patch.object(
            lo, "_deliver_text", self.deliver
        )
        deliver_patcher.start()
        self.addCleanup(deliver_patcher.stop)

        warning_patcher = mock.patch.object(lo, "_emit_warning")
        warning_patcher.start()
        self.addCleanup(warning_patcher.stop)

        output_logger_patcher = mock.patch.object(lo, "logger")
        output_logger_patcher.start()
        self.addCleanup(output_logger_patcher.stop)

        self.addCleanup(lt.stop)
        self.addCleanup(lo.stop)
        self.addCleanup(lambda: audio.release_microphone("live"))
        self.addCleanup(lambda: audio.release_microphone("f8"))

    def test_ordered_shutdown_releases_mic_threads_and_whisper(self):
        self.assertIsNone(whisper_engine.whisper_process)

        ok, reason = lt.start()
        self.assertTrue(ok, reason)
        ok, reason = lo.start()
        self.assertTrue(ok, reason)

        self.assertEqual(audio._mic_owner, "live")
        self.assertTrue(lt.is_active())
        self.assertEqual(len(session_threads()), 2)
        self.assertEqual(len(output_threads()), 1)
        self.assertEqual(lo.stats()["state"], "RUNNING")

        ok, reason = lt.stop()
        self.assertTrue(ok, reason)
        ok, reason = lo.stop()
        self.assertTrue(ok, reason)

        self.assertIsNone(audio._mic_owner)
        self.assertEqual(all_live_threads(), [])
        self.assertFalse(lt.is_active())
        self.assertFalse(lt.stats()["worker_alive"])
        self.assertFalse(lt.stats()["transcription_alive"])
        self.assertFalse(lt.stats()["capture_active"])
        self.assertEqual(lo.stats()["state"], "STOPPED")
        self.assertFalse(lo.stats()["worker_alive"])
        self.assertIsNone(whisper_engine.whisper_process)


if __name__ == "__main__":
    unittest.main()
