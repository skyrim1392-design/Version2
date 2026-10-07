"""7d.2 config persistence tests (approved decision 9).

Every test runs against a temporary config file: `config.CONFIG_FILE`
is redirected for the duration of the test and the repository's
config.json is byte-compared before and after, so a test can never
write the runtime file (D9).

Contract under test:

- round trip:  set_config_value -> load_config through CONFIG_FILE,
               atomic replacement, no leftover config_*.tmp files.
- defaults:    a missing file is created with DEFAULT_CONFIG
               (live_mode_enabled False); an unreadable file falls
               back to defaults without raising.
- D8:          tray.live_toggle persists live_mode_enabled only after
               a successful start; a refused start writes nothing at
               all; stopping writes False.
- decision 6:  main.py holds no config writer and still reads
               live_mode_enabled at startup, i.e. a persisted True
               simply causes another start attempt on the next launch
               (behaviour documented, not changed).

Run: python -m unittest test_config_persistence -v
"""

import ast
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import config
import tray
from app_state import state
from test_live_supervisor import SupervisorSessionBase

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def read_bytes(path):
    if not os.path.exists(path):
        return None

    with open(path, "rb") as handle:
        return handle.read()


def assert_file_unchanged(path, before):
    after = read_bytes(path)

    if before is None:
        if after is not None:
            raise AssertionError(f"{path} was created by the test")
    elif after != before:
        raise AssertionError(f"{path} was modified by the test")


def use_temp_config(test):
    """Redirect config.CONFIG_FILE to a fresh temp file, silence the
    config and tray loggers, and assert the repository config.json is
    byte-identical when the test ends."""
    real_path = config.CONFIG_FILE
    real_bytes = read_bytes(real_path)

    directory = tempfile.mkdtemp(prefix="va_config_test_")
    test.addCleanup(shutil.rmtree, directory, ignore_errors=True)

    config_file = os.path.join(directory, "config.json")

    file_patcher = mock.patch.object(config, "CONFIG_FILE", config_file)
    file_patcher.start()
    test.addCleanup(file_patcher.stop)

    config_logger_patcher = mock.patch.object(config, "logger")
    config_logger_patcher.start()
    test.addCleanup(config_logger_patcher.stop)

    tray_logger_patcher = mock.patch.object(tray, "logger")
    tray_logger_patcher.start()
    test.addCleanup(tray_logger_patcher.stop)

    test.addCleanup(assert_file_unchanged, real_path, real_bytes)

    return config_file


class TempConfigBase(unittest.TestCase):
    def setUp(self):
        self.config_file = use_temp_config(self)


class RoundTripTests(TempConfigBase):
    def test_set_value_round_trips_without_temp_leftovers(self):
        self.assertFalse(os.path.exists(self.config_file))

        config.set_config_value("live_mode_enabled", True)

        self.assertTrue(os.path.exists(self.config_file))
        self.assertTrue(config.load_config().get("live_mode_enabled"))

        config.set_config_value("live_mode_enabled", False)

        self.assertFalse(config.load_config().get("live_mode_enabled"))

        leftovers = [
            name
            for name in os.listdir(os.path.dirname(self.config_file))
            if name.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [])

    def test_missing_file_is_created_with_defaults(self):
        loaded = config.load_config()

        self.assertFalse(loaded["live_mode_enabled"])
        self.assertEqual(loaded["model"], config.DEFAULT_CONFIG["model"])
        self.assertTrue(os.path.exists(self.config_file))

        reloaded = config.load_config()
        self.assertEqual(reloaded["live_mode_enabled"], False)
        self.assertEqual(reloaded["whisper_backend"], "AUTO")

    def test_unreadable_file_falls_back_to_defaults(self):
        with open(self.config_file, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")

        loaded = config.load_config()

        self.assertFalse(loaded["live_mode_enabled"])
        self.assertEqual(loaded, config.load_config())


class LiveTogglePersistenceTests(SupervisorSessionBase):
    """D8 through the real tray callback and the real supervisor,
    against faked capture/delivery seams."""

    def setUp(self):
        SupervisorSessionBase.setUp(self)
        self.config_file = use_temp_config(self)

    def stored_live_flag(self):
        with open(self.config_file, encoding="utf-8") as handle:
            return json.load(handle).get("live_mode_enabled")

    def test_refused_start_writes_nothing(self):
        config.set_config_value("live_mode_enabled", False)
        stored = read_bytes(self.config_file)

        self.capture.start_result = (False, "refused")

        tray.live_toggle(None, None)

        self.assertEqual(state.live_status, "LIVE_ERROR")
        self.assertEqual(read_bytes(self.config_file), stored)

    def test_successful_start_persists_true(self):
        config.set_config_value("live_mode_enabled", False)

        tray.live_toggle(None, None)

        self.assertEqual(state.live_status, "LIVE_LISTENING")
        self.assertTrue(self.stored_live_flag())

    def test_successful_stop_persists_false(self):
        tray.live_toggle(None, None)
        self.assertEqual(state.live_status, "LIVE_LISTENING")
        self.assertTrue(self.stored_live_flag())

        tray.live_toggle(None, None)

        self.assertEqual(state.live_status, "LIVE_IDLE")
        self.assertFalse(self.stored_live_flag())


class StartupRestoreTests(unittest.TestCase):
    """Decision 6: documented behaviour, verified structurally, with no
    production change (main.py is frozen F8/startup scope)."""

    def read_main(self):
        with open(
            os.path.join(BASE_DIR, "main.py"), encoding="utf-8"
        ) as handle:
            return handle.read()

    def test_main_holds_no_config_writer(self):
        tree = ast.parse(self.read_main())

        writers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                isinstance(node.func, ast.Name)
                and node.func.id in {"set_config_value", "save_config"}
                or isinstance(node.func, ast.Attribute)
                and node.func.attr in {"set_config_value", "save_config"}
            )
        ]

        self.assertEqual(writers, [])

    def test_main_still_reads_live_mode_enabled_at_startup(self):
        self.assertIn(
            'config.load_config().get("live_mode_enabled"', self.read_main()
        )


if __name__ == "__main__":
    unittest.main()
