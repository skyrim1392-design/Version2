import time

import pyautogui
import pyperclip


def type_text(text):

    print("TYPE FUNCTION RECEIVED:")
    print(repr(text))


    if not text:

        print("NO TEXT TO TYPE")

        return


    pyperclip.copy(text)

    time.sleep(0.1)

    pyautogui.hotkey(
        "ctrl",
        "v"
    )


    print("TEXT PASTED")