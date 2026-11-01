from __future__ import annotations
import base64
import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass, field
import json
import mimetypes
import re
from pathlib import Path
import time
from urllib.parse import urlsplit, unquote
import weakref
from comtypes import COMObject, COMError
from PyQt6.QtCore import QTimer, Qt
import webview2_com_interfaces as I
from webview2_com_loader import EnvironmentOptions, load_loader, detect_runtime, take_string, check, E_FAIL

BODY_LIMIT=8*1024*1024
user32=C.WinDLL('user32',use_last_error=True)
user32.GetClientRect.argtypes=[W.HWND,C.POINTER(W.RECT)]
user32.GetClientRect.restype=W.BOOL
user32.IsWindow.argtypes=[W.HWND];user32.IsWindow.restype=W.BOOL


def owned(obj):
    return obj.QueryInterface(type(obj).__com_interface__) if obj else None


def text(obj, name):
    return take_string(getattr(obj,name)())


def json_value(value):
    if len(value)>24*1024*1024:check(0x800700df)
    return json.loads(value)


def stream_bytes(stream):
    if not stream: return b''
    parts=[];size=0
    while True:
        buffer=C.create_string_buffer(16384)
        count=stream.Read(buffer,len(buffer))
        block=buffer.raw[:count]
        size+=len(block)
        if size>BODY_LIMIT: check(0x800700df)
        parts.append(block)
        if len(block)<16384: return b''.join(parts)


def memory_stream(body=b''):
    sh=C.WinDLL('shlwapi')
    sh.SHCreateMemStream.argtypes=[C.c_void_p,W.UINT]
    sh.SHCreateMemStream.restype=C.POINTER(I.IStream)
    buffer=C.create_string_buffer(body) if body else None
    stream=sh.SHCreateMemStream(buffer,len(body))
    if not stream: check(0x8007000e)
    return stream


def headers_list(headers):
    it=headers.GetIterator(); result=[]
    while it.get_HasCurrentHeader():
        name,value=it.GetCurrentHeader();result.append([take_string(name),take_string(value)])
        if not it.MoveNext():break
    return result


_handler_types={}

def handler_type(interface):
    if interface not in _handler_types:
        def Invoke(self,this,*args):
            for arg in args:
                if arg and hasattr(type(arg), '__com_interface__'):
                    arg.AddRef()
            try:
                self.fn(*args)
            except Exception as error:
                self.failure(error)
            finally:
                if self.once:self.retire()
            return 0
        _handler_types[interface]=type('Handler_'+interface.__name__,(COMObject,),
                                     {'_com_interfaces_':[interface],'Invoke':Invoke})
    return _handler_types[interface]


@dataclass
class Page:
    id:int
    hwnd:int
    widget:object=None
    controller:object=None
    web:object=None
    web2:object=None
    web3:object=None
    web4:object=None
    web13:object=None
    web18:object=None
    ready:bool=False
    closed:bool=False
    navigation:int=0
    subscriptions:list=field(default_factory=list)
    frames:list=field(default_factory=list)
    popup:object=None
    deferral:object=None


class ComHost:
    def __init__(self,parent,folder,agent,script,notify):
        if not user32.IsWindow(parent):check(0x80070057)
        self.parent,self.folder,self.agent,self.script,self.notify=parent,folder,agent,script,notify
        self.pages={};self.next_page=2;self.callbacks={};self.next_callback=1
        self.resources={};self.next_resource=1;self.next_response=1
        self.mappings={};self.filters=[];self.environment=None;self.environment5=None
        self.environment_pending=False;self.controller_pending=0;self.controller_attempted=False
        self.closing=False;self.closed=False;self.browser_pid=0;self.exited=set()
        self.exit_subscription=None;self.deadline=None;self.close_warned=False
        self.timer=QTimer();self.timer.setInterval(1000);self.timer.timeout.connect(self.expire);self.timer.start()

    def emit(self,name,**data):
        if self.closed and name!='closed':return 0
        return self.notify(name,data)

    def error(self,operation,error=E_FAIL,page=1):
        hr=error if isinstance(error,int) else getattr(error,'hresult',E_FAIL)
        self.emit('error',page_id=page,operation=operation,hresult=C.c_int32(hr).value)

    def later(self,fn):
        def run():
            try:fn()
            except Exception as error:self.error('deferred_operation',error)
        QTimer.singleShot(0,run)

    def handler(self,name,fn,*,once=False,page=1):
        interface=getattr(I,name)
        obj=handler_type(interface)();obj.fn=fn;obj.once=once
        ref=weakref.ref(self);key=self.next_callback;self.next_callback+=1
        def failure(error):
            host=ref()
            if host:
                host.error('com_callback',error,page)
                host.later(lambda:host.close_page(page))
        def retire():
            host=ref()
            if host:host.later(lambda:host.callbacks.pop(key,None))
        obj.failure=failure;obj.retire=retire
        pointer=obj.QueryInterface(interface)
        self.callbacks[key]=(obj,pointer)
        return pointer

    def subscribe(self,source,event,interface,fn,subscriptions,page=1):
        cb=self.handler(interface,fn,page=page)
        token=getattr(source,'add_'+event)(cb)
        subscriptions.append((source,event,token,cb))

    def revoke(self,subscriptions):
        for source,event,token,cb in subscriptions:
            try:getattr(source,'remove_'+event)(token)
            except COMError:pass
        subscriptions.clear()

    def start(self):
        self.runtime,hr=detect_runtime();check(hr)
        self.options=EnvironmentOptions()
        self.options_pointer=self.options.QueryInterface(I.ICoreWebView2EnvironmentOptions)
        self.environment_pending=True
        def created(hr,environment):
            self.environment_pending=False
            if hr<0 or not environment:
                self.error('environment',hr if hr<0 else E_FAIL);self.maybe_closed();return
            self.environment=owned(environment)
            self.environment5=environment.QueryInterface(I.ICoreWebView2Environment5)
            subscriptions=[]
            def exited(sender,args):
                self.exited.add(args.get_BrowserProcessId())
                self.later(self.maybe_closed)
            self.subscribe(self.environment5,'BrowserProcessExited','ICoreWebView2BrowserProcessExitedEventHandler',exited,subscriptions)
            self.exit_subscription=subscriptions
            if self.closing:self.later(self.maybe_closed);return
            env7=environment.QueryInterface(I.ICoreWebView2Environment7)
            actual=text(env7,'get_UserDataFolder')
            if str(Path(actual).resolve()).casefold()!=str(Path(self.folder).resolve()).casefold():check(0x80070005)
            if text(environment,'get_BrowserVersionString')!=self.runtime:check(0x80070005)
            self.create_page(1,'')
        cb=self.handler('ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler',created,once=True)
        try:check(load_loader().CreateCoreWebView2EnvironmentWithOptions(None,self.folder,self.options_pointer,cb))
        except Exception:self.environment_pending=False;raise

    def allowed(self,page,url,frame=False):
        p=urlsplit(url)
        network=(p.scheme in ('http','https') and bool(p.hostname)) or url in ('about:blank','about:srcdoc')
        return bool(network and self.emit('navigation_check',page_id=page.id,url=url,is_frame=frame)==1)

    def navigation(self,page,args,frame=False):
        args.put_Cancel(True)
        if self.closing or page.closed:return
        url=text(args,'get_Uri');nav=args.get_NavigationId()
        allow=self.allowed(page,url,frame)
        if allow:args.put_Cancel(False)
        if not frame and allow:page.navigation=nav
        self.emit('navigation',page_id=page.id,url=url,is_frame=frame,allowed=allow,navigation_id=nav)

    def message(self,page,args,frame=False):
        self.emit('message',page_id=page.id,source=text(args,'get_Source'),
                  value=json_value(text(args,'get_WebMessageAsJson')),is_frame=frame)

    def track_frame(self,page,frame,nested=False):
        f=frame.QueryInterface(I.ICoreWebView2Frame2);f7=frame.QueryInterface(I.ICoreWebView2Frame7)
        subscriptions=[];page.frames.append(subscriptions)
        if nested:
            self.subscribe(f,'NavigationStarting','ICoreWebView2FrameNavigationStartingEventHandler',
                lambda s,a:self.navigation(page,a,True),subscriptions,page.id)
        self.subscribe(f,'WebMessageReceived','ICoreWebView2FrameWebMessageReceivedEventHandler',
            lambda s,a:self.message(page,a,True),subscriptions,page.id)
        self.subscribe(f7,'FrameCreated','ICoreWebView2FrameChildFrameCreatedEventHandler',
            lambda s,a:self.track_frame(page,a.get_Frame(),True),subscriptions,page.id)
        def destroyed(sender,args):
            def remove():
                self.revoke(subscriptions)
                page.frames[:]=[item for item in page.frames if item is not subscriptions]
            self.later(remove)
        self.subscribe(f,'Destroyed','ICoreWebView2FrameDestroyedEventHandler',destroyed,subscriptions,page.id)

    def configure(self,page):
        web=page.controller.get_CoreWebView2();page.web=web
        for n in (2,3,4,13,18):setattr(page,'web'+str(n),web.QueryInterface(getattr(I,'ICoreWebView2_'+str(n))))
        settings=web.get_Settings();settings2=settings.QueryInterface(I.ICoreWebView2Settings2)
        settings4=settings.QueryInterface(I.ICoreWebView2Settings4)
        settings.put_IsWebMessageEnabled(True);settings.put_AreHostObjectsAllowed(False)
        settings.put_AreDevToolsEnabled(False);settings.put_IsStatusBarEnabled(False)
        settings4.put_IsPasswordAutosaveEnabled(False);settings4.put_IsGeneralAutofillEnabled(False)
        if self.agent:settings2.put_UserAgent(self.agent)
        for name,folder in self.mappings.items():
            if not self.intercepted_host(name):
                page.web3.SetVirtualHostNameToFolderMapping(name,folder,2)
        def sub(source,event,interface,fn):self.subscribe(source,event,interface,fn,page.subscriptions,page.id)
        def focus(sender,args):
            args.put_Handled(not bool(page.widget))
            if not page.widget:self.emit('focus_requested',page_id=page.id,reason='previous' if args.get_Reason()==2 else 'next')
        sub(page.controller,'MoveFocusRequested','ICoreWebView2MoveFocusRequestedEventHandler',focus)
        sub(web,'NavigationStarting','ICoreWebView2NavigationStartingEventHandler',lambda s,a:self.navigation(page,a))
        sub(web,'FrameNavigationStarting','ICoreWebView2NavigationStartingEventHandler',lambda s,a:self.navigation(page,a,True))
        def external(sender,args):
            args.put_Cancel(True)
            self.emit('navigation',page_id=page.id,url=text(args,'get_Uri'),is_frame=False,allowed=False,kind='external',navigation_id=page.navigation)
        sub(page.web18,'LaunchingExternalUriScheme','ICoreWebView2LaunchingExternalUriSchemeEventHandler',external)
        sub(web,'SourceChanged','ICoreWebView2SourceChangedEventHandler',
            lambda s,a:self.emit('url',page_id=page.id,url=text(s,'get_Source'),navigation_id=page.navigation))
        sub(web,'NavigationCompleted','ICoreWebView2NavigationCompletedEventHandler',
            lambda s,a:self.emit('loaded',page_id=page.id,success=bool(a.get_IsSuccess()),navigation_id=a.get_NavigationId(),web_error=a.get_WebErrorStatus()))
        sub(web,'WebMessageReceived','ICoreWebView2WebMessageReceivedEventHandler',lambda s,a:self.message(page,a))
        sub(page.web4,'FrameCreated','ICoreWebView2FrameCreatedEventHandler',lambda s,a:self.track_frame(page,a.get_Frame()))
        def popup(sender,args):
            args.put_Handled(True);url=text(args,'get_Uri')
            if self.closing or not self.allowed(page,url):
                self.emit('navigation',page_id=page.id,url=url,is_frame=False,allowed=False,kind='popup',navigation_id=page.navigation);return
            pid=self.next_page;self.next_page+=1;self.create_page(pid,'',args)
        sub(web,'NewWindowRequested','ICoreWebView2NewWindowRequestedEventHandler',popup)
        sub(web,'WindowCloseRequested','ICoreWebView2WindowCloseRequestedEventHandler',lambda s,a:self.later(lambda:self.close_page(page.id)))
        def failed(sender,args):
            self.error('browser_process',page=page.id);self.later(lambda:self.close_page(page.id))
        sub(web,'ProcessFailed','ICoreWebView2ProcessFailedEventHandler',failed)
        sub(web,'PermissionRequested','ICoreWebView2PermissionRequestedEventHandler',lambda s,a:a.put_State(2))
        sub(page.web4,'DownloadStarting','ICoreWebView2DownloadStartingEventHandler',lambda s,a:a.put_Cancel(True))

    def create_page(self,pid,url,popup=None):
        if self.closing or self.closed:check(0x80004004)
        page=Page(pid,self.parent);self.pages[pid]=page
        if popup:page.popup=owned(popup);page.deferral=popup.GetDeferral()
        if pid!=1:
            from PyQt6.QtWidgets import QWidget
            host_ref=weakref.ref(self)
            class Popup(QWidget):
                def resizeEvent(widget,event):
                    host=host_ref()
                    if host:host.bounds(page)
                    super().resizeEvent(event)
                def closeEvent(widget,event):
                    if not page.closed:
                        event.ignore();host=host_ref()
                        if host:host.later(lambda:host.close_page(pid))
                    else:event.accept()
                def moveEvent(widget,event):
                    if page.controller:page.controller.NotifyParentWindowPositionChanged()
                    super().moveEvent(event)
            widget=Popup();widget.setWindowTitle('渠道登录');widget.resize(900,700)
            widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
            if popup:
                features=popup.get_WindowFeatures()
                if features.get_HasSize():widget.resize(max(320,min(1600,features.get_Width())),max(300,min(1200,features.get_Height())))
            page.widget=widget;page.hwnd=int(widget.winId())
        self.controller_attempted=True;self.controller_pending+=1
        def created(hr,controller):
            self.controller_pending-=1
            if controller:
                web=controller.get_CoreWebView2();self.browser_pid=web.get_BrowserProcessId()
            if self.closing or page.closed:
                if controller:controller.Close()
                self.later(self.maybe_closed);return
            if hr<0 or not controller:
                self.error('controller',hr if hr<0 else E_FAIL,pid);self.later(lambda:self.close_page(pid));return
            page.controller=owned(controller)
            try:self.configure(page);self.bounds(page)
            except Exception as error:
                self.error('configure',error,pid);self.later(lambda:self.close_page(pid));return
            def script_ready(hr,script_id):
                if page.closed or self.closing:return
                try:
                    check(hr)
                    if page.popup:page.popup.put_NewWindow(page.web)
                    self.network(page);page.ready=True
                    if page.deferral:page.deferral.Complete();page.deferral=None;page.popup=None
                    if page.widget:page.widget.show()
                    page.controller.put_IsVisible(True)
                    self.emit('ready' if pid==1 else 'page',page_id=pid,runtime_version=self.runtime)
                    if url:page.web.Navigate(url)
                except Exception as error:
                    self.error('document_setup',error,pid);self.later(lambda:self.close_page(pid))
            cb=self.handler('ICoreWebView2AddScriptToExecuteOnDocumentCreatedCompletedHandler',script_ready,once=True,page=pid)
            page.web.AddScriptToExecuteOnDocumentCreated(self.script,cb)
        cb=self.handler('ICoreWebView2CreateCoreWebView2ControllerCompletedHandler',created,once=True,page=pid)
        try:self.environment.CreateCoreWebView2Controller(page.hwnd,cb)
        except Exception:
            self.controller_pending-=1;self.close_page(pid);raise

    def intercepted_host(self,name):
        return any(urlsplit(pattern).hostname==name for pattern,_ in self.filters)

    def network(self,page):
        for name in self.mappings:
            if self.intercepted_host(name):
                page.web.AddWebResourceRequestedFilter('https://'+name+'/*',0)
        for pattern,document in self.filters:
            page.web.AddWebResourceRequestedFilter(pattern,0)
            if document:page.web.AddWebResourceRequestedFilter(pattern+'?*',1)
        if self.filters:self.subscribe(page.web,'WebResourceRequested','ICoreWebView2WebResourceRequestedEventHandler',
            lambda s,a:self.resource_request(page,a),page.subscriptions,page.id)
        self.subscribe(page.web2,'WebResourceResponseReceived','ICoreWebView2WebResourceResponseReceivedEventHandler',
            lambda s,a:self.response_received(page,a),page.subscriptions,page.id)

    def resource_request(self,page,args):
        rid=self.next_resource;self.next_resource+=1
        self.resources[rid]=(owned(args),args.GetDeferral(),time.monotonic()+30,page.id)
        try:
            request=args.get_Request();url=text(request,'get_Uri');method=text(request,'get_Method')
            if not any(re.fullmatch(re.escape(pattern).replace(r'\*','.*').replace(r'\?','.'), url)
                       or (document and url.split('?',1)[0]==pattern)
                       for pattern,document in self.filters):
                self.static_resource(rid,url,method);return
            headers=request.get_Headers();pairs=headers_list(headers);values={k.lower():v for k,v in pairs}
            parsed=urlsplit(url);origin=f'{parsed.scheme}://{parsed.netloc}'
            supplied=values.get('origin','');site=values.get('sec-fetch-site','');dest=values.get('sec-fetch-dest','')
            origin_ok=not supplied or supplied==origin
            context=args.get_ResourceContext();entry=url.split('?',1)[0].split('#',1)[0]
            document=(any(d and pattern==entry for pattern,d in self.filters)
                      and method=='GET' and context==1 and dest in ('','document'))
            allowed_sites=('', 'none', 'same-origin') if document else ('', 'same-origin')
            if method=='OPTIONS' or not origin_ok or site not in allowed_sites:
                self.respond(rid,403,'Forbidden','',b'');return
            body=stream_bytes(request.get_Content())
            self.emit('resource_request',page_id=page.id,request_id=rid,url=url,method=method,source_origin=supplied,
                      resource_context=context,is_document_entry=bool(document),headers=pairs,body_base64=base64.b64encode(body).decode())
        except Exception as error:
            self.respond(rid,400,'Bad Request','',b'');self.error('resource_request',error,page.id)

    def static_resource(self,rid,url,method):
        parsed=urlsplit(url);folder=self.mappings.get(parsed.hostname)
        if parsed.scheme!='https' or not folder:
            self.respond(rid,403,'Forbidden','',b'');return
        if method not in ('GET','HEAD'):
            self.respond(rid,405,'Method Not Allowed','Allow: GET, HEAD\r\n',b'');return
        root=Path(folder).resolve()
        relative=unquote(parsed.path).lstrip('/')
        if ':' in relative or '\\' in relative or '\0' in relative:
            self.respond(rid,403,'Forbidden','',b'');return
        try:
            path=(root/relative).resolve()
            if not path.is_relative_to(root):
                self.respond(rid,403,'Forbidden','',b'');return
            if not path.is_file():
                self.respond(rid,404,'Not Found','',b'');return
            with path.open('rb') as source:body=source.read(BODY_LIMIT+1)
            if len(body)>BODY_LIMIT:
                self.respond(rid,413,'Content Too Large','',b'');return
        except (OSError,ValueError):
            self.respond(rid,404,'Not Found','',b'');return
        mime=mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        headers=f'Content-Type: {mime}\r\nContent-Length: {len(body)}\r\nX-Content-Type-Options: nosniff\r\n'
        self.respond(rid,200,'OK',headers,body if method=='GET' else b'')

    def response_received(self,page,args):
        if self.closing or page.closed:return
        request=args.get_Request();response=args.get_Response();url=text(request,'get_Uri')
        rid=self.next_response;self.next_response+=1
        data=dict(page_id=page.id,url=url,status=response.get_StatusCode(),response_id=rid,observed_navigation_id=page.navigation)
        if self.emit('response_check',**data)!=1:return
        def done(hr,stream):
            if self.closed or page.closed:return
            body=b''
            if hr>=0:
                try:body=stream_bytes(stream)
                except Exception as error:hr=getattr(error,'hresult',E_FAIL)
            self.emit('response',**data,hresult=hr,body_base64=base64.b64encode(body).decode())
        cb=self.handler('ICoreWebView2WebResourceResponseViewGetContentCompletedHandler',done,once=True,page=page.id)
        response.GetContent(cb)

    def page(self,pid):
        page=self.pages.get(pid)
        if not page or page.closed or not page.ready:check(0x8000000a)
        return page

    def complete(self,rid,hr,value=None):
        self.emit('result',request_id=rid,hresult=hr,value=value)

    def evaluate(self,pid,script,rid):
        def done(hr,value):self.complete(rid,hr,json_value(value) if hr>=0 and value else None)
        self.page(pid).web.ExecuteScript(script,self.handler('ICoreWebView2ExecuteScriptCompletedHandler',done,once=True,page=pid))

    def devtools(self,pid,method,params,rid):
        def done(hr,value):self.complete(rid,hr,json_value(value) if hr>=0 and value else None)
        self.page(pid).web.CallDevToolsProtocolMethod(method,params,self.handler('ICoreWebView2CallDevToolsProtocolMethodCompletedHandler',done,once=True,page=pid))

    def subscribe_devtools(self,pid,event):
        page=self.page(pid);receiver=page.web.GetDevToolsProtocolEventReceiver(event)
        self.subscribe(receiver,'DevToolsProtocolEventReceived','ICoreWebView2DevToolsProtocolEventReceivedEventHandler',
            lambda s,a:self.emit('devtools',page_id=pid,event=event,params=json_value(text(a,'get_ParameterObjectAsJson'))),page.subscriptions,pid)

    def navigate(self,pid,url):self.page(pid).web.Navigate(url)
    def post_message(self,pid,value):self.page(pid).web.PostWebMessageAsJson(value)
    def stop(self,pid):self.page(pid).web.Stop()
    def new_page(self,url):
        self.page(1);pid=self.next_page;self.next_page+=1;self.create_page(pid,url);return pid
    def bounds(self,page):
        if page.controller and user32.IsWindow(page.hwnd):
            rect=W.RECT()
            if not user32.GetClientRect(page.hwnd,C.byref(rect)):raise C.WinError(C.get_last_error())
            page.controller.put_Bounds(rect)
    def resize(self,width,height):
        if 1 in self.pages:self.bounds(self.pages[1])
    def notify_position_changed(self):self.page(1).controller.NotifyParentWindowPositionChanged()
    def visible(self,value):self.page(1).controller.put_IsVisible(value)
    def focus(self,pid,reason):
        page=self.page(pid)
        if page.widget:page.widget.showNormal();page.widget.raise_();page.widget.activateWindow()
        page.controller.MoveFocus(reason)
    def clear_data(self,rid):
        profile=self.page(1).web13.get_Profile().QueryInterface(I.ICoreWebView2Profile2)
        profile.ClearBrowsingDataAll(self.handler('ICoreWebView2ClearBrowsingDataCompletedHandler',lambda hr:self.complete(rid,hr,True),once=True))
    def capture_preview(self,pid,rid):
        stream=memory_stream()
        def done(hr):
            result=None
            if hr>=0:
                stream.Seek(0,0);result=base64.b64encode(stream_bytes(stream)).decode()
            self.complete(rid,hr,result)
        self.page(pid).web.CapturePreview(0,stream,self.handler('ICoreWebView2CapturePreviewCompletedHandler',done,once=True,page=pid))
    def virtual_host(self,name,folder):self.mappings[name]=folder
    def resource_filter(self,pattern,document):self.filters.append((pattern,document))
    def respond(self,rid,status,reason,headers,body):
        item=self.resources.pop(rid,None)
        if item is None:check(0x80070490)
        args,deferral,deadline,pid=item
        try:args.put_Response(self.environment.CreateWebResourceResponse(memory_stream(body),status,reason,headers))
        except Exception:
            page=self.pages.get(pid)
            if page and page.controller:page.controller.Close()
            if page:self.later(lambda:self.close_page(pid))
            raise
        finally:deferral.Complete();self.emit('resource_completed',request_id=rid,page_id=pid)

    def dispose_page(self,page):
        if page.closed:return
        page.closed=True
        if page.deferral:page.deferral.Complete();page.deferral=None;page.popup=None
        for frames in page.frames:self.revoke(frames)
        page.frames.clear();self.revoke(page.subscriptions)
        if page.controller:page.controller.Close()
        for name in ('web18','web13','web4','web3','web2','web','controller'):setattr(page,name,None)
        if page.widget:page.widget.close();page.widget.deleteLater();page.widget=None

    def close_page(self,pid):
        page=self.pages.get(pid)
        if not page:return
        for rid,item in tuple(self.resources.items()):
            if item[3]==pid:self.respond(rid,410,'Gone','',b'')
        self.dispose_page(page);self.pages.pop(pid,None)
        self.emit('page_closed',page_id=pid)

    def close(self):
        if self.closing or self.closed:return
        self.closing=True;self.deadline=time.monotonic()+30
        for rid in tuple(self.resources):self.respond(rid,410,'Gone','',b'')
        for page in tuple(self.pages.values()):self.dispose_page(page)
        self.pages.clear();self.later(self.maybe_closed)

    def maybe_closed(self):
        if not self.closing or self.closed or self.environment_pending or self.controller_pending:return
        if self.controller_attempted and (not self.browser_pid or self.browser_pid not in self.exited):return
        if self.exit_subscription:self.revoke(self.exit_subscription)
        self.environment5=None;self.environment=None;self.options_pointer=None;self.options=None
        self.callbacks.clear();self.timer.stop();self.timer.deleteLater();self.closed=True
        self.emit('closed',safe_to_release=True)

    def expire(self):
        if self.closing and not self.closed and self.deadline and time.monotonic()>=self.deadline and not self.close_warned:
            self.close_warned=True;self.error('close_timeout',-2147023436)
        for rid,item in tuple(self.resources.items()):
            if time.monotonic()>=item[2]:self.respond(rid,504,'Gateway Timeout','',b'')
