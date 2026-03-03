from __future__ import annotations

import logging
from typing import Any

from .config import config


class DebugLogger:
    def __init__(self):
        self._logger = logging.getLogger("flow_captcha_service")
        if not self._logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                fmt="%(asctime)s | %(levelname)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            handler.setFormatter(formatter)
            self._logger.addHandler(handler)
        self._logger.setLevel(getattr(logging, config.log_level, logging.INFO))

    def log_info(self, message: str, *args: Any):
        self._logger.info(message, *args)

    def log_warning(self, message: str, *args: Any):
        self._logger.warning(message, *args)

    def log_error(self, message: str, *args: Any):
        self._logger.error(message, *args)

    def log_debug(self, message: str, *args: Any):
        self._logger.debug(message, *args)


debug_logger = DebugLogger()
