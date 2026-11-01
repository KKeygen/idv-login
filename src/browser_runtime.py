from __future__ import annotations
import asyncio
import contextlib
from dataclasses import dataclass, field
import json
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid
from browser_cookies import CookieExportError, cookie_parameters
from browser_profile import BrowserProfileError, ProfileLease

class BrowserUnavailable(RuntimeError):
    pass

class _CommandError(RuntimeError):
    pass

class BrowserRuntime:
    _instance = None
    _lock = threading.Lock()
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.sessions = set()
        self.stopping = False
        self.thread = threading.Thread(target=self.loop.run_forever, name='IDV WebView2 sessions', daemon=True)
        self.thread.start()
    @classmethod
    def instance(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance
    def submit(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)
    @classmethod
    def shutdown(cls):
        runtime = cls._instance
        if runtime is None:
            return
        runtime.stopping = True
        async def stop():
            await asyncio.gather(*(s._finish() for s in tuple(runtime.sessions)), return_exceptions=True)
        result = runtime.submit(stop())
        from PyQt6.QtCore import QCoreApplication, QEventLoop, QThread
        app = QCoreApplication.instance()
        deadline = time.monotonic() + 35
        if app is not None and QThread.currentThread() == app.thread():
            while (not result.done() or runtime.sessions) and time.monotonic() < deadline:
                app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
                time.sleep(.005)
        else:
            with contextlib.suppress(Exception):
                result.result(timeout=35)
        if not result.done() or runtime.sessions:
            raise BrowserUnavailable('WebView2 尚未确认释放独立登录资料。')
        async def drain():
            pending = tuple(t for t in asyncio.all_tasks() if t is not asyncio.current_task())
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await runtime.loop.shutdown_asyncgens()
        runtime.submit(drain()).result(timeout=2)
        runtime.loop.call_soon_threadsafe(runtime.loop.stop)
        runtime.thread.join(timeout=2)
        cls._instance = None

@dataclass
class _Page:
    page_id: str
    target: int
    sid: int
    frame: str = ''
    url: str = 'about:blank'
    context: str | None = None
    ready: bool = False
    epoch: int = 0
    navigation: int = 0
    configured: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

class BrowserSession:
    def __init__(self, *, dispatch, on_event, parent_hwnd, profile_path, persistent=True,
                 user_agent='', scripts=(), callback_schemes=(), cookie_export=None):
        self.runtime = BrowserRuntime.instance()
        self.dispatch, self.on_event = dispatch, on_event
        self.parent_hwnd, self.profile_path, self.persistent = int(parent_hwnd), profile_path, bool(persistent)
        self.user_agent, self.scripts = user_agent, tuple(scripts)
        self.callback_schemes = frozenset(str(s).lower() for s in callback_schemes)
        self.cookie_export = cookie_export
        self.pages, self._native_pages, self._tasks = {}, {}, set()
        self._requested_pages = {}
        self._host = self._lease = None
        self._finished = self._finishing = self._started = self._close_requested = self._start_called = False
        self._starting = True
        self._startup_task = None
        self._preparing_runtime = False
        self._startup_complete = asyncio.Event()
        self._ready = None
        self._host_closed = False
        self._host_serial = 0
        self._late_release = False
        self._done = asyncio.Event()
        self._initial_url = None
        self._exports, self._export_docs = {}, {}
        self._cookie_snapshot = []
        self._committed_token = self._finishing_token = None
        self._clear_epoch, self._clear_pending = 0, False
        self._clear_future = None
        self._callback_seen = set()

    @property
    def is_closing(self):
        return self._close_requested or self._finishing or self._finished

    @property
    def is_resetting(self):
        return self._clear_pending

    def emit(self, event, *args):
        epoch, serial = self._clear_epoch, self._host_serial
        guarded = event in ('cookies_snapshot', 'cookie_boundary', 'cookies_committed', 'url', 'loaded', 'console', 'page')
        def deliver():
            if guarded and (self._clear_pending or epoch != self._clear_epoch or serial != self._host_serial):
                return
            self.on_event(event, *args)
        self.dispatch(deliver)

    def _task(self, coroutine):
        serial = self._host_serial
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        def done(value):
            self._tasks.discard(value)
            if not value.cancelled() and value.exception() and not self.is_closing and not self._clear_pending and serial == self._host_serial:
                self.emit('error', '登录页面通信失败，本次登录已取消，请重试。')
                self._task(self._finish())
        task.add_done_callback(done)
        return task

    async def _gui(self, function, *, callback=False, timeout=15):
        loop = asyncio.get_running_loop()
        result = loop.create_future()
        def complete(value=None, error=None):
            def settle():
                if result.done():
                    return
                if error is not None:
                    result.set_exception(error)
                else:
                    result.set_result(value)
            loop.call_soon_threadsafe(settle)
        def invoke():
            try:
                if callback:
                    function(complete)
                else:
                    complete(function())
            except Exception as error:
                complete(None, error)
        self.dispatch(invoke)
        return await asyncio.wait_for(result, timeout)

    async def command(self, method, params=None, sid=None, timeout=8):
        if self._host is None or self._host_closed:
            raise BrowserUnavailable('登录页面未连接。')
        host, serial = self._host, self._host_serial
        try:
            value = await self._gui(lambda cb: host.call_devtools(sid or 1, method, params or {}, cb), callback=True, timeout=timeout)
            if serial != self._host_serial:
                raise BrowserUnavailable('登录页面实例已更换。')
            return value
        except Exception:
            raise _CommandError('WebView2 document command failed') from None

    def _receive(self, event, data, serial):
        if not self.runtime.loop.is_running():
            return
        def enqueue():
            if serial == self._host_serial:
                self._task(self._event(event, data, serial))
        self.runtime.loop.call_soon_threadsafe(enqueue)

    def _navigation_policy(self, page_id, url, is_frame):
        try:
            parsed = urlsplit(url)
            return (not self.is_closing and
                    (url in ('about:blank', 'about:srcdoc') or
                     (parsed.scheme in ('http', 'https') and bool(parsed.hostname)
                      and parsed.username is None and parsed.password is None)))
        except (ValueError, TypeError):
            return False

    def _page(self, native_id, alias=None):
        native_id = int(native_id)
        page = self._native_pages.get(native_id)
        if page is None:
            alias = alias or self._requested_pages.pop(native_id, None) or ('main' if native_id == 1 else uuid.uuid4().hex)
            page = _Page(alias, native_id, native_id)
            self.pages[alias] = self._native_pages[native_id] = page
        return page

    async def _configure(self, page):
        host, serial = self._host, self._host_serial
        def current():
            if serial != self._host_serial or self._native_pages.get(page.target) is not page:
                raise BrowserUnavailable('登录页面实例已更换。')
        for event in ('Runtime.executionContextCreated', 'Runtime.executionContextDestroyed', 'Runtime.executionContextsCleared'):
            current()
            await self._gui(lambda name=event: host.subscribe_devtools(page.target, name))
            current()
        tree = await self.command('Page.getFrameTree', sid=page.sid)
        current()
        page.frame = tree['frameTree']['frame']['id']
        await self.command('Runtime.enable', sid=page.sid)
        current()
        page.configured.set()

    def start(self, url):
        if self._start_called:
            raise RuntimeError('Browser session already started')
        self._start_called = True
        return self.runtime.submit(self._start(url))

    async def _start(self, url):
        if self.is_closing or self.runtime.stopping:
            return
        self._startup_task = asyncio.current_task()
        self._initial_url = str(url)
        self.runtime.sessions.add(self)
        failed = False
        try:
            if sys.platform != 'win32':
                raise BrowserUnavailable('系统 WebView2 登录仅支持 Windows。')
            from webview2_host import runtime_status, runtime_version
            from webview2_setup import ensure_runtime, needs_install
            status = await self._gui(runtime_status)
            if needs_install(status):
                self._preparing_runtime = True
                try:
                    await asyncio.to_thread(ensure_runtime,
                        lambda message: self.emit('preparing_runtime', message))
                finally:
                    self._preparing_runtime = False
                if self.is_closing or self.runtime.stopping:
                    return
            await self._gui(runtime_version)
            if self.is_closing or self.runtime.stopping:
                return
            self._lease = ProfileLease(self.profile_path, self.persistent)
            path = await asyncio.to_thread(self._lease.prepare)
            if self.is_closing:
                return
            await self._open_host(path)
            if self.is_closing:
                return
            if self._clear_pending or self._lease.needs_cookie_clear:
                await asyncio.to_thread(self._lease.mark_cookies_cleared)
                await self._gui(lambda cb: self._host.clear_data(cb), callback=True, timeout=30)
                await asyncio.to_thread(self._lease.confirm_cookies_cleared)
                self._clear_pending = False
                self.emit('cookies_reset')
            else:
                saved = await asyncio.to_thread(self._lease.load_cookies)
                if saved:
                    await self.command('Storage.setCookies', {'cookies': cookie_parameters(saved)})
            self._starting = False
            self._started = True
            self._task(self._poll_cookies())
            await self._navigate('main', url)
            if not self.is_closing:
                self.emit('started')
        except Exception as error:
            failed = True
            if not self.is_closing:
                known = (BrowserUnavailable, BrowserProfileError)
                try:
                    from webview2_host import WebView2Error
                    from webview2_setup import RuntimeSetupError
                    known += (WebView2Error, RuntimeSetupError)
                except ImportError:
                    pass
                message = str(error) if isinstance(error, known) else '无法初始化系统 WebView2 登录页面，请重试。'
                self.emit('startup_failed', message)
        finally:
            self._startup_complete.set()
            if failed or self.is_closing:
                await self._finish()

    async def _open_host(self, path):
        self._host_serial += 1
        serial = self._host_serial
        self._host_closed = False
        self._ready = asyncio.get_running_loop().create_future()
        self.pages.clear(); self._native_pages.clear(); self._requested_pages.clear()
        self._page(1)
        def create():
            from webview2_host import WebView2Host
            host = WebView2Host(parent_hwnd=self.parent_hwnd, user_data_folder=str(path),
                user_agent=self.user_agent, scripts=self.scripts,
                on_event=lambda event, data: self._receive(event, data, serial),
                navigation_policy=self._navigation_policy)
            self._host = host
            host.start()
            return host
        await self._gui(create)
        await asyncio.wait_for(self._ready, 20)
        await self._configure(self.pages['main'])

    def _callback(self, page, url):
        try:
            parsed = urlsplit(url)
            expected = {'auth': 'tauth.qq.com', 'honorid': 'redirect_url'}
            valid = (parsed.scheme in self.callback_schemes and parsed.netloc.lower() == expected.get(parsed.scheme)
                     and parsed.username is None and parsed.password is None and parsed.port is None
                     and parsed.path in ('', '/'))
        except (ValueError, TypeError):
            valid = False
        if valid and self._started and not self.is_closing and not self._clear_pending and url not in self._callback_seen:
            self._callback_seen.add(url)
            self.emit('url', page.page_id, url)
        return valid

    async def _event(self, name, data, serial):
        if serial != self._host_serial:
            return
        if name == 'closed':
            if data.get('safe_to_release'):
                self._host_closed = True
                if self._ready is not None and not self._ready.done():
                    self._ready.cancel()
                if not self.is_closing and not self._clear_pending:
                    self._task(self._finish())
                if self._late_release:
                    await self._release_profile()
            return
        if name == 'ready':
            if self._ready and not self._ready.done():
                self._ready.set_result(True)
            return
        if name == 'error':
            from webview2_host import WebView2Error
            detail = str(WebView2Error(str(data.get('operation', 'runtime')), data.get('hresult')))
            if self._starting and self._ready and not self._ready.done():
                self._ready.set_exception(BrowserUnavailable('系统 WebView2 初始化失败：' + detail))
            elif not self.is_closing:
                self.emit('error', '系统 WebView2 登录页面出现错误，本次登录已取消。' + detail)
                self._task(self._finish())
            return
        if self.is_closing:
            return
        native_id = data.get('page_id', 1)
        page = self._native_pages.get(int(native_id))
        if name == 'page':
            page = self._page(native_id)
            if page.page_id != 'main':
                await self._configure(page)
                if serial == self._host_serial:
                    self.emit('page', page.page_id)
            return
        if page is None:
            return
        if name == 'navigation':
            if not data.get('allowed', True):
                self._callback(page, data.get('url', ''))
            elif not data.get('is_frame'):
                page.navigation = int(data.get('navigation_id', 0))
                page.ready = False
                page.context = None
                page.epoch += 1
        elif name == 'devtools':
            event, p = data.get('event'), data.get('params', {})
            if event == 'Runtime.executionContextCreated':
                context = p.get('context', {})
                auxiliary = context.get('auxData', {})
                if auxiliary.get('isDefault') and auxiliary.get('frameId') == page.frame:
                    page.context = context.get('uniqueId')
            elif event == 'Runtime.executionContextsCleared':
                page.context = None
            elif event == 'Runtime.executionContextDestroyed' and p.get('executionContextUniqueId') == page.context:
                page.context = None
        elif name == 'url':
            url = str(data.get('url', ''))
            if url != page.url:
                page.epoch += 1
            page.url = url
            if self._started and not self._clear_pending:
                if self.cookie_export is None:
                    self.emit('url', page.page_id, page.url)
                elif page.ready:
                    await self._page_event('url', page, page.url)
        elif name == 'loaded':
            navigation = int(data.get('navigation_id', 0))
            if page.navigation and navigation != page.navigation:
                return
            await asyncio.wait_for(page.configured.wait(), 15)
            if serial != self._host_serial:
                return
            page.ready = bool(data.get('success'))
            if self._started and not self._clear_pending:
                if page.ready and self.cookie_export is not None:
                    await self._page_event('url', page, page.url)
                await self._page_event('loaded', page, page.ready)
        elif name == 'focus_requested':
            self.emit('focus_move', page.page_id, data.get('reason', 'next'))
        elif name == 'console' and self._started and not self._clear_pending:
            self.emit('console', page.page_id, data.get('level', 'log'), str(data.get('message', '')),
                      data.get('line', 0), str(data.get('source', page.url)))
        elif name == 'page_closed':
            self.pages.pop(page.page_id, None)
            self._native_pages.pop(page.target, None)
            if page.page_id == 'main':
                await self._finish()
            else:
                self.emit('page_closed', page.page_id)

    async def _page_event(self, name, page, value):
        if self.is_closing or self._clear_pending or page.page_id not in self.pages:
            return
        epoch = self._clear_epoch
        if self.cookie_export is not None and self.cookie_export.matches(page.url):
            try:
                async with page.lock:
                    token, records = await self._snapshot(page)
                    self.emit('cookies_snapshot', token, records)
                    self.emit('cookie_boundary', page.page_id, token)
                    self.emit(name, page.page_id, value)
            except (CookieExportError, BrowserUnavailable, _CommandError):
                if not self._clear_pending and not self.is_closing and epoch == self._clear_epoch:
                    self.emit('cookie_error', '登录状态在读取期间改变，请重新登录。')
        else:
            self.emit(name, page.page_id, value)

    async def _document(self, page):
        if self._clear_pending or self.is_closing:
            raise BrowserUnavailable('登录页面事务已取消。')
        if not page.ready or not page.context:
            raise BrowserUnavailable('登录页面尚未就绪。')
        doc = (page.target, page.context, page.epoch, page.url, self._clear_epoch)
        response = await self.command('Runtime.evaluate', {'expression': 'location.href',
            'uniqueContextId': page.context, 'returnByValue': True}, page.sid)
        if response.get('exceptionDetails') or response.get('result', {}).get('value') != page.url:
            raise BrowserUnavailable('登录页面已经切换。')
        if doc != (page.target, page.context, page.epoch, page.url, self._clear_epoch):
            raise BrowserUnavailable('登录页面已经切换。')
        return doc

    async def _snapshot(self, page):
        before = await self._document(page)
        raw = (await self.command('Storage.getCookies'))['cookies']
        if before != await self._document(page):
            raise BrowserUnavailable('登录状态来源页面已经切换。')
        records = self.cookie_export.select(raw, page.url)
        self._cookie_snapshot = raw
        token = uuid.uuid4().hex
        self._exports[token] = json.loads(json.dumps(records))
        self._export_docs[token] = before
        while len(self._exports) > 128:
            oldest = next(iter(self._exports))
            self._exports.pop(oldest, None); self._export_docs.pop(oldest, None)
        return token, records

    async def _poll_cookies(self):
        while not self.is_closing:
            await asyncio.sleep(1)
            if self._starting or self.is_closing or self._clear_pending:
                continue
            if self.cookie_export is not None:
                for page in tuple(self.pages.values()):
                    if page.ready and self.cookie_export.matches(page.url):
                        await self._page_event('url', page, page.url)
            elif self.persistent:
                self._cookie_snapshot = (await self.command('Storage.getCookies'))['cookies']

    def evaluate(self, page_id, script, callback=None, *, cookie_token=None):
        epoch = self._clear_epoch
        async def evaluate():
            result = None
            try:
                page = self.pages[page_id]
                async with page.lock:
                    doc = await self._document(page)
                    if cookie_token is not None and self._export_docs.get(cookie_token) != doc:
                        raise BrowserUnavailable('登录页面事务已失效。')
                    expression = ('if(location.href!==' + json.dumps(doc[3]) +
                        ')throw new Error("Document changed");\n' + str(script))
                    response = await self.command('Runtime.evaluate', {'expression': expression,
                        'uniqueContextId': doc[1], 'returnByValue': True, 'awaitPromise': True,
                        'allowUnsafeEvalBlockedByCSP': False}, page.sid)
                    if response.get('exceptionDetails') or doc != await self._document(page):
                        raise BrowserUnavailable('登录页面事务已失效。')
                    result = response.get('result', {}).get('value')
            except Exception:
                if cookie_token is not None:
                    self._exports.pop(cookie_token, None); self._export_docs.pop(cookie_token, None)
                    if not self._clear_pending and not self.is_closing and epoch == self._clear_epoch:
                        self.emit('cookie_error', '登录回调页面已变化，不能读取新的页面代替原结果。')
            if callback:
                self.dispatch(lambda: callback(result))
        return self.runtime.submit(evaluate())

    def read_body(self, page_id, callback, *, cookie_token=None):
        return self.evaluate(page_id, 'document.body ? document.body.innerText : ""',
                             callback, cookie_token=cookie_token)


    def navigate(self, page_id, url):
        return self.runtime.submit(self._navigate(page_id, url))

    async def _navigate(self, page_id, url):
        if self.is_closing:
            return
        page = self.pages.get(page_id)
        if page is None:
            raise BrowserUnavailable('登录页面已关闭。')
        parsed = urlsplit(str(url))
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise BrowserUnavailable('仅允许打开 HTTP(S) 登录页面。')
        page.ready = False
        host = self._host
        await self._gui(lambda: host.navigate(page.target, str(url)))

    def new_page(self, url):
        alias = uuid.uuid4().hex
        async def create():
            if self.is_closing or self._clear_pending:
                return
            host, serial = self._host, self._host_serial
            def open_page():
                if serial != self._host_serial or self.is_closing or self._clear_pending:
                    raise BrowserUnavailable('登录页面实例已更换。')
                native_id = host.new_page(str(url))
                self._requested_pages[native_id] = alias
                return native_id
            native_id = await self._gui(open_page)
            if serial == self._host_serial:
                self._page(native_id, alias)
        async def observed():
            self._task(create())
        self.runtime.submit(observed())
        return alias

    def _surface_call(self, operation):
        if self._host is None or not self._started or self._host_closed or self.is_closing or self._clear_pending:
            return
        try:
            operation(self._host)
        except Exception:
            self.emit('error', '系统 WebView2 窗口已不可用，本次登录已取消。')
            self.finish()

    def resize(self, width, height):
        self._surface_call(lambda host: host.resize(int(width), int(height)))

    def focus(self, page_id='main', reason='programmatic'):
        page = self.pages.get(page_id)
        if page and self._host is not None and not self.is_closing and not self._clear_pending:
            serial = self._host_serial
            def move():
                if serial == self._host_serial:
                    self._surface_call(lambda host: host.focus(page.target, reason=reason))
            self.dispatch(move)

    def notify_position_changed(self):
        self._surface_call(lambda host: host.notify_position_changed())

    def clear_cookies(self):
        if self._clear_future is not None and not self._clear_future.done():
            return self._clear_future
        self._clear_pending = True
        self._clear_epoch += 1
        async def clear():
            self._exports.clear(); self._export_docs.clear(); self._cookie_snapshot = []
            try:
                if self._start_called and not self._startup_complete.is_set():
                    await self._startup_complete.wait()
                    if self._started and not self._finished and not self._clear_pending:
                        return True
                if self._lease:
                    await asyncio.to_thread(self._lease.mark_cookies_cleared)
                if not self._started or self._finished:
                    if self._lease is None and self.persistent:
                        lease = ProfileLease(self.profile_path, True)
                        try:
                            await asyncio.to_thread(lease.prepare)
                            await asyncio.to_thread(lease.mark_cookies_cleared)
                        finally:
                            await asyncio.to_thread(lease.close)
                    self.emit('cookies_reset')
                    return True
                for page in self.pages.values():
                    page.ready = False; page.context = None; page.epoch += 1
                    if page.page_id != 'main':
                        self.emit('page_closed', page.page_id)
                await self._gui(lambda cb: self._host.close(cb), callback=True, timeout=35)
                self._host_closed = True
                if self.is_closing:
                    return False
                pending = tuple(task for task in self._tasks if task is not asyncio.current_task())
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                await self._open_host(self._lease.path)
                await self._gui(lambda cb: self._host.clear_data(cb), callback=True, timeout=30)
                if self._lease:
                    await asyncio.to_thread(self._lease.confirm_cookies_cleared)
                self._clear_pending = False
                self._task(self._poll_cookies())
                self.emit('cookies_reset')
                if self._initial_url:
                    await self._navigate('main', self._initial_url)
                return True
            except Exception:
                self.emit('cookie_error', '清除登录状态失败，已禁止恢复旧状态；请关闭登录窗口后重试。')
                await self._finish()
                return False
        self._clear_future = self.runtime.submit(clear())
        return self._clear_future

    def close_page(self, page_id):
        if page_id == 'main':
            return self.finish()
        page = self.pages.get(page_id)
        if page and self._host is not None and not self._clear_pending and not self.is_closing:
            serial = self._host_serial
            self.dispatch(lambda: self._surface_call(lambda host: host.close_page(page.target))
                          if serial == self._host_serial else None)

    def finish(self, cookie_token=None):
        self._close_requested = True
        return self.runtime.submit(self._finish(cookie_token))

    async def _release_profile(self):
        if self._lease:
            await asyncio.to_thread(self._lease.close)
            self._lease = None
        self.runtime.sessions.discard(self)

    async def _finish(self, cookie_token=None):
        if self._finishing or self._finished:
            accepted = self._committed_token if self._finished else self._finishing_token
            if cookie_token is not None and cookie_token != accepted:
                self.emit('cookie_error', '登录页面关闭先于状态提交，本次登录已取消。')
            if not self._finished:
                await self._done.wait()
            return
        self._finishing_token = cookie_token
        self._finishing = self._close_requested = True
        committed = None
        if self._startup_task and self._startup_task is not asyncio.current_task():
            if self._preparing_runtime and self._lease is None and self._host is None:
                self._startup_task.cancel()
            await self._startup_complete.wait()
        try:
            if cookie_token is not None:
                records = self._exports.get(cookie_token)
                doc = self._export_docs.get(cookie_token)
                if records is None or doc is None or doc[4] != self._clear_epoch or self._clear_pending:
                    self.emit('cookie_error', '登录状态事务已失效，没有保存任何账号信息。')
                else:
                    committed = json.loads(json.dumps(records))
            if self._started and self._lease and not self._clear_pending:
                with contextlib.suppress(Exception):
                    self._cookie_snapshot = (await self.command('Storage.getCookies', timeout=2))['cookies']
                try:
                    epoch = self._clear_epoch
                    await asyncio.to_thread(self._lease.save_cookies, self._cookie_snapshot)
                    if self._clear_pending or epoch != self._clear_epoch:
                        await asyncio.to_thread(self._lease.mark_cookies_cleared)
                except Exception:
                    self.emit('error', '无法保存本次页面登录状态，下次可能需要重新登录。')
            if self._host is not None and not self._host_closed:
                await self._gui(lambda cb: self._host.close(cb), callback=True, timeout=35)
                self._host_closed = True
            await self._release_profile()
            if committed is not None and (self._clear_pending or doc[4] != self._clear_epoch):
                committed = None
                self.emit('cookie_error', '登录状态已被清除，本次账号信息未提交。')
            if committed is not None:
                self._committed_token = cookie_token
                self.emit('cookies_committed', cookie_token, committed)
        except Exception:
            committed = None
            if self._host is None or self._host_closed:
                await self._release_profile()
            else:
                self._late_release = True
            self.emit('cookie_error', '未确认 WebView2 已安全释放，已取消账号保存；请重启本工具后重试。')
        finally:
            current = asyncio.current_task()
            for task in tuple(self._tasks):
                if task is not current:
                    task.cancel()
            self._exports.clear(); self._export_docs.clear(); self._cookie_snapshot = []
            self._callback_seen.clear()
            self._finished = True
            self.emit('finished')
            self._done.set()
