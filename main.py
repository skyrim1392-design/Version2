import atexit
import os
import threading
import time

import keyboard

import audio
import logger
import text_output
import tray
import whisper_engine
from app_state import state

print("AUDIO MODULE:", audio.__file__)
logger.info(f"AUDIO MODULE: {audio.__file__}")

# ================= GLOBAL =================

is_processing = False
f8_active = False


WARNING_DURATION = 3.0


def show_microphone_warning(message):
    print("⚠️", message)
    logger.warning(message)

    tray.show_warning(
        message,
        duration=WARNING_DURATION,
    )


# ================= CLEANUP =================


def cleanup():

    print("Cleaning up...")

    if audio.is_recording:
        print("Stopping active recording")

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

    if is_processing:
        print("Ignoring F8: already processing")
        return

    if audio.is_recording:
        return

    # Lock this physical F8 press.
    # It remains locked until F8 is released.
    f8_active = True

    print("F8 pressed")

    try:
        started = audio.start_recording()

    except (OSError, RuntimeError) as error:
        print("⚠️ Microphone unavailable")
        logger.error(f"Microphone unavailable: {error}")

        tray.show_microphone_error()

        with state.lock:
            state.recording = False
            state.processing = False

        return

    if not started:
        print("⚠️ Microphone unavailable")

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

        print("STATE:", state.status)


def f8_up():

    global is_processing
    global f8_active

    if not f8_active:
        print("Ignoring duplicate F8 release")

        return

    f8_active = False

    print("===== F8 RELEASE HANDLER ENTERED =====")

    if is_processing:
        print("Ignoring F8: already processing")

        return

    if not audio.is_recording:
        print("No active recording")

        return

    print("F8 released")

    data = audio.stop_recording()
    with state.lock:
        state.recording = False

    print("STATE:", state.status)

    if data is None:
        show_microphone_warning("No usable audio was captured from the microphone.")

        print("No usable audio - transcription cancelled")

        logger.warning(
            "Transcription cancelled because no usable microphone audio was captured"
        )

        return

    is_processing = True

    try:
        with state.lock:
            state.status = "PROCESSING"
            state.processing = True

        wav_path = audio.save_wav(data)

        if not os.path.exists(wav_path):
            raise RuntimeError("WAV file was not created")

        if os.path.getsize(wav_path) < 1000:
            raise RuntimeError("WAV file is too small")

        print("Running Whisper...")

        text = whisper_engine.whisper_transcribe(wav_path)
        audio.cleanup_old_wavs()

        print("Whisper finished")

    except (OSError, RuntimeError, ValueError) as error:
        print("PROCESSING ERROR:", error)
        logger.error(f"Processing error: {error}")

        with state.lock:
            state.status = "IDL"

        return

    finally:
        is_processing = False

        with state.lock:
            state.processing = False

            state.status = "IDLE"

    print("FINAL TEXT:")

    print(repr(text))

    text_output.type_text(text)


# ================= SAFE HANDLERS =================


def safe_f8_down(e):

    try:
        f8_down()

    except (OSError, RuntimeError) as error:
        print("F8 press error:")

        print(error)

        logger.error(f"F8 press error: {error}")


def safe_f8_up(e):

    try:
        f8_up()

    except (OSError, RuntimeError) as error:
        print("F8 release error:")

        print(error)

        logger.error(f"F8 release error: {error}")


# ================= START =================

print("================================")

print(" Windows Voice Assistant")

logger.info("Voice Assistant started")

print(" Press and hold F8 to speak")

print(" Release F8 to transcribe")

print("================================")


keyboard.on_press_key("f8", safe_f8_down)


keyboard.on_release_key("f8", safe_f8_up)


# ================= MAIN LOOP =================

try:
    while True:
        time.sleep(1)


except KeyboardInterrupt:
    print("!!! KeyboardInterrupt detected !!!")

    logger.info("Voice Assistant stopped")

    print("\nVoice assistant stopped.")
