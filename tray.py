import os

import pystray
from PIL import Image, ImageDraw

from config import load_config, set_config_value


# ================= ICON =================

def create_icon():

    image = Image.new(
        "RGB",
        (64, 64),
        "black"
    )

    draw = ImageDraw.Draw(image)

    draw.ellipse(
        (16, 8, 48, 56),
        outline="white",
        width=4
    )

    draw.line(
        (32, 56, 32, 60),
        fill="white",
        width=4
    )

    return image


# ================= GPU =================

def set_gpu(device):

    set_config_value(
        "gpu_device",
        device
    )

    print(
        f"GPU changed to device {device}"
    )


def gpu_rx580(icon, item):

    set_gpu(0)


def gpu_intel(icon, item):

    set_gpu(1)


def is_gpu_rx580(item):

    return load_config().get(
        "gpu_device",
        0
    ) == 0


def is_gpu_intel(item):

    return load_config().get(
        "gpu_device",
        0
    ) == 1


# ================= LANGUAGE =================

def set_language(language):

    set_config_value(
        "language_mode",
        language
    )

    print(
        f"Language changed to {language}"
    )


def language_auto(icon, item):

    set_language("AUTO")


def language_english(icon, item):

    set_language("ENGLISH")


def language_persian(icon, item):

    set_language("PERSIAN")


def is_language_auto(item):

    return load_config().get(
        "language_mode",
        "AUTO"
    ) == "AUTO"


def is_language_english(item):

    return load_config().get(
        "language_mode",
        "AUTO"
    ) == "ENGLISH"


def is_language_persian(item):

    return load_config().get(
        "language_mode",
        "AUTO"
    ) == "PERSIAN"


# ================= TASK =================

def set_task(task):

    set_config_value(
        "task_mode",
        task
    )

    print(
        f"Task changed to {task}"
    )


def task_transcribe(icon, item):

    set_task("TRANSCRIBE")


def task_translate(icon, item):

    set_task("TRANSLATE")


def is_task_transcribe(item):

    return load_config().get(
        "task_mode",
        "TRANSCRIBE"
    ) == "TRANSCRIBE"


def is_task_translate(item):

    return load_config().get(
        "task_mode",
        "TRANSCRIBE"
    ) == "TRANSLATE"


# ================= MODEL =================

def get_available_models():

    models_dir = os.path.join(
        os.path.dirname(
            os.path.abspath(__file__)
        ),
        "models"
    )

    if not os.path.exists(models_dir):

        return []

    models = []

    for filename in os.listdir(models_dir):

        if filename.lower().endswith(".bin"):

            models.append(filename)

    models.sort()

    return models


def set_model(model):

    set_config_value(
        "model",
        model
    )

    print(
        f"Model changed to {model}"
    )


def model_selected(icon, item):

    model = item.text

    print(
        f"Model selected: {model}"
    )

    set_model(model)


def is_model_selected(model):

    def check(item):

        return load_config().get(
            "model",
            ""
        ) == model

    return check


# ================= EXIT =================

def exit_application(icon, item):

    print(
        "Exiting Voice Assistant..."
    )

    icon.stop()


# ================= TRAY =================

def start_tray():

    icon_image = create_icon()


    # ================= GPU MENU =================

    gpu_menu = pystray.Menu(

        pystray.MenuItem(
            "Radeon RX 580",
            gpu_rx580,
            checked=is_gpu_rx580
        ),

        pystray.MenuItem(
            "Intel HD Graphics 530",
            gpu_intel,
            checked=is_gpu_intel
        )
    )


    # ================= LANGUAGE MENU =================

    language_menu = pystray.Menu(

        pystray.MenuItem(
            "Auto",
            language_auto,
            checked=is_language_auto
        ),

        pystray.MenuItem(
            "English",
            language_english,
            checked=is_language_english
        ),

        pystray.MenuItem(
            "Persian",
            language_persian,
            checked=is_language_persian
        )
    )


    # ================= TASK MENU =================

    task_menu = pystray.Menu(

        pystray.MenuItem(
            "Transcribe",
            task_transcribe,
            checked=is_task_transcribe
        ),

        pystray.MenuItem(
            "Translate to English",
            task_translate,
            checked=is_task_translate
        )
    )


    # ================= MODEL MENU =================

    model_items = []

    models = get_available_models()

    for model in models:

        model_items.append(
            pystray.MenuItem(
                model,
                model_selected,
                checked=is_model_selected(model)
            )
        )


    model_menu = pystray.Menu(
        *model_items
    )


    # ================= MAIN MENU =================

    menu = pystray.Menu(

        pystray.MenuItem(
            "Voice Assistant",
            None,
            enabled=False
        ),

        pystray.Menu.SEPARATOR,

        pystray.MenuItem(
            "GPU",
            gpu_menu
        ),

        pystray.MenuItem(
            "Language",
            language_menu
        ),

        pystray.MenuItem(
            "Task",
            task_menu
        ),

        pystray.MenuItem(
            "Model",
            model_menu
        ),

        pystray.Menu.SEPARATOR,

        pystray.MenuItem(
            "Exit",
            exit_application
        )
    )


    # ================= START =================

    icon = pystray.Icon(
        "VoiceAssistant",
        icon_image,
        "Voice Assistant",
        menu
    )

    icon.run()