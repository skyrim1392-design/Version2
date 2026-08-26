import os
import wave

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

import config
import logger

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


# ================= AUDIO CALLBACK =================

def audio_callback(
    indata,
    frames,
    time_info,
    status
):

    if status:

        print(status)

        logger.info(
            f"Audio callback status: {status}"
        )

    recording.append(
        indata.copy()
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

    try:

        stream = sd.InputStream(
            samplerate=input_sample_rate,
            channels=channels,
            dtype="float32",
            device=microphone_index,
            callback=audio_callback
        )

        stream.start()

    except Exception as e:

        stream = None
        is_recording = False

        logger.error(
            f"Failed to start audio stream: {e}"
        )

        raise

    is_recording = True

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

    print(
        "STOP RECORD FUNCTION"
    )

    logger.info(
        "Recording stop requested"
    )

    if not is_recording:

        return None

    try:

        if stream is not None:

            stream.stop()
            stream.close()

    except sd.PortAudioError as e:

        print(
            "Stream cleanup error:",
            e
        )

        logger.error(
            f"Stream cleanup error: {e}"
        )

    except OSError as e:

        print(
            "OS-level audio error:",
            e
        )

        logger.error(
            f"OS-level audio error: {e}"
        )

    finally:

        stream = None
        is_recording = False

    if len(recording) == 0:

        print(
            "No audio"
        )

        logger.warning(
            "Recording stopped but no audio was captured"
        )

        return None

    audio = np.concatenate(
        recording,
        axis=0
    )

    print(
        "Recording finished"
    )

    logger.info(
        f"Recording finished, samples={audio.shape}"
    )

    print(
        "Audio samples:",
        audio.shape
    )

    print(
        "Max volume:",
        np.max(
            np.abs(audio)
        )
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