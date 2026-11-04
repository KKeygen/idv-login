"""Offline Oracle-alignment regression tests using synthetic protocol responses.

This fixture harness intentionally contains fake account/device/SDK values.
It is not a live-API client and must not supply live request configuration.
"""
import ast
import base64
import copy
import importlib.util
import json
import sys
import threading
import time
import uuid
import types
import unittest
from concurrent.futures import Future
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'


def load_file(path, namespace=None, classes=None, functions=None):
    ns = {} if namespace is None else namespace
    tree = ast.parse(path.read_text())
    if classes is not None or functions is not None:
        tree.body = [n for n in tree.body if (isinstance(n, ast.ClassDef) and n.name in (classes or ()))
                     or (isinstance(n, ast.FunctionDef) and n.name in (functions or ()))]
    exec(compile(tree, str(path), 'exec'), ns)
    return ns


class Logger:
    def __init__(self): self.messages = []
    def __getattr__(self, name):
        return lambda message, *args, **kw: self.messages.append(str(message).format(*args))


class Response:
    status_code = 200
    def __init__(self, body): self.body = body
    def raise_for_status(self): pass
    def json(self): return copy.deepcopy(self.body)


def session(token='fixture-token', ttl=604800, uid='fixture-open'):
    ut = {'openId': uid, 'token': token}
    if ttl != 'absent': ut['expireTimeout'] = ttl
    return {'errorCode': 0, 'loginData': {'errorCode': 0, 'data': {'unionToken': ut, 'displayName': 'fixture-name'}}}


PURE = load_file(SRC / 'channelHandler/honorLogin/session.py')
CONSTS = load_file(SRC / 'channelHandler/honorLogin/consts.py')


class Harness:
    def __init__(self, platform='windows', replies=None, ttl=9999999999):
        self.events, self.logs = [], Logger()
        self.replies = list(replies or [])
        h = self
        class Account:
            def __init__(self, saved): pass
            def authorization_code(self, config): h.events.append(('silent_code', {})); return 'fixture-code'
        self.ns = dict(CONSTS, **PURE, json=json, time=time,
            HonorAccountSession=Account, setup_logger=lambda: self.logs,
            app_state=types.SimpleNamespace(run_on_main_thread=lambda fn: fn()),
            genv=types.SimpleNamespace(set=lambda *a: None),
            threading=threading, requests=types.SimpleNamespace(post=self.post),
            should_verify_ssl=lambda: True, HonorBrowser=object)
        mix = load_file(SRC / 'channelHandler/honorLogin/account.py', self.ns,
                        classes=('HonorAccountLogin',))['HonorAccountLogin']
        cls = load_file(SRC / f'channelHandler/honorLogin/honorChannel_{platform}.py', self.ns,
                        classes=('HonorLogin',))['HonorLogin']
        self.login = cls.__new__(cls)
        self.login.logger = self.logs
        self.login.channelConfig = {'app_id': 'fixture-app', 'package_name': 'fixture.pkg', 'app_version': '9.1.0', 'app_version_code': '42'}
        self.login.device = {'deviceId': 'fixture-device'}
        self.login.unionToken = session()['loginData']['data']['unionToken']
        self.login.accountSession = {'serviceToken': 'fixture-st'}
        self.login.expiredTime = ttl
        self.login.lastLoginTime = 0
        self.login.lastSessionError = None
        self.login.displayName = ''
    def post(self, url, **kwargs):
        self.events.append((url.rsplit('/', 1)[-1], kwargs))
        response = self.replies.pop(0) if self.replies else session('fixture-renewed')
        if isinstance(response, Exception): raise response
        return Response(response)


def make_adapter(h):
    utils = {'getShortGameId': lambda game: game, 'cmp_game_id': lambda a,b: a==b}
    cache = load_file(SRC / 'channel_cache.py', dict(base64=base64, json=json, copy=copy, time=time, **utils),
                      functions=('packet_identity', 'unisdk_expires_at', 'credential_expires_at'))
    cache['unquote'] = lambda x:x
    base_ns = dict(cache, copy=copy, time=time, genv=types.SimpleNamespace(set=lambda *a:None), setup_logger=lambda:h.logs)
    base = load_file(SRC / 'channelmgr.py', base_ns, classes=('channel',))['channel']
    def build(**kw):
        h.events.append(('build_sauth', kw))
        return {'gameid':kw['game_id'], 'login_channel':kw['login_channel'], 'sdkuid':kw['uid'], 'sessionid':kw['session']}
    ns = dict(time=time, copy=copy, json=json, base64=base64, urlencode=urlencode, **utils,
        channelmgr=types.SimpleNamespace(channel=base), setup_logger=lambda:h.logs,
        genv=types.SimpleNamespace(set=lambda *a:None),
        buildSAUTH=build, postSignedData=lambda *a: {'unisdk_login_json':base64.b64encode(b'{"username":"fixture-name"}').decode()},
        app_state=types.SimpleNamespace(fake_device={'udid':'fixture-device'},run_on_main_thread=lambda fn:fn()), threading=threading)
    cls=load_file(SRC / 'channelHandler/honorChannelHandler.py',ns,classes=('honorChannel',))['honorChannel']
    record=cls.__new__(cls)
    base.__init__(record, {'login_channel':'honor_sdk','code':'fixture'}, uuid='fixture-record')
    record.game_id='h55';record.sdk_version='fixture-version';record.logger=h.logs
    record.unionToken=copy.deepcopy(h.login.unionToken);record.accountSession=copy.deepcopy(h.login.accountSession)
    record.honorLogin=h.login
    record.request_user_login=lambda **kw: (h.events.append(('interactive',{})) or (kw['on_complete'](False) if kw.get('on_complete') else False))
    return record


class ProtocolTests(unittest.TestCase):
    def test_positive_and_unknown_ttl_success(self):
        for ttl in ('absent',None,0,-1,'0',604800,'604800'):
            with self.subTest(ttl=ttl):
                token,name,lifetime=PURE['parse_session'](session(ttl=ttl))
                self.assertEqual(lifetime,604800 if str(ttl)=='604800' else None)
                self.assertEqual(token['token'],'fixture-token')
    def test_malformed_ttl_is_not_accepted_or_logged(self):
        for ttl in ('secret',{},[],True):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError): PURE['parse_session'](session(ttl=ttl))
    def test_identity_and_error_still_rejected(self):
        for data in ({'errorCode':7001},session(token=''),session(uid=''),{'loginData':{'errorCode':7002}},[] ):
            with self.subTest(data=data), self.assertRaises(ValueError): PURE['parse_session'](data)
    def test_error_stage_is_distinguished(self):
        for code in (7008,7009):
            for nested in (True,False):
                data={'errorCode':code}
                if nested:data={'loginData':data}
                with self.assertRaises(PURE['HonorSessionError']) as caught:PURE['parse_session'](data)
                self.assertEqual(caught.exception.blocks_reauthorization,nested)
    def test_game_version_code_not_sdk_or_version_name(self):
        for value,expected in [(None,0),('',0),('9.1.0',0),('42',42),(0,0),(2**32,0),(True,0)]:
            self.assertEqual(PURE['app_version_code']({'app_version_code':value}),expected)
        self.assertEqual(PURE['app_version_code']({'app_version':'123'}),0)
    def test_config_login_body_and_no_fallback_after_success(self):
        for platform in ('windows','legacy'):
            h=Harness(platform);self.assertTrue(h.login.refreshSession())
            self.assertEqual([e[0] for e in h.events],['configLogin'])
            body=h.events[0][1]['json']
            self.assertEqual(set(body),{'amsPackageName','appVersionCode','realNameAuthMode','scene','trackingStr','tInfo'})
            self.assertEqual(body['appVersionCode'],42)
            self.assertEqual(body['tInfo']['accessToken'],'fixture-token')
    def test_rejected_old_token_follows_config_then_st_then_login(self):
        for platform in ('windows','legacy'):
            for failure in ({'loginData':{'errorCode':7001}}, {'loginData':{'errorCode':7002}}, {'loginData':{'errorCode':7003}}, {'loginData':{'errorCode':555}}, RuntimeError('fixture-private')):
                h=Harness(platform,[failure,{'errorCode':0},session('fixture-new')])
                self.assertTrue(h.login.refreshSession())
                self.assertEqual([e[0] for e in h.events],['configLogin','config','silent_code','login'])
                cfg=h.events[1][1]['json'];self.assertEqual(set(cfg),{'amsPackageName','appVersionCode','tInfo'})
                login=h.events[-1][1]['json'];self.assertEqual(login['oauthCode'],'fixture-code')
                self.assertNotIn('appVersionCode',login)
                self.assertNotIn('fixture-private',' '.join(h.logs.messages))
    def test_config_failure_is_best_effort(self):
        for failure in (RuntimeError('fixture-private'),{'errorCode':9},[]):
            h=Harness(replies=[failure,session('fixture-new')]);h.login.unionToken=None
            self.assertTrue(h.login.refreshSession())
            self.assertEqual([e[0] for e in h.events],['config','silent_code','login'])
    def test_restriction_stops_recovery_and_preserves_old_record(self):
        for platform in ('windows','legacy'):
            for code in (7008,7009):
                h=Harness(platform,[{'loginData':{'errorCode':code,'errorMessage':'fixture-secret'}}])
                old=copy.deepcopy(h.login.unionToken)
                self.assertFalse(h.login.refreshSession());self.assertFalse(h.login.canReauthorize())
                self.assertEqual([e[0] for e in h.events],['configLogin'])
                self.assertEqual(h.login.unionToken,old)
                self.assertNotIn('fixture-secret',' '.join(h.logs.messages))
    def test_no_st_returns_false_without_fake_account_flow(self):
        h=Harness(replies=[{'loginData':{'errorCode':7001}}]);h.login.accountSession={}
        self.assertFalse(h.login.refreshSession());self.assertEqual([e[0] for e in h.events],['configLogin'])
    def test_unknown_ttl_works_on_both_platforms(self):
        for platform in ('windows','legacy'):
            for ttl in (None,0,-1,'absent'):
                h=Harness(platform,[session(ttl=ttl)])
                self.assertTrue(h.login.configLogin());self.assertIsNone(h.login.expiredTime)
                self.assertFalse(h.login.is_token_expired())
    def test_account_login_restriction_does_not_open_oauth(self):
        for method in ('qr','web'):
            h=Harness();h.login._update_qrcode_cache=lambda *a:None
            h.login.qrLogin=lambda:True
            def blocked():
                h.login.lastSessionError=PURE['HonorSessionError'](7008,login_data=True);return False
            h.login.loginWithAccount=blocked
            def web(on_complete=None):
                blocked()
                return on_complete(True,False) if on_complete else (True,False)
            h.login._web_account_login=web
            h.login.newOAuthLogin=lambda *a,**kw:self.fail('restricted login opened OAuth')
            self.assertFalse(h.login.newAccountLogin(login_method=method))
            callbacks=[];h.login.newAccountLogin(login_method=method,on_complete=callbacks.append)
            deadline=time.monotonic()+1
            while not callbacks and time.monotonic()<deadline:time.sleep(.005)
            self.assertEqual(callbacks,[False])


class EntryTests(unittest.TestCase):
    def test_fresh_shared_cache_cannot_skip_server_validation(self):
        h=Harness();r=make_adapter(h)
        original=r.request_unisdk(interactive=False)
        self.assertIsInstance(original,dict)
        h.events.clear();h.replies=[session('fixture-new')]
        packet=r.request_unisdk(interactive=False)
        self.assertEqual([e[0] for e in h.events],['configLogin','build_sauth'])
        self.assertEqual(base64.b64decode(packet['token']),b'fixture-new')
    def test_unexpired_but_server_rejected_uses_st_before_sauth(self):
        h=Harness(replies=[{'loginData':{'errorCode':7002}}, {'errorCode':0}, session('fixture-new')]);r=make_adapter(h)
        result=r.request_unisdk(interactive=False)
        self.assertIsInstance(result,dict)
        self.assertEqual([e[0] for e in h.events],['configLogin','config','silent_code','login','build_sauth'])
        self.assertEqual(h.events[-1][1]['session'],'fixture-new')
    def test_restriction_no_sauth_or_interactive(self):
        for callbacks in (False,True):
            h=Harness(replies=[{'loginData':{'errorCode':7009}}]);r=make_adapter(h);seen=[]
            result=r.request_unisdk(interactive=True,on_complete=seen.append if callbacks else None)
            if callbacks:
                deadline=time.monotonic()+2
                while not seen and time.monotonic()<deadline:time.sleep(.005)
            self.assertEqual(seen,[False] if callbacks else [])
            self.assertIs(result,None if callbacks else False)
            self.assertEqual([e[0] for e in h.events],['configLogin'])
    def test_cancellation_does_not_validate_or_callback_twice(self):
        h=Harness();r=make_adapter(h);event=threading.Event();event.set();seen=[]
        self.assertIsNone(r.request_unisdk(cancel_event=event,on_complete=seen.append))
        deadline=time.monotonic()+2
        while not seen and time.monotonic()<deadline:time.sleep(.005)
        self.assertEqual(seen,[None]);self.assertEqual(h.events,[])
    def test_failure_does_not_emit_stale_token(self):
        h=Harness(replies=[{'loginData':{'errorCode':7001}}, {}, {'loginData':{'errorCode':7002}}]);r=make_adapter(h)
        self.assertFalse(r.request_unisdk(interactive=False))
        self.assertNotIn('build_sauth',[e[0] for e in h.events])


class PrefetchTests(unittest.TestCase):
    def test_fresh_honor_cache_is_validated_at_startup_and_game_launch(self):
        tree=ast.parse((SRC/'mpay_db_sync.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='MpayDBSync')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_prepare_selected')
        ns=dict(Future=Future,time=time,threading=threading,getShortGameId=lambda x:x)
        exec(compile(ast.Module(body=[method],type_ignores=[]),'mpay_db_sync.py','exec'),ns)
        context=types.ModuleType('prefetch_context');context.prefetch_scope=lambda *a:nullcontext()
        for force_startup in (False,True):
            h=Harness();r=make_adapter(h);r.request_unisdk(interactive=False);h.events.clear()
            def clone(record):
                obj=copy.copy(record);obj.honorLogin=copy.copy(record.honorLogin)
                obj.unisdk_cache=copy.deepcopy(record.unisdk_cache);return obj
            sync=types.SimpleNamespace(_packet=lambda rec,game:rec.cached_unisdk(game),
                _renewal_state=lambda rec,game:(id(rec.unisdk_cache.get(game)),rec.unisdk_cache.get(game,{}).get('expires_at')),
                _clone=clone,lock=threading.Lock(),stopping=False,
                manager=types.SimpleNamespace(query_channel=lambda uuid:r,save_records=lambda:None),
                _record_error=lambda *a,**kw:self.fail('prefetch error'))
            with patch.dict(sys.modules,{'prefetch_context':context}):
                ns['_prepare_selected'](sync,{'h55':[r]},force_game_token=force_startup)
            self.assertEqual([e[0] for e in h.events],['configLogin','build_sauth'])
            self.assertIsInstance(r.cached_unisdk('h55'),dict)


class RegressionTests(unittest.TestCase):
    def test_failed_validation_invalidates_old_shared_cache(self):
        h=Harness();r=make_adapter(h);r.request_unisdk(interactive=False)
        self.assertIsInstance(r.cached_unisdk('h55'),dict)
        h.replies=[{'loginData':{'errorCode':7008}}]
        self.assertFalse(r.request_unisdk(interactive=False))
        self.assertIsNone(r.cached_unisdk('h55'));self.assertTrue(r.is_login_expired('h55'))
    def test_bad_saved_game_token_can_restore_from_st(self):
        for platform in ('windows','legacy'):
            for token in (['bad'], 'bad', {'openId':'x'}, {'token':'x'}, {'openId':5,'token':'x'}):
                h=Harness(platform,[{},session('fixture-new')]);h.login.unionToken=token
                self.assertTrue(h.login.refreshSession())
                self.assertEqual([e[0] for e in h.events],['config','silent_code','login'])
    def test_async_packet_request_returns_before_network_and_cancellation_prevents_ui(self):
        h=Harness();r=make_adapter(h);release=threading.Event();entered=threading.Event();cancel=threading.Event();seen=[];pending=[]
        r.request_user_login=lambda **kw:self.fail('cancelled request opened UI')
        def refresh():entered.set();release.wait(2);return False
        r._refresh_session=refresh
        r.request_unisdk.__func__.__globals__['app_state'].run_on_main_thread=pending.append
        start=time.monotonic();r.request_unisdk(on_complete=seen.append,cancel_event=cancel)
        self.assertLess(time.monotonic()-start,.2);self.assertTrue(entered.wait(1));cancel.set();release.set()
        deadline=time.monotonic()+2
        while not pending and time.monotonic()<deadline:time.sleep(.005)
        self.assertEqual(seen,[]);pending.pop(0)();self.assertEqual(seen,[None])
    def test_oauth_config_preparation_is_async_and_cancel_safe(self):
        h=Harness();release=threading.Event();entered=threading.Event();cancel=threading.Event();seen=[];pending=[]
        def config():entered.set();release.wait(2);return False
        h.login.gameConfig=config;h.ns['app_state'].run_on_main_thread=pending.append
        h.login._openOAuthLogin=lambda **kw:self.fail('cancelled OAuth opened a browser')
        start=time.monotonic();h.login.newOAuthLogin(on_complete=seen.append,cancelled=cancel.is_set)
        self.assertLess(time.monotonic()-start,.2);self.assertTrue(entered.wait(1));cancel.set();release.set()
        deadline=time.monotonic()+2
        while not pending and time.monotonic()<deadline:time.sleep(.005)
        pending.pop(0)();self.assertEqual(seen,[None])
    def test_late_refresh_does_not_overwrite_new_account(self):
        h=Harness();r=make_adapter(h);release=threading.Event();entered=threading.Event();seen=[];pending=[]
        original_post=h.post
        def post(url,**kw):
            entered.set();release.wait(2);return Response(session('fixture-old-late'))
        h.ns['requests'].post=post
        r.request_unisdk.__func__.__globals__['app_state'].run_on_main_thread=pending.append
        r.request_unisdk(interactive=False,on_complete=seen.append)
        self.assertTrue(entered.wait(1))
        r.unionToken={'openId':'fixture-other','token':'fixture-other-token'}
        r.honorLogin.unionToken=r.unionToken;r.accountSession={'serviceToken':'fixture-other-st'}
        r.honorLogin.accountSession=r.accountSession;r.last_login_time=time.time();r.name='fixture-other-name'
        release.set();deadline=time.monotonic()+2
        while not pending and time.monotonic()<deadline:time.sleep(.005)
        pending.pop(0)()
        self.assertEqual(seen,[None]);self.assertEqual(r.unionToken['token'],'fixture-other-token')
        self.assertEqual(r.honorLogin.unionToken,r.unionToken);self.assertEqual(r.name,'fixture-other-name')
        self.assertEqual(r.unisdk_cache,{})
    def test_async_success_adopts_verified_packet(self):
        h=Harness();r=make_adapter(h);seen=[];pending=[]
        r.request_unisdk.__func__.__globals__['app_state'].run_on_main_thread=pending.append
        r.request_unisdk(interactive=False,on_complete=seen.append)
        deadline=time.monotonic()+2
        while not pending and time.monotonic()<deadline:time.sleep(.005)
        self.assertEqual(r.unionToken['token'],'fixture-token');pending.pop(0)()
        self.assertEqual(len(seen),1);self.assertIsInstance(seen[0],dict)
        self.assertEqual(r.unionToken['token'],'fixture-renewed');self.assertIsInstance(r.cached_unisdk('h55'),dict)

    def test_successful_interactive_login_does_not_repeat_configlogin(self):
        h=Harness(replies=[{'loginData':{'errorCode':7002}},session('fixture-new'),RuntimeError('must-not-be-used-in-same-flow')])
        h.login.accountSession={};r=make_adapter(h);seen=[];pending=[]
        r.request_unisdk.__func__.__globals__['app_state'].run_on_main_thread=pending.append
        def user_login(on_complete=None):
            success=r.honorLogin._game_center_login('fixture-user-code')
            r.unionToken=r.honorLogin.unionToken
            on_complete(success)
        r.request_user_login=user_login
        cache_module=types.ModuleType('channel_cache');cache_module.credential_expires_at=lambda rec:rec.honorLogin.expiredTime
        with patch.dict(sys.modules,{'channel_cache':cache_module}):
            r.request_unisdk(interactive=True,on_complete=seen.append)
            deadline=time.monotonic()+2
            while not seen and time.monotonic()<deadline:
                while pending:pending.pop(0)()
                time.sleep(.005)
        self.assertEqual(len(seen),1);self.assertIsInstance(seen[0],dict)
        self.assertEqual([e[0] for e in h.events],['configLogin','login','build_sauth'])
        self.assertEqual(base64.b64decode(seen[0]['token']),b'fixture-new')
        self.assertFalse(r.request_unisdk(interactive=False))
        self.assertEqual(h.events[-1][0],'configLogin')
    def test_created_continues_fresh_login_without_revalidation(self):
        h=Harness(replies=[session('fixture-new')]);r=make_adapter(h)
        self.assertTrue(r.honorLogin._game_center_login('fixture-code'));r.unionToken=r.honorLogin.unionToken
        tree=ast.parse((SRC/'mpay_db_sync.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='MpayDBSync')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='created')
        ns={};exec(compile(ast.Module(body=[method],type_ignores=[]),'mpay_db_sync.py','exec'),ns)
        cache_module=types.ModuleType('channel_cache');cache_module.credential_expires_at=lambda rec:rec.honorLogin.expiredTime
        with patch.dict(sys.modules,{'channel_cache':cache_module}):
            self.assertTrue(ns['created'](types.SimpleNamespace(stopping=False),r))
        self.assertEqual([e[0] for e in h.events],['login','build_sauth'])
    def test_targeted_relogin_marks_only_current_success_as_ready(self):
        calls=[];record=types.SimpleNamespace(channel_name='honor_sdk',request_unisdk=lambda *a,**kw:calls.append(kw))
        app=types.SimpleNamespace(run_on_main_thread=lambda fn:fn())
        ns={'threading':threading,'uuid':uuid,'app_state':app}
        cls=load_file(SRC/'channel_relogin.py',ns,classes=('ExpiredChannelLogin',))['ExpiredChannelLogin']
        sync=types.SimpleNamespace(manager=types.SimpleNamespace(query_channel=lambda uid:record))
        record.uuid='fixture';attempt=cls(sync,record,'h55',lambda:None)
        attempt._logged_in(True)
        self.assertEqual(len(calls),1);self.assertTrue(calls[0]['_session_ready'])

    def projected_attempt(self,replies):
        h=Harness();r=make_adapter(h);r.request_unisdk(interactive=False);r.expire_unisdk('h55')
        h.events.clear();h.replies=list(replies);pending=[];toasts=[];opened=[];saved=[]
        r.report_login_failure=lambda *a,**kw:toasts.append('failed')
        def clone(record):
            obj=copy.copy(record);obj.honorLogin=copy.copy(record.honorLogin)
            obj.unisdk_cache=copy.deepcopy(record.unisdk_cache);return obj
        sync=types.SimpleNamespace(_clone=clone,stopping=False,
            _renewal_state=lambda rec,game:(id(rec.unisdk_cache.get(game)),rec.unisdk_cache.get(game,{}).get('expires_at')),
            refresh_record=lambda *a:saved.append('projected'),
            manager=types.SimpleNamespace(query_channel=lambda uid:r,save_records=lambda:saved.append('saved')))
        app=types.SimpleNamespace(channels_helper=sync.manager,run_on_main_thread=pending.append,
             toast=lambda *a,**kw:toasts.append(a[0]),ui_mgr=types.SimpleNamespace(open_for_game=lambda *a,**kw:opened.append(True)))
        ns={'threading':threading,'uuid':uuid,'app_state':app}
        cls=load_file(SRC/'channel_relogin.py',ns,classes=('ExpiredChannelLogin',))['ExpiredChannelLogin']
        attempt=cls(sync,r,'h55',lambda:None,try_silent=True)
        cache_module=types.ModuleType('channel_cache');cache_module.credential_expires_at=lambda rec:rec.honorLogin.expiredTime
        with patch.dict(sys.modules,{'channel_cache':cache_module}):
            attempt.start();deadline=time.monotonic()+2
            while not pending and time.monotonic()<deadline:time.sleep(.005)
            self.assertTrue(pending)
            pending.pop(0)()
        return h,r,attempt,opened,saved
    def test_projected_expiry_recovers_without_user_login(self):
        h,r,attempt,opened,saved=self.projected_attempt([session('fixture-new')])
        self.assertTrue(attempt.success);self.assertTrue(attempt.finished);self.assertEqual(opened,[])
        self.assertEqual(saved,['saved','projected']);self.assertEqual([e[0] for e in h.events],['configLogin','build_sauth'])
        self.assertEqual(r.unionToken['token'],'fixture-new')
    def test_projected_expiry_server_rejection_uses_st(self):
        h,r,attempt,opened,saved=self.projected_attempt([{'loginData':{'errorCode':7002}},{},session('fixture-new')])
        self.assertTrue(attempt.success);self.assertEqual(opened,[])
        self.assertEqual([e[0] for e in h.events],['configLogin','config','silent_code','login','build_sauth'])
    def test_projected_restriction_does_not_open_ui(self):
        h,r,attempt,opened,saved=self.projected_attempt([{'loginData':{'errorCode':7008}}])
        self.assertTrue(attempt.finished);self.assertFalse(attempt.success);self.assertEqual(opened,[]);self.assertEqual(saved,[])
    def test_projected_failed_recovery_opens_existing_targeted_ui(self):
        h,r,attempt,opened,saved=self.projected_attempt([{'loginData':{'errorCode':7002}},{},{'loginData':{'errorCode':7002}}])
        self.assertFalse(attempt.finished);self.assertEqual(opened,[True]);self.assertEqual(saved,[])
        self.assertEqual(attempt.status,'waiting');self.assertFalse(attempt.started)


if __name__=='__main__':unittest.main(verbosity=2)
