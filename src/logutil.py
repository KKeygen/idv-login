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
    logger.add(_get_console_sink(), level="INFO")
    logger.add("log.txt",rotation="10MB", encoding="utf-8", diagnose=True)
except Exception as e:
    print(e)
    from logging import getLogger
    logger=getLogger("log")

def setup_logger():
    return logger
