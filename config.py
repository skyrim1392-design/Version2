import copy
import json
import os
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


# ================= DEFAULT CONFIG =================

DEFAULT_CONFIG = {
    "model": "ggml-large-v3.bin",
    "language_mode": "AUTO",
    "task_mode": "TRANSCRIBE",
    "microphone": "auto",
    "push_key": "f8",
    "audio": {"input_sample_rate": 44100, "whisper_sample_rate": 16000, "channels": 1},
    "threads": 8,
    "whisper_backend": "AUTO",
    "gpu_device": "AUTO",
}


VALID_WHISPER_BACKENDS = {"AUTO", "CPU", "VULKAN", "CUDA"}

# ================= LOAD =================


def load_config():

    if not os.path.exists(CONFIG_FILE):
        config = copy.deepcopy(DEFAULT_CONFIG)

        save_config(config)

        return config

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)

    except (json.JSONDecodeError, OSError) as e:
        print("⚠ Config error:", e)
        print("Using default configuration.")

        return copy.deepcopy(DEFAULT_CONFIG)

    # Make sure the loaded object is a dictionary

    if not isinstance(config, dict):
        print("⚠ Invalid config format.")
        print("Using default configuration.")

        return copy.deepcopy(DEFAULT_CONFIG)

    # Add missing settings

    changed = False

    for key, default_value in DEFAULT_CONFIG.items():
        if key not in config:
            config[key] = copy.deepcopy(default_value)

            changed = True

    # Add missing nested audio settings

    if not isinstance(config.get("audio"), dict):
        config["audio"] = copy.deepcopy(DEFAULT_CONFIG["audio"])

        changed = True

    else:
        for key, default_value in DEFAULT_CONFIG["audio"].items():
            if key not in config["audio"]:
                config["audio"][key] = copy.deepcopy(default_value)

                changed = True

    if changed:
        save_config(config)

    config, validation_changed = validate_config(config)

    if validation_changed:
        save_config(config)

    return config


def validate_config(config):
    if not isinstance(config, dict):
        print("⚠ Invalid config format.")
        print("Using default configuration.")
        return copy.deepcopy(DEFAULT_CONFIG), True

    validated = copy.deepcopy(config)
    changed = False

    backend = validated.get("whisper_backend")

    if not isinstance(backend, str):
        print("⚠ Invalid whisper_backend.")
        print("Using AUTO.")
        validated["whisper_backend"] = "AUTO"
        changed = True
    else:
        backend = backend.upper()

        if backend not in VALID_WHISPER_BACKENDS:
            print(f"⚠ Unsupported whisper_backend: {backend}")
            print("Using AUTO.")
            validated["whisper_backend"] = "AUTO"
            changed = True
        elif backend != validated["whisper_backend"]:
            validated["whisper_backend"] = backend
            changed = True

    gpu_device = validated.get("gpu_device")

    if gpu_device != "AUTO":
        try:
            gpu_device = int(gpu_device)

            if gpu_device < 0:
                raise ValueError

            if gpu_device != validated["gpu_device"]:
                validated["gpu_device"] = gpu_device
                changed = True

        except (TypeError, ValueError):
            print("⚠ Invalid gpu_device.")
            print("Using AUTO.")
            validated["gpu_device"] = "AUTO"
            changed = True

    return validated, changed


# ================= SAVE =================


def save_config(config):

    # Write to a temporary file first.
    # Only replace config.json after the write succeeds.

    config_dir = os.path.dirname(CONFIG_FILE)

    fd, temp_file = tempfile.mkstemp(
        prefix="config_", suffix=".tmp", dir=config_dir, text=True
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)

            f.write("\n")

            f.flush()

            os.fsync(f.fileno())

        # Replace original configuration atomically

        os.replace(temp_file, CONFIG_FILE)

    except Exception:
        try:
            os.remove(temp_file)

        except OSError:
            pass

        raise


# ================= GET =================


def get_config_value(key):

    config = load_config()

    keys = key.split(".")

    value = config

    for current_key in keys:
        if not isinstance(value, dict):
            return None

        if current_key not in value:
            return None

        value = value[current_key]

    return value


# ================= SET =================


def set_config_value(key, value):

    config = load_config()

    keys = key.split(".")

    current = config

    # Walk through all parent dictionaries

    for current_key in keys[:-1]:
        if current_key not in current or not isinstance(current[current_key], dict):
            current[current_key] = {}

        current = current[current_key]

    # Set final value

    current[keys[-1]] = value

    save_config(config)
