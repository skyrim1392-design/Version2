import threading


class AppState:

    def __init__(self):

        self.lock = threading.Lock()

        self.status = "IDLE"

        self.recording = False

        self.processing = False

        self.microphone_connected = False

        self.microphone_available = False

        self.microphone_level = 0.0

        self.microphone_error = None

        self.last_microphone_check = 0.0

        self.last_audio_samples = 0

        self.error = None


state = AppState()