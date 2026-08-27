import os
import wave
import threading
import time

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

import config
import logger
from app_state import state

# ================= PATHS =================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

WAV_FILE = os.path.join(
    BASE_DIR,
    "temp",
    "record.wav"
)


# ================= RECORDING STATE =================

recording = []

stream = None

is_recording = False

microphone_monitor_thread = None
microphone_monitor_stop = None

MICROPHONE_CHECK_INTERVAL = 15.0


# ================= MICROPHONE =================

def find_microphone(mic_name):

    devices = sd.query_devices()

    # Automatic microphone selection

    if (
        mic_name is None
        or str(mic_name).lower() == "auto"
    ):

        for index, device in enumerate(devices):

            if device["max_input_channels"] > 0:

                print(
                    "Using microphone:"
                )

                print(
                    index,
                    device["name"]
                )

                logger.info(
                    f"Auto selected microphone: "
                    f"{index} {device['name']}"
                )

                return index

        raise RuntimeError(
            "No input microphone found"
        )

    # Search for configured microphone

    mic_name = str(
        mic_name
    ).strip()

    for index, device in enumerate(devices):

        if device["max_input_channels"] <= 0:

            continue

        device_name = str(
            device["name"]
        )

        if mic_name.lower() in device_name.lower():

            print(
                "Using microphone:"
            )

            print(
                index,
                device_name
            )

            logger.info(
                f"Selected microphone: "
                f"{index} {device_name}"
            )

            return index

    logger.error(
        f"Configured microphone not found: "
        f"{mic_name}"
    )

    raise RuntimeError(
        f"Microphone not found: {mic_name}"
    )


# ================= MICROPHONE HEALTH =================

def check_microphone_available():

    current_config = config.load_config()

    mic_name = current_config.get(
        "microphone",
        "auto"
    )

    try:
        microphone_index = find_microphone(
            mic_name
        )

        device = sd.query_devices(
            microphone_index
        )

        if device["max_input_channels"] <= 0:
            raise RuntimeError(
                f"Microphone has no input channels: {device['name']}"
            )

        # query_devices confirms that PortAudio/Windows currently exposes
        # the device. We deliberately do not open a stream here because
        # probing every few seconds should not disturb active recording.
        with state.lock:
            state.microphone_available = True
            state.microphone_connected = True
            state.microphone_error = None
            state.last_microphone_check = time.time()

        return True

    except (OSError, RuntimeError, sd.PortAudioError) as error:

        with state.lock:
            state.microphone_available = False
            state.microphone_connected = False
            state.microphone_error = str(error)
            state.last_microphone_check = time.time()

            # Never overwrite an active recording/processing state.
            if not state.recording and not state.processing:
                state.status = "ERROR"
                state.error = f"Microphone unavailable: {error}"

        logger.error(
            f"Microphone health check failed: {error}"
        )

        return False


def _microphone_monitor():

    logger.info(
        "Microphone health monitor started"
    )

    last_available = None

    while microphone_monitor_stop is not None and not microphone_monitor_stop.is_set():

        available = check_microphone_available()

        if available != last_available:

            if available:
                print("MICROPHONE HEALTH: AVAILABLE")
                logger.info("Microphone is available")
            else:
                print("MICROPHONE HEALTH: UNAVAILABLE")
                logger.error("Microphone is unavailable")

            last_available = available

        microphone_monitor_stop.wait(
            MICROPHONE_CHECK_INTERVAL
        )

    logger.info(
        "Microphone health monitor stopped"
    )


def start_microphone_monitor():

    global microphone_monitor_thread
    global microphone_monitor_stop

    if (
        microphone_monitor_thread is not None
        and microphone_monitor_thread.is_alive()
    ):
        return

    microphone_monitor_stop = threading.Event()

    microphone_monitor_thread = threading.Thread(
        target=_microphone_monitor,
        name="MicrophoneHealthMonitor",
        daemon=True
    )

    microphone_monitor_thread.start()


def stop_microphone_monitor():

    global microphone_monitor_stop

    if microphone_monitor_stop is not None:
        microphone_monitor_stop.set()


# ================= AUDIO CALLBACK =================

def audio_callback(indata, frames, time_info, status):

    if status:
        logger.warning(
            f"Audio callback warning: {status}"
        )

    recording.append(indata.copy())

    with state.lock:

        state.last_audio_samples += frames

        state.microphone_level = float(
            np.max(np.abs(indata))
        )


# ================= START RECORDING =================

def start_recording():

    global stream
    global recording
    global is_recording

    print(
        "START RECORD FUNCTION"
    )

    logger.info(
        "Recording start requested"
    )

    # Load current configuration

    current_config = config.load_config()

    audio_config = current_config.get(
        "audio",
        {}
    )

    input_sample_rate = audio_config.get(
        "input_sample_rate",
        44100
    )

    channels = audio_config.get(
        "channels",
        1
    )

    mic_name = current_config.get(
        "microphone",
        "auto"
    )

    logger.info(
        f"Audio config: "
        f"input={input_sample_rate}, "
        f"channels={channels}, "
        f"microphone={mic_name}"
    )

    # Find microphone using current configuration

    microphone_index = find_microphone(
        mic_name
    )

    recording = []

    # Reset per-recording diagnostics before opening the stream.
    with state.lock:
        state.status = "RECORDING"
        state.recording = False
        state.processing = False
        state.microphone_connected = False
        state.microphone_level = 0.0
        state.last_audio_samples = 0
        state.error = None

    try:

        stream = sd.InputStream(
            samplerate=input_sample_rate,
            channels=channels,
            dtype="float32",
            device=microphone_index,
            callback=audio_callback
        )

        stream.start()

    except (sd.PortAudioError, OSError) as e:

        stream = None
        is_recording = False

        with state.lock:
            state.status = "ERROR"
            state.recording = False
            state.microphone_connected = False
            state.microphone_level = 0.0
            state.error = f"Microphone start failed: {e}"

        logger.error(
            f"Failed to start audio stream: {e}"
        )

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
        state.error = None

    print(
        "🎙️ Recording..."
    )

    logger.info(
        "Microphone recording started"
    )


# ================= STOP RECORDING =================

def stop_recording():

    global stream
    global is_recording

    print("STOP RECORD FUNCTION")
    logger.info("Recording stop requested")

    if not is_recording:
        print("Recording was not active")
        return None

    current_stream = stream

    # Immediately change state
    is_recording = False

    with state.lock:
        state.recording = False

    if current_stream:

        try:

            print("Stopping audio stream")

            # abort immediately, don't wait for callbacks
            current_stream.abort()

            print("Audio stream aborted")

        except (sd.PortAudioError, OSError) as e:

            logger.error(
                f"Audio abort error: {e}"
            )

        try:

            current_stream.close()

            print("Audio stream closed")

        except (sd.PortAudioError, OSError) as e:

            logger.error(
                f"Audio close error: {e}"
            )

    stream = None

    with state.lock:
        state.last_audio_samples = len(recording)

    if len(recording) == 0:

        print("No audio")

        logger.warning(
            "Recording stopped but no audio was captured"
        )

        with state.lock:
            state.status = "ERROR"
            state.microphone_connected = False
            state.microphone_level = 0.0
            state.error = "No audio was captured from the microphone"

        return None

    audio = np.concatenate(
        recording,
        axis=0
    )

    # A stream can remain open while delivering only zeros/silence.
    # Do not send an empty/faulty microphone recording to Whisper.
    peak_level = float(
        np.max(np.abs(audio))
    )

    rms_level = float(
        np.sqrt(np.mean(np.square(audio)))
    )

    with state.lock:
        state.microphone_level = peak_level

    # These are intentionally conservative diagnostics. Normal quiet
    # speech should pass; completely zero/near-zero input is rejected.
    silence_peak_threshold = 1e-5
    silence_rms_threshold = 1e-6

    if (
        peak_level <= silence_peak_threshold
        or rms_level <= silence_rms_threshold
    ):

        print(
            "Microphone fault: no usable audio signal detected"
        )

        logger.error(
            f"Microphone produced no usable audio: "
            f"peak={peak_level:.8f}, rms={rms_level:.8f}"
        )

        with state.lock:
            state.status = "ERROR"
            state.microphone_connected = False
            state.microphone_level = peak_level
            state.error = (
                "Microphone is not producing a usable audio signal"
            )

        return None

    print("Recording finished")

    logger.info(
        f"Recording finished, samples={audio.shape}, "
        f"peak={peak_level:.8f}, rms={rms_level:.8f}"
    )

    return audio


# ================= SAVE WAV =================

def save_wav(audio):

    # Load current configuration

    current_config = config.load_config()

    audio_config = current_config.get(
        "audio",
        {}
    )

    input_sample_rate = audio_config.get(
        "input_sample_rate",
        44100
    )

    whisper_sample_rate = audio_config.get(
        "whisper_sample_rate",
        16000
    )

    channels = audio_config.get(
        "channels",
        1
    )

    logger.info(
        f"Resampling audio: "
        f"{input_sample_rate} Hz -> "
        f"{whisper_sample_rate} Hz"
    )

    # Convert to one-dimensional audio

    audio = audio.flatten()

    # Resample from hardware rate to Whisper rate

    audio = resample_poly(
        audio,
        whisper_sample_rate,
        input_sample_rate
    )

    # Convert float32 [-1, 1] to signed 16-bit PCM

    audio_int16 = (
        audio * 32767
    ).astype(
        np.int16
    )

    # Save WAV

    with wave.open(
        WAV_FILE,
        "wb"
    ) as wf:

        wf.setnchannels(
            channels
        )

        wf.setsampwidth(
            2
        )

        wf.setframerate(
            whisper_sample_rate
        )

        wf.writeframes(
            audio_int16.tobytes()
        )

    print(
        "WAV saved"
    )

    logger.info(
        f"WAV saved: {WAV_FILE}"
    )

    return WAV_FILE
