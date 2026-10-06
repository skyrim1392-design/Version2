import os

import logger
from config import load_config
from hardware import select_backend

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

last_error = None

MODEL_MIN_BYTES = 1024 * 1024  # 1 MiB

MODEL_MAGICS = (
    b"\x6c\x6d\x67\x67",  # GGML magic 0x67676d6c stored little-endian
    b"GGUF",
)


def validate_model_file(model_path):
    """Pure file-format sanity check for a model file.

    Cheap by design: stat plus a 4-byte binary read. No checksums, no
    header parsing, and no attempt to load the model.

    Returns None when the file passes, otherwise a short reason string.
    """
    if not os.path.exists(model_path):
        return "missing"

    if not os.path.isfile(model_path):
        return "not a regular file"

    try:
        if os.path.getsize(model_path) < MODEL_MIN_BYTES:
            return "too small"

    except OSError as error:
        return f"unreadable ({type(error).__name__})"

    try:
        with open(model_path, "rb") as handle:
            header = handle.read(4)

    except OSError as error:
        return f"unreadable ({type(error).__name__})"

    if header not in MODEL_MAGICS:
        return "bad magic"

    return None


def run_startup_validation(quiet=False):
    """Run full startup validation (model + executable/backend).

    quiet=True suppresses the per-attempt start/failure log lines so a
    periodic retry caller does not repeat them every cycle. last_error is
    still recorded and the success line is always logged. Default
    behavior is unchanged for the boot path and direct callers.
    """
    global last_error

    if not quiet:
        logger.info("Running startup validation")

    try:
        config = load_config()

        model_name = config["model"]
        model_path = os.path.join(BASE_DIR, "models", model_name)

        problem = validate_model_file(model_path)

        if problem is not None:
            if problem == "missing":
                raise RuntimeError(
                    f"Whisper model not found: {model_path}"
                )

            raise RuntimeError(
                f"Whisper model corrupt: {model_path} ({problem})"
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

        if not quiet:
            logger.error(f"Startup validation failed: {error}")

        return False
