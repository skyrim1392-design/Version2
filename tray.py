import os
import threading
import time

import pystray
from PIL import Image

import hardware
import live_supervisor
import logger
import microphone_diagnostics
from app_state import state
from config import load_config, set_config_value
from runtime_validation import validate_model_file

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ICON_DIR = os.path.join(BASE_DIR, "icons")

# ================= TEMPORARY WARNING =================

tray_icon = None
warning_until = 0.0
warning_message = None
warning_lock = threading.Lock()
microphone_error = False
startup_error = False
startup_error_message = None
stopping = False

# Display states that always win over the temporary warning overlay.
ACTIVE_STATES = ("RECORDING", "PROCESSING", "OUTPUTTING")


def show_warning(message, duration=3.0):
    """
    Show a temporary warning in the tray.

    This is UI-only.
    It does NOT modify application state.status.
    The warning content is logged here at its source, so it is recorded
    even when the tray loop is busy with a persistent error state.
    """
    global warning_until, warning_message

    logger.warning(f"Tray warning: {message}")

    with warning_lock:
        warning_message = message
        warning_until = time.monotonic() + duration


def create_icon(status):
    files = {
        "IDLE": "idle.png",
        "RECORDING": "recording.png",
        "PROCESSING": "processing.png",
        "OUTPUTTING": "processing.png",
        "WARNING": "error.png",
        "ERROR": "error.png",
        "LIVE_LISTENING": "recording.png",
        "LIVE_ERROR": "error.png",
        # Use the existing error icon until a dedicated suspect icon is designed.
        "SUSPECT": "error.png",
    }

    filename = files.get(status, "idle.png")
    path = os.path.join(ICON_DIR, filename)

    if os.path.exists(path):
        return Image.open(path)

    logger.warning(f"Icon missing: {path}")
    return Image.new("RGB", (64, 64), "black")


# ================= BACKEND =================


def apply_backend_option(option):
    set_config_value("whisper_backend", option["backend"])

    if option["device"] is not None:
        set_config_value("gpu_device", option["device"])

    logger.info(f"Backend changed to {option['label']}")


def backend_selected(option):
    def select(icon, item):
        apply_backend_option(option)

    return select


def is_backend_selected(option):
    def selected(item):
        config = load_config()

        if config.get("whisper_backend", "AUTO") != option["backend"]:
            return False

        # Only Vulkan options pin a specific device. Auto and CPU are
        # selected by their backend value alone.
        if option["backend"] == "VULKAN":
            return config.get("gpu_device", "AUTO") == option["device"]

        return True

    return selected


def build_backend_items():
    """Build Backend menu entries from hardware's discovered options."""

    return [
        pystray.MenuItem(
            option["label"],
            backend_selected(option),
            checked=is_backend_selected(option),
        )
        for option in hardware.list_backend_options()
    ]


# ================= LANGUAGE =================


def set_language(language):
    set_config_value("language_mode", language)
    logger.info(f"Language changed to {language}")


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
    logger.info(f"Task changed to {task}")


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
    logger.info(f"Model changed to {model}")


def model_selected(icon, item):
    model = item.text
    problem = validate_model_file(os.path.join(BASE_DIR, "models", model))

    if problem is not None:
        logger.warning(f"Model selection rejected: {model} ({problem})")
        show_warning(f"Model not usable: {model} ({problem})")
        return

    logger.info(f"Model selected: {model}")
    set_model(model)


def is_model_selected(model):
    def check(item):
        return load_config().get("model", "") == model

    return check


def build_model_items():
    """Build Model menu entries from disk plus the configured model.

    Re-evaluated whenever the menu opens, so models added or removed at
    runtime appear without a restart. The configured model stays visible
    and checked even when its file is missing from disk; showing it does
    not make the file available. When nothing is listed at all, a single
    disabled placeholder is shown.
    """
    configured = load_config().get("model", "")
    models = list(get_available_models())

    if configured and configured not in models:
        models.append(configured)
        models.sort()

    if not models:
        return [pystray.MenuItem("No models found", None, enabled=False)]

    return [
        pystray.MenuItem(model, model_selected, checked=is_model_selected(model))
        for model in models
    ]


# ================= LIVE TRANSCRIPTION =================


def is_live_active(item):
    return state.live_status == live_supervisor.LIVE_LISTENING


def live_toggle(icon, item):
    ok, reason = live_supervisor.toggle()

    if not ok:
        logger.warning(f"Live transcription toggle refused: {reason}")
        return

    set_config_value(
        "live_mode_enabled",
        state.live_status == live_supervisor.LIVE_LISTENING,
    )

    logger.info(f"Live transcription: {state.live_status}")


# ================= MICROPHONE DIAGNOSTICS =================


def test_microphone(icon, item):
    """Run the explicit microphone test in a worker so the tray stays responsive."""

    def worker():
        logger.info("Starting microphone diagnostic")
        result = microphone_diagnostics.run_microphone_test()

        status = result.get("status", "UNKNOWN")
        logger.info(f"Microphone diagnostic: {status}")

        if status == "OK":
            logger.info("Microphone diagnostic passed")
        elif status == "SUSPECT":
            logger.warning(
                "Microphone diagnostic suspect: visible and capture active, "
                "but no usable signal was detected"
            )
        else:
            logger.warning(
                "Microphone diagnostic failed: "
                + str(result.get("message", state.microphone_error))
            )

    threading.Thread(
        target=worker,
        name="MicrophoneDiagnostic",
        daemon=True,
    ).start()


def show_microphone_error():
    global microphone_error

    # Notify only when transitioning into the error state so repeated
    # failed presses do not spam OS notifications. Icon/title persist.
    if microphone_error:
        return

    microphone_error = True

    if tray_icon:
        tray_icon.icon = create_icon("ERROR")
        tray_icon.title = (
            "Voice Assistant - Microphone unavailable.\n"
            "Check microphone and restart the app."
        )

        tray_icon.notify(
            "Check your microphone connection and restart Voice Assistant.",
            "Microphone unavailable",
        )


def clear_microphone_error():
    global microphone_error

    if not microphone_error:
        return

    microphone_error = False

    logger.info("Tray: microphone recovered")


# ================= STARTUP ERROR =================


def show_startup_error(message):
    """
    Record a persistent startup-validation failure.

    This is UI-only.
    It does NOT modify application state.status.
    """

    global startup_error, startup_error_message

    startup_error = True
    startup_error_message = str(message)

    logger.error(f"Tray startup error: {startup_error_message}")


def clear_startup_error():
    """Clear a startup-validation failure after a successful retry.

    The status loop resumes normal display on its next iteration.
    """
    global startup_error, startup_error_message

    if not startup_error:
        return

    startup_error = False
    startup_error_message = None

    logger.info("Tray: startup error cleared")


# ================= EXIT =================


def exit_application(icon, item):
    global stopping

    logger.info("Exiting Voice Assistant")

    # stopping is asserted FIRST: the status loop stops rendering from
    # this moment on, even if teardown raises below.
    stopping = True

    try:
        live_supervisor.shutdown()
    finally:
        # The icon must always be stopped, otherwise the tray thread
        # would outlive a failed teardown and the process would hang.
        icon.stop()


# ================= STATUS =================


def display_status(status, live_status):
    """Resolve one render tick of the D6 precedence matrix.

    The F8 active states always win over live rendering, so the tray
    can never claim live is the foreground operation while F8 owns the
    microphone. Live rendering is applied only while status is IDLE.
    """
    if status == "IDLE":
        if live_status == live_supervisor.LIVE_ERROR:
            return "LIVE_ERROR"
        if live_status == live_supervisor.LIVE_LISTENING:
            return "LIVE_LISTENING"

    return status


def update_tray(icon):
    global tray_icon

    tray_icon = icon

    last_status = None
    warning_active = False
    last_warning_message = None

    while not stopping:
        with state.lock:
            current_status = state.status
            current_live = state.live_status

        display = display_status(current_status, current_live)

        with warning_lock:
            current_warning_until = warning_until
            current_warning_message = warning_message

        now = time.monotonic()

        # -------------------------------------------------
        # PERSISTENT STARTUP ERROR
        # -------------------------------------------------

        if startup_error:
            if icon.icon is None or last_status != "STARTUP_ERROR":
                logger.info("Tray status: STARTUP_ERROR")

                icon.icon = create_icon("ERROR")

                icon.title = (
                    "Voice Assistant - Startup error\n" + str(startup_error_message)
                )

                last_status = "STARTUP_ERROR"

            time.sleep(0.5)
            continue

        # -------------------------------------------------
        # PERSISTENT MICROPHONE ERROR
        # -------------------------------------------------

        if microphone_error:
            if icon.icon is None or last_status != "MICROPHONE_ERROR":
                logger.info("Tray status: MICROPHONE_ERROR")

                icon.icon = create_icon("ERROR")

                icon.title = (
                    "Voice Assistant - Microphone unavailable.\n"
                    "Check microphone and restart the app."
                )

                last_status = "MICROPHONE_ERROR"

            # Temporary warning notification may still run,
            # but the tray icon remains ERROR.
            time.sleep(0.5)
            continue

        # -------------------------------------------------
        # ACTIVE STATE WINS OVER WARNING
        # -------------------------------------------------

        if current_status in ACTIVE_STATES and warning_active:
            logger.info(f"Tray warning superseded by {current_status}")

            warning_active = False
            last_warning_message = None

        # -------------------------------------------------
        # TEMPORARY WARNING (only over IDLE)
        # -------------------------------------------------

        if now < current_warning_until and current_status == "IDLE":
            if not warning_active or current_warning_message != last_warning_message:
                icon.icon = create_icon("WARNING")

                icon.title = "Voice Assistant - " + str(current_warning_message)

                last_warning_message = current_warning_message
                warning_active = True

            time.sleep(0.1)
            continue

        # -------------------------------------------------
        # WARNING FINISHED
        # -------------------------------------------------

        if warning_active:
            logger.info("Tray warning finished")

            warning_active = False
            last_warning_message = None
            last_status = None

        # -------------------------------------------------
        # NORMAL APPLICATION STATUS
        # -------------------------------------------------

        if display != last_status:
            logger.info(f"Tray status: {display}")

            icon.icon = create_icon(display)

            last_status = display

        icon.title = "Voice Assistant - " + display

        time.sleep(0.5)


# ================= TRAY =================
def start_tray():
    icon_image = create_icon("IDLE")

    backend_menu = pystray.Menu(build_backend_items)

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

    model_menu = pystray.Menu(build_model_items)

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
        pystray.MenuItem(
            "Live Transcription",
            live_toggle,
            checked=is_live_active,
        ),
        pystray.MenuItem("Backend", backend_menu),
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
