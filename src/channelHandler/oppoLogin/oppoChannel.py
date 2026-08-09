import json
import time
from typing import Any, Dict, Optional

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtWebEngineWidgets import QWebEngineView

from channelHandler.WebLoginUtils import WebBrowser
from channelHandler.oppoLogin.consts import (
    DEFAULT_CONSTS,
    OPPO_UC_CLIENT_DOMAIN,
    OPPO_WEBVIEW_UA,
    OppoNativeConsts,
)
from channelHandler.oppoLogin.jsbridge import OPPO_CONSOLE_PREFIX, build_mock_native_js
from channelHandler.oppoOpenAccount.client import OppoSecureSession
from channelHandler.oppoOpenAccount.envinfo import build_env_param_minimal
from channelHandler.oppoOpenAccount.sign import sign_request
from logutil import setup_logger


class OppoBrowser(WebBrowser):
    def __init__(self, consts: OppoNativeConsts = DEFAULT_CONSTS):
        super().__init__("oppo", True, frameless=True)
        self.logger = setup_logger()
        self._captured: Optional[Dict[str, Any]] = None
        self._observed_popup_view: Optional[QWebEngineView] = None
        self._observed_popup_page = None
        self._pending_call_executor_callback_id: Optional[str] = None
        self._pending_call_executor_business_id: str = ""
        self.consts = consts

        # UA 必须在加载前设置
        self.set_user_agent(OPPO_WEBVIEW_UA)

        # 文档创建前注入 JSBridge mock
        self.add_init_script(build_mock_native_js(self.consts), name="oppo_mock_native")

    def verify(self, url: str) -> bool:
        # Oppo 登录不靠 URL 跳转判定成功，靠 JSBridge 回调。
        return False

    def _open_observed_webview(self, url: str):
        if not url:
            return

        try:
            if self._observed_popup_view is not None:
                self._observed_popup_view.close()
                self._observed_popup_view.deleteLater()
        except Exception:
            pass

        popup = QWebEngineView(self)
        popup.setWindowTitle("Oppo Verify")
        popup.resize(800, 900)

        popup_page = self.create_page(self.profile, popup)
        popup.setPage(popup_page)

        self._observed_popup_view = popup
        self._observed_popup_page = popup_page

        popup.load(QUrl(url))
        popup.show()

    def _trigger_main_page_refresh(self):
        try:
            self.page.runJavaScript("if(window.onRefresh){onRefresh()}")
            self.logger.info("已在主页面触发 onRefresh")
        except Exception as e:
            self.logger.debug(f"触发主页面 onRefresh 失败: {e}")

    def _schedule_main_page_refresh(self, delay_ms: int = 50):
        QTimer.singleShot(delay_ms, self._trigger_main_page_refresh)

    def _close_observed_popup(self):
        try:
            if self._observed_popup_view is not None:
                self._observed_popup_view.close()
                self._observed_popup_view.deleteLater()
        except Exception:
            pass
        self._observed_popup_view = None
        self._observed_popup_page = None

    def _finish_popup_verify(self):
        self._close_observed_popup()
        self._schedule_main_page_refresh()

    def _emit_main_page_callback(self, callback_id: str, payload: Dict[str, Any]):
        if not callback_id:
            return
        payload_str = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        callback_id_js = json.dumps(callback_id, ensure_ascii=False)
        payload_arg_js = json.dumps(payload_str, ensure_ascii=False)
        script = (
            "(function(){"
            "try{"
            "if(window.HeytapJsApi && typeof window.HeytapJsApi.callback==='function'){"
            f"window.HeytapJsApi.callback({callback_id_js}, {payload_arg_js});"
            "}"
            "}catch(e){}"
            "})();"
        )
        self.page.runJavaScript(script)

    def _build_verify_url_from_call_executor(self, param: Dict[str, Any]) -> str:
        try:
            session = OppoSecureSession(base_url="https://client-uc.heytapmobi.com/", consts=self.consts)

            original_build_common_headers = session._build_common_headers

            def _patched_headers() -> Dict[str, str]:
                h = original_build_common_headers()
                h["X-Sys-TalkBackState"] = "false"
                h["X-BusinessSystem"] = "other"
                h["X-Client-package"] = "com.heytap.htms"
                h["Ext-Mobile"] = "///1/CN"
                h["X-Client-DUID"] = self.consts.GUID
                return h

            session._build_common_headers = _patched_headers  # type: ignore[method-assign]

            # CallMethodExecutor 入参固定只消费这几个字段，其余按探索脚本默认行为填充。
            payload: Dict[str, Any] = {
                "mspBizK": param.get("bizk") or "",
                "mspBizSec": param.get("bizs") or "",
                "appId": param.get("appId") or "3574817",
                "ssoId": "",
                "businessId": param.get("businessId") or "",
                "deviceId": "",
                "userToken": "",
                "processToken": param.get("processToken") or "",
                "captchaCode": "",
                "envParam": build_env_param_minimal(self.consts),
                "isBiometricClear": True,
                "isLockScreenClear": False,
                "validateSdkVersion": "2.2.1",
                "duid": self.consts.GUID or "",
                "source": "app",
                "bizk": self.consts.BIZK,
                "timestamp": int(time.time() * 1000),
            }
            payload["sign"] = sign_request(payload)

            resp = session.post_json(
                "api/v2/business/authentication/auth",
                payload,
                allow_plain_fallback=True,
            )
            self.logger.debug(f"authentication/auth 响应: {resp}")
            if isinstance(resp, dict):
                data = resp.get("data")
                if not isinstance(data, dict):
                    data = resp.get("result")
                if isinstance(data, dict):
                    verification_url = data.get("verificationUrl") or ""
                    next_process_token = data.get("nextProcessToken") or ""
                    if verification_url:
                        self.logger.info("已通过 authentication/auth 获取 verificationUrl")
                        if next_process_token:
                            self.logger.debug(f"authentication/auth nextProcessToken={next_process_token}")
                        return verification_url
        except Exception as e:
            self.logger.warning(f"请求 authentication/auth 获取 verificationUrl 失败: {e}")

        raise RuntimeError("无法构建有效的 verificationUrl，无法进行后续登录校验。请加群反馈此问题并提供相关日志以便修复。")

    def _extract_verify_ticket(self, param: Any) -> str:
        if not isinstance(param, dict):
            return ""
        data = param.get("data")
        if not isinstance(data, dict):
            return ""
        ticket = data.get("ticket")
        if ticket is None:
            return ""
        return ticket

    def _is_popup_verify_success(self, param: Any) -> bool:
        if not isinstance(param, dict):
            return False
        code = param.get("code")
        if code is None:
            return False
        return code == "200"

    def cleanup(self):
        self._close_observed_popup()
        self._pending_call_executor_callback_id = None
        self._pending_call_executor_business_id = ""
        super().cleanup()

    def _is_verify_finish_payload(self, param: Any) -> bool:
        if not isinstance(param, dict):
            return False
        data = param.get("data")
        if not isinstance(data, dict):
            return False
        operate = data.get("operate")
        if not isinstance(operate, dict):
            return False
        if operate.get("operateType") != "loginVerify":
            return False
        if operate.get("operateSuccess") is not True:
            return False
        return data.get("needResult") is True

    def on_console_message(self, level, message: str, lineNumber: int, sourceID: str, page=None):
        if not isinstance(message, str):
            return
        if not message.startswith(OPPO_CONSOLE_PREFIX):
            return

        payload = message[len(OPPO_CONSOLE_PREFIX) :].strip()
        try:
            data = json.loads(payload)
        except Exception:
            self.logger.debug(f"Oppo console payload 解析失败: {payload}")
            return

        method = (data or {}).get("method")
        callback_id = (data or {}).get("callbackid")
        param = json.loads((data or {}).get("param"))
        #self.logger.debug(f"Oppo console method {method} 不关心，payload: {payload}")
        # 只关心网页登录完成回调（或 setToken 透传 loginResp）
        if method not in ("vip.onFinish", "accountExternalSdk.setToken","vip.makeToast","vip.openAndObserveWebview","account.CallMethodExecutor"):
            self.logger.debug(f"Oppo console method {method} 不关心，payload: {payload}")
            return
        if method == "vip.makeToast":
            content = ""
            if isinstance(param, dict):
                content = str(param.get("content", "") or "")
            self.logger.warning(f"网页提示： {content}")
            # 在浏览器所在 widget 上显示一个提示（持续 5 秒）
            if content:
                try:
                    self.show_toast(content, duration_ms=5000)
                except Exception:
                    pass
            return
        if method == "vip.openAndObserveWebview":
            content = ""
            if isinstance(param, dict):
                content = str(param.get("content", "") or "")
            self._open_observed_webview(content)
            return
        if method == "vip.onFinish" and page is self._observed_popup_page:
            ticket = self._extract_verify_ticket(param)
            if self._is_popup_verify_success(param) and ticket and self._pending_call_executor_callback_id:
                callback_payload = {
                    "code": 0,
                    "msg": "success!",
                    "data": {
                        "businessId": self._pending_call_executor_business_id,
                        "code": "VERIFY_RESULT_CODE_SUCCESS",
                        "msg": "success",
                        "requestCode": "",
                        "ticket": ticket,
                    },
                }
                self._emit_main_page_callback(self._pending_call_executor_callback_id, callback_payload)
                self.logger.info("子页面登录校验完成，已向主页面手动回调 CallMethodExecutor 结果")
                self._pending_call_executor_callback_id = None
                self._pending_call_executor_business_id = ""
                QTimer.singleShot(0, self._finish_popup_verify)
                return

            if self._is_verify_finish_payload(param):
                self.logger.info("子页面登录校验完成，准备关闭子页面并刷新主页面")
                QTimer.singleShot(0, self._finish_popup_verify)
            return
        if method == "account.CallMethodExecutor":
            if isinstance(param, dict):
                self._pending_call_executor_callback_id = callback_id or ""
                self._pending_call_executor_business_id = param.get("businessId") or ""
                verify_url = self._build_verify_url_from_call_executor(param)
                self.logger.info("收到 CallMethodExecutor，打开 OPPO 校验子页面")
                self._open_observed_webview(verify_url)
            else:
                self.logger.warning("CallMethodExecutor 参数异常，无法打开校验页面")
            return
        login_resp = None
        if isinstance(param, dict):
            login_resp = param.get("loginResp") or param

        if not isinstance(login_resp, dict):
            return

        self._captured = login_resp
        self.result = login_resp
        self.logger.info("已捕获 Oppo 网页登录 loginResp，准备退出浏览器")

        # 避免在 console 回调栈内直接 teardown
        QTimer.singleShot(0, self.cleanup)


class OppoLogin:
    def __init__(self, start_url: str = OPPO_UC_CLIENT_DOMAIN):
        self.logger = setup_logger()
        self.start_url = start_url
        self._active_browser: Optional[OppoBrowser] = None  # 异步模式下保持强引用

    def webLogin(self, consts: OppoNativeConsts = DEFAULT_CONSTS, on_complete=None) -> Optional[Dict[str, Any]]:
        browser = OppoBrowser(consts=consts)
        browser.set_url(self.start_url)
        resp = browser.run()

        if resp is None:
            # 异步模式：浏览器已显示，等待用户登录完成
            # 必须保持对 browser 的强引用，否则函数返回后局部变量被销毁，
            # 导致 profile 被释放而 WebEnginePage 仍在运行。
            self._active_browser = browser
            if on_complete is not None:
                def _on_async_done(b):
                    self._active_browser = None  # 登录完成后释放引用
                    try:
                        result = b.result
                        if isinstance(result, dict) and result:
                            on_complete(result)
                        else:
                            on_complete(None)
                    except Exception:
                        self.logger.exception("OPPO异步登录处理失败")
                        on_complete(None)
                browser._async_completion_callback = _on_async_done
            return None

        if isinstance(resp, dict) and resp:
            return resp
        return None
