"""Amendment 4 verification: whisper_engine.cancel_requested lifecycle.

7b is forbidden from modifying whisper_engine.py. Before the live
pipeline relies on setting cancel_requested during stop, these tests
prove the existing engine already guarantees:

    stop live -> cancel current invocation
    -> next transcription does NOT inherit cancellation

The engine runs against a faked subprocess: no real whisper-cli, no
model inference, no config.json access, no production file changes.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

import whisper_engine

MODEL_SENTINEL = "ggml-cancel-lifecycle-test.bin"


class FakeProcess:
    def __init__(self, returncode=0, output="committed transcript"):
        self.returncode = returncode
        self.output = output
        self.killed = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def communicate(self):
        if self.killed:
            return "", ""
        return self.output, ""


class CancelLifecycleTests(unittest.TestCase):

    def setUp(self):
        descriptor, self.wav_path = tempfile.mkstemp(suffix=".wav")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(b"RIFF" + b"\x00" * 2000)

        self.original_cancel = whisper_engine.cancel_requested
        self.original_isfile = os.path.isfile
        self.created = []

    def tearDown(self):
        whisper_engine.cancel_requested = self.original_cancel
        os.remove(self.wav_path)

    def fake_isfile(self, path):
        sentinel = os.path.join("models", MODEL_SENTINEL)
        if str(path).endswith(sentinel):
            return True
        return self.original_isfile(path)

    def spawn_process(self, *args, **kwargs):
        process = FakeProcess(returncode=0, output="fresh transcript")
        self.created.append(process)
        return process

    def spawn_process_then_cancel(self, *args, **kwargs):
        whisper_engine.cancel_requested = True
        process = FakeProcess(returncode=None, output="partial transcript")
        self.created.append(process)
        return process

    def run_transcribe(self, popen_factory):
        config = {
            "model": MODEL_SENTINEL,
            "language_mode": "AUTO",
            "task_mode": "TRANSCRIBE",
            "threads": 2,
        }
        backend = {
            "executable": sys.executable,
            "backend": "cpu",
            "device": 0,
            "device_args": [],
        }

        with mock.patch.object(whisper_engine, "logger"), \
                mock.patch.object(whisper_engine, "load_config",
                                  return_value=config), \
                mock.patch.object(whisper_engine, "select_backend",
                                  return_value=backend), \
                mock.patch.object(whisper_engine.os.path, "isfile",
                                  side_effect=self.fake_isfile), \
                mock.patch.object(whisper_engine.subprocess, "Popen",
                                  side_effect=popen_factory):
            return whisper_engine.whisper_transcribe(self.wav_path)

    def test_leaked_cancel_flag_does_not_inherit_into_next_call(self):
        whisper_engine.cancel_requested = True

        text = self.run_transcribe(self.spawn_process)

        self.assertEqual(len(self.created), 1)
        self.assertFalse(self.created[0].killed)
        self.assertEqual(text, "fresh transcript")
        self.assertFalse(whisper_engine.cancel_requested)

    def test_cancel_requested_kills_current_invocation(self):
        whisper_engine.cancel_requested = False

        text = self.run_transcribe(self.spawn_process_then_cancel)

        self.assertEqual(len(self.created), 1)
        self.assertTrue(self.created[0].killed)
        self.assertEqual(text, "")

    def test_flag_reset_precedes_process_spawn(self):
        whisper_engine.cancel_requested = True
        observed = {}

        def factory(*args, **kwargs):
            observed["cancel_at_spawn"] = whisper_engine.cancel_requested
            process = FakeProcess(returncode=0, output="fresh transcript")
            self.created.append(process)
            return process

        self.run_transcribe(factory)

        self.assertFalse(observed["cancel_at_spawn"])


if __name__ == "__main__":
    unittest.main()
