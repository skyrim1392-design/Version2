import os
import subprocess

import logger
from config import load_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

WHISPER_PATH = os.path.join(
    BASE_DIR,
    "whisper",
    "Vulkan",
    "whisper-cli.exe",
)


def detect_vulkan_devices():
    """
    Ask the bundled Whisper executable to enumerate Vulkan devices.

    Returns:
        list[dict]: Detected Vulkan devices.
    """

    if not os.path.isfile(WHISPER_PATH):
        logger.warning(f"Whisper executable not found: {WHISPER_PATH}")
        return []

    try:
        result = subprocess.run(
            [WHISPER_PATH, "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning(f"Could not probe Vulkan devices: {error}")
        return []

    output = f"{result.stdout}\n{result.stderr}"

    devices = []

    for line in output.splitlines():
        line = line.strip()

        if not line.startswith("ggml_vulkan:"):
            continue

        if " = " not in line:
            continue

        try:
            device_part = line.split(":", 1)[1].strip()
            device_id, device_info = device_part.split(" = ", 1)

            if not device_id.isdigit():
                continue

            devices.append(
                {
                    "id": int(device_id),
                    "name": device_info.split(" | ", 1)[0].strip(),
                }
            )

        except (ValueError, IndexError):
            continue

    logger.info(f"Detected Vulkan devices: {devices}")

    return devices


def get_vulkan_devices():
    """Return the currently detected Vulkan devices."""

    return detect_vulkan_devices()


def is_vulkan_available():
    """Return True when at least one Vulkan device is available."""

    return bool(detect_vulkan_devices())


def select_backend():
    """
    Select the Whisper backend from configuration.

    AUTO:
        Prefer Vulkan when available, otherwise CPU.

    VULKAN:
        Require a Vulkan device.

    CPU:
        Always use CPU.
    """

    config = load_config()

    backend = str(config.get("whisper_backend", "AUTO")).upper()

    if backend == "CPU":
        logger.info("Whisper backend selected: CPU")
        return {
            "backend": "CPU",
            "device": None,
        }

    devices = detect_vulkan_devices()

    if backend == "VULKAN":
        if not devices:
            raise RuntimeError(
                "Vulkan backend was requested, but no Vulkan device is available."
            )

        device_id = config.get("gpu_device", "AUTO")

        if str(device_id).upper() == "AUTO":
            selected_device = devices[0]["id"]
        else:
            try:
                selected_device = int(device_id)
            except (TypeError, ValueError) as error:
                raise RuntimeError(f"Invalid Vulkan device ID: {device_id}") from error

            available_ids = {device["id"] for device in devices}

            if selected_device not in available_ids:
                raise RuntimeError(
                    f"Configured Vulkan device {selected_device} is not available."
                )

        logger.info(f"Whisper backend selected: VULKAN, device={selected_device}")

        return {
            "backend": "VULKAN",
            "device": selected_device,
        }

    if backend == "AUTO":
        if devices:
            selected_device = devices[0]["id"]

            logger.info(
                f"Whisper backend AUTO selected: VULKAN, device={selected_device}"
            )

            return {
                "backend": "VULKAN",
                "device": selected_device,
            }

        logger.info("Whisper backend AUTO selected: CPU")

        return {
            "backend": "CPU",
            "device": None,
        }

    raise RuntimeError(
        f"Unsupported Whisper backend: {backend}. Use AUTO, VULKAN, or CPU."
    )
