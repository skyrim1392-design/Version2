import atexit
import threading

import keyboard

import audio
import logger
import text_output
import tray
import whisper_engine

# ================= GLOBAL =================

is_processing = False
f8_active = False


def cleanup():

    print("Cleaning up...")

    if audio.is_recording:
        audio.stop_recording()


atexit.register(cleanup)

# ================= TRAY =================

tray_thread = threading.Thread(
    target=tray.start_tray,
    daemon=True
)

tray_thread.start()


# ================= F8 CONTROL =================

def f8_down():

    global f8_active

    if f8_active:
        return

    f8_active = True

    if is_processing:
        print("Ignoring F8: Whisper is processing")
        return

    if audio.is_recording:
        return

    print("F8 pressed")

    audio.start_recording()


def f8_up():

    global is_processing
    global f8_active

    f8_active = False

    if not audio.is_recording:
        return

    print("F8 released")

    data = audio.stop_recording()

    if data is None:
        return

    is_processing = True

    try:

        audio.save_wav(data)

        text = whisper_engine.whisper_transcribe()

    finally:

        is_processing = False


    print("FINAL TEXT:")
    print(repr(text))

    text_output.type_text(text)



# ================= MAIN =================

print("================================")
print(" Windows Voice Assistant")
logger.info("Voice Assistant started")
print(" Press and hold F8 to speak")
print(" Release F8 to transcribe")
print("================================")


keyboard.on_press_key(
    "f8",
    lambda e: f8_down()
)


keyboard.on_release_key(
    "f8",
    lambda e: f8_up()
)


try:

    keyboard.wait()

except KeyboardInterrupt:

    logger.info("Voice Assistant stopped")
    print("\nVoice assistant stopped.")