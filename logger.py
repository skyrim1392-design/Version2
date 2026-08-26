import logging
import os
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LOG_DIR = os.path.join(
    BASE_DIR,
    "logs"
)

os.makedirs(LOG_DIR, exist_ok=True)


LOG_FILE = os.path.join(
    LOG_DIR,
    "assistant.log"
)


app_logger = logging.getLogger(
    "========== Voice Assistant Started =========="
)

app_logger.setLevel(logging.INFO)


if not app_logger.handlers:

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,   # 5 MB
        backupCount=3,
        encoding="utf-8"
    )

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    file_handler.setFormatter(formatter)

    app_logger.addHandler(file_handler)


def info(message):

    app_logger.info(message)


def error(message):

    app_logger.error(message)