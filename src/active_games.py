"""Games observed through MPay, without opening game processes or reading paths."""
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone


def _creation_time(value):
    """WMI's DMTF timestamp includes microseconds and its UTC offset in minutes."""
    value = str(value)
    if len(value) != 25 or value[21] not in '+-' or '*' in value:
        raise ValueError('Unknown process creation time')
    offset = int(value[22:]) * (1 if value[21] == '+' else -1)
    return datetime.strptime(value[:21], '%Y%m%d%H%M%S.%f').replace(
        tzinfo=timezone(timedelta(minutes=offset))).timestamp()


def process_creation_times(pids):
    """Query only PID and creation time through WMI, not process handles/names/paths."""
    if sys.platform != 'win32' or not pids:
        return {}
    import comtypes
    from comtypes.client import CreateObject
    comtypes.CoInitialize()
    try:
        locator = CreateObject('WbemScripting.SWbemLocator', dynamic=True)
        service = locator.ConnectServer('.', r'root\cimv2')
        where = ' OR '.join(f'ProcessId={int(pid)}' for pid in sorted(pids))
        rows = service.ExecQuery('SELECT ProcessId, CreationDate FROM Win32_Process WHERE ' + where)
        return {int(row.ProcessId): _creation_time(row.CreationDate) for row in rows}
    finally:
        comtypes.CoUninitialize()


class ActiveGames:
    def __init__(self, logger):
        self.logger = logger
        self._lock = threading.Lock()
        self._native = {}

    def note_native_request(self, game, process_id, observed_at=None):
        try:
            pid = int(process_id)
        except (TypeError, ValueError):
            return
        if not game or pid <= 0 or pid == os.getpid():
            return
        with self._lock:
            self._native[(game, pid)] = time.time() if observed_at is None else float(observed_at)

    def running_game_ids(self):
        with self._lock:
            observed = dict(self._native)
        if not observed:
            return set()
        try:
            created = process_creation_times({pid for _, pid in observed})
        except Exception as error:
            self.logger.debug('无法确认原生游戏是否仍在运行，本轮不刷新其凭据: {}', type(error).__name__)
            return set()
        running = set()
        with self._lock:
            for key, seen_at in observed.items():
                game, pid = key
                # Reject PID reuse after the request was observed. No WMI work
                # is needed on the proxy's request thread.
                if pid in created and created[pid] <= seen_at:
                    running.add(game)
                elif self._native.get(key) == seen_at:
                    self._native.pop(key, None)
        return running
