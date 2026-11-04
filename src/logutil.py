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


def _telemetry_sink(message):
    """Upload the same log record as the file sink."""
    if not message.record['extra'].get('user_notice', False):
        from telemetry import submit
        submit(message.record)


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
    logger.add(_telemetry_sink, level="DEBUG")

    # Only application-owned standard loggers are bridged. Third-party loggers,
    # including mitmproxy's raw traffic, remain outside this pipeline.
    import logging
    class _ApplicationLogHandler(logging.Handler):
        def emit(self, record):
            try:
                level = record.levelname if record.levelname in (
                    'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL') else 'INFO'
                logger.bind(stdlib_logger=record.name).log(level, record.getMessage())
            except Exception:
                self.handleError(record)

    for _name in ('hotfixmgr', 'install_actions', 'native_ui.chrome', 'webview2_host'):
        _standard = logging.getLogger(_name)
        if not any(isinstance(h, _ApplicationLogHandler) for h in _standard.handlers):
            _standard.addHandler(_ApplicationLogHandler())
        _standard.setLevel(logging.DEBUG)
        _standard.propagate = False
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
    """Log call sites and useful diagnostic fields from known exception types."""
    import json
    import os
    import subprocess
    import traceback
    exc_type, error, tb = sys.exc_info()
    locations = [f"{os.path.basename(frame.filename)}:{frame.lineno}:{frame.name}"
                 for frame in traceback.extract_tb(tb)] if tb else []
    fields = [exc_type.__name__ if exc_type else 'unknown', f'stack={locations}']
    if isinstance(error, json.JSONDecodeError):
        # The decoder's message and position describe the parsing failure;
        # the original input document is deliberately not included.
        fields.extend((f'msg={error.msg}', f'line={error.lineno}', f'column={error.colno}'))
    elif isinstance(error, subprocess.CalledProcessError):
        # Do not record the command line, which may contain credentials.
        fields.append(f'returncode={error.returncode}')
    elif isinstance(error, subprocess.TimeoutExpired):
        fields.append(f'timeout={error.timeout}')
    elif isinstance(error, OSError):
        # Derive text from the OS error number, not from an arbitrary exception
        # message that may embed a URL, request, or private filesystem path.
        if type(error.errno) is int:
            fields.append(f'errno={error.errno}')
            try:
                fields.append(f'error={os.strerror(error.errno)}')
            except (ValueError, OverflowError):
                pass
        winerror = getattr(error, 'winerror', None)
        if type(winerror) is int:
            fields.append(f'winerror={winerror}')
    detail = ' [' + '; '.join(fields) + ']'
    target_logger.error(str(message) + detail, *args)
    # HTTP error response bodies are explicitly useful for troubleshooting.
    # Never include the exception text: it can contain a credential-bearing URL.
    try:
        import requests
        if isinstance(error, requests.HTTPError) and error.response is not None:
            target_logger.error(f"HTTP status={error.response.status_code} response={error.response.text}")
    except Exception:
        pass
