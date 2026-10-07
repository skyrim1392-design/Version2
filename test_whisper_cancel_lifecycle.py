"""M1 verification (7c.3): serialized whisper invocation ownership.

Replaces the pre-7c global cancel_requested lifecycle tests: the flag
is gone, so the contract under test is whisper_transcribe's internal
_invocation lock plus its per-call cancel_event. Proven here:

    A. two simultaneous calls peak at exactly one active invocation;
       the second waits for the first to finish
    B. an already-cancelled event returns "" and never spawns
    C. cancelling one invocation stops only that invocation
    D. an F8-style caller (cancel_event=None) is unaffected by a
       separate live cancellation event set while it runs
    E. a live caller waiting behind another invocation returns ""
       without spawning once its stop event is set

The engine runs against faked subprocesses: no real whisper-cli, no
model inference, no config.json access, no production file changes.
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import whisper_engine

MODEL_SENTINEL = "ggml-cancel-lifecycle-test.bin"

ACQUISITION_CANCEL_LOG = (
    "Whisper invocation cancelled before acquisition"
)


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class ControlledProcess:
    """Fake subprocess: completion is test-driven, kill is recorded.

    auto_complete processes finish on the first poll; otherwise
    completion waits for complete(), so an invocation stays inside the
    engine's poll loop exactly as long as the test needs.
    """

    def __init__(self, output="transcript text", auto_complete=False):
        self.returncode = None
        self.output = output
        self.killed = False
        self._complete = threading.Event()
        if auto_complete:
            self._complete.set()
            self.returncode = 0

    def complete(self):
        self._complete.set()

    def poll(self):
        if self._complete.is_set() and self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._complete.set()

    def communicate(self):
        if self.killed:
            return "", ""
        return self.output, ""


class WhisperInvokeTestBase(unittest.TestCase):
    """Faked config, backend, model check, subprocess, and logger.

    Active for the whole test (started in setUp) so worker threads
    run entirely under the patches. spawn_count counts Popen calls;
    peak is the maximum number of spawned-but-uncommunicated
    processes, i.e. the maximum number of concurrently active
    invocations.
    """

    def setUp(self):
        descriptor, self.wav_path = tempfile.mkstemp(suffix=".wav")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(b"RIFF" + b"\x00" * 2000)
        # Registered first so it runs last (LIFO): os.remove happens
        # after every patch, including os.path.isfile, is restored.
        self.addCleanup(os.remove, self.wav_path)

        self.original_isfile = os.path.isfile
        self.processes = []
        self.process_factory = lambda: ControlledProcess(
            auto_complete=True
        )
        self.state_lock = threading.Lock()
        self.state = {"active": 0, "peak": 0, "spawn_count": 0}
        self.ready = {}

        logger_patcher = mock.patch.object(whisper_engine, "logger")
        self.logger_mock = logger_patcher.start()
        self.addCleanup(logger_patcher.stop)

        config_patcher = mock.patch.object(
            whisper_engine,
            "load_config",
            return_value={
                "model": MODEL_SENTINEL,
                "language_mode": "AUTO",
                "task_mode": "TRANSCRIBE",
                "threads": 2,
            },
        )
        config_patcher.start()
        self.addCleanup(config_patcher.stop)

        backend_patcher = mock.patch.object(
            whisper_engine,
            "select_backend",
            return_value={
                "executable": sys.executable,
                "backend": "cpu",
                "device": 0,
                "device_args": [],
            },
        )
        backend_patcher.start()
        self.addCleanup(backend_patcher.stop)

        isfile_patcher = mock.patch.object(
            whisper_engine.os.path,
            "isfile",
            side_effect=self.fake_isfile,
        )
        isfile_patcher.start()
        self.addCleanup(isfile_patcher.stop)

        popen_patcher = mock.patch.object(
            whisper_engine.subprocess,
            "Popen",
            side_effect=self._popen,
        )
        popen_patcher.start()
        self.addCleanup(popen_patcher.stop)

    def fake_isfile(self, path):
        sentinel = os.path.join("models", MODEL_SENTINEL)
        if str(path).endswith(sentinel):
            return True
        return self.original_isfile(path)

    def _popen(self, *args, **kwargs):
        process = self.process_factory()

        with self.state_lock:
            self.state["spawn_count"] += 1
            self.state["active"] += 1
            self.state["peak"] = max(
                self.state["peak"], self.state["active"]
            )

        original_communicate = process.communicate

        def communicate():
            with self.state_lock:
                self.state["active"] -= 1
            return original_communicate()

        process.communicate = communicate
        self.processes.append(process)
        return process

    def script_processes(self, *processes):
        """Hand out these processes, in order, as spawns occur."""
        pending = list(processes)
        self.process_factory = lambda: pending.pop(0)

    def run_transcribe(self, cancel_event=None):
        return whisper_engine.whisper_transcribe(
            self.wav_path, cancel_event=cancel_event
        )

    def start_caller(self, name, cancel_event, results):
        """Run one caller on its own thread; ready fires just before
        the call so a test can prove the caller has started."""
        ready = threading.Event()
        self.ready[name] = ready

        def run():
            ready.set()
            results[name] = self.run_transcribe(cancel_event)

        thread = threading.Thread(target=run, name=name, daemon=True)
        thread.start()
        return thread

    def logged_info(self):
        return [
            call.args[0]
            for call in self.logger_mock.info.call_args_list
            if call.args
        ]

    def join_thread(self, thread):
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())


class InvocationLifecycleTests(WhisperInvokeTestBase):

    def test_simultaneous_calls_peak_at_one_active_invocation(self):
        first_process = ControlledProcess(output="first transcript")
        second_process = ControlledProcess(
            output="second transcript", auto_complete=True
        )
        self.script_processes(first_process, second_process)

        results = {}
        first_thread = self.start_caller("first", None, results)
        self.assertTrue(
            wait_until(lambda: self.state["spawn_count"] == 1)
        )

        second_thread = self.start_caller("second", None, results)
        self.assertTrue(self.ready["second"].wait(5.0))
        time.sleep(0.15)

        # The second call must be waiting on the lock, not invoking.
        self.assertEqual(self.state["spawn_count"], 1)
        self.assertNotIn("second", results)

        first_process.complete()
        self.join_thread(first_thread)
        self.join_thread(second_thread)

        self.assertEqual(
            results,
            {"first": "first transcript", "second": "second transcript"},
        )
        self.assertEqual(self.state["spawn_count"], 2)
        self.assertEqual(self.state["peak"], 1)
        self.assertFalse(first_process.killed)
        self.assertFalse(second_process.killed)

    def test_pre_cancelled_event_returns_empty_without_spawning(self):
        cancel_event = threading.Event()
        cancel_event.set()

        text = self.run_transcribe(cancel_event=cancel_event)

        self.assertEqual(text, "")
        self.assertEqual(self.state["spawn_count"], 0)
        self.assertEqual(self.processes, [])
        self.assertIsNone(whisper_engine.whisper_process)
        self.assertIn(ACQUISITION_CANCEL_LOG, self.logged_info())

    def test_cancelling_live_invocation_leaves_other_caller_running(self):
        live_process = ControlledProcess(output="partial transcript")
        other_process = ControlledProcess(
            output="other transcript", auto_complete=True
        )
        self.script_processes(live_process, other_process)

        live_event = threading.Event()
        other_event = threading.Event()

        results = {}
        live_thread = self.start_caller("live", live_event, results)
        self.assertTrue(
            wait_until(lambda: self.state["spawn_count"] == 1)
        )

        other_thread = self.start_caller("other", other_event, results)
        self.assertTrue(self.ready["other"].wait(5.0))
        time.sleep(0.15)
        self.assertEqual(self.state["spawn_count"], 1)

        live_event.set()
        self.join_thread(live_thread)
        self.assertEqual(results["live"], "")
        self.assertTrue(live_process.killed)

        self.join_thread(other_thread)
        self.assertEqual(results["other"], "other transcript")
        self.assertFalse(other_process.killed)
        self.assertFalse(other_event.is_set())
        self.assertEqual(self.state["spawn_count"], 2)
        self.assertEqual(self.state["peak"], 1)

    def test_f8_caller_unaffected_by_separate_live_event(self):
        f8_process = ControlledProcess(output="f8 transcript")
        spare_process = ControlledProcess(auto_complete=True)
        self.script_processes(f8_process, spare_process)

        live_event = threading.Event()

        results = {}
        f8_thread = self.start_caller("f8", None, results)
        self.assertTrue(
            wait_until(lambda: self.state["spawn_count"] == 1)
        )

        # A live caller queues behind the F8 invocation, then its
        # session stops: the live event is set while F8 is in flight.
        live_thread = self.start_caller("live", live_event, results)
        self.assertTrue(self.ready["live"].wait(5.0))
        time.sleep(0.15)
        self.assertEqual(self.state["spawn_count"], 1)

        live_event.set()
        self.join_thread(live_thread)
        self.assertEqual(results["live"], "")

        # F8 must be untouched by that cancellation: still in flight,
        # process alive, and no second spawn.
        self.assertEqual(self.state["spawn_count"], 1)
        self.assertFalse(f8_process.killed)
        self.assertNotIn("f8", results)

        f8_process.complete()
        self.join_thread(f8_thread)
        self.assertEqual(results["f8"], "f8 transcript")
        self.assertFalse(f8_process.killed)
        self.assertEqual(self.state["spawn_count"], 1)

    def test_waiting_live_caller_cancels_without_spawning(self):
        holder_process = ControlledProcess(output="holder transcript")
        spare_process = ControlledProcess(auto_complete=True)
        self.script_processes(holder_process, spare_process)

        wait_event = threading.Event()

        results = {}
        holder_thread = self.start_caller("holder", None, results)
        self.assertTrue(
            wait_until(lambda: self.state["spawn_count"] == 1)
        )

        waiter_thread = self.start_caller("waiter", wait_event, results)
        self.assertTrue(self.ready["waiter"].wait(5.0))
        time.sleep(0.15)
        self.assertEqual(self.state["spawn_count"], 1)

        wait_event.set()
        self.join_thread(waiter_thread)
        self.assertEqual(results["waiter"], "")
        self.assertEqual(self.state["spawn_count"], 1)
        self.assertEqual(len(self.processes), 1)
        self.assertIn(ACQUISITION_CANCEL_LOG, self.logged_info())

        holder_process.complete()
        self.join_thread(holder_thread)
        self.assertEqual(results["holder"], "holder transcript")
        self.assertEqual(self.state["spawn_count"], 1)


if __name__ == "__main__":
    unittest.main()
