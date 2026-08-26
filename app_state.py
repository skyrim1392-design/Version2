# app_state.py

import threading


class ApplicationState:

    def __init__(self):

        self.recording = False
        self.processing = False

        self.microphone_connected = False
        self.microphone_level = 0.0
        self.last_audio_samples = 0

        self.error = None

        self.lock = threading.Lock()


state = ApplicationState()