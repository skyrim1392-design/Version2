import os
import subprocess
import time

import logger
from config import load_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

WHISPER_PATH = os.path.join(
    BASE_DIR,
    "whisper",
    "Vulkan",
    "whisper-cli.exe",
)

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
        raise RuntimeError(f"WAV file does not exist: {wav_path}")

    if os.path.getsize(wav_path) < 1000:
        logger.error(f"WAV too small: {wav_path}")
        raise RuntimeError("WAV file is too small")

    if not os.path.isfile(WHISPER_PATH):
        logger.error(f"Whisper executable missing: {WHISPER_PATH}")
        raise RuntimeError(f"Whisper executable not found: {WHISPER_PATH}")

    print("🧠 Whisper processing...")
    cancel_requested = False

    config = load_config()
    model_name = config["model"]
    model_path = os.path.join(BASE_DIR, "models", model_name)

    if not os.path.isfile(model_path):
        logger.error(f"Whisper model missing: {model_path}")
        raise RuntimeError(f"Whisper model not found: {model_path}")

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

    logger.info(f"Whisper command started: {cmd[0]}")
    print("WHISPER COMMAND:")
    print(cmd)

    process = None
    try:
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
        except FileNotFoundError as error:
            logger.error(f"Whisper executable could not be started: {error}")
            raise RuntimeError(
                f"Whisper executable could not be started: {WHISPER_PATH}"
            ) from error

        whisper_process = process

        while True:
            if cancel_requested:
                logger.info("Whisper cancellation requested")
                try:
                    process.kill()
                except OSError:
                    pass
                process.communicate()
                logger.info("Whisper process cancelled")
                return ""

            if process.poll() is not None:
                break

            time.sleep(0.1)

        stdout, stderr = process.communicate()
        return_code = process.returncode

        print("WHISPER STDOUT:")
        print(stdout)
        print("WHISPER STDERR:")
        print(stderr)

        if stderr.strip():
            logger.info(f"Whisper stderr: {stderr.strip()}")

        if return_code != 0:
            error_message = f"Whisper process failed with exit code {return_code}"
            if stderr.strip():
                error_message += f": {stderr.strip()}"
            logger.error(error_message)
            raise RuntimeError(error_message)

        text = stdout.strip()
        logger.info(f"Whisper transcription completed, characters={len(text)}")
        print("----------------")
        print(text)
        print("----------------")
        return text

    finally:
        if whisper_process is process:
            whisper_process = None
