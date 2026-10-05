import threading
import time

import pyautogui

typing_lock = threading.Lock()


def type_text(text):

    if not text:
        return

    with typing_lock:

        print("START TYPING")

        pyautogui.keyUp("ctrl")
        pyautogui.keyUp("shift")
        pyautogui.keyUp("alt")

        time.sleep(0.1)

        pyautogui.write(text, interval=0.03)

        print("TEXT TYPED")
