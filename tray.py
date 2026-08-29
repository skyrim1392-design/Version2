import os
import threading
import time

import pystray
from PIL import Image

import microphone_diagnostics
from app_state import state
from config import load_config, set_config_value

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ICON_DIR = os.path.join(BASE_DIR, "icons")

# ================= TEMPORARY WARNING =================

tray_icon = None
warning_until = 0.0
warning_message = None
warning_lock = threading.Lock()


def show_warning(message, duration=3.0):
    """
    Show a temporary microphone warning in the tray.

    This is UI-only.
    It does NOT modify application state.status.
    """
    global warning_until, warning_message

    print("⚠️", message)

    with warning_lock:
        warning_message = message
        warning_until = time.monotonic() + duration


def create_icon(status):
    files = {
        "IDLE": "idle.png",
        "RECORDING": "recording.png",
        "PROCESSING": "processing.png",
        "WARNING": "error.png",
        "ERROR": "error.png",
        # Use the existing error icon until a dedicated suspect icon is designed.
        "SUSPECT": "error.png",
    }

    filename = files.get(status, "idle.png")
    path = os.path.join(ICON_DIR, filename)

    if os.path.exists(path):
        return Image.open(path)

    print("Icon missing:", path)
    return Image.new("RGB", (64, 64), "black")


# ================= GPU =================


def set_gpu(device):
    set_config_value("gpu_device", device)
    print(f"GPU changed to device {device}")


def gpu_rx580(icon, item):
    set_gpu(0)


def gpu_intel(icon, item):
    set_gpu(1)


def is_gpu_rx580(item):
    return load_config().get("gpu_device", 0) == 0


def is_gpu_intel(item):
    return load_config().get("gpu_device", 0) == 1


# ================= LANGUAGE =================


def set_language(language):
    set_config_value("language_mode", language)
    print(f"Language changed to {language}")


def language_auto(icon, item):
    set_language("AUTO")


def language_english(icon, item):
    set_language("ENGLISH")


def language_persian(icon, item):
    set_language("PERSIAN")


def is_language_auto(item):
    return load_config().get("language_mode", "AUTO") == "AUTO"


def is_language_english(item):
    return load_config().get("language_mode", "AUTO") == "ENGLISH"


def is_language_persian(item):
    return load_config().get("language_mode", "AUTO") == "PERSIAN"


# ================= TASK =================


def set_task(task):
    set_config_value("task_mode", task)
    print(f"Task changed to {task}")


def task_transcribe(icon, item):
    set_task("TRANSCRIBE")


def task_translate(icon, item):
    set_task("TRANSLATE")


def is_task_transcribe(item):
    return load_config().get("task_mode", "TRANSCRIBE") == "TRANSCRIBE"


def is_task_translate(item):
    return load_config().get("task_mode", "TRANSCRIBE") == "TRANSLATE"


# ================= MODEL =================


def get_available_models():
    models_dir = os.path.join(BASE_DIR, "models")

    if not os.path.exists(models_dir):
        return []

    models = [
        filename
        for filename in os.listdir(models_dir)
        if filename.lower().endswith(".bin")
    ]
    models.sort()
    return models


def set_model(model):
    set_config_value("model", model)
    print(f"Model changed to {model}")


def model_selected(icon, item):
    model = item.text
    print(f"Model selected: {model}")
    set_model(model)


def is_model_selected(model):
    def check(item):
        return load_config().get("model", "") == model

    return check


# ================= MICROPHONE DIAGNOSTICS =================


def test_microphone(icon, item):
    """Run the explicit microphone test in a worker so the tray stays responsive."""

    def worker():
        print("Starting microphone diagnostic...")
        result = microphone_diagnostics.run_microphone_test()

        status = result.get("status", "UNKNOWN")
        print(f"MICROPHONE DIAGNOSTIC: {status}")

        if status == "OK":
            print("Microphone diagnostic passed.")
        elif status == "SUSPECT":
            print(
                "Microphone is visible and capture is active, "
                "but no usable signal was detected."
            )
        else:
            print(
                "Microphone diagnostic failed: "
                + str(result.get("message", state.microphone_error))
            )

    threading.Thread(
        target=worker,
        name="MicrophoneDiagnostic",
        daemon=True,
    ).start()


# ================= EXIT =================


def exit_application(icon, item):
    print("Exiting Voice Assistant...")
    icon.stop()


# ================= STATUS =================
def update_tray(icon):
    global tray_icon

    tray_icon = icon

    last_status = None
    warning_active = False

    while True:
        with state.lock:
            current_status = state.status

        with warning_lock:
            current_warning_until = warning_until
            current_warning_message = warning_message

        now = time.monotonic()

        # -------------------------------------------------
        # TEMPORARY WARNING
        # -------------------------------------------------

        if now < current_warning_until:
            if not warning_active:
                print("TRAY WARNING: Microphone unavailable")

                icon.icon = create_icon("WARNING")
                icon.title = "Voice Assistant - " + str(current_warning_message)

                warning_active = True

            time.sleep(0.1)
            continue

        # -------------------------------------------------
        # WARNING FINISHED
        # -------------------------------------------------

        if warning_active:
            print("TRAY WARNING FINISHED")

            warning_active = False
            last_status = None

        # -------------------------------------------------
        # NORMAL APPLICATION STATUS
        # -------------------------------------------------

        if current_status != last_status:
            print("TRAY STATUS:", current_status)

            icon.icon = create_icon(current_status)

            last_status = current_status

        if current_status == "ERROR":
            icon.title = "Voice Assistant - Error"

        else:
            icon.title = "Voice Assistant - " + current_status

        time.sleep(0.5)


# ================= TRAY =================
def start_tray():
    icon_image = create_icon("IDLE")

    gpu_menu = pystray.Menu(
        pystray.MenuItem("Radeon RX 580", gpu_rx580, checked=is_gpu_rx580),
        pystray.MenuItem("Intel HD Graphics 530", gpu_intel, checked=is_gpu_intel),
    )

    language_menu = pystray.Menu(
        pystray.MenuItem("Auto", language_auto, checked=is_language_auto),
        pystray.MenuItem("English", language_english, checked=is_language_english),
        pystray.MenuItem("Persian", language_persian, checked=is_language_persian),
    )

    task_menu = pystray.Menu(
        pystray.MenuItem("Transcribe", task_transcribe, checked=is_task_transcribe),
        pystray.MenuItem(
            "Translate to English", task_translate, checked=is_task_translate
        ),
    )

    model_items = [
        pystray.MenuItem(model, model_selected, checked=is_model_selected(model))
        for model in get_available_models()
    ]
    model_menu = pystray.Menu(*model_items)

    microphone_menu = pystray.Menu(
        pystray.MenuItem(
            "Test Microphone",
            test_microphone,
            enabled=lambda item: not state.recording and not state.processing,
        ),
    )

    menu = pystray.Menu(
        pystray.MenuItem("Voice Assistant", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Microphone", microphone_menu),
        pystray.MenuItem("GPU", gpu_menu),
        pystray.MenuItem("Language", language_menu),
        pystray.MenuItem("Task", task_menu),
        pystray.MenuItem("Model", model_menu),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", exit_application),
    )

    icon = pystray.Icon(
        "VoiceAssistant",
        icon_image,
        "Voice Assistant",
        menu,
    )

    status_thread = threading.Thread(
        target=update_tray,
        args=(icon,),
        daemon=True,
    )
    status_thread.start()

    icon.run()
