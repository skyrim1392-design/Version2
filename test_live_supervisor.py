"""7d.1 live supervisor lifecycle tests.

Contract under test (7d.1 approved scope):

- start_live:  owner == "live", lt active, lo running,
               state.live_status == LIVE_LISTENING
- stop:        lt stopped, lo stopped, owner None,
               state.live_status == LIVE_IDLE
- shutdown race: the shutdown flag is set BEFORE teardown, and once set
               every new request (F8 gate, start, stop, tray toggle)
               is rejected without blocking while teardown runs;
               cleanup then completes: owner None, zero session
               threads, lo STOPPED, status LIVE_IDLE, shutdown
               idempotent afterwards.
- failure path: lt.start() refusal -> owner released, lo not running,
               LIVE_ERROR; the user recovers by starting again
               (wait-for-user, Decision 5 - no auto-restart anywhere).
- reconcile:   a LISTENING session whose lt died is cleaned up
               (lt/lo stopped, mic released) and reported LIVE_ERROR.

Contract under test (7d.2 approved scope):

- quiescence:  shutdown waits for EVERY F8 state (RECORDING,
               PROCESSING, OUTPUTTING) and both F8 flags before
               teardown (Decision 2), bounded by QUIESCE_TIMEOUT.
- timeout:     when that wait expires, an outstanding Whisper child
               is terminated and reaped before teardown continues
               (Decision 3, fault-injected - no real whisper-cli); a
               clean quiesce never touches the child handle.
- LIVE_ERROR:  F8 stays allowed, toggle() restarts the session, and
               reconcile() never auto-restarts (Decision 8 / D5).

Runs entirely against faked capture/delivery seams - no microphone
hardware, no whisper subprocess, no clipboard.

Run: python -m unittest test_live_supervisor -v
"""

import threading
import time
import unittest
from unittest import mock

import audio
import live_output as lo
import live_supervisor
import live_transcribe as lt
import whisper_engine
from app_state import state
from test_live_output import DeliveryRecorder
from test_live_transcribe import (
    FakeLiveCapture,
    FakeWhisperEngine,
    session_threads,
)

F8_BLOCKED_MESSAGE = "Microphone is currently used by Live Transcription"


def output_threads():
    return [
        thread
        for thread in threading.enumerate()
        if thread.name == "LiveOutput"
    ]


def all_live_threads():
    return session_threads() + output_threads()


def reset_supervisor():
    lt.stop()
    lo.stop()
    live_supervisor._shutdown.clear()

    audio.release_microphone("live")
    audio.release_microphone("f8")

    with state.lock:
        state.live_status = live_supervisor.LIVE_IDLE


def call_during_shutdown(test, function, timeout=5.0):
    """Run a supervisor request from this thread with a hard timeout.

    During the race test teardown holds the supervisor lock; requests
    must reject on the shutdown flag WITHOUT reaching the lock. A
    blocked request fails the test instead of hanging it.
    """
    result = {}

    def runner():
        try:
            result["value"] = function()
        except Exception as error:  # noqa: BLE001 - relay to test thread
            result["error"] = error

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout)

    test.assertFalse(
        thread.is_alive(),
        f"request {function.__name__} blocked during shutdown",
    )

    if "error" in result:
        raise result["error"]

    return result["value"]


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


class SupervisorSessionBase(unittest.TestCase):
    """Seams identical to the 7c session suites: faked capture,
    faked whisper engine, delivery recorder, silenced loggers."""

    def setUp(self):
        reset_supervisor()

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

        supervisor_logger_patcher = mock.patch.object(
            live_supervisor, "logger"
        )
        supervisor_logger_patcher.start()
        self.addCleanup(supervisor_logger_patcher.stop)

        self.addCleanup(lt.stop)
        self.addCleanup(lo.stop)
        self.addCleanup(lambda: audio.release_microphone("live"))
        self.addCleanup(lambda: audio.release_microphone("f8"))


class LifecycleTests(SupervisorSessionBase):
    def test_start_live_acquires_ownership_and_starts_both(self):
        ok, reason = live_supervisor.start()

        self.assertTrue(ok, reason)
        self.assertEqual(state.live_status, live_supervisor.LIVE_LISTENING)
        self.assertEqual(audio._mic_owner, "live")
        self.assertTrue(lt.is_active())
        self.assertEqual(lo.stats()["state"], "RUNNING")
        self.assertEqual(len(session_threads()), 2)
        self.assertEqual(len(output_threads()), 1)

    def test_stop_live_stops_both_and_releases_ownership(self):
        ok, reason = live_supervisor.start()
        self.assertTrue(ok, reason)

        ok, reason = live_supervisor.stop()

        self.assertTrue(ok, reason)
        self.assertEqual(state.live_status, live_supervisor.LIVE_IDLE)
        self.assertIsNone(audio._mic_owner)
        self.assertFalse(lt.is_active())
        self.assertFalse(lt.stats()["worker_alive"])
        self.assertFalse(lt.stats()["transcription_alive"])
        self.assertEqual(lo.stats()["state"], "STOPPED")
        self.assertEqual(all_live_threads(), [])

    def test_f8_request_rejected_while_live_listening(self):
        ok, reason = live_supervisor.start()
        self.assertTrue(ok, reason)

        allowed, code = live_supervisor.f8_request()

        self.assertFalse(allowed)
        self.assertEqual(code, live_supervisor.F8_BLOCKED)
        self.assertEqual(live_supervisor.F8_BLOCKED_MESSAGE, F8_BLOCKED_MESSAGE)

        ok, reason = live_supervisor.stop()
        self.assertTrue(ok, reason)

        allowed, code = live_supervisor.f8_request()
        self.assertTrue(allowed)
        self.assertIsNone(code)


class ShutdownRaceTests(SupervisorSessionBase):
    def test_requests_during_shutdown_are_rejected_and_cleanup_completes(
        self,
    ):
        ok, reason = live_supervisor.start()
        self.assertTrue(ok, reason)

        real_stop = lt.stop
        gate = threading.Event()
        entered = threading.Event()

        def slow_stop():
            entered.set()
            gate.wait(10.0)
            return real_stop()

        with mock.patch.object(lt, "stop", slow_stop):
            worker = threading.Thread(
                target=live_supervisor.shutdown,
                name="ShutdownUnderTest",
            )
            worker.start()

            self.assertTrue(
                entered.wait(5.0),
                "shutdown never reached the teardown stage",
            )

            self.assertTrue(live_supervisor.is_shutting_down())

            ok, code = call_during_shutdown(self, live_supervisor.start)
            self.assertFalse(ok)
            self.assertEqual(code, "shutting_down")

            ok, code = call_during_shutdown(self, live_supervisor.stop)
            self.assertFalse(ok)
            self.assertEqual(code, "shutting_down")

            ok, code = call_during_shutdown(self, live_supervisor.toggle)
            self.assertFalse(ok)
            self.assertEqual(code, "shutting_down")

            allowed, code = call_during_shutdown(
                self, live_supervisor.f8_request
            )
            self.assertFalse(allowed)
            self.assertEqual(code, "shutting_down")

            gate.set()
            worker.join(10.0)
            self.assertFalse(worker.is_alive())

        self.assertIsNone(audio._mic_owner)
        self.assertEqual(all_live_threads(), [])
        self.assertFalse(lt.is_active())
        self.assertEqual(lo.stats()["state"], "STOPPED")
        self.assertEqual(state.live_status, live_supervisor.LIVE_IDLE)

        live_supervisor.shutdown()
        self.assertIsNone(audio._mic_owner)
        self.assertEqual(all_live_threads(), [])


class FailurePathTests(SupervisorSessionBase):
    def test_start_failure_enters_error_state_with_clean_ownership(self):
        self.capture.start_result = (False, "refused")

        ok, reason = live_supervisor.start()

        self.assertFalse(ok)
        self.assertEqual(reason, "refused")
        self.assertEqual(state.live_status, live_supervisor.LIVE_ERROR)
        self.assertIsNone(audio._mic_owner)
        self.assertFalse(lt.is_active())
        self.assertEqual(lo.stats()["state"], "STOPPED")
        self.assertEqual(all_live_threads(), [])

        self.capture.start_result = (True, "started")

        ok, reason = live_supervisor.toggle()

        self.assertTrue(ok, reason)
        self.assertEqual(state.live_status, live_supervisor.LIVE_LISTENING)
        self.assertEqual(audio._mic_owner, "live")
        self.assertTrue(lt.is_active())
        self.assertEqual(lo.stats()["state"], "RUNNING")


class ReconcileTests(SupervisorSessionBase):
    def test_reconcile_cleans_dead_session_and_enters_error(self):
        ok, reason = live_supervisor.start()
        self.assertTrue(ok, reason)

        live_supervisor.reconcile()
        self.assertEqual(state.live_status, live_supervisor.LIVE_LISTENING)

        lt.stop()

        self.assertEqual(state.live_status, live_supervisor.LIVE_LISTENING)

        live_supervisor.reconcile()

        self.assertEqual(state.live_status, live_supervisor.LIVE_ERROR)
        self.assertIsNone(audio._mic_owner)
        self.assertEqual(lo.stats()["state"], "STOPPED")
        self.assertEqual(all_live_threads(), [])


class LiveErrorStateTests(SupervisorSessionBase):
    """Decision 8: LIVE_ERROR keeps F8 open and is recovered only by an
    explicit user action; D5 (no auto-restart) stays in force."""

    def enter_live_error(self):
        self.capture.start_result = (False, "refused")

        ok, reason = live_supervisor.start()

        self.assertFalse(ok)
        self.assertEqual(reason, "refused")
        self.assertEqual(state.live_status, live_supervisor.LIVE_ERROR)

    def test_f8_remains_allowed_in_live_error(self):
        self.enter_live_error()

        allowed, code = live_supervisor.f8_request()

        self.assertTrue(allowed)
        self.assertIsNone(code)

    def test_toggle_from_live_error_starts_a_new_session(self):
        self.enter_live_error()
        self.capture.start_result = (True, "started")

        ok, reason = live_supervisor.toggle()

        self.assertTrue(ok, reason)
        self.assertEqual(state.live_status, live_supervisor.LIVE_LISTENING)
        self.assertEqual(audio._mic_owner, "live")
        self.assertTrue(lt.is_active())
        self.assertEqual(lo.stats()["state"], "RUNNING")

    def test_reconcile_never_restarts_from_live_error(self):
        self.enter_live_error()

        live_supervisor.reconcile()

        self.assertEqual(state.live_status, live_supervisor.LIVE_ERROR)
        self.assertFalse(lt.is_active())
        self.assertIsNone(audio._mic_owner)
        self.assertEqual(lo.stats()["state"], "STOPPED")
        self.assertEqual(all_live_threads(), [])


class F8StateBase(SupervisorSessionBase):
    """Supervisor seams plus restore of the F8-owned state fields that
    the quiescence tests drive directly."""

    def setUp(self):
        super().setUp()
        self.addCleanup(self.restore_f8_state)

        supervisor_logger_patcher = mock.patch.object(
            live_supervisor, "logger"
        )
        self.supervisor_log = supervisor_logger_patcher.start()
        self.addCleanup(supervisor_logger_patcher.stop)

    @staticmethod
    def restore_f8_state():
        with state.lock:
            state.status = "IDLE"
            state.recording = False
            state.processing = False

    def drive_f8_status(self, status):
        """Simulate F8 sitting in one of its own states."""
        with state.lock:
            state.status = status
            state.recording = status == "RECORDING"
            state.processing = status == "PROCESSING"

    def timeout_warnings(self):
        return [
            call.args[0]
            for call in self.supervisor_log.warning.call_args_list
            if call.args
        ]


class QuiescenceTests(F8StateBase):
    """Decision 2: teardown must not proceed until F8 has returned to
    IDLE, subject to the existing QUIESCE_TIMEOUT."""

    def test_quiesce_waits_for_every_f8_state(self):
        for status in live_supervisor.F8_BUSY_STATUSES:
            with self.subTest(status=status):
                reset_supervisor()
                self.restore_f8_state()
                self.drive_f8_status(status)

                def release():
                    time.sleep(0.2)
                    self.restore_f8_state()

                releaser = threading.Thread(target=release, daemon=True)
                releaser.start()

                started = time.monotonic()
                live_supervisor.shutdown()
                elapsed = time.monotonic() - started

                releaser.join(5.0)

                self.assertGreaterEqual(elapsed, 0.15)
                self.assertEqual(self.timeout_warnings(), [])
                self.assertTrue(live_supervisor.is_shutting_down())
                self.assertEqual(lo.stats()["state"], "STOPPED")
                self.assertEqual(state.live_status, live_supervisor.LIVE_IDLE)

    def test_quiesce_returns_immediately_when_f8_is_idle(self):
        started = time.monotonic()
        live_supervisor.shutdown()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.5)
        self.assertEqual(self.timeout_warnings(), [])


class QuiesceTimeoutTests(F8StateBase):
    """Decision 3: a quiesce timeout terminates and reaps an
    outstanding Whisper child. Fault-injected throughout: the child is
    a fake handle, never a real whisper-cli invocation."""

    def setUp(self):
        super().setUp()
        self.original_child = whisper_engine.whisper_process
        self.addCleanup(self.restore_child)

        whisper_logger_patcher = mock.patch.object(whisper_engine, "logger")
        whisper_logger_patcher.start()
        self.addCleanup(whisper_logger_patcher.stop)

    def restore_child(self):
        whisper_engine.whisper_process = self.original_child

    def shorten_quiesce(self, timeout=0.3):
        patcher = mock.patch.object(
            live_supervisor, "QUIESCE_TIMEOUT", timeout
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_timeout_terminates_and_reaps_outstanding_child(self):
        child = FakeWhisperChild()
        whisper_engine.whisper_process = child
        self.drive_f8_status("PROCESSING")
        self.shorten_quiesce()

        started = time.monotonic()
        live_supervisor.shutdown()
        elapsed = time.monotonic() - started

        self.assertTrue(child.killed)
        self.assertTrue(child.waited)
        self.assertIsNone(whisper_engine.whisper_process)
        self.assertIn(
            "Shutdown quiesce timed out waiting for F8 processing",
            self.timeout_warnings(),
        )
        self.assertGreaterEqual(elapsed, 0.28)

        self.assertEqual(lo.stats()["state"], "STOPPED")
        self.assertEqual(state.live_status, live_supervisor.LIVE_IDLE)
        self.assertIsNone(audio._mic_owner)
        self.assertEqual(all_live_threads(), [])

    def test_timeout_without_child_still_completes_teardown(self):
        whisper_engine.whisper_process = None
        self.drive_f8_status("OUTPUTTING")
        self.shorten_quiesce()

        live_supervisor.shutdown()

        self.assertIsNone(whisper_engine.whisper_process)
        self.assertIn(
            "Shutdown quiesce timed out waiting for F8 processing",
            self.timeout_warnings(),
        )
        self.assertEqual(lo.stats()["state"], "STOPPED")
        self.assertEqual(state.live_status, live_supervisor.LIVE_IDLE)

    def test_clean_quiesce_never_touches_the_child_handle(self):
        child = FakeWhisperChild()
        whisper_engine.whisper_process = child
        self.restore_f8_state()

        live_supervisor.shutdown()

        self.assertFalse(child.killed)
        self.assertFalse(child.waited)
        self.assertIs(whisper_engine.whisper_process, child)
        self.assertEqual(self.timeout_warnings(), [])


class FakeWhisperChild:
    """Fault-injected whisper-cli handle: records kill/wait, owns no
    operating-system process."""

    def __init__(self):
        self.killed = False
        self.waited = False
        self.returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited = True
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


if __name__ == "__main__":
    unittest.main()
