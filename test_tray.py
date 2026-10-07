"""7d.2 tray rendering and Exit tests (approved decisions 1 and 4).

Contract under test:

- display_status(): the D6 precedence matrix in isolation, including
  the overlap case (F8 active state while live reports LISTENING or
  ERROR) where the F8 state must win.
- update_tray(): overlay priority - startup error beats microphone
  error beats the F8 states (which supersede a rendered warning) beats
  the temporary warning (which beats live rendering while IDLE) beats
  normal status rendering - plus one thin end-to-end loop run proving
  the loop reads app_state and formats the title.
- exit_application(): `stopping` is asserted before teardown, and
  `icon.stop()` is attempted even when teardown raises.

Runs against a fake icon, a patched create_icon and a silenced logger:
no pystray event loop, no OS notification, no icon file reads, no
microphone, no whisper.

Run: python -m unittest test_tray -v
"""

import threading
import time
import unittest
from unittest import mock

import live_supervisor
import tray
from app_state import state

WAIT_TIMEOUT = 3.0

MICROPHONE_ERROR_TITLE = (
    "Voice Assistant - Microphone unavailable.\n"
    "Check microphone and restart the app."
)


def wait_until(predicate, timeout=WAIT_TIMEOUT):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)

    return predicate()


class FakeIcon:
    """The pystray.Icon surface update_tray and exit_application use."""

    def __init__(self):
        self.icon = None
        self.title = None
        self.stopped = False
        self.notifications = []

    def stop(self):
        self.stopped = True

    def notify(self, message, title=None):
        self.notifications.append((title, message))


class TrayLoopBase(unittest.TestCase):
    """Fake icon, faked create_icon, silenced logger, clean tray and
    app_state globals before and after every test."""

    def setUp(self):
        tray.stopping = False
        tray.tray_icon = None
        tray.startup_error = False
        tray.startup_error_message = None
        tray.microphone_error = False

        with state.lock:
            state.status = "IDLE"
            state.live_status = live_supervisor.LIVE_IDLE

        create_icon_patcher = mock.patch.object(
            tray, "create_icon", side_effect=lambda status: f"icon:{status}"
        )
        create_icon_patcher.start()
        self.addCleanup(create_icon_patcher.stop)

        logger_patcher = mock.patch.object(tray, "logger")
        self.log = logger_patcher.start()
        self.addCleanup(logger_patcher.stop)

        self.addCleanup(self._reset_tray)

    def _reset_tray(self):
        tray.stopping = True
        tray.tray_icon = None
        tray.warning_until = 0.0
        tray.warning_message = None
        tray.startup_error = False
        tray.startup_error_message = None
        tray.microphone_error = False

        with state.lock:
            state.status = "IDLE"
            state.live_status = live_supervisor.LIVE_IDLE

    def start_loop(self):
        """Start update_tray on a fake icon; the loop is stopped and
        joined by a cleanup registered here, before _reset_tray."""
        icon = FakeIcon()
        thread = threading.Thread(
            target=tray.update_tray,
            args=(icon,),
            name="TrayUnderTest",
            daemon=True,
        )
        thread.start()
        self.addCleanup(self._stop_loop, thread)
        return icon

    def _stop_loop(self, thread):
        tray.stopping = True
        thread.join(WAIT_TIMEOUT)
        self.assertFalse(thread.is_alive(), "tray loop did not stop")

    def info_messages(self):
        return [
            call.args[0]
            for call in self.log.info.call_args_list
            if call.args
        ]

    def warning_messages(self):
        return [
            call.args[0]
            for call in self.log.warning.call_args_list
            if call.args
        ]

    def set_state(self, status="IDLE", live_status=live_supervisor.LIVE_IDLE):
        with state.lock:
            state.status = status
            state.live_status = live_status


class DisplayStatusMatrixTests(unittest.TestCase):
    """D6: the matrix itself, in isolation."""

    def test_d6_matrix(self):
        matrix = [
            ("IDLE", "LIVE_IDLE", "IDLE"),
            ("IDLE", "LIVE_LISTENING", "LIVE_LISTENING"),
            ("IDLE", "LIVE_ERROR", "LIVE_ERROR"),
            ("RECORDING", "LIVE_IDLE", "RECORDING"),
            ("RECORDING", "LIVE_LISTENING", "RECORDING"),
            ("RECORDING", "LIVE_ERROR", "RECORDING"),
            ("PROCESSING", "LIVE_IDLE", "PROCESSING"),
            ("PROCESSING", "LIVE_LISTENING", "PROCESSING"),
            ("PROCESSING", "LIVE_ERROR", "PROCESSING"),
            ("OUTPUTTING", "LIVE_IDLE", "OUTPUTTING"),
            ("OUTPUTTING", "LIVE_LISTENING", "OUTPUTTING"),
            ("OUTPUTTING", "LIVE_ERROR", "OUTPUTTING"),
            ("ERROR", "LIVE_LISTENING", "ERROR"),
            ("ERROR", "LIVE_ERROR", "ERROR"),
        ]

        for status, live_status, expected in matrix:
            with self.subTest(status=status, live_status=live_status):
                self.assertEqual(
                    tray.display_status(status, live_status), expected
                )

    def test_f8_active_state_wins_over_live_overlap(self):
        for status in tray.ACTIVE_STATES:
            for live_status in (
                live_supervisor.LIVE_LISTENING,
                live_supervisor.LIVE_ERROR,
            ):
                with self.subTest(status=status, live_status=live_status):
                    self.assertEqual(
                        tray.display_status(status, live_status), status
                    )


class OverlayPriorityTests(TrayLoopBase):
    """update_tray overlay chain, scenario by scenario."""

    def test_startup_error_overlay_wins_over_everything(self):
        tray.show_warning("temporary warning", duration=30.0)
        tray.startup_error = True
        tray.startup_error_message = "model missing"
        tray.microphone_error = True
        self.set_state(
            status="RECORDING",
            live_status=live_supervisor.LIVE_LISTENING,
        )

        icon = self.start_loop()

        self.assertTrue(
            wait_until(
                lambda: icon.title
                == "Voice Assistant - Startup error\nmodel missing"
            ),
            f"unexpected title: {icon.title!r}",
        )
        self.assertEqual(icon.icon, "icon:ERROR")
        self.assertIn("Tray status: STARTUP_ERROR", self.info_messages())

    def test_microphone_error_overlay_beats_live_and_warning(self):
        tray.show_warning("temporary warning", duration=30.0)
        tray.microphone_error = True
        self.set_state(
            status="IDLE",
            live_status=live_supervisor.LIVE_LISTENING,
        )

        icon = self.start_loop()

        self.assertTrue(
            wait_until(lambda: icon.title == MICROPHONE_ERROR_TITLE),
            f"unexpected title: {icon.title!r}",
        )
        self.assertEqual(icon.icon, "icon:ERROR")

    def test_active_state_supersedes_rendered_warning(self):
        tray.show_warning("temporary warning", duration=30.0)
        self.set_state(status="IDLE")

        icon = self.start_loop()

        self.assertTrue(
            wait_until(
                lambda: icon.title == "Voice Assistant - temporary warning"
            ),
            f"unexpected title: {icon.title!r}",
        )
        self.assertEqual(icon.icon, "icon:WARNING")

        self.set_state(status="PROCESSING")

        self.assertTrue(
            wait_until(
                lambda: icon.title == "Voice Assistant - PROCESSING"
            ),
            f"unexpected title: {icon.title!r}",
        )
        self.assertEqual(icon.icon, "icon:PROCESSING")
        self.assertIn(
            "Tray warning superseded by PROCESSING", self.info_messages()
        )

    def test_temporary_warning_outranks_live_rendering_while_idle(self):
        tray.show_warning("temporary warning", duration=30.0)
        self.set_state(
            status="IDLE",
            live_status=live_supervisor.LIVE_LISTENING,
        )

        icon = self.start_loop()

        self.assertTrue(
            wait_until(
                lambda: icon.title == "Voice Assistant - temporary warning"
            ),
            f"unexpected title: {icon.title!r}",
        )
        self.assertEqual(icon.icon, "icon:WARNING")

    def test_expired_warning_returns_to_live_rendering(self):
        tray.show_warning("short warning", duration=0.3)
        self.set_state(
            status="IDLE",
            live_status=live_supervisor.LIVE_ERROR,
        )

        icon = self.start_loop()

        self.assertTrue(
            wait_until(lambda: icon.title == "Voice Assistant - short warning"),
            f"unexpected title: {icon.title!r}",
        )
        self.assertTrue(
            wait_until(
                lambda: icon.title == "Voice Assistant - LIVE_ERROR"
            ),
            f"unexpected title: {icon.title!r}",
        )
        self.assertEqual(icon.icon, "icon:LIVE_ERROR")
        self.assertIn("Tray warning finished", self.info_messages())


class UpdateTrayIntegrationTests(TrayLoopBase):
    """Thin end-to-end run of the real loop against app_state."""

    def test_loop_reads_state_and_formats_title(self):
        self.set_state(
            status="IDLE",
            live_status=live_supervisor.LIVE_LISTENING,
        )

        icon = self.start_loop()

        self.assertTrue(
            wait_until(
                lambda: icon.title == "Voice Assistant - LIVE_LISTENING"
            ),
            f"unexpected title: {icon.title!r}",
        )
        self.assertEqual(icon.icon, "icon:LIVE_LISTENING")

        self.set_state(
            status="RECORDING",
            live_status=live_supervisor.LIVE_LISTENING,
        )
        self.assertTrue(
            wait_until(lambda: icon.title == "Voice Assistant - RECORDING"),
            f"unexpected title: {icon.title!r}",
        )

        self.set_state(
            status="IDLE",
            live_status=live_supervisor.LIVE_IDLE,
        )
        self.assertTrue(
            wait_until(lambda: icon.title == "Voice Assistant - IDLE"),
            f"unexpected title: {icon.title!r}",
        )

        self.assertEqual(
            self.info_messages()[-1], "Tray status: IDLE"
        )


class ExitApplicationTests(unittest.TestCase):
    """Decision 4: stopping first, icon.stop() in a finally."""

    def setUp(self):
        tray.stopping = False
        self.addCleanup(setattr, tray, "stopping", False)

        logger_patcher = mock.patch.object(tray, "logger")
        logger_patcher.start()
        self.addCleanup(logger_patcher.stop)

    def test_exit_asserts_stopping_before_teardown_then_stops_icon(self):
        icon = FakeIcon()
        observed = {}

        def shutdown():
            observed["stopping"] = tray.stopping

        with mock.patch.object(
            tray.live_supervisor, "shutdown", side_effect=shutdown
        ) as shutdown_mock:
            tray.exit_application(icon, None)

        self.assertTrue(tray.stopping)
        self.assertEqual(observed["stopping"], True)
        shutdown_mock.assert_called_once()
        self.assertTrue(icon.stopped)

    def test_exit_stops_icon_even_when_teardown_raises(self):
        icon = FakeIcon()

        def boom():
            raise RuntimeError("teardown failed")

        with mock.patch.object(
            tray.live_supervisor, "shutdown", side_effect=boom
        ), self.assertRaises(RuntimeError):
            tray.exit_application(icon, None)

        self.assertTrue(tray.stopping)
        self.assertTrue(icon.stopped)


if __name__ == "__main__":
    unittest.main()
