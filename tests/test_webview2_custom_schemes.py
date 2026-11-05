"""Callback registration contracts; native COM cases run on Windows only.

Portable cases exercise the real implementations with an explicit allocator/COM
model. They do not substitute for a real WebView2 navigation integration test.
"""
import ast
import asyncio
import ctypes as C
import gc
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlparse, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'


def source_tree(path):
    return ast.parse((SRC / path).read_text(encoding='utf-8'))


def source_class(path, name, methods, **namespace):
    original = next(n for n in source_tree(path).body if isinstance(n, ast.ClassDef) and n.name == name)
    node = ast.ClassDef(name=name, bases=[], keywords=[], decorator_list=[], body=[
        n for n in original.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in methods])
    scope = dict(urlsplit=urlsplit, urlparse=urlparse, parse_qs=parse_qs, **namespace)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), path, 'exec'), scope)
    return scope[name]


class Allocator:
    def __init__(self):
        self.blocks = {}
        self.fail = False

    def CoTaskMemAlloc(self, size):
        if self.fail:
            return None
        block = C.create_string_buffer(size)
        address = C.addressof(block)
        self.blocks[address] = block
        return address

    def CoTaskMemFree(self, address):
        del self.blocks[address]


class FakeInterface(C.Structure):
    _fields_ = [('unused', C.c_int)]


class FakeCOM:
    def QueryInterface(self, interface):
        pointer = C.pointer(FakeInterface())
        self.references = 1
        pointer.owner = self
        def add_ref():
            self.references += 1
            return self.references
        def release():
            self.references -= 1
            return self.references
        pointer.AddRef, pointer.Release = add_ref, release
        return pointer


def portable_options():
    allocator = Allocator()
    names = ('_string_get', '_scalar_get', '_deny', 'CallbackSchemeRegistration', 'EnvironmentOptions')
    nodes = [n for n in source_tree('webview2_com_loader.py').body if getattr(n, 'name', '') in names]
    interfaces = SimpleNamespace(**{name: FakeInterface for name in (
        'ICoreWebView2EnvironmentOptions', 'ICoreWebView2EnvironmentOptions2',
        'ICoreWebView2EnvironmentOptions4', 'ICoreWebView2EnvironmentOptions7',
        'ICoreWebView2CustomSchemeRegistration')})
    scope = dict(C=C, COMObject=FakeCOM, I=interfaces, ole32=allocator, MINIMUM_VERSION='136.0.3240.50',
                 E_POINTER=-2147467261, E_OUTOFMEMORY=-2147024882,
                 E_ACCESSDENIED=-2147024891, E_FAIL=-2147467259)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'webview2_com_loader.py', 'exec'), scope)
    return scope['EnvironmentOptions'](), allocator, scope


class RegistrationContractTests(unittest.TestCase):
    def setUp(self):
        self.options, self.allocator, self.scope = portable_options()

    def test_exact_fixed_schemes_and_no_added_privileges(self):
        self.assertEqual([p.owner._scheme_name for p in self.options._scheme_registrations], ['auth', 'honorid'])
        for pointer in self.options._scheme_registrations:
            registration = pointer.owner
            for name, expected in [('get_HasAuthorityComponent', 1), ('get_TreatAsSecure', 0)]:
                value = C.c_int32(-1)
                self.assertEqual(getattr(registration, name)(None, C.pointer(value)), 0)
                self.assertEqual(value.value, expected)
            count, origins = C.c_uint32(99), C.POINTER(C.c_void_p)()
            self.assertEqual(registration.GetAllowedOrigins(None, C.pointer(count), C.pointer(origins)), 0)
            self.assertEqual(count.value, 0)
            self.assertFalse(origins)
            self.assertEqual(registration.SetAllowedOrigins(None, 0, None), self.scope['E_ACCESSDENIED'])
            self.assertEqual(registration.put_TreatAsSecure(None, 1), self.scope['E_ACCESSDENIED'])
            self.assertEqual(registration.put_HasAuthorityComponent(None, 0), self.scope['E_ACCESSDENIED'])

    def test_owned_array_addrefs_once_per_call(self):
        pointers = self.options._scheme_registrations
        addresses = []
        for expected in (2, 3):
            count, array = C.c_uint32(), C.POINTER(C.POINTER(FakeInterface))()
            self.assertEqual(self.options.GetCustomSchemeRegistrations(None, C.pointer(count), C.pointer(array)), 0)
            self.assertEqual(count.value, 2)
            raw = C.cast(array, C.POINTER(C.c_void_p))
            self.assertEqual([raw[i] for i in range(2)], [C.cast(p, C.c_void_p).value for p in pointers])
            self.assertEqual([p.owner.references for p in pointers], [expected, expected])
            addresses.append(C.cast(array, C.c_void_p).value)
        self.assertNotEqual(*addresses)
        for address in addresses:
            self.allocator.CoTaskMemFree(address)
        for p in pointers:
            p.Release()
            p.Release()
            self.assertEqual(p.owner.references, 1)
        self.assertFalse(self.allocator.blocks)

    def test_allocation_failure_resets_outputs_and_does_not_addref(self):
        self.allocator.fail = True
        count, array = C.c_uint32(99), C.c_void_p(123)
        self.assertEqual(self.options.GetCustomSchemeRegistrations(None, C.pointer(count), C.pointer(array)), self.scope['E_OUTOFMEMORY'])
        self.assertEqual(count.value, 0)
        self.assertIsNone(array.value)
        self.assertEqual([p.owner.references for p in self.options._scheme_registrations], [1, 1])

    def test_partial_failure_releases_refs_and_array(self):
        pointer = self.options._scheme_registrations[1]
        def fail():
            raise RuntimeError('synthetic AddRef failure')
        pointer.AddRef = fail
        count, array = C.c_uint32(99), C.c_void_p(123)
        self.assertEqual(self.options.GetCustomSchemeRegistrations(None, C.pointer(count), C.pointer(array)), self.scope['E_FAIL'])
        self.assertEqual(count.value, 0)
        self.assertIsNone(array.value)
        self.assertEqual([p.owner.references for p in self.options._scheme_registrations], [1, 1])
        self.assertFalse(self.allocator.blocks)

    def test_failure_after_addref_rolls_back_that_reference_too(self):
        class InvalidPointer:
            references = 1
            def AddRef(self):
                self.references += 1
            def Release(self):
                self.references -= 1
        invalid = InvalidPointer()
        first = self.options._scheme_registrations[0]
        self.options._scheme_registrations = (first, invalid)
        count, array = C.c_uint32(99), C.c_void_p(123)
        self.assertEqual(self.options.GetCustomSchemeRegistrations(None, C.pointer(count), C.pointer(array)), self.scope['E_FAIL'])
        self.assertEqual((first.owner.references, invalid.references), (1, 1))
        self.assertEqual(count.value, 0)
        self.assertIsNone(array.value)
        self.assertFalse(self.allocator.blocks)

    def test_null_outputs_and_mutation_are_rejected(self):
        count, array = C.c_uint32(), C.c_void_p()
        self.assertEqual(self.options.GetCustomSchemeRegistrations(None, None, C.pointer(array)), self.scope['E_POINTER'])
        self.assertEqual(self.options.GetCustomSchemeRegistrations(None, C.pointer(count), None), self.scope['E_POINTER'])
        self.assertEqual(self.options.SetCustomSchemeRegistrations(None, 0, None), self.scope['E_ACCESSDENIED'])
        registration = self.options._scheme_registrations[0].owner
        self.assertEqual(registration.GetAllowedOrigins(None, None, C.pointer(array)), self.scope['E_POINTER'])
        self.assertEqual(registration.GetAllowedOrigins(None, C.pointer(count), None), self.scope['E_POINTER'])

    def test_scheme_name_is_caller_owned_memory(self):
        for pointer in self.options._scheme_registrations:
            out = C.c_void_p()
            self.assertEqual(pointer.owner.get_SchemeName(None, C.pointer(out)), 0)
            self.assertEqual(C.wstring_at(out.value), pointer.owner._scheme_name)
            self.allocator.CoTaskMemFree(out.value)
        self.assertFalse(self.allocator.blocks)


class CallbackRouteTests(unittest.TestCase):
    def make_session(self, scheme):
        cls = source_class('browser_runtime.py', 'BrowserSession', {'_callback', '_event'})
        session = cls()
        session.callback_schemes = (scheme,)
        session._started, session.is_closing, session._clear_pending = True, False, False
        session._callback_seen, session._host_serial = set(), 1
        page = SimpleNamespace(id=1, page_id='main', closed=False, navigation=7)
        session._native_pages = {1: page}
        captured = []
        session.emit = lambda *args: captured.append(args)
        return session, page, captured

    def test_event_routes_preserve_qq_miqq_and_honor_callback_validation(self):
        qq = source_class('channelHandler/qqLogin/qqChannel_windows.py', 'QQBrowser', {'verify'})()
        mi = source_class('channelHandler/miLogin/miChannel_windows.py', 'MiBrowser', {'verify'})()
        mi.isQQ = True
        honor_scope = {}
        exec(compile(source_tree('channelHandler/honorLogin/session.py'), 'session.py', 'exec'), honor_scope)
        cases = [('auth', 'auth://tauth.qq.com/?code=synthetic', qq.verify),
                 ('auth', 'auth://tauth.qq.com/#access_token=synthetic&openid=synthetic', mi.verify),
                 ('honorid', 'honorid://redirect_url?code=synthetic&state=test-state',
                  lambda url: honor_scope['valid_redirect'](url, 'test-state', 'honorid://redirect_url'))]
        host_type = source_class('webview2_com.py', 'ComHost', {'allowed', 'navigation'},
                                 text=lambda obj, name: getattr(obj, name)())
        original = next(n for n in source_tree('webview2_com.py').body if isinstance(n, ast.ClassDef) and n.name == 'ComHost')
        configure = next(n for n in original.body if isinstance(n, ast.FunctionDef) and n.name == 'configure')
        for scheme, url, verify in cases:
            for route in ('main', 'iframe', 'external', 'popup'):
                with self.subTest(scheme=scheme, url=url, route=route):
                    session, page, captured = self.make_session(scheme)
                    host = host_type()
                    host.closing = False
                    def emit(name, **data):
                        if name == 'navigation':
                            asyncio.run(session._event(name, data, 1))
                        return 0
                    host.emit = emit
                    args = SimpleNamespace(get_Uri=lambda: url, get_NavigationId=lambda: 7)
                    args.put_Cancel = lambda value: setattr(args, 'cancelled', value)
                    args.put_Handled = lambda value: setattr(args, 'handled', value)
                    if route in ('main', 'iframe'):
                        host.navigation(page, args, route == 'iframe')
                    else:
                        node = next(n for n in ast.walk(configure) if isinstance(n, ast.FunctionDef) and n.name == route)
                        scope = dict(self=host, page=page, text=lambda obj, name: getattr(obj, name)())
                        exec(compile(ast.Module(body=[node], type_ignores=[]), 'webview2_com.py', 'exec'), scope)
                        scope[route](None, args)
                    self.assertEqual(len(captured), 1)
                    self.assertTrue(verify(captured[0][2]))
                    self.assertTrue(args.handled if route == 'popup' else args.cancelled)
                    session._callback(page, url)
                    self.assertEqual(len(captured), 1)

    def test_callback_scope_and_lifecycle_remain_strict(self):
        session, page, captured = self.make_session('auth')
        for url in ('auth://evil.example/?code=x', 'auth://tauth.qq.com:443/?code=x',
                    'auth://user@tauth.qq.com/?code=x', 'auth://tauth.qq.com/other?code=x',
                    'honorid://redirect_url?code=x', 'hms://callback', 'idvlogin://open',
                    'https://auth/?code=x'):
            self.assertFalse(session._callback(page, url), url)
        self.assertFalse(captured)
        for flag in ('is_closing', '_clear_pending'):
            setattr(session, flag, True)
            session._callback(page, 'auth://tauth.qq.com/?code=synthetic')
            setattr(session, flag, False)
        session._started = False
        session._callback(page, 'auth://tauth.qq.com/?code=synthetic')
        self.assertFalse(captured)


@unittest.skipUnless(sys.platform == 'win32', 'Requires native Windows COM; not a browser navigation test')
class NativeCOMRegistrationTests(unittest.TestCase):
    def test_native_abi_and_returned_reference_lifetime(self):
        sys.path.insert(0, str(SRC))
        import webview2_com_loader as loader
        import webview2_com_interfaces as interfaces
        options = loader.EnvironmentOptions()
        options4 = options.QueryInterface(interfaces.ICoreWebView2EnvironmentOptions4)
        count, array = options4.GetCustomSchemeRegistrations()
        registrations = []
        try:
            self.assertEqual(count, 2)
            raw = C.cast(array, C.POINTER(C.c_void_p))
            for index in range(count):
                # Each wrapper adopts the one returned COM reference. Do not
                # Release it explicitly in addition to its destructor.
                registrations.append(C.cast(raw[index], C.POINTER(interfaces.ICoreWebView2CustomSchemeRegistration)))
        finally:
            loader.ole32.CoTaskMemFree(C.cast(array, C.c_void_p).value)
        for registration in registrations:
            self.assertEqual(registration.AddRef(), 3)
            self.assertEqual(registration.Release(), 2)
        options4 = options = None
        gc.collect()
        for registration, expected in zip(registrations, ('auth', 'honorid')):
            self.assertEqual(registration.AddRef(), 2)
            self.assertEqual(registration.Release(), 1)
            self.assertEqual(loader.take_string(registration.get_SchemeName()), expected)
            self.assertEqual(registration.get_HasAuthorityComponent(), 1)
            self.assertEqual(registration.get_TreatAsSecure(), 0)
            count, origins = registration.GetAllowedOrigins()
            self.assertEqual(count, 0)
            self.assertFalse(origins)


if __name__ == '__main__':
    unittest.main()
