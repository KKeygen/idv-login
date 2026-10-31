#仅供技术交流，请下载后24小时内删除，禁止商用！如有侵权请联系仓库维护者删除！谢谢！
import json
from typing import Any, Dict

from channelHandler.oppoLogin.consts import DEFAULT_CONSTS, OppoNativeConsts, build_vip_header_json


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
    device_brand = _js_string_literal(consts.DEVICE.brand)
    device_rpname = _js_string_literal(consts.DEVICE.rpname)
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
