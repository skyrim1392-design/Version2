import threading
import time

import pyautogui

import logger

typing_lock = threading.Lock()


def type_text(text):

    if not text:
        return

    with typing_lock:

        logger.info("Start typing")

        pyautogui.keyUp("ctrl")
        pyautogui.keyUp("shift")
        pyautogui.keyUp("alt")

        time.sleep(0.1)

        pyautogui.write(text, interval=0.03)

        logger.info("Text typed")
