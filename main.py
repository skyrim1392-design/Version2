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

# ================= GLOBAL =================

is_processing = False
f8_active = False


# ================= CLEANUP =================


def cleanup():

    print("Cleaning up...")

    audio.stop_microphone_monitor()

    if audio.is_recording:

        print("Stopping active recording")

        audio.stop_recording()


atexit.register(cleanup)


# ================= MICROPHONE HEALTH MONITOR =================

audio.start_microphone_monitor()


# ================= TRAY =================

tray_thread = threading.Thread(target=tray.start_tray, daemon=True)

tray_thread.start()


# ================= F8 CONTROL =================


def f8_down():

    global f8_active

    if f8_active:
        return

    f8_active = True

    if is_processing:
        print("Ignoring F8: already processing")
        return

    if audio.is_recording:
        return

    print("F8 pressed")

    try:
        audio.start_recording()
    except (OSError, RuntimeError) as error:
        with state.lock:
            state.status = "ERROR"
            state.recording = False
            state.processing = False
            state.error = str(error)

        print("MICROPHONE ERROR:", error)
        logger.error(f"Microphone error: {error}")
        return

    with state.lock:
        state.status = "RECORDING"
        state.recording = True

    print("STATE:", state.status)


def f8_up():

    global is_processing
    global f8_active

    text = ""

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

        with state.lock:
            state.status = "ERROR"
            state.recording = False
            state.processing = False
            if not state.error:
                state.error = "No usable audio data was captured"

        print("No usable audio data - transcription cancelled")

        logger.error(
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

        print("Whisper finished")

    except (OSError, RuntimeError, ValueError) as error:

        print("PROCESSING ERROR:", error)
        logger.error(f"Processing error: {error}")

        with state.lock:
            state.status = "ERROR"
            state.error = str(error)

        return

    finally:

        is_processing = False

        with state.lock:
            state.processing = False
            if state.status != "ERROR":
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
