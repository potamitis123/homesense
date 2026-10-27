"""
Dedicated logging namespace for the mobile API.

Reuses the existing logging factory (misc.log_test.init_logger) so format
and the LOG env-var gate stay consistent with the rest of the app, but
points at its own log file (logs_mobile_api/logs_mobile_api.log) rather
than logs_ast_app/logs_ast_app.log -- mobile API activity must not be
mixed into the existing Sentinel logs.

Usage:
    from mobile_api.logging_setup import get_logger
    logger = get_logger()
    logger.info("message", extra={"module_name": "MOBILE_AUTH"})

Never pass passwords, tokens, MQTT credentials, or raw audio bytes to
any log call in this package.
"""

from __future__ import annotations

from misc.log_test import init_logger

_logger = None


def get_logger():
    global _logger
    if _logger is None:
        _logger = init_logger(
            log_dir="logs_mobile_api",
            log_name="logs_mobile_api",
            print_log=True,
        )
    return _logger


def log_extra(module_name: str) -> dict:
    """Shorthand for the extra={'module_name': ...} the shared formatter expects."""
    return {"module_name": module_name}
