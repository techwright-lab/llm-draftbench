"""Refuse live dispatch while SDK or HTTP debug logging could print prompts."""

import logging
import os

_ENV = ("OPENAI_LOG", "ANTHROPIC_LOG")
_LOGGERS = ("openai", "anthropic", "httpx", "httpcore")


class SDKLoggingError(ValueError):
    def __init__(self):
        super().__init__("sdk_debug_logging_forbidden")


def refuse_sdk_debug_logging():
    # Both SDKs configure logger levels from these variables at import time, so
    # clearing the environment later cannot undo it; check both sources.
    if any(os.environ.get(name) for name in _ENV) or any(
        logging.getLogger(name).isEnabledFor(logging.DEBUG) for name in _LOGGERS
    ):
        raise SDKLoggingError()
