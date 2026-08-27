import threading
import time

import numpy as np
import sounddevice as sd

import config
import logger
from app_state import state


DIAGNOSTIC_DURATION = 1.0
SIGNAL_PEAK_THRESHOLD = 1e-5
SIGNAL_RMS_THRESHOLD = 1e-6


def run_microphone_test():
    """Run a short real capture test without interfering with F8 recording."""

    with state.lock:
        if state.recording or state.processing:
            return {
                "ok": False,
                "status": "BUSY",
                "message": "Microphone test cannot run while recording or processing."
            }

    current_config = config.load_config()
    audio_config = current_config.get("audio", {})
    sample_rate = audio_config.get("input_sample_rate", 44100)
    channels = audio_config.get("channels", 1)
    mic_name = current_config.get("microphone", "auto")

    callback_count = 0
    frame_count = 0
    callback_errors = []
    chunks = []
    callback_lock = threading.Lock()

    try:
        # Reuse the application's configured-device selection logic.
        import audio
        device_index = audio.find_microphone(mic_name)
        device = sd.query_devices(device_index)

        if device["max_input_channels"] <= 0:
            raise RuntimeError("Selected microphone has no input channels")

        def callback(indata, frames, time_info, status):
            nonlocal callback_count, frame_count

            with callback_lock:
                callback_count += 1
                frame_count += frames
                chunks.append(indata.copy())

                if status:
                    callback_errors.append(str(status))

        logger.info(
            f"Microphone diagnostic started: device={device_index}, "
            f"name={device['name']}, rate={sample_rate}, channels={channels}"
        )

        stream = sd.InputStream(
            samplerate=sample_rate,
            channels=channels,
            dtype="float32",
            device=device_index,
            callback=callback,
        )

        with stream:
            stream.start()
            time.sleep(DIAGNOSTIC_DURATION)

        if not chunks:
            raise RuntimeError("No audio callbacks were received")

        samples = np.concatenate(chunks, axis=0)
        peak = float(np.max(np.abs(samples)))
        rms = float(np.sqrt(np.mean(np.square(samples))))
        zero_ratio = float(np.mean(np.abs(samples) <= SIGNAL_PEAK_THRESHOLD))

        result = {
            "ok": (
                callback_count > 0
                and frame_count > 0
                and peak > SIGNAL_PEAK_THRESHOLD
                and rms > SIGNAL_RMS_THRESHOLD
            ),
            "status": "OK" if (
                peak > SIGNAL_PEAK_THRESHOLD
                and rms > SIGNAL_RMS_THRESHOLD
            ) else "SUSPECT",
            "device": device["name"],
            "device_index": device_index,
            "callback_count": callback_count,
            "frame_count": frame_count,
            "peak": peak,
            "rms": rms,
            "zero_ratio": zero_ratio,
            "callback_errors": callback_errors,
        }

        with state.lock:
            state.microphone_available = True
            state.microphone_connected = True
            state.microphone_level = peak
            state.microphone_diagnostic = result["status"]
            state.microphone_error = None if result["ok"] else (
                "Device is visible and capture callbacks are arriving, "
                "but no usable audio signal was detected."
            )
            state.last_microphone_check = time.time()

            if result["ok"]:
                if state.status in ("ERROR", "SUSPECT"):
                    state.status = "IDLE"
                    state.error = None
            else:
                state.status = "SUSPECT"
                state.error = state.microphone_error

        logger.info(
            f"Microphone diagnostic result: {result}"
        )
        return result

    except (OSError, RuntimeError, sd.PortAudioError) as error:
        with state.lock:
            state.microphone_available = False
            state.microphone_connected = False
            state.microphone_diagnostic = "ERROR"
            state.microphone_error = str(error)
            state.status = "ERROR"
            state.error = f"Microphone diagnostic failed: {error}"
            state.last_microphone_check = time.time()

        logger.error(
            f"Microphone diagnostic failed: {error}"
        )

        return {
            "ok": False,
            "status": "ERROR",
            "message": str(error),
            "callback_count": callback_count,
            "frame_count": frame_count,
            "callback_errors": callback_errors,
        }
