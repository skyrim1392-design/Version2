"""Tests for live_output (Phase 7c.2, output integration).

Covers the approved 7c design:

- lifecycle: double-start refused, idempotent stop, exactly one
  LiveOutput thread while running and none after a successful stop,
  stop during an in-flight delivery completes it first, and nothing
  begins delivering after stop() returns successfully
- drain: count-based only - zero pending delivers nothing, up to
  DRAIN_MAX_SEGMENTS pending are fully delivered in order, more than
  that delivers exactly the budget and drops the remainder with a
  count-only log (no wall-clock limit anywhere)
- cursor integration through the real 7c.1 API: exactly-once, order,
  stale-session realign after a session reset
- deterministic warning suppression: a repeated same failure warns
  once (logging continues), A -> B -> A warns three times, a success
  resets the suppression; no timers, timestamps, or throttles
- static/privacy: import scope (no forbidden modules; text_output and
  tray only inside their seams), tray confined to the warning path,
  logger arguments cannot carry text, runtime logs never contain the
  delivered marker text, and warning-message parity with main.py is
  proven by parsing main.py (never imported)

No clipboard, no tray UI, no microphone, no Whisper: delivery and
warnings are injected fakes; the cursor runs against the real
live_transcribe module (import-safe - no session threads required).
"""

import ast
import os
import threading
import time
import unittest
from unittest import mock

import live_output as lo
import live_transcribe as lt
from test_live_transcribe import (
    LOGGER_ARGUMENT_IDENTIFIERS,
    call_arguments,
    identifiers_outside_type_name,
    wait_until,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ALLOWED_IMPORTS = {
    "threading",
    "logger",
    "live_transcribe",
    "text_output",
    "tray",
}

MODULE_LEVEL_IMPORTS = {"threading", "logger", "live_transcribe"}


def parse_module(filename):
    path = os.path.join(BASE_DIR, filename)
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    return ast.parse(source)


class DeliveryRecorder:
    """Injected delivery seam: scripted outcomes plus an optional gate.

    block_first makes the first delivery wait on release, so a test can
    hold a delivery in flight deterministically.
    """

    def __init__(self, outcomes=()):
        self.calls = []
        self.outcomes = list(outcomes)
        self.block_first = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, text):
        if self.block_first and not self.calls:
            self.entered.set()
            self.release.wait(10.0)

        self.calls.append(text)

        if self.outcomes:
            return self.outcomes.pop(0)

        return lo.DELIVERED


class LiveOutputTestBase(unittest.TestCase):
    """Shared fixtures: injected seams, mock logger, isolated store."""

    def setUp(self):
        self.deliver = DeliveryRecorder()
        self.warnings = []

        deliver_patcher = mock.patch.object(
            lo, "_deliver_text", self.deliver
        )
        deliver_patcher.start()
        self.addCleanup(deliver_patcher.stop)

        warning_patcher = mock.patch.object(
            lo, "_emit_warning", self.warnings.append
        )
        warning_patcher.start()
        self.addCleanup(warning_patcher.stop)

        logger_patcher = mock.patch.object(lo, "logger")
        self.logger_mock = logger_patcher.start()
        self.addCleanup(logger_patcher.stop)

        # Added last so it runs first (LIFO): the worker is stopped
        # while the injected seams are still active.
        self.addCleanup(lo.stop)

        lo.stop()
        lt._transcript = []

    def append_segments(self, *segments):
        for segment in segments:
            lt._transcript.append(segment)

    def output_threads(self):
        return [
            thread
            for thread in threading.enumerate()
            if thread.name == "LiveOutput"
        ]

    def log_strings(self):
        calls = []
        for name in ("debug", "info", "warning", "error"):
            method = getattr(self.logger_mock, name)
            calls.extend(str(call) for call in method.call_args_list)
        return calls

    def result_logs(self):
        return [
            entry
            for entry in self.log_strings()
            if "Live output result:" in entry
        ]

    def wait_records_done(self, count):
        """Wait until _record_result finished 'count' records.

        The result line is logged last in _record_result, so its
        arrival guarantees every suppression decision and tray warning
        for that record is already observable.
        """
        self.assertTrue(
            wait_until(lambda: len(self.result_logs()) == count)
        )

    def release_when_stopping(self):
        """Open the delivery gate once stop() has set the stop event."""

        def run():
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                event = lo._stop_event
                if event is not None and event.is_set():
                    break
                time.sleep(0.005)
            self.deliver.release.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread


class LifecycleTests(LiveOutputTestBase):

    def test_start_twice_is_refused(self):
        self.assertEqual(lo.start(), (True, "started"))
        self.assertEqual(lo.start(), (False, "already active"))
        self.assertEqual(lo.stats()["state"], lo.STATE_RUNNING)

    def test_stop_twice_is_safe(self):
        self.assertEqual(lo.start(), (True, "started"))
        self.assertEqual(lo.stop(), (True, "stopped"))
        self.assertEqual(lo.stop(), (True, "inactive"))
        self.assertEqual(lo.stats()["state"], lo.STATE_STOPPED)

    def test_no_worker_thread_remains_after_stop(self):
        self.assertEqual(lo.start(), (True, "started"))
        self.assertEqual(len(self.output_threads()), 1)

        self.assertEqual(lo.stop(), (True, "stopped"))
        self.assertEqual(self.output_threads(), [])
        self.assertFalse(lo.stats()["worker_alive"])

    def test_stop_during_inflight_delivery_completes(self):
        self.deliver.block_first = True

        self.assertEqual(lo.start(), (True, "started"))
        self.append_segments("slow segment")
        self.assertTrue(self.deliver.entered.wait(5.0))

        helper = self.release_when_stopping()
        self.assertEqual(lo.stop(), (True, "stopped"))

        helper.join(timeout=5.0)
        self.assertFalse(helper.is_alive())
        self.assertEqual(self.deliver.calls, ["slow segment"])
        self.assertEqual(self.output_threads(), [])

    def test_no_delivery_begins_after_stop_returns(self):
        self.assertEqual(lo.start(), (True, "started"))
        self.append_segments("delivered once")
        self.assertTrue(wait_until(lambda: len(self.deliver.calls) == 1))

        self.assertEqual(lo.stop(), (True, "stopped"))

        self.append_segments("never delivered")
        time.sleep(0.25)
        self.assertEqual(self.deliver.calls, ["delivered once"])


class DrainTests(LiveOutputTestBase):

    def test_stop_with_zero_pending_delivers_nothing(self):
        self.assertEqual(lo.start(), (True, "started"))
        self.assertEqual(lo.stop(), (True, "stopped"))

        self.assertEqual(self.deliver.calls, [])
        self.assertEqual(lo.stats()["pending_segments"], 0)
        self.assertFalse(
            any("dropped=" in entry for entry in self.log_strings())
        )

    def test_drain_delivers_up_to_eight_pending_in_order(self):
        self.assertEqual(lo.start(), (True, "started"))
        self.append_segments("s1", "s2", "s3", "s4", "s5")

        self.assertEqual(lo.stop(), (True, "stopped"))

        self.assertEqual(
            self.deliver.calls, ["s1", "s2", "s3", "s4", "s5"]
        )
        self.assertFalse(
            any("dropped=" in entry for entry in self.log_strings())
        )

    def test_drain_beyond_eight_drops_remainder_with_count_only_log(self):
        segments = [f"segment {index}" for index in range(12)]

        self.deliver.block_first = True
        self.assertEqual(lo.start(), (True, "started"))
        self.append_segments(*segments)
        self.assertTrue(self.deliver.entered.wait(5.0))

        helper = self.release_when_stopping()
        self.assertEqual(lo.stop(), (True, "stopped"))

        helper.join(timeout=5.0)
        self.assertFalse(helper.is_alive())

        # First segment was in flight at the stop request; the drain
        # then delivered exactly DRAIN_MAX_SEGMENTS more.
        self.assertEqual(
            self.deliver.calls,
            segments[: lo.DRAIN_MAX_SEGMENTS + 1],
        )

        dropped_logs = [
            entry
            for entry in self.log_strings()
            if "dropped=" in entry
        ]
        self.assertEqual(len(dropped_logs), 1)
        self.assertIn("dropped=3", dropped_logs[0])

        for entry in self.log_strings():
            for name in ("segment 9", "segment 10", "segment 11"):
                self.assertNotIn(name, entry)


class CursorIntegrationTests(LiveOutputTestBase):

    def test_segments_delivered_exactly_once_in_order(self):
        self.assertEqual(lo.start(), (True, "started"))
        self.append_segments("alpha", "beta", "gamma")

        self.assertTrue(wait_until(lambda: len(self.deliver.calls) == 3))
        time.sleep(0.2)

        self.assertEqual(self.deliver.calls, ["alpha", "beta", "gamma"])
        self.assertEqual(lo.stats()["pending_segments"], 0)
        self.assertEqual(lo.stats()["state"], lo.STATE_RUNNING)

        self.assertEqual(lo.stop(), (True, "stopped"))
        self.assertEqual(self.deliver.calls, ["alpha", "beta", "gamma"])

    def test_stale_session_cursor_realigns_and_skips_old_segments(self):
        self.assertEqual(lo.start(), (True, "started"))
        self.append_segments("old one")
        self.assertTrue(wait_until(lambda: len(self.deliver.calls) == 1))

        # Same reset sequence start() performs: clear, then bump the
        # session id so the live_output cursor becomes stale.
        lt._transcript = []
        lt._transcript_session += 1
        self.append_segments("new one")

        self.assertTrue(wait_until(lambda: len(self.deliver.calls) == 2))
        self.assertEqual(
            self.deliver.calls, ["old one", "new one"]
        )
        self.assertEqual(self.deliver.calls.count("old one"), 1)

        self.assertEqual(lo.stop(), (True, "stopped"))


class FailureWarningTests(LiveOutputTestBase):

    def test_repeated_same_failure_warns_once(self):
        self.deliver.outcomes = [
            lo.NO_TARGET,
            lo.NO_TARGET,
            lo.NO_TARGET,
        ]

        self.assertEqual(lo.start(), (True, "started"))
        self.append_segments("a", "b", "c")
        self.wait_records_done(3)

        self.assertEqual(
            self.warnings,
            [lo.OUTPUT_WARNING_MESSAGES[lo.NO_TARGET]],
        )

        # Suppression affects tray warnings only; logging continues.
        suppressed_logs = [
            entry
            for entry in self.log_strings()
            if "suppressed" in entry
        ]
        self.assertEqual(len(self.result_logs()), 3)
        self.assertEqual(len(suppressed_logs), 2)

        self.assertEqual(lo.stop(), (True, "stopped"))

    def test_alternating_failures_warn_every_time(self):
        self.deliver.outcomes = [
            lo.NO_TARGET,
            lo.CAPTURE_FAILED,
            lo.NO_TARGET,
        ]

        self.assertEqual(lo.start(), (True, "started"))
        self.append_segments("a", "b", "c")
        self.wait_records_done(3)

        self.assertEqual(
            self.warnings,
            [
                lo.OUTPUT_WARNING_MESSAGES[lo.NO_TARGET],
                lo.OUTPUT_WARNING_MESSAGES[lo.CAPTURE_FAILED],
                lo.OUTPUT_WARNING_MESSAGES[lo.NO_TARGET],
            ],
        )

        self.assertEqual(lo.stop(), (True, "stopped"))

    def test_success_resets_failure_suppression(self):
        self.deliver.outcomes = [
            lo.NO_TARGET,
            lo.DELIVERED,
            lo.NO_TARGET,
        ]

        self.assertEqual(lo.start(), (True, "started"))
        self.append_segments("a", "b", "c")
        self.wait_records_done(3)

        self.assertEqual(
            self.warnings,
            [
                lo.OUTPUT_WARNING_MESSAGES[lo.NO_TARGET],
                lo.OUTPUT_WARNING_MESSAGES[lo.NO_TARGET],
            ],
        )

        self.assertEqual(lo.stop(), (True, "stopped"))


class StaticScopeTests(unittest.TestCase):
    """Import scope, tray confinement, and message parity (AST only)."""

    @classmethod
    def setUpClass(cls):
        cls.tree = parse_module("live_output.py")
        cls.main_tree = parse_module("main.py")

    def collected_imports(self):
        entries = []

        class Scanner(ast.NodeVisitor):
            def __init__(self):
                self.depth = 0

            def visit_FunctionDef(self, node):
                self.depth += 1
                self.generic_visit(node)
                self.depth -= 1

            def visit_Import(self, node):
                for alias in node.names:
                    entries.append(
                        (alias.name.split(".")[0], self.depth)
                    )

            def visit_ImportFrom(self, node):
                if node.module:
                    entries.append(
                        (node.module.split(".")[0], self.depth)
                    )

        Scanner().visit(self.tree)
        return entries

    def test_import_scope(self):
        entries = self.collected_imports()
        found = {name for name, _ in entries}

        forbidden = found - ALLOWED_IMPORTS
        self.assertFalse(
            forbidden, f"forbidden imports in live_output: {sorted(forbidden)}"
        )

        module_level = {
            name for name, depth in entries if depth == 0
        }
        self.assertFalse(
            module_level - MODULE_LEVEL_IMPORTS,
            f"live_output must stay import-safe: "
            f"{sorted(module_level - MODULE_LEVEL_IMPORTS)}",
        )

        for name, depth in entries:
            if name in ("text_output", "tray"):
                self.assertGreaterEqual(
                    depth, 1, f"{name} must stay inside a seam function"
                )

    def test_tray_confined_to_warning_path(self):
        locations = []

        class TrayScanner(ast.NodeVisitor):
            def __init__(self):
                self.enclosing = None

            def visit_FunctionDef(self, node):
                previous = self.enclosing
                self.enclosing = node.name
                self.generic_visit(node)
                self.enclosing = previous

            def visit_Name(self, node):
                if node.id == "tray":
                    locations.append(self.enclosing)

            def visit_Import(self, node):
                for alias in node.names:
                    if alias.name.split(".")[0] == "tray":
                        locations.append(self.enclosing)

            def visit_ImportFrom(self, node):
                if node.module and node.module.split(".")[0] == "tray":
                    locations.append(self.enclosing)

        TrayScanner().visit(self.tree)

        self.assertTrue(locations, "live_output must use the tray seam")
        self.assertEqual(set(locations), {"_emit_warning"})

    def test_warning_messages_match_main(self):
        expected = {}

        for node in ast.walk(self.main_tree):
            if not isinstance(node, ast.Assign):
                continue
            targets = [
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            ]
            if "OUTPUT_WARNING_MESSAGES" not in targets:
                continue
            if not isinstance(node.value, ast.Dict):
                continue
            for key, value in zip(node.value.keys, node.value.values):
                if isinstance(key, ast.Attribute):
                    name = key.attr
                elif isinstance(key, ast.Constant):
                    name = key.value
                else:
                    continue
                if isinstance(value, ast.Constant):
                    expected[name] = value.value

        self.assertEqual(dict(lo.OUTPUT_WARNING_MESSAGES), expected)
        self.assertEqual(
            {code for code in expected}, set(lo.FAILURE_CODES)
        )


class RuntimePrivacyTests(LiveOutputTestBase):

    def test_static_logger_arguments_cannot_carry_text(self):
        tree = parse_module("live_output.py")

        for node in ast.walk(tree):
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

    def test_runtime_logs_never_contain_delivered_text(self):
        self.assertEqual(lo.start(), (True, "started"))
        self.append_segments("SECRET_ALPHA", "SECRET_BETA")

        self.assertTrue(wait_until(lambda: len(self.deliver.calls) == 2))
        self.assertEqual(lo.stop(), (True, "stopped"))

        for entry in self.log_strings():
            self.assertNotIn("SECRET", entry)

        for entry in self.warnings:
            self.assertNotIn("SECRET", entry)


if __name__ == "__main__":
    unittest.main()
