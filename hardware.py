import os
import subprocess

import logger
from config import load_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Whisper runtime payloads, one executable per backend.
# These are runtime files (gitignored), not repository content.

WHISPER_EXE = {
    "CPU": os.path.join(
        BASE_DIR,
        "whisper",
        "CPU",
        "whisper-cli.exe",
    ),
    "VULKAN": os.path.join(
        BASE_DIR,
        "whisper",
        "Vulkan",
        "whisper-cli.exe",
    ),
}


def detect_vulkan_devices():
    """
    Ask the bundled Whisper executable to enumerate Vulkan devices.

    This probe is the origin of all device information. Backend selection
    and tray presentation consume its records through get_vulkan_devices(),
    so they can never disagree. Device names come only from Vulkan
    discovery, never from hard-coded values.

    Returns:
        list[dict]: Discovered Vulkan devices, each record containing:
            id   - device index passed to whisper as -dev
            name - device name reported by Vulkan discovery
    """

    # Device discovery always probes the Vulkan build: a CPU-only
    # executable cannot enumerate Vulkan devices.
    vulkan_exe = WHISPER_EXE["VULKAN"]

    if not os.path.isfile(vulkan_exe):
        logger.warning(f"Whisper executable not found: {vulkan_exe}")
        return []

    try:
        result = subprocess.run(
            [vulkan_exe, "--help"],
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
    """
    Return the currently discovered Vulkan devices.

    This is the authoritative accessor for device records. Backend
    selection and the tray both go through it, so the menu can only ever
    offer devices that selection is able to use.
    """

    return detect_vulkan_devices()


def is_vulkan_available():
    """Return True when at least one Vulkan device is available."""

    return bool(get_vulkan_devices())


def list_backend_options():
    """
    Return the selectable backend options for tray presentation.

    The tray renders these entries directly: it performs no device
    discovery itself and decides nothing about which backends are usable.
    Device names come from get_vulkan_devices(), so a device that
    selection cannot use can never appear in the menu.

    Returns:
        list[dict]: One option per selectable choice, each containing:
            label   - display text for the menu entry
            backend - value to store in config whisper_backend
            device  - value to store in config gpu_device, or None to
                      leave gpu_device unchanged
    """

    options = [
        {"label": "Auto", "backend": "AUTO", "device": "AUTO"},
        {"label": "CPU", "backend": "CPU", "device": None},
    ]

    for device in get_vulkan_devices():
        options.append(
            {
                "label": device["name"],
                "backend": "VULKAN",
                "device": device["id"],
            }
        )

    return options


SUPPORTED_BACKENDS = ("AUTO", "CPU", "VULKAN")


def resolve_vulkan_device(devices, requested_device):
    """
    Resolve gpu_device against the discovered Vulkan devices.

    AUTO:
        Use the first discovered device.

    numeric:
        Must match a discovered device, otherwise RuntimeError.

    Raises:
        RuntimeError: When no device can be resolved.
    """

    if not devices:
        raise RuntimeError("No Vulkan device is available.")

    if str(requested_device).strip().upper() == "AUTO":
        return devices[0]["id"]

    try:
        selected_device = int(requested_device)
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            f"Invalid Vulkan device ID: {requested_device}"
        ) from error

    available_ids = [device["id"] for device in devices]

    if selected_device not in available_ids:
        raise RuntimeError(
            f"Configured Vulkan device {selected_device} is not available. "
            f"Available devices: {available_ids}"
        )

    return selected_device


def _cpu_backend_spec():
    """Backend specification for running Whisper on the CPU."""

    return {
        "backend": "CPU",
        "device": None,
        "executable": WHISPER_EXE["CPU"],
        "device_args": ["--no-gpu"],
    }


def select_backend():
    """
    Select the Whisper backend and device from configuration.

    Returns:
        dict: Backend specification used to build the Whisper command line.
            backend      - "CPU" or "VULKAN"
            device       - Vulkan device id, or None for CPU
            executable   - Whisper executable to launch
            device_args  - backend-specific command line arguments

    CPU:
        Always use CPU.

    VULKAN:
        Require a usable Vulkan device, otherwise RuntimeError.

    AUTO:
        Use Vulkan when a usable Vulkan device exists, otherwise CPU.
        An unusable configured gpu_device also falls back to CPU.
    """

    config = load_config()

    backend = str(config.get("whisper_backend", "AUTO")).strip().upper()

    if backend == "CPU":
        logger.info("Whisper backend selected: CPU")
        return _cpu_backend_spec()

    if backend not in SUPPORTED_BACKENDS:
        raise RuntimeError(
            f"Unsupported Whisper backend: {backend}. Use AUTO, VULKAN, or CPU."
        )

    devices = get_vulkan_devices()

    requested_device = config.get("gpu_device", "AUTO")

    if not devices:
        if backend == "VULKAN":
            raise RuntimeError(
                "Vulkan backend was requested, but no Vulkan device is available."
            )

        logger.info("Whisper backend AUTO selected: CPU (no Vulkan device found)")

        return _cpu_backend_spec()

    try:
        selected_device = resolve_vulkan_device(devices, requested_device)
    except RuntimeError as error:
        if backend == "VULKAN":
            raise

        logger.warning(f"Whisper backend AUTO fell back to CPU: {error}")

        return _cpu_backend_spec()

    logger.info(
        f"Whisper backend selected: {backend} -> VULKAN, device={selected_device}"
    )

    return {
        "backend": "VULKAN",
        "device": selected_device,
        "executable": WHISPER_EXE["VULKAN"],
        "device_args": ["-dev", str(selected_device)],
    }
