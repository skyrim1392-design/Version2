"""Streaming capture foundation (Phase 7a).

Import-safe: no stream, thread, queue, or config access happens at
import time. Capture only starts when start() is called explicitly.

Pipeline (peer consumer of the F8 recorder, never routed through
start_recording/stop_recording):

    sd.InputStream @ hardware rate (audio.py device behavior, float32)
        -> bounded raw-block queue (drop-oldest)
        -> worker: 1-second hardware windows -> resample_poly -> 16 kHz
        -> int16 -> exact 16000-sample chunker (residual carry)
        -> bounded chunk queue (drop-oldest) -> consumer via read_chunk()

Contract at the abstraction boundary:
    - chunks are always int16, mono (ndim == 1), exactly 16000 samples
    - buffering is bounded; a stalled consumer drops OLDEST data
      (fresh audio has priority) with throttled overflow logging
    - stop(): stream closed, partial chunk DISCARDED (7a behavior;
      7b decides whether end-of-utterance partials reach Whisper),
      queues drained, worker joined, ownership released
    - microphone exclusivity via audio.claim_microphone("live"):
      refusal is deterministic, logged, and disturbs no other session

7a non-goals: no transcription, no output, no hotkey, no tray, no
config keys/writes, no app_state live states.
"""

import queue
import threading

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

import audio
import config
import logger

OUTPUT_SAMPLE_RATE = 16000
CHUNK_SAMPLES = 16000
RAW_BLOCK_QUEUE_SIZE = 64
CHUNK_QUEUE_SIZE = 8
DROP_LOG_EVERY = 100
WORKER_POLL_TIMEOUT = 0.1

_lock = threading.Lock()
_stream = None
_worker = None
_stop_event = None
_raw_blocks = None
_chunks = None
_active = False

_raw_dropped = 0
_chunk_dropped = 0


class ResampleChunker:
    """Accumulate hardware-rate float32 windows into exact output chunks.

    feed() returns every complete chunk immediately; a residual sample
    carry is preserved so no fractional resampling output is lost.
    Partial (sub-chunk) data is held, never emitted (7a stop discards it).
    """

    def __init__(self, input_rate, output_rate=OUTPUT_SAMPLE_RATE,
                 chunk_samples=CHUNK_SAMPLES):
        self.input_rate = int(input_rate)
        self.output_rate = int(output_rate)
        self.chunk_samples = int(chunk_samples)
        self._pending = []
        self._pending_frames = 0
        self._carry = np.empty(0, dtype=np.int16)

    def feed(self, frames):
        self._pending.append(np.asarray(frames, dtype=np.float32))
        self._pending_frames += len(self._pending[-1])

        chunks = []

        while self._pending_frames >= self.input_rate:
            window = np.concatenate(self._pending)
            take = self.input_rate
            head = window[:take]
            self._pending = [window[take:]] if len(window) > take else []
            self._pending_frames = len(window) - take

            resampled = resample_poly(
                head, self.output_rate, self.input_rate
            )
            block = (np.clip(resampled, -1.0, 1.0) * 32767).astype(np.int16)

            combined = np.concatenate([self._carry, block])
            while len(combined) >= self.chunk_samples:
                chunks.append(combined[:self.chunk_samples])
                combined = combined[self.chunk_samples:]

            self._carry = combined

        return chunks

    def discard_partial(self):
        """7a stop behavior: drop pending input and residual carry."""
        self._pending = []
        self._pending_frames = 0
        self._carry = np.empty(0, dtype=np.int16)


def _offer(queue_obj, item, which):
    """Atomically enqueue with drop-oldest overflow (bounded always).

    The queue's own put/get operations are the synchronization point:
    Full/Empty races are handled inline and never propagate.
    """
    global _raw_dropped, _chunk_dropped

    try:
        queue_obj.put_nowait(item)
        return
    except queue.Full:
        pass

    try:
        queue_obj.get_nowait()
    except queue.Empty:
        pass

    try:
        queue_obj.put_nowait(item)
    except queue.Full:
        pass

    if which == "raw":
        _raw_dropped += 1
        dropped = _raw_dropped
    else:
        _chunk_dropped += 1
        dropped = _chunk_dropped

    if dropped == 1 or dropped % DROP_LOG_EVERY == 0:
        logger.warning(
            f"Live capture queue overflow ({which}): dropped={dropped}"
        )


def _audio_callback(indata, frames, time_info, status):
    if status:
        logger.warning(f"Live audio callback warning: {status}")

    if _raw_blocks is not None:
        _offer(_raw_blocks, indata.copy(), "raw")


def _worker_loop(stop_event, chunker, raw_blocks, chunks):
    while True:
        if stop_event.is_set() and raw_blocks.empty():
            break

        try:
            block = raw_blocks.get(timeout=WORKER_POLL_TIMEOUT)
        except queue.Empty:
            continue

        if block.ndim > 1:
            block = block.mean(axis=1)

        for chunk in chunker.feed(block):
            _offer(chunks, chunk, "chunk")

    chunker.discard_partial()


def start():
    """Start live capture. Returns (ok, reason)."""
    global _stream, _worker, _stop_event, _raw_blocks, _chunks, _active
    global _raw_dropped, _chunk_dropped

    with _lock:
        if _active:
            return False, "already active"

        if not audio.claim_microphone("live"):
            logger.warning("Live capture refused: microphone busy (f8)")
            return False, "microphone busy (f8)"

        current_config = config.load_config()
        audio_config = current_config.get("audio", {})
        input_rate = int(audio_config.get("input_sample_rate", 44100))
        channels = int(audio_config.get("channels", 1))
        mic_name = current_config.get("microphone", "auto")

        _raw_blocks = queue.Queue(maxsize=RAW_BLOCK_QUEUE_SIZE)
        _chunks = queue.Queue(maxsize=CHUNK_QUEUE_SIZE)
        _raw_dropped = 0
        _chunk_dropped = 0

        try:
            microphone_index = audio.find_microphone(mic_name)
            _stream = sd.InputStream(
                samplerate=input_rate,
                channels=channels,
                dtype="float32",
                device=microphone_index,
                callback=_audio_callback,
            )
            _stream.start()

        except Exception as error:
            if _stream is not None:
                try:
                    _stream.close()
                except Exception:
                    pass
                _stream = None

            _raw_blocks = None
            _chunks = None
            audio.release_microphone("live")
            logger.error(
                f"Live capture start failed: {type(error).__name__}: {error}"
            )
            return False, f"start failed: {type(error).__name__}"

        _stop_event = threading.Event()
        chunker = ResampleChunker(input_rate)
        _worker = threading.Thread(
            target=_worker_loop,
            args=(_stop_event, chunker, _raw_blocks, _chunks),
            name="LiveCaptureWorker",
            daemon=True,
        )
        _worker.start()

        _active = True
        logger.info("Live capture started")
        return True, "started"


def stop():
    """Stop live capture (idempotent). Returns (ok, reason).

    Order: stream stop/close -> worker drains remaining raw blocks ->
    partial chunk discarded -> queues drained -> worker joined ->
    ownership released.
    """
    global _stream, _worker, _stop_event, _raw_blocks, _chunks, _active

    with _lock:
        if not _active:
            return True, "inactive"

        stream = _stream
        _stream = None

        if stream is not None:
            try:
                stream.stop()
            except Exception as error:
                logger.error(f"Live capture stream stop error: {error}")

            try:
                stream.close()
            except Exception as error:
                logger.error(f"Live capture stream close error: {error}")

        if _stop_event is not None:
            _stop_event.set()

        if _worker is not None:
            _worker.join(timeout=5.0)
            if _worker.is_alive():
                logger.error("Live capture worker did not stop in time")
            _worker = None

        if _chunks is not None:
            try:
                while True:
                    _chunks.get_nowait()
            except queue.Empty:
                pass

        _raw_blocks = None
        _chunks = None
        _stop_event = None
        _active = False

        audio.release_microphone("live")
        logger.info("Live capture stopped")
        return True, "stopped"


def is_active():
    return _active


def read_chunk(timeout=1.0):
    """Consumer API: next full 16000-sample int16 chunk, or None."""
    chunks = _chunks
    if chunks is None:
        return None

    try:
        return chunks.get(timeout=timeout)
    except queue.Empty:
        return None


def stats():
    return {
        "active": _active,
        "raw_queued": _raw_blocks.qsize() if _raw_blocks is not None else 0,
        "chunks_queued": _chunks.qsize() if _chunks is not None else 0,
        "raw_dropped": _raw_dropped,
        "chunks_dropped": _chunk_dropped,
    }
