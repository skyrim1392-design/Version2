import os
import subprocess
import threading
import time

import logger
from config import load_config
from hardware import select_backend

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

whisper_process = None

# M1 (7c.3): held for the entire whisper-cli invocation lifetime, so
# every caller (live transcription, F8) is serialized with no
# caller-side ordering assumptions.
_invoke_lock = threading.Lock()
INVOKE_POLL_INTERVAL = 0.05

# Shutdown-path bound: how long terminate_outstanding() waits for the
# child to be reaped after kill().
TERMINATE_TIMEOUT = 2.0


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


def whisper_transcribe(wav_path, cancel_event=None):
    """Run one whisper-cli invocation for one caller.

    _invoke_lock is held for the entire invocation lifetime, so
    concurrent callers are serialized automatically. Cancellation is
    owned per call, never global:

    - while waiting for the lock, a set cancel_event returns "" with
      the "cancelled before acquisition" log and never spawns;
      cancel_event=None waits normally until the lock is granted
    - inside the invocation only the cancel_event this call was given
      is polled, so cancelling one invocation never affects another
      (None never cancels - the F8 path)
    - a cancel_event set between a successful acquisition and the
      spawn is observed by the in-invocation poll right after spawn
    """
    logger.info("Whisper transcription started")

    if cancel_event is None:
        _invoke_lock.acquire()
    else:
        while True:
            if cancel_event.is_set():
                logger.info(
                    "Whisper invocation cancelled before acquisition"
                )
                return ""
            if _invoke_lock.acquire(timeout=INVOKE_POLL_INTERVAL):
                break

    try:
        return _invoke_whisper(wav_path, cancel_event)
    finally:
        _invoke_lock.release()


def _invoke_whisper(wav_path, cancel_event):
    """Validate, spawn, and reap one whisper-cli process (lock held).

    The process poll loop observes only the cancel_event this call was
    given (None never cancels). whisper_process is kept for
    diagnostics and cleared in finally, exactly as before.
    """
    global whisper_process

    if not os.path.exists(wav_path):
        logger.error(f"WAV missing: {wav_path}")
        raise RuntimeError(f"WAV file does not exist: {wav_path}")

    if os.path.getsize(wav_path) < 1000:
        logger.error(f"WAV too small: {wav_path}")
        raise RuntimeError("WAV file is too small")

    backend_spec = select_backend()

    if not os.path.isfile(backend_spec["executable"]):
        logger.error(f"Whisper executable missing: {backend_spec['executable']}")
        raise RuntimeError(
            f"Whisper executable not found: {backend_spec['executable']}"
        )

    config = load_config()
    model_name = config["model"]
    model_path = os.path.join(BASE_DIR, "models", model_name)

    if not os.path.isfile(model_path):
        logger.error(f"Whisper model missing: {model_path}")
        raise RuntimeError(f"Whisper model not found: {model_path}")

    language_mode = config["language_mode"].upper()
    task_mode = config["task_mode"].upper()

    threads = str(config.get("threads", 8))

    logger.info(
        f"Whisper config: model={model_name}, "
        f"language={language_mode}, "
        f"task={task_mode}, "
        f"threads={threads}, "
        f"backend={backend_spec['backend']}, "
        f"gpu={backend_spec['device']}"
    )

    cmd = [
        backend_spec["executable"],
        "-m",
        model_path,
        "-f",
        wav_path,
        "-t",
        threads,
        "--no-timestamps",
        "--temperature",
        "0",
    ]

    cmd.extend(backend_spec["device_args"])

    language = get_whisper_language(language_mode)
    if language:
        cmd.extend(["-l", language])

    if task_mode == "TRANSLATE":
        cmd.append("--translate")

    logger.info(f"Whisper command started: {cmd[0]}")
    logger.info(f"Whisper command: {' '.join(cmd)}")

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
                f"Whisper executable could not be started: "
                f"{backend_spec['executable']}"
            ) from error

        whisper_process = process

        cancelled = False

        while True:
            if cancel_event is not None and cancel_event.is_set():
                logger.info("Whisper cancellation requested")
                try:
                    process.kill()
                except OSError:
                    pass
                process.communicate()
                logger.info("Whisper process cancelled")
                cancelled = True
                break

            if process.poll() is not None:
                break

            time.sleep(0.1)

        if cancelled:
            return ""

        stdout, stderr = process.communicate()
        return_code = process.returncode

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
        return text
    finally:
        if whisper_process is process:
            whisper_process = None


def terminate_outstanding():
    """Shutdown-path helper: terminate and reap an outstanding child.

    Called only from live_supervisor.shutdown() when the F8 quiesce
    window expired, i.e. when the application is exiting while an
    invocation may still be in flight. The F8 path passes
    cancel_event=None and owns no cancellation of its own, so without
    this call that child would outlive the process.

    Deliberately does not touch _invoke_lock: it has to interrupt the
    invocation currently holding it, and the normal-path serialization
    of whisper_transcribe (M1) is left untouched.

    Kill plus wait only. The invocation owner keeps its own handle and
    finishes through its normal poll/communicate loop, so this helper
    never reads a pipe another thread may be reading. The module handle
    is cleared once the child is confirmed reaped. Returns True when a
    child handle was found.
    """
    global whisper_process

    process = whisper_process

    if process is None:
        return False

    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            logger.warning("Whisper child could not be terminated")

    try:
        process.wait(timeout=TERMINATE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as error:
        logger.warning(f"Whisper child not confirmed reaped: {error}")
    else:
        if whisper_process is process:
            whisper_process = None

    return True
