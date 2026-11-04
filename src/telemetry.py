"""Opt-in, asynchronous upload of the same application records written to log.txt."""
import atexit
import json
import queue
import re
import ssl
import sys
import threading
import time
import urllib.request
import urllib.error
import uuid

from envmgr import genv

SETTING = 'debug_mode_enabled'
ACTIVE = 'DEBUG_MODE_ACTIVE'
# Public ingest identifier; distributed with the application.
PROJECT_ID = 1
PUBLIC_KEY = '14f60041-8648-4182-9660-eb3c744604d5'

_VERSION = re.compile(r'v?\d+\.\d+(?:\.\d+){0,2}(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?\Z')


def release_version(value):
    return isinstance(value, str) and bool(_VERSION.fullmatch(value))


def active():
    return genv.get(ACTIVE, False) is True


def state(enabled=None):
    enabled = active() if enabled is None else enabled
    supported = release_version(genv.get('VERSION', ''))
    return {'success': True, 'enabled': enabled, 'upload_supported': supported,
            'upload_enabled': enabled and supported}


def configure(cli_args):
    enabled = bool(getattr(cli_args, 'debug', False)) or genv.get(SETTING, False) is True
    cli_args.debug = enabled
    genv.set(ACTIVE, enabled)
    return enabled


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _send(payload):
    # Do not route through the app-managed or inherited proxy.
    request = urllib.request.Request(
        f'https://idv-telemetry.keygen.eu.org/api/{PROJECT_ID}/envelope/', data=payload,
        headers={'Content-Type': 'application/x-sentry-envelope',
                 'X-Sentry-Auth': f'Sentry sentry_version=7, sentry_key={PUBLIC_KEY}',
                 'User-Agent': 'idv-login-telemetry/1'}, method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                         urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(request, timeout=3) as response:
            response.read(1)
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except Exception:
        return None


def _envelope(batch, version, session):
    levels = {'TRACE': ('trace', 1), 'DEBUG': ('debug', 5), 'INFO': ('info', 9),
              'SUCCESS': ('info', 9), 'WARNING': ('warn', 13), 'ERROR': ('error', 17),
              'CRITICAL': ('fatal', 21)}
    items = []
    for message, severity, timestamp, source, line in batch:
        level, number = levels.get(severity, ('info', 9))
        items.append({'trace_id': uuid.uuid4().hex, 'timestamp': timestamp,
                      'level': level, 'severity_number': number, 'body': message,
                      'attributes': {
                          'sentry.release': {'value': version, 'type': 'string'},
                          'idv.session_id': {'value': session, 'type': 'string'},
                          'idv.source': {'value': source, 'type': 'string'},
                          'idv.line': {'value': line, 'type': 'integer'},
                          'idv.platform': {'value': sys.platform, 'type': 'string'}}})
    parts = ({}, {'type': 'log', 'item_count': len(items),
                  'content_type': 'application/vnd.sentry.items.log+json'}, {'items': items})
    return ('\n'.join(json.dumps(p, separators=(',', ':'), ensure_ascii=True) for p in parts) + '\n').encode('ascii')


class Telemetry:
    def __init__(self, version, transport=_send):
        self.version = version
        self.session = uuid.uuid4().hex
        self.transport = transport
        self.queue = queue.Queue(maxsize=128)
        self.stop_event = threading.Event()
        self.discard = False
        self.batch_window = 10.0
        self.send_interval = 1.0
        self.rate_limit_backoff = 30.0
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._worker, name='telemetry', daemon=True)
        self.thread.start()

    def submit(self, record):
        with self.lock:
            if self.stop_event.is_set():
                return
            try:
                self.queue.put_nowait((record['message'], record['level'].name,
                                       record['time'].timestamp(), record['name'] or '', record['line']))
            except queue.Full:
                pass

    def _worker(self):
        next_send = 0.0
        while not self.discard:
            if self.stop_event.is_set() and self.queue.empty():
                return
            try:
                item = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            batch = [item]
            deadline = time.monotonic() + self.batch_window
            while len(batch) < 32 and not self.discard:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or (self.stop_event.is_set() and self.queue.empty()):
                    break
                try:
                    batch.append(self.queue.get(timeout=min(0.2, remaining)))
                except queue.Empty:
                    pass
            while not self.discard and time.monotonic() < next_send:
                time.sleep(min(0.2, max(0, next_send - time.monotonic())))
            if not self.discard:
                try:
                    status = self.transport(_envelope(batch, self.version, self.session))
                    delay = self.rate_limit_backoff if status == 429 else self.send_interval
                    next_send = time.monotonic() + delay
                except Exception:
                    next_send = time.monotonic() + self.send_interval

    def close(self, flush=False):
        # User disable discards queued messages. Normal exit waits a bounded
        # time for a best-effort flush; an in-flight request may still finish.
        with self.lock:
            self.discard = not flush
            self.stop_event.set()
            if not flush:
                while True:
                    try:
                        self.queue.get_nowait()
                    except queue.Empty:
                        break
        if flush:
            self.thread.join(timeout=4)


_current = None
_lock = threading.RLock()


def start_telemetry(enabled, version):
    global _current
    with _lock:
        if _current is not None:
            _current.close()
        _current = Telemetry(version) if enabled and release_version(version) else None
        return _current is not None


def stop_telemetry(flush=False):
    global _current
    with _lock:
        if _current is not None:
            _current.close(flush=flush)
            _current = None


def submit(record):
    with _lock:
        if _current is not None:
            _current.submit(record)


# Read settings before the program alters the environment or proxy.
import os
from urllib.parse import urlsplit

_ENV_NAMES = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')
_UNKNOWN = 'unknown'
_READ_ERROR = object()


def _tool_proxy(value, port):
    """Match the tool's own loopback endpoint exactly, not by substring."""
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        value = value.strip()
        parsed = urlsplit(value if '://' in value else '//' + value)
        return (parsed.scheme in ('', 'http') and parsed.hostname in ('127.0.0.1', 'localhost')
                and parsed.port == port and parsed.username is None
                and parsed.password is None and parsed.path in ('', '/')
                and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError):
        return False


def _env_state(values, port):
    unknown = False
    for value in values:
        if not isinstance(value, str):
            unknown = True
            continue
        if value.strip() and not _tool_proxy(value, port):
            return True
    return _UNKNOWN if unknown else False


def _query(key, name, registry):
    try:
        return registry.QueryValueEx(key, name)[0]
    except FileNotFoundError:
        return None
    except OSError:
        return _READ_ERROR


def _registry_values(registry, subkey, names):
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, subkey) as key:
            return [_query(key, name, registry) for name in names]
    except FileNotFoundError:
        return [None] * len(names)
    except OSError:
        return [_READ_ERROR] * len(names)


def collect_proxy_state(proxy_port, *, platform=None, environ=None, registry=None):
    """Return booleans or ``unknown``; never return raw proxy configuration.

    Call before the application changes proxy settings. ``external_env_proxy``
    covers the process environment and, on Windows, the user's persisted
    environment. ``windows_system_proxy`` covers WinINET enabled/PAC state.
    """
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    try:
        if type(proxy_port) is not int or not 1 <= proxy_port <= 65535:
            raise ValueError('invalid local proxy port')
        process_values = [value for name, value in environ.items()
                          if name.upper() in _ENV_NAMES]
        env_state = _env_state(process_values, proxy_port)
    except (AttributeError, TypeError, ValueError, OSError):
        env_state = _UNKNOWN

    result = {}
    if platform == 'win32':
        try:
            if registry is None:
                import winreg as registry
            persisted = _registry_values(registry, r'Environment', _ENV_NAMES)
            known_values = [v for v in persisted if v is not None and v is not _READ_ERROR]
            persisted_state = _env_state(known_values, proxy_port)
            if env_state is True or persisted_state is True:
                env_state = True
            elif env_state == _UNKNOWN or persisted_state == _UNKNOWN or _READ_ERROR in persisted:
                env_state = _UNKNOWN
            else:
                env_state = False

            enabled, pac, autodetect = _registry_values(
                registry, r'Software\Microsoft\Windows\CurrentVersion\Internet Settings',
                ('ProxyEnable', 'AutoConfigURL', 'AutoDetect'))
            valid_enable = enabled is None or type(enabled) is int and enabled in (0, 1)
            valid_pac = pac is None or isinstance(pac, str)
            valid_auto = autodetect is None or type(autodetect) is int and autodetect in (0, 1)
            if enabled == 1 or autodetect == 1 or isinstance(pac, str) and bool(pac.strip()):
                result['windows_system_proxy'] = True
            elif valid_enable and valid_pac and valid_auto:
                result['windows_system_proxy'] = False
            else:
                result['windows_system_proxy'] = _UNKNOWN
        except (ImportError, OSError, AttributeError, TypeError, ValueError):
            result['windows_system_proxy'] = _UNKNOWN
            if env_state is not True:
                env_state = _UNKNOWN
    result['external_env_proxy'] = env_state
    return result


atexit.register(stop_telemetry, flush=True)
