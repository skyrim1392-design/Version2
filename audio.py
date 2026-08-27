import os
import threading
import time
import wave

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

import config
import logger
from app_state import state

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WAV_FILE = os.path.join(BASE_DIR, "temp", "record.wav")

recording = []
stream = None
is_recording = False
microphone_monitor_thread = None
microphone_monitor_stop = None
MICROPHONE_CHECK_INTERVAL = 15.0


def find_microphone(mic_name):
    devices = sd.query_devices()

    if mic_name is None or str(mic_name).lower() == "auto":
        for index, device in enumerate(devices):
            if device["max_input_channels"] > 0:
                logger.info(f"Auto selected microphone: {index} {device['name']}")
                return index
        raise RuntimeError("No input microphone found")

    mic_name = str(mic_name).strip()
    for index, device in enumerate(devices):
        if device["max_input_channels"] <= 0:
            continue
        if mic_name.lower() in str(device["name"]).lower():
            logger.info(f"Selected microphone: {index} {device['name']}")
            return index

    logger.error(f"Configured microphone not found: {mic_name}")
    raise RuntimeError(f"Microphone not found: {mic_name}")


def check_microphone_available():
    current_config = config.load_config()
    mic_name = current_config.get("microphone", "auto")

    try:
        microphone_index = find_microphone(mic_name)
        device = sd.query_devices(microphone_index)

        if device["max_input_channels"] <= 0:
            raise RuntimeError(f"Microphone has no input channels: {device['name']}")

        with state.lock:
            state.microphone_available = True
            state.microphone_connected = True
            state.microphone_error = None
            state.last_microphone_check = time.time()

            # Clear only microphone-generated errors after recovery.
            if (
                state.status == "ERROR"
                and state.error
                and (
                    state.error.startswith("Microphone unavailable:")
                    or state.error.startswith("Microphone start failed:")
                    or state.error.startswith("Microphone is not producing")
                    or state.error.startswith("No audio was captured")
                )
            ):
                state.status = "IDLE"
                state.error = None

        return True

    except (OSError, RuntimeError, sd.PortAudioError) as error:
        with state.lock:
            state.microphone_available = False
            state.microphone_connected = False
            state.microphone_error = str(error)
            state.last_microphone_check = time.time()

            if not state.recording and not state.processing:
                state.status = "ERROR"
                state.error = f"Microphone unavailable: {error}"

        logger.error(f"Microphone health check failed: {error}")
        return False


def _microphone_monitor():
    logger.info("Microphone health monitor started")
    last_available = None

    while microphone_monitor_stop is not None and not microphone_monitor_stop.is_set():
        available = check_microphone_available()

        if available != last_available:
            logger.info(
                "Microphone health: " + ("AVAILABLE" if available else "UNAVAILABLE")
            )
            print(
                "MICROPHONE HEALTH:",
                "AVAILABLE" if available else "UNAVAILABLE"
            )
            last_available = available

        microphone_monitor_stop.wait(MICROPHONE_CHECK_INTERVAL)

    logger.info("Microphone health monitor stopped")


def start_microphone_monitor():
    global microphone_monitor_thread, microphone_monitor_stop

    if microphone_monitor_thread is not None and microphone_monitor_thread.is_alive():
        return

    microphone_monitor_stop = threading.Event()
    microphone_monitor_thread = threading.Thread(
        target=_microphone_monitor,
        name="MicrophoneHealthMonitor",
        daemon=True,
    )
    microphone_monitor_thread.start()


def stop_microphone_monitor():
    if microphone_monitor_stop is not None:
        microphone_monitor_stop.set()


def audio_callback(indata, frames, time_info, status):
    if status:
        logger.warning(f"Audio callback warning: {status}")

    recording.append(indata.copy())

    with state.lock:
        state.audio_callback_count += 1
        state.audio_frames_received += frames
        if status:
            state.audio_callback_errors += 1
        state.last_audio_samples += frames
        state.microphone_level = float(np.max(np.abs(indata)))


def start_recording():
    global stream, recording, is_recording

    logger.info("Recording start requested")

    current_config = config.load_config()
    audio_config = current_config.get("audio", {})
    input_sample_rate = audio_config.get("input_sample_rate", 44100)
    channels = audio_config.get("channels", 1)
    mic_name = current_config.get("microphone", "auto")

    microphone_index = find_microphone(mic_name)
    recording = []

    with state.lock:
        state.status = "RECORDING"
        state.recording = False
        state.processing = False
        state.microphone_connected = False
        state.microphone_available = False
        state.microphone_level = 0.0
        state.last_audio_samples = 0
        state.audio_callback_count = 0
        state.audio_callback_errors = 0
        state.audio_frames_received = 0
        state.audio_zero_sample_ratio = 0.0
        state.microphone_diagnostic = "NOT_TESTED"
        state.error = None

    try:
        stream = sd.InputStream(
            samplerate=input_sample_rate,
            channels=channels,
            dtype="float32",
            device=microphone_index,
            callback=audio_callback,
        )
        stream.start()
    except (sd.PortAudioError, OSError) as error:
        stream = None
        is_recording = False
        with state.lock:
            state.status = "ERROR"
            state.recording = False
            state.microphone_connected = False
            state.microphone_available = False
            state.microphone_error = str(error)
            state.error = f"Microphone start failed: {error}"
        logger.error(f"Failed to start audio stream: {error}")
        raise

    is_recording = True
    with state.lock:
        state.status = "RECORDING"
        state.recording = True
        state.microphone_connected = True
        state.microphone_available = True
        state.microphone_level = 0.0
        state.microphone_error = None
        state.last_audio_samples = 0
        state.microphone_diagnostic = "RECORDING"
        state.error = None

    print("🎙️ Recording...")
    logger.info("Microphone recording started")


def stop_recording():
    global stream, is_recording

    logger.info("Recording stop requested")

    if not is_recording:
        return None

    current_stream = stream
    is_recording = False

    with state.lock:
        state.recording = False

    if current_stream:
        try:
            current_stream.abort()
        except (sd.PortAudioError, OSError) as error:
            logger.error(f"Audio abort error: {error}")
        try:
            current_stream.close()
        except (sd.PortAudioError, OSError) as error:
            logger.error(f"Audio close error: {error}")

    stream = None

    with state.lock:
        state.last_audio_samples = len(recording)
        callback_count = state.audio_callback_count
        callback_errors = state.audio_callback_errors
        frame_count = state.audio_frames_received

    if len(recording) == 0:
        with state.lock:
            state.status = "ERROR"
            state.microphone_connected = False
            state.microphone_available = False
            state.microphone_diagnostic = "ERROR"
            state.error = "No audio was captured from the microphone"
        logger.error("Recording stopped but no audio was captured")
        return None

    audio = np.concatenate(recording, axis=0)
    peak_level = float(np.max(np.abs(audio)))
    rms_level = float(np.sqrt(np.mean(np.square(audio))))
    zero_ratio = float(np.mean(np.abs(audio) <= 1e-5))

    with state.lock:
        state.microphone_level = peak_level
        state.audio_zero_sample_ratio = zero_ratio

    logger.info(
        f"Recording diagnostics: callbacks={callback_count}, "
        f"frames={frame_count}, callback_errors={callback_errors}, "
        f"peak={peak_level:.8f}, rms={rms_level:.8f}, "
        f"zero_ratio={zero_ratio:.6f}"
    )

    if peak_level <= 1e-5 or rms_level <= 1e-6:
        with state.lock:
            state.status = "SUSPECT"
            state.microphone_connected = True
            state.microphone_available = True
            state.microphone_diagnostic = "SUSPECT"
            state.microphone_error = (
                "Microphone capture is active, but no usable audio signal was detected."
            )
            state.error = state.microphone_error

        logger.error(
            f"Microphone produced no usable audio: peak={peak_level:.8f}, rms={rms_level:.8f}"
        )
        return None

    with state.lock:
        state.microphone_diagnostic = "OK"
        state.microphone_error = None

    logger.info(f"Recording finished, samples={audio.shape}")
    return audio


def save_wav(audio):
    current_config = config.load_config()
    audio_config = current_config.get("audio", {})
    input_sample_rate = audio_config.get("input_sample_rate", 44100)
    whisper_sample_rate = audio_config.get("whisper_sample_rate", 16000)
    channels = audio_config.get("channels", 1)

    logger.info(
        f"Resampling audio: {input_sample_rate} Hz -> {whisper_sample_rate} Hz"
    )

    audio = resample_poly(
        audio.flatten(),
        whisper_sample_rate,
        input_sample_rate,
    )
    audio_int16 = (audio * 32767).astype(np.int16)

    with wave.open(WAV_FILE, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(whisper_sample_rate)
        wf.writeframes(audio_int16.tobytes())

    logger.info(f"WAV saved: {WAV_FILE}")
    return WAV_FILE
