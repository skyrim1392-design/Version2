import os

import logger
from config import load_config
from hardware import select_backend

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

last_error = None


def run_startup_validation():
    global last_error

    logger.info("Running startup validation")

    try:
        config = load_config()

        model_name = config["model"]
        model_path = os.path.join(BASE_DIR, "models", model_name)

        if not os.path.isfile(model_path):
            raise RuntimeError(
                f"Whisper model not found: {model_path}"
            )

        backend_spec = select_backend()

        if not os.path.isfile(backend_spec["executable"]):
            raise RuntimeError(
                f"Whisper executable not found: {backend_spec['executable']}"
            )

        logger.info(
            f"Startup validation successful: "
            f"model={model_name}, "
            f"backend={backend_spec['backend']}, "
            f"device={backend_spec['device']}, "
            f"executable={backend_spec['executable']}"
        )

        return True

    except Exception as error:
        last_error = str(error)
        logger.error(f"Startup validation failed: {error}")
        return False
