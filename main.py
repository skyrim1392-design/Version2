import atexit
import os
import threading
import time

import keyboard

import audio
import logger
import runtime_validation
import text_output
import tray
import whisper_engine
from app_state import state

logger.info(f"AUDIO MODULE: {audio.__file__}")

# ================= GLOBAL =================

f8_active = False


WARNING_DURATION = 3.0


OUTPUT_WARNING_MESSAGES = {
    text_output.NO_TARGET: "No active window for the text",
    text_output.CAPTURE_FAILED: "Output failed: clipboard unavailable",
    text_output.WRITE_FAILED: "Output failed: clipboard unavailable",
    text_output.HOTKEY_FAILED: "Output failed: text kept in clipboard",
    text_output.RESTORE_FAILED: "Text pasted; clipboard not restored",
}


def show_microphone_warning(message):
    logger.warning(message)

    tray.show_warning(
        message,
        duration=WARNING_DURATION,
    )


# ================= CLEANUP =================


def cleanup():

    logger.info("Cleaning up")

    if audio.is_recording:
        logger.info("Stopping active recording")

        audio.stop_recording()


atexit.register(cleanup)


# ================= TRAY =================

tray_thread = threading.Thread(target=tray.start_tray, daemon=True)

tray_thread.start()


# ================= F8 CONTROL =================


def f8_down():

    global f8_active

    if f8_active:
        return

    if state.processing:
        logger.info("Ignoring F8: already processing")
        return

    if state.status == "OUTPUTTING":
        logger.info("Ignoring F8: output in progress")
        return

    if audio.is_recording:
        return

    # Lock this physical F8 press.
    # It remains locked until F8 is released.
    f8_active = True

    try:
        logger.info("F8 pressed")

        try:
            started = audio.start_recording()

        except (OSError, RuntimeError) as error:
            logger.error(f"Microphone unavailable: {error}")

            tray.show_microphone_error()

            with state.lock:
                state.recording = False
                state.processing = False

            return

        if not started:
            logger.warning("Microphone unavailable: audio.start_recording() returned False")

            tray.show_microphone_error()

            with state.lock:
                state.recording = False
                state.processing = False

            return

        # -------------------------------------------------
        # MICROPHONE STARTED SUCCESSFULLY
        # -------------------------------------------------

        tray.clear_microphone_error()

        with state.lock:
            state.status = "RECORDING"
            state.recording = True

            logger.info(f"State: {state.status}")

    except Exception:
        # The press transaction failed, so release the lock here.
        # Re-raise: safe_f8_down() reports the original failure.
        f8_active = False

        raise


def f8_up():

    global f8_active

    if not f8_active:
        logger.info("Ignoring duplicate F8 release")

        return

    f8_active = False

    if state.processing:
        logger.info("Ignoring F8: already processing")

        return

    if not audio.is_recording:
        logger.info("No active recording")

        return

    logger.info("F8 released")

    try:
        data = audio.stop_recording()

    except (OSError, RuntimeError) as error:
        logger.error(f"Recording stop failed: {error}")

        data = None

    with state.lock:
        state.recording = False
        state.status = "IDLE"

    logger.info(f"State: {state.status}")

    if data is None:
        show_microphone_warning("No usable audio was captured from the microphone.")

        logger.warning(
            "Transcription cancelled because no usable microphone audio was captured"
        )

        return

    try:
        with state.lock:
            state.status = "PROCESSING"
            state.processing = True

        wav_path = audio.save_wav(data)

        if not os.path.exists(wav_path):
            raise RuntimeError("WAV file was not created")

        if os.path.getsize(wav_path) < 1000:
            raise RuntimeError("WAV file is too small")

        logger.info("Running Whisper")

        text = whisper_engine.whisper_transcribe(wav_path)
        audio.cleanup_old_wavs()

        logger.info("Whisper finished")

    except (OSError, RuntimeError, ValueError) as error:
        logger.error(f"Processing error: {error}")

        with state.lock:
            state.status = "IDLE"

        return

    finally:
        with state.lock:
            state.processing = False

            state.status = "IDLE"

    with state.lock:
        state.status = "OUTPUTTING"

    logger.info(f"State: {state.status}")

    result = text_output.deliver(text)

    warning = OUTPUT_WARNING_MESSAGES.get(result)

    if warning:
        tray.show_warning(warning, duration=WARNING_DURATION)

    with state.lock:
        state.status = "IDLE"

    logger.info(f"State: {state.status}")


# ================= SAFE HANDLERS =================


def safe_f8_down(e):

    try:
        f8_down()

    except Exception as error:
        logger.error(f"F8 press error: {type(error).__name__}: {error}")


def safe_f8_up(e):

    global f8_active

    try:
        f8_up()

    except Exception as error:
        # Release must unlock even if f8_up failed before clearing the lock.
        f8_active = False

        logger.error(f"F8 release error: {type(error).__name__}: {error}")


# ================= START =================

print("================================")

print(" Windows Voice Assistant")

logger.info("Voice Assistant started")

print(" Press and hold F8 to speak")

print(" Release F8 to transcribe")

print("================================")


if runtime_validation.run_startup_validation():
    keyboard.on_press_key("f8", safe_f8_down)
    keyboard.on_release_key("f8", safe_f8_up)
else:
    logger.error("Startup validation failed; F8 hotkey not registered")
    tray.show_startup_error(runtime_validation.last_error)


# ================= MAIN LOOP =================

try:
    while True:
        time.sleep(1)


except KeyboardInterrupt:
    logger.info("Voice Assistant stopped")

    print("\nVoice assistant stopped.")
