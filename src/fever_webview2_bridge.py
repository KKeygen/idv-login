# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

ORIGIN = 'https://idv-ui.invalid'
ENTRY = ORIGIN + '/index.html'
GAME_ORIGIN = 'https://goldrush.snakekiss.com'
MAX_BODY = 1024 * 1024
BASE_HEADERS = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                'Referrer-Policy': 'no-referrer'}


class RequestRejected(ValueError):
    def __init__(self, status=403, message='Forbidden'):
        self.status = status
        super().__init__(message)


def exact_origin(url, origin=ORIGIN):
    try:
        value = urlsplit(url)
        return (value.scheme + '://' + value.netloc == origin and not value.username
                and not value.password and value.port is None)
    except (TypeError, ValueError):
        return False


def trusted_document(url):
    return exact_origin(url) and urlsplit(url).path == '/index.html'


def navigation_allowed(page_id, url, is_frame):
    if page_id != 1:
        return False
    if is_frame:
        return exact_origin(url, GAME_ORIGIN)
    return trusted_document(url)


def request_headers(data):
    result = {}
    for key, value in data.get('header_items', []):
        key = str(key).lower()
        if key in result:
            raise RequestRejected()
        result[key] = str(value)
    return result


def authorize(data, document_url):
    url = data.get('url', '')
    if data.get('page_id') != 1 or not exact_origin(url):
        raise RequestRejected()
    p = urlsplit(url)
    if p.fragment or '%' in p.path or '\\' in p.path or len(url) > 16384:
        raise RequestRejected(400, 'Invalid request')
    headers = request_headers(data)
    method = data.get('method')
    dest = headers.get('sec-fetch-dest')
    if data.get('is_document_entry') is True:
        if (p.path != '/index.html' or method != 'GET'
                or data.get('resource_context') != 1):
            raise RequestRejected()
        return 'document', p.path, {}, None
    if not trusted_document(document_url):
        raise RequestRejected()
    if headers.get('origin', ORIGIN) != ORIGIN:
        raise RequestRejected()
    if data.get('resource_context') == 1 or dest in ('document', 'iframe', 'frame'):
        raise RequestRejected()
    if not p.path.startswith('/_idv-login/'):
        raise RequestRejected(404, 'Not found')
    name = p.path.removeprefix('/_idv-login/')
    if (data.get('resource_context') not in (7, 8)
            or headers.get('x-idv-ui') != 'fever-webview2'
            or method not in ('GET', 'POST')):
        raise RequestRejected()
    raw = data.get('body', b'')
    if not isinstance(raw, bytes) or len(raw) > MAX_BODY:
        raise RequestRejected(413, 'Payload too large')
    body = None
    if method == 'POST':
        if headers.get('content-type', '').split(';')[0].strip().lower() not in ('', 'application/json'):
            raise RequestRejected(415, 'JSON required')
        try:
            body = json.loads(raw) if raw else {}
        except (ValueError, UnicodeError):
            raise RequestRejected(400, 'Invalid JSON') from None
        if not isinstance(body, dict):
            raise RequestRejected(400, 'JSON object required')
    elif raw:
        raise RequestRejected(400, 'GET body forbidden')
    try:
        query = parse_qs(p.query, keep_blank_values=True, max_num_fields=64)
    except ValueError:
        raise RequestRejected(400, 'Invalid query') from None
    if any(len(values) != 1 for values in query.values()):
        raise RequestRejected(400, 'Duplicate query key')
    query = {key: values[0] for key, values in query.items()}
    if name == 'open-external-url':
        target = query.get('url', '')
        try:
            external = urlsplit(target)
            if (external.scheme not in ('http', 'https') or not external.hostname
                    or external.username is not None or external.password is not None
                    or any(ord(char) < 33 for char in target) or '\\' in target):
                raise ValueError
        except ValueError:
            raise RequestRejected(400, 'Invalid external URL') from None
    return 'api', p.path, query, body


def json_response(status, payload):
    return status, {**BASE_HEADERS, 'Content-Type': 'application/json; charset=utf-8'}, json.dumps(payload, ensure_ascii=False).encode()



def cloud_transport_script():
    return r"""(() => {
      if (window !== window.top || location.origin !== 'https://idv-ui.invalid'
          || location.pathname !== '/index.html') return;
      const nativeFetch = window.fetch.bind(window);
      window.fetch = (input, init) => {
        const request = new Request(input, init);
        const url = new URL(request.url);
        if (url.origin !== location.origin || !url.pathname.startsWith('/_idv-login/'))
          return nativeFetch(request);
        const headers = new Headers(request.headers);
        headers.set('X-IDV-UI', 'fever-webview2');
        return nativeFetch(new Request(request, {headers, mode: 'same-origin'}));
      };
      const api = async (path, body) => {
        const response = await fetch('/_idv-login/' + path, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body)
        });
        const readResult = async response => {
          const result = await response.json();
          if (!response.ok || (result.success === false && !result.cancelled))
            throw new Error('本地操作失败');
          return result;
        };
        let result = await readResult(response);
        if (result.status === 'pending') {
          const taskId = result.task_id;
          while (result.status === 'pending') {
            await new Promise(resolve => setTimeout(resolve, 200));
            result = await readResult(await fetch('/_idv-login/native/task-status?task_id='
              + encodeURIComponent(taskId)));
          }
        }
        return result;
      };
      if (navigator.clipboard)
        navigator.clipboard.writeText = text => api('webview2/copy-text', {text}).then(() => undefined);
      const anchorClick = HTMLAnchorElement.prototype.click;
      HTMLAnchorElement.prototype.click = function () {
        if (/^idv-cloud-master-key(?:-\d{4}-\d{2}-\d{2})?\.txt$/.test(this.download)
            && this.href.startsWith('blob:')) {
          nativeFetch(this.href).then(response => response.text())
            .then(text => api('webview2/save-text', {text, filename: this.download}))
            .catch(() => alert('保存失败，请重试'));
          return;
        }
        return anchorClick.call(this);
      };
      const openWindow = window.open.bind(window);
      window.open = (url, ...args) => {
        if (!/^https?:\/\//i.test(String(url))) return openWindow(url, ...args);
        const target = new URL(url);
        if (target.protocol === 'http:' || target.protocol === 'https:')
          fetch('/_idv-login/open-external-url?url=' + encodeURIComponent(target.href));
        return null;
      };
    })();"""
