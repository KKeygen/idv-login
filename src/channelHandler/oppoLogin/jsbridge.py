#仅供技术交流，请下载后24小时内删除，禁止商用！如有侵权请联系仓库维护者删除！谢谢！
import json
import time
from typing import Any, Dict, Optional

from PyQt6.QtCore import QTimer

from channelHandler.oppoLogin.consts import DEFAULT_CONSTS, OPPO_WEBVIEW_UA, OppoNativeConsts, build_vip_header_json
from channelHandler.oppoOpenAccount.client import OppoSecureSession
from channelHandler.oppoOpenAccount.envinfo import build_env_param_minimal
from channelHandler.oppoOpenAccount.sign import sign_request


OPPO_CONSOLE_PREFIX = "__OPPO_NATIVE_INVOKE__::"


def _js_string_literal(s: str) -> str:
    # 生成 JS 字符串字面量（双引号）
    return json.dumps(s, ensure_ascii=False)


def build_mock_native_js(consts: OppoNativeConsts = DEFAULT_CONSTS) -> str:
    """生成注入到 WebView 的 HeytapNativeApi mock。

    - 常量来自 Python（后续 idToken 请求也可复用）
    - 额外捕获 invoke('vip.onFinish', param, ...) 并通过 console 回传
    """

    vip_header_json = json.dumps(build_vip_header_json(consts), ensure_ascii=False, separators=(",", ":"))

    device_model = _js_string_literal(consts.DEVICE.model)
    device_rom_build = _js_string_literal(consts.DEVICE.rom_build_display)
    device_time_zone = _js_string_literal(consts.TIME_ZONE)
    device_locale = _js_string_literal(consts.LOCALE)
    device_language = _js_string_literal(consts.LANGUAGE)
    device_language_tag = _js_string_literal(consts.LANGUAGE_TAG)
    device_country = _js_string_literal(consts.COUNTRY)
    device_color_os_version = _js_string_literal(consts.COLOR_OS_VERSION)

    return f"""(function() {{
  if (window.HeytapNativeApi) return;

  const OPPO_CONSOLE_PREFIX = {_js_string_literal(OPPO_CONSOLE_PREFIX)};

  const PKG_ACCOUNT_SDK = {_js_string_literal(consts.PKG_ACCOUNT_SDK)};
  const PKG_HOST = {_js_string_literal(consts.PKG_HOST)};
  const DEVICE_ID = {_js_string_literal(consts.DEVICE_ID)};
  const GUID = {_js_string_literal(consts.GUID)};
  const APP_VERSION = {consts.APP_VERSION};
  const APP_VERSION_STR = {_js_string_literal(consts.APP_VERSION_STR)};
  const SSOID = {_js_string_literal(consts.SSOID)};
  const TOKEN = {_js_string_literal(consts.TOKEN)};
  const BIZK = {_js_string_literal(consts.BIZK)};
  const APP_ID = {_js_string_literal(consts.APP_ID)};
  const PKG_NAME_SIGN = {_js_string_literal(consts.PKG_NAME_SIGN)};

  const VIP_HEADER_JSON = {vip_header_json};

  function _emitInvoke(method, param, callbackid) {{
    try {{
      const payload = JSON.stringify({{
        method: method,
        param: param,
        callbackid: callbackid
      }});
      console.log(OPPO_CONSOLE_PREFIX + payload);
    }} catch (e) {{}}
  }}

  // ============ 与 mockNative.js 保持一致的返回映射 ============
  const methodResults = {{
    'accountExternalSdk.getSDKConfig': {{
      code: 0,
      msg: 'success!',
      data: {{
        bizk: BIZK,
        brand: 'other',
        business: PKG_HOST,
        country: 'CN',
        envInfo: JSON.stringify({{
          appId: APP_ID,
          deviceId: DEVICE_ID,
          envParam: "",
          pkgName: PKG_ACCOUNT_SDK,
          pkgNameSign: PKG_NAME_SIGN
        }})
      }}
    }},
    'vip.getClientContext': {{
      code: 0,
      msg: 'success!',
      data: {{
        ColorOsVersion: {device_color_os_version},
        GUID: GUID,
        appVersion: 0,
        buzRegion: '',
        deviceId: GUID,
        deviceRegion: {device_country},
        fromPackageName: PKG_HOST,
        isHTExp: false,
        language: {device_language},
        languageTag: {device_language_tag},
        locale: {device_locale},
        model: {device_model},
        openId: GUID,
        packagename: PKG_HOST,
        payApkVersionCode: 0,
        romBuildDisplay: {device_rom_build},
        timeZone: {device_time_zone}
      }}
    }},
    'account.getCurrentDomain': {{
      code: 0,
      msg: 'success!',
      data: {{ domain: 'https://uc-client-cn.heytapmobi.com' }}
    }},
    'vip.reportWebLog': {{ code: 0, msg: 'success!', data: {{}} }},
    'accountExternalSdk.getSupportThirdLoginTypes': {{
      code: 0,
      msg: 'success!',
      data: {{ loginTypes: '[]' }}
    }},
    'accountExternalSdk.isOpLogin': {{ code: 5999, msg: 'handleJsApi failed! exception', data: {{}} }},
    'account.getClientHeader': {{ code: 1, msg: 'unsupported operation!', data: {{}} }},
    'vip.getToken': {{
      code: 0,
      msg: 'success!',
      data: {{
        accountName: '',
        classifyByAge: '',
        country: '',
        ssoid: SSOID,
        ssoid_s: '',
        token: TOKEN,
        token_s: ''
      }}
    }},
    'vip.getHeaderJson': {{
      code: 0,
      msg: 'success!',
      data: VIP_HEADER_JSON
    }},
    'vip.setTitle': {{ code: 1, msg: 'unsupported operation!', data: {{}} }},
    'vip.setClientTitle': {{ code: 0, msg: 'success!', data: {{}} }}
  }};

  window.HeytapNativeApi = {{
    getNavBarType: function() {{ return 0; }},
    invoke: function(method, param, callbackid) {{
      // 关键：捕获网页登录完成回调
        _emitInvoke(method, param, callbackid);

      // These calls complete only when the native child returns a result.
      if (method === 'account.CallMethodExecutor' || method === 'vip.openAndObserveWebview') {{
        return true;
      }}


      setTimeout(function() {{
        let result = methodResults[method];
        if (!result) {{
          result = {{ code: -1, msg: 'unknown method', data: {{}} }};
        }}
        const resultStr = JSON.stringify(result);
        if (window.HeytapJsApi && typeof window.HeytapJsApi.callback === 'function' && callbackid !== undefined && callbackid !== null) {{
          try {{
            window.HeytapJsApi.callback(callbackid.toString(), resultStr);
          }} catch (e) {{}}
        }}
      }}, 0);
      return true;
    }}
  }};
}})();
"""


def configure_bridge(browser, consts: OppoNativeConsts) -> None:
    browser._captured = None
    browser._observed_popup_view = None
    browser._observed_popup_page = None
    browser._pending_observed_callback = None
    browser._pending_call_executor_callback_id = None
    browser._pending_call_executor_business_id = ""
    browser.consts = consts

    # Set the UA and inject the bridge before loading the first page.
    browser.set_user_agent(OPPO_WEBVIEW_UA)
    browser.add_init_script(build_mock_native_js(consts), name="oppo_mock_native")


def clear_bridge(browser) -> None:
    browser._close_observed_popup()
    browser._pending_call_executor_callback_id = None
    browser._pending_call_executor_business_id = ""


def handle_popup_closed(browser, page):
    if page is not None and page is browser._observed_popup_page:
        browser._observed_popup_view = None
        browser._observed_popup_page = None
        browser._pending_call_executor_callback_id = None
        browser._pending_call_executor_business_id = ""
        if browser._pending_observed_callback is not None:
            _emit_observed_result(browser, {})
        else:
            _schedule_main_page_refresh(browser)


def _trigger_main_page_refresh(browser):
    try:
        browser.page.runJavaScript("if(window.onRefresh){onRefresh()}")
        browser.logger.info("已在主页面触发 onRefresh")
    except Exception as e:
        browser.logger.debug(f"触发主页面 onRefresh 失败: {type(e).__name__}")


def _schedule_main_page_refresh(browser, delay_ms: int = 50):
    QTimer.singleShot(delay_ms, lambda: _trigger_main_page_refresh(browser))


def _finish_popup_verify(browser):
    browser._close_observed_popup()
    _schedule_main_page_refresh(browser)


def _emit_observed_result(browser, param: Any):
    pending = browser._pending_observed_callback
    browser._pending_observed_callback = None
    if pending is not None:
        source_page, callback_id = pending
        _emit_main_page_callback(browser, callback_id, {
            "code": 0,
            "msg": "success!",
            "data": param if isinstance(param, dict) else {},
        }, page=source_page)


def _finish_observed_popup(browser, page, param: Any):
    if page is not browser._observed_popup_page:
        return
    # Native openAndObserveWebview returns the full onFinish object to its
    # original caller before popping the child; it is not a refresh signal.
    _emit_observed_result(browser, param)
    if page is browser._observed_popup_page:
        browser._close_observed_popup()


def _emit_main_page_callback(browser, callback_id: str, payload: Dict[str, Any], page=None):
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
    target_page = browser.page if page is None else page
    target_page.runJavaScript(script)


def _build_verify_url_from_call_executor(browser, param: Dict[str, Any]) -> str:
    session = OppoSecureSession(base_url="https://client-uc.heytapmobi.com/", consts=browser.consts)

    original_build_common_headers = session._build_common_headers

    def _patched_headers() -> Dict[str, str]:
        h = original_build_common_headers()
        h["X-Sys-TalkBackState"] = "false"
        h["X-BusinessSystem"] = "other"
        h["X-Client-package"] = "com.heytap.htms"
        h["Ext-Mobile"] = "///1/CN"
        h["X-Client-DUID"] = browser.consts.GUID
        return h

    session._build_common_headers = _patched_headers  # type: ignore[method-assign]

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
        "envParam": build_env_param_minimal(browser.consts),
        "isBiometricClear": True,
        "isLockScreenClear": False,
        "validateSdkVersion": "2.2.1",
        "duid": browser.consts.GUID or "",
        "source": "app",
        "bizk": browser.consts.BIZK,
        "timestamp": int(time.time() * 1000),
    }
    payload["sign"] = sign_request(payload)

    resp = session.post_json(
        "api/v2/business/authentication/auth",
        payload,
        allow_plain_fallback=True,
    )
    browser.logger.debug("authentication/auth 已响应")
    if isinstance(resp, dict):
        data = resp.get("data")
        if not isinstance(data, dict):
            data = resp.get("result")
        if isinstance(data, dict):
            verification_url = data.get("verificationUrl") or ""
            next_process_token = data.get("nextProcessToken") or ""
            if verification_url:
                browser.logger.info("已通过 authentication/auth 获取 verificationUrl")
                if next_process_token:
                    browser.logger.debug("authentication/auth 已提供 nextProcessToken")
                return verification_url

    raise RuntimeError("无法构建有效的 verificationUrl，无法进行后续登录校验。请加群反馈此问题并提供相关日志以便修复。")


def _extract_verify_ticket(param: Any) -> str:
    if not isinstance(param, dict):
        return ""
    data = param.get("data")
    if not isinstance(data, dict):
        return ""
    ticket = data.get("ticket")
    if ticket is None:
        return ""
    return ticket


def _is_popup_verify_success(param: Any) -> bool:
    if not isinstance(param, dict):
        return False
    code = param.get("code")
    if code is None:
        return False
    return code == "200"


def _is_verify_finish_payload(param: Any) -> bool:
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


def handle_console_message(browser, level, message: str, lineNumber: int, sourceID: str, page=None):
    if not isinstance(message, str):
        return
    if not message.startswith(OPPO_CONSOLE_PREFIX):
        return

    payload = message[len(OPPO_CONSOLE_PREFIX) :].strip()
    try:
        data = json.loads(payload)
    except Exception:
        browser.logger.debug("Oppo console payload 解析失败")
        return

    method = (data or {}).get("method")
    callback_id = (data or {}).get("callbackid")
    param = json.loads((data or {}).get("param"))
    # 只关心网页登录完成回调（或 setToken 透传 loginResp）
    if method not in ("vip.onFinish", "accountExternalSdk.setToken","vip.makeToast","vip.openAndObserveWebview","account.CallMethodExecutor"):
        browser.logger.debug("Oppo console method {} 未处理", method)
        return
    if method == "vip.makeToast":
        content = ""
        if isinstance(param, dict):
            content = str(param.get("content", "") or "")
        browser.logger.warning("OPPO 网页显示提示")
        # 在浏览器所在 widget 上显示一个提示（持续 5 秒）
        if content:
            try:
                browser.show_toast(content, duration_ms=5000)
            except Exception:
                pass
        return
    if method == "vip.openAndObserveWebview":
        content = ""
        if isinstance(param, dict):
            content = str(param.get("content", "") or "")
        callback_id = str(callback_id) if callback_id is not None else None
        if not content:
            _emit_main_page_callback(browser, callback_id, {
                "code": 5999,
                "msg": "apiObject param content is null or empty" if isinstance(param, dict) else "apiObject is null",
                "data": {},
            }, page=page)
            return
        browser._open_observed_webview(content)
        browser._pending_observed_callback = (
            browser.page if page is None else page, callback_id,
        )
        return
    if method == "vip.onFinish" and page is browser._observed_popup_page:
        if browser._pending_observed_callback is not None:
            QTimer.singleShot(0, lambda: _finish_observed_popup(browser, page, param))
            return
        ticket = _extract_verify_ticket(param)
        if _is_popup_verify_success(param) and ticket and browser._pending_call_executor_callback_id:
            callback_payload = {
                "code": 0,
                "msg": "success!",
                "data": {
                    "businessId": browser._pending_call_executor_business_id,
                    "code": "VERIFY_RESULT_CODE_SUCCESS",
                    "msg": "success",
                    "requestCode": "",
                    "ticket": ticket,
                },
            }
            _emit_main_page_callback(browser, browser._pending_call_executor_callback_id, callback_payload)
            browser.logger.info("子页面登录校验完成，已向主页面手动回调 CallMethodExecutor 结果")
            browser._pending_call_executor_callback_id = None
            browser._pending_call_executor_business_id = ""
            QTimer.singleShot(0, lambda: _finish_popup_verify(browser))
            return

        if _is_verify_finish_payload(param):
            browser.logger.info("子页面登录校验完成，准备关闭子页面并刷新主页面")
            QTimer.singleShot(0, lambda: _finish_popup_verify(browser))
        return
    if method == "account.CallMethodExecutor":
        if isinstance(param, dict):
            browser._pending_call_executor_callback_id = callback_id or ""
            browser._pending_call_executor_business_id = param.get("businessId") or ""
            try:
                verify_url = _build_verify_url_from_call_executor(browser, param)
            except Exception as error:
                browser.result = error
                QTimer.singleShot(0, browser.cleanup)
                return
            browser.logger.info("收到 CallMethodExecutor，打开 OPPO 校验子页面")
            browser._open_observed_webview(verify_url)
        else:
            browser.logger.warning("CallMethodExecutor 参数异常，无法打开校验页面")
        return
    login_resp = None
    if isinstance(param, dict):
        login_resp = param.get("loginResp") or param

    if not isinstance(login_resp, dict):
        return

    browser._captured = login_resp
    browser.result = login_resp
    browser.logger.info("已捕获 Oppo 网页登录 loginResp，准备退出浏览器")

    # 避免在 console 回调栈内直接 teardown
    QTimer.singleShot(0, browser.cleanup)


def run_web_login(login, browser, on_complete=None) -> Optional[Dict[str, Any]]:
    browser.set_url(login.start_url)
    resp = browser.run()

    if resp is None:
        # 异步模式下持有浏览器，直到完成回调释放引用。
        login._active_browser = browser
        if on_complete is not None:
            def _on_async_done(b):
                login._active_browser = None  # 登录完成后释放引用
                try:
                    result = b.result
                    if not isinstance(result, Exception) and not (isinstance(result, dict) and result):
                        result = None if result is None or result == "" else False
                except Exception as error:
                    result = error
                on_complete(result)
            browser._async_completion_callback = _on_async_done
        return None

    if isinstance(resp, Exception):
        if on_complete is None:
            raise resp
        result = resp
    else:
        result = resp if isinstance(resp, dict) and resp else (None if resp is None or resp == "" else False)
    if on_complete is not None:
        on_complete(result)
    return result
