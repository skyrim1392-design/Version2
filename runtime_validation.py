import os

import logger
from config import load_config
from hardware import select_backend
from whisper_engine import WHISPER_PATH

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def run_startup_validation():
    logger.info("Running startup validation")

    try:
        config = load_config()

        model_name = config["model"]
        model_path = os.path.join(BASE_DIR, "models", model_name)

        if not os.path.isfile(model_path):
            raise RuntimeError(
                f"Whisper model not found: {model_path}"
            )

        if not os.path.isfile(WHISPER_PATH):
            raise RuntimeError(
                f"Whisper executable not found: {WHISPER_PATH}"
            )

        backend = select_backend()

        logger.info(
            f"Startup validation successful: "
            f"model={model_name}, "
            f"backend={backend['backend']}, "
            f"device={backend['device']}"
        )

        return True

    except Exception as error:
        logger.error(f"Startup validation failed: {error}")
        return False
