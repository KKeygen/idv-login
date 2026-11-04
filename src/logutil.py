# coding=UTF-8
"""
 Copyright (c) 2025 KKeygen & fwilliamhe

 This program is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 This program is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with this program. If not, see <https://www.gnu.org/licenses/>.
 """

from loguru import logger
import sys


def _get_console_sink():
    stream = sys.stdout
    if sys.platform != "win32":
        return stream
    try:
        # Loguru only auto-wraps the original stdio objects on Windows.  Our
        # diagnostics tee is a proxy object, so wrap it explicitly to retain
        # colors without leaking raw ANSI escape sequences to the console.
        from colorama import AnsiToWin32
        return AnsiToWin32(stream, autoreset=False).stream
    except Exception:
        return stream


try:
    logger.remove(0)
    from prefetch_context import in_prefetch
    logger.add(_get_console_sink(), level="INFO", filter=lambda record: not in_prefetch())
    logger.add("log.txt", rotation="10MB", encoding="utf-8", diagnose=False,
               filter=lambda record: not record["extra"].get("user_notice", False))
except Exception as e:
    print(e)
    from logging import getLogger
    logger=getLogger("log")

def setup_logger():
    return logger


def user_notice(message, level="INFO"):
    """Show an instruction on the console without appending it to log.txt."""
    if hasattr(logger, "bind"):
        logger.bind(user_notice=True).log(level, message)
    else:
        print(message)


def safe_exception(target_logger, message, *args):
    """Log the exception type and call sites without its potentially secret text."""
    import os
    import traceback
    exc_type, error, tb = sys.exc_info()
    locations = [f"{os.path.basename(frame.filename)}:{frame.lineno}:{frame.name}"
                 for frame in traceback.extract_tb(tb)] if tb else []
    detail = f" [{exc_type.__name__ if exc_type else 'unknown'}; stack={locations}]"
    target_logger.error(str(message) + detail, *args)
    # HTTP error response bodies are explicitly useful for troubleshooting.
    # Never include the exception text: it can contain a credential-bearing URL.
    try:
        import requests
        if isinstance(error, requests.HTTPError) and error.response is not None:
            target_logger.error(f"HTTP status={error.response.status_code} response={error.response.text}")
    except Exception:
        pass
