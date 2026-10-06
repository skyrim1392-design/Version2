import ctypes
import threading
import time

import pyautogui
import pygetwindow
import pyperclip

import logger

typing_lock = threading.Lock()

SETTLE_DELAY = 0.1

RESTORE_DELAY = 0.3

CF_UNICODETEXT = 13

DELIVERED = "DELIVERED"
SKIPPED = "SKIPPED"
NO_TARGET = "NO_TARGET"
CAPTURE_FAILED = "CAPTURE_FAILED"
WRITE_FAILED = "WRITE_FAILED"
HOTKEY_FAILED = "HOTKEY_FAILED"
RESTORE_FAILED = "RESTORE_FAILED"


def deliver(text):
    """Deliver transcribed text to the foreground application.

    Clipboard transaction contract:
      - the previous clipboard state is captured before any write
      - a capture failure aborts delivery while the clipboard is untouched
      - the target is re-checked after the settle delay; a disappearing
        target aborts with the previous clipboard restored and no paste
      - after a successful paste the previous state is restored, but only
        while the clipboard still holds the delivered text
      - transcription contents are never written to the log

    Returns one of the module result constants.
    """

    if not text:
        logger.info("Text output skipped: empty transcription")
        return SKIPPED

    with typing_lock:
        logger.info("Text output started")

        if not _has_foreground_target():
            logger.error("No valid foreground target")
            return NO_TARGET

        previous = _capture_clipboard()

        if previous is None:
            return CAPTURE_FAILED

        try:
            pyperclip.copy(text)

        except Exception as error:
            logger.error(f"Clipboard write failed: {type(error).__name__}")
            _force_restore(previous)
            return WRITE_FAILED

        time.sleep(SETTLE_DELAY)

        if not _has_foreground_target():
            logger.error("Foreground target disappeared before paste")
            _restore_clipboard(previous, text)
            return NO_TARGET

        try:
            pyautogui.hotkey("ctrl", "v")

        except Exception as error:
            logger.error(
                f"Ctrl+V delivery failed: {type(error).__name__}; "
                "transcription left in clipboard"
            )
            return HOTKEY_FAILED

        time.sleep(RESTORE_DELAY)

        if not _restore_clipboard(previous, text):
            return RESTORE_FAILED

        logger.info(f"Text output completed (characters={len(text)})")
        return DELIVERED


def _has_foreground_target():
    try:
        return pygetwindow.getActiveWindow() is not None

    except Exception:
        return False


def _capture_clipboard():
    """Return the clipboard text, or None when it cannot be captured.

    A clipboard holding non-text data (an image, files) cannot be restored
    from text alone, so it is treated as uncapturable and never overwritten.
    """

    try:
        if _has_text_format():
            return pyperclip.paste()

        if not _clipboard_is_empty():
            logger.error("Clipboard capture failed: non-text clipboard content")
            return None

        return ""

    except Exception as error:
        logger.error(f"Clipboard capture failed: {type(error).__name__}")
        return None


def _has_text_format():
    return bool(
        ctypes.windll.user32.IsClipboardFormatAvailable(CF_UNICODETEXT)
    )


def _clipboard_is_empty():
    if not ctypes.windll.user32.OpenClipboard(None):
        raise OSError("clipboard is locked by another process")

    try:
        return not ctypes.windll.user32.CountClipboardFormats()

    finally:
        ctypes.windll.user32.CloseClipboard()


def _force_restore(previous):
    try:
        pyperclip.copy(previous)

    except Exception as error:
        logger.error(f"Clipboard restoration failed: {type(error).__name__}")


def _restore_clipboard(previous, transcription):
    """Return the clipboard to its captured state after a paste.

    The clipboard is only overwritten while it still holds the delivered
    text; any other content was written by an external actor and is kept.
    """

    try:
        current = pyperclip.paste()

    except Exception as error:
        logger.warning(f"Clipboard restoration check failed: {type(error).__name__}")
        current = None

    if current == previous:
        return True

    if current is not None and current != transcription:
        return True

    try:
        pyperclip.copy(previous)

    except Exception as error:
        logger.error(f"Clipboard restoration failed: {type(error).__name__}")
        return False

    return True
