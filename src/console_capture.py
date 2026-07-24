# coding=UTF-8
"""Tee process stdout/stderr into a bounded buffer for the diagnostics UI."""
from __future__ import annotations

from collections import deque
import re
import sys
import threading


_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class _ConsoleBuffer:
    def __init__(self, max_chars: int = 512 * 1024):
        self._max_chars = max_chars
        self._chunks = deque()
        self._cursor = 0
        self._size = 0
        self._lock = threading.Lock()

    def append(self, value) -> None:
        if value is None:
            return
        text = _ANSI_ESCAPE.sub("", str(value))
        if not text:
            return
        with self._lock:
            start = self._cursor
            self._chunks.append((start, text))
            self._cursor += len(text)
            self._size += len(text)
            while self._size > self._max_chars and self._chunks:
                chunk_start, chunk = self._chunks[0]
                excess = self._size - self._max_chars
                if excess >= len(chunk):
                    self._chunks.popleft()
                    self._size -= len(chunk)
                    continue
                self._chunks[0] = (chunk_start + excess, chunk[excess:])
                self._size -= excess

    def read(self, cursor: int = 0) -> dict:
        with self._lock:
            end = self._cursor
            earliest = self._chunks[0][0] if self._chunks else end
            reset = cursor < earliest or cursor > end
            if reset:
                cursor = earliest
            parts = []
            for start, chunk in self._chunks:
                chunk_end = start + len(chunk)
                if chunk_end <= cursor:
                    continue
                offset = max(0, cursor - start)
                parts.append(chunk[offset:])
            return {
                "output": "".join(parts),
                "cursor": end,
                "reset": reset,
            }


class _TeeStream:
    def __init__(self, original, buffer: _ConsoleBuffer):
        self._original = original
        self._buffer = buffer

    def write(self, value):
        self._buffer.append(value)
        if self._original is None:
            return len(value or "")
        return self._original.write(value)

    def flush(self):
        if self._original is not None:
            return self._original.flush()
        return None

    def __getattr__(self, name):
        if self._original is None:
            raise AttributeError(name)
        return getattr(self._original, name)


_buffer = _ConsoleBuffer()
_installed = False
_install_lock = threading.Lock()


def install_console_capture() -> None:
    """Install process-wide tee streams once, retaining normal console output."""
    global _installed
    with _install_lock:
        if _installed:
            return
        sys.stdout = _TeeStream(sys.stdout, _buffer)
        sys.stderr = _TeeStream(sys.stderr, _buffer)
        _installed = True


def read_console_output(cursor: int = 0) -> dict:
    try:
        normalized_cursor = max(0, int(cursor))
    except (TypeError, ValueError):
        normalized_cursor = 0
    return _buffer.read(normalized_cursor)
