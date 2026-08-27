import os
import subprocess
import time

import logger
from config import load_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

WHISPER_PATH = os.path.join(BASE_DIR, "whisper", "Vulkan", "whisper-cli.exe")


whisper_process = None
cancel_requested = False


def get_whisper_language(language_mode):

    mode = language_mode.upper()

    if mode == "AUTO":
        return None

    elif mode == "ENGLISH":
        return "en"

    elif mode == "PERSIAN":
        return "fa"

    else:
        return None


def whisper_transcribe(wav_path):

    global whisper_process
    global cancel_requested
    logger.info("Whisper transcription started")

    if not os.path.exists(wav_path):
        logger.error(f"WAV missing: {wav_path}")
        return ""

    if os.path.getsize(wav_path) < 1000:
        logger.error(f"WAV too small: {wav_path}")
        return ""

    print("🧠 Whisper processing...")

    cancel_requested = False

    # Load current configuration
    config = load_config()

    model_name = config["model"]

    model_path = os.path.join(BASE_DIR, "models", model_name)

    language_mode = config["language_mode"].upper()

    task_mode = config["task_mode"].upper()

    threads = str(config.get("threads", 8))

    gpu_device = str(config.get("gpu_device", 0))
    logger.info(
        f"Whisper config: model={model_name}, "
        f"language={language_mode}, "
        f"task={task_mode}, "
        f"threads={threads}, "
        f"gpu={gpu_device}"
    )

    cmd = [
        WHISPER_PATH,
        "-m",
        model_path,
        "-f",
        wav_path,
        "-t",
        threads,
        "-dev",
        gpu_device,
        "--no-timestamps",
        "--temperature",
        "0",
    ]

    language = get_whisper_language(language_mode)

    if language:

        cmd.extend(["-l", language])

    if task_mode == "TRANSLATE":

        cmd.append("--translate")

    print("WHISPER COMMAND:")
    print(cmd)
    logger.info(f"Whisper command started: {cmd[0]}")

    whisper_process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8"
    )

    # Wait while allowing ESC cancellation
    while True:

        if cancel_requested:

            print("Stopping Whisper process...")

            whisper_process.kill()

            whisper_process = None

            return ""

        if whisper_process.poll() is not None:
            break

        time.sleep(0.1)

    stdout, stderr = whisper_process.communicate()

    whisper_process = None

    print("WHISPER STDOUT:")
    print(stdout)

    print("WHISPER STDERR:")
    print(stderr)

    if stderr.strip():

        logger.error(f"Whisper stderr: {stderr.strip()}")
    text = stdout.strip()

    print("----------------")
    print(text)
    print("----------------")
    logger.info(f"Whisper transcription completed, characters={len(text)}")

    return text
