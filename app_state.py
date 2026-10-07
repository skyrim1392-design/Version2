import threading


class AppState:
    def __init__(self):

        self.lock = threading.Lock()

        self.status = "IDLE"

        self.live_status = "LIVE_IDLE"

        self.recording = False

        self.processing = False

        self.microphone_connected = False

        self.microphone_available = None
        self.microphone_level = 0.0

        self.microphone_error = None

        self.last_microphone_check = 0.0

        self.last_audio_samples = 0

        self.audio_callback_count = 0

        self.audio_callback_errors = 0

        self.audio_frames_received = 0

        self.audio_zero_sample_ratio = 0.0

        self.microphone_diagnostic = "NOT_TESTED"

        self.error = None


state = AppState()
