"""Games observed through MPay, without opening game processes or reading paths."""
import ctypes
import os
import sys
import threading


def running_process_ids():
    """EnumProcesses returns IDs only; it does not open any process handle."""
    if sys.platform != 'win32':
        return set()
    enum = ctypes.WinDLL('psapi', use_last_error=True).EnumProcesses
    dword = ctypes.c_uint32
    enum.argtypes = [ctypes.POINTER(dword), dword, ctypes.POINTER(dword)]
    enum.restype = ctypes.c_int
    size = 1024
    while True:
        buffer = (dword * size)()
        needed = dword()
        if not enum(buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        if needed.value < ctypes.sizeof(buffer):
            return set(buffer[:needed.value // ctypes.sizeof(dword)])
        size *= 2


class ActiveGames:
    def __init__(self, logger):
        self.logger = logger
        self._lock = threading.Lock()
        self._native = {}

    def note_native_request(self, game, process_id):
        try:
            pid = int(process_id)
        except (TypeError, ValueError):
            return
        if not game or pid <= 0 or pid == os.getpid():
            return
        with self._lock:
            self._native[pid] = game

    def running_game_ids(self):
        with self._lock:
            observed = dict(self._native)
        if not observed:
            return set()
        try:
            alive = running_process_ids()
        except Exception as error:
            self.logger.debug('无法确认原生游戏是否仍在运行，本轮不刷新其凭据: {}', type(error).__name__)
            return set()
        running = set()
        with self._lock:
            for pid, game in observed.items():
                if pid in alive:
                    running.add(game)
                elif self._native.get(pid) == game:
                    self._native.pop(pid, None)
        return running
