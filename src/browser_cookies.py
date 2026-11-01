from __future__ import annotations

from dataclasses import dataclass
import math
import re
import time
from urllib.parse import urlsplit


class CookieExportError(ValueError):
    pass


_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def _url_parts(url):
    try:
        p = urlsplit(url)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username is not None or p.password is not None:
            raise ValueError
        host = p.hostname.lower().encode('idna').decode('ascii')
        port = p.port
        authority = '[' + host + ']' if ':' in host else host
        if port is not None and port != (443 if p.scheme == 'https' else 80):
            authority += ':' + str(port)
        return p.scheme, host, p.path or '/', p.scheme + '://' + authority
    except (ValueError, TypeError, AttributeError, UnicodeError):
        raise CookieExportError('Cookie export requires an HTTP(S) destination') from None


def normalize_cookie(raw):
    if not isinstance(raw, dict):
        raise CookieExportError('Invalid cookie record')
    name, value = raw.get('name'), raw.get('value')
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise CookieExportError('Invalid cookie name')
    if (not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 or ord(c) > 255 or c == ';' for c in value)):
        raise CookieExportError('Invalid cookie value encoding')
    domain, path = raw.get('domain'), raw.get('path', '/')
    if not isinstance(domain, str) or not domain or not isinstance(path, str) or not path.startswith('/'):
        raise CookieExportError('Invalid cookie scope metadata')
    try:
        expires = float(raw.get('expires', -1))
        if not math.isfinite(expires):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise CookieExportError('Invalid cookie expiry') from None
    for key in ('secure', 'httpOnly', 'session', 'partitionKeyOpaque'):
        if key in raw and type(raw[key]) is not bool:
            raise CookieExportError('Invalid cookie security metadata')
    out = dict(raw)
    out.update(name=name, value=value, domain=domain.lower(), path=path,
               expires=expires, session=raw.get('session', expires < 0),
               secure=raw.get('secure', False), httpOnly=raw.get('httpOnly', False))
    return out


def _applies(cookie, url, now):
    scheme, host, path, _ = _url_parts(url)
    domain, cp = cookie.get('domain'), cookie.get('path', '/')
    if not isinstance(domain, str) or not domain or not isinstance(cp, str) or not cp.startswith('/'):
        raise CookieExportError('Invalid cookie scope metadata')
    domain = domain.lower()
    matches = (host == domain[1:] or host.endswith(domain)) if domain.startswith('.') else host == domain
    if not matches or (cookie.get('secure', False) and scheme != 'https'):
        return False
    if not (path == cp or (path.startswith(cp) and (cp.endswith('/') or path[len(cp):].startswith('/')))):
        return False
    try:
        expires = float(cookie.get('expires', -1))
        if not math.isfinite(expires):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise CookieExportError('Invalid cookie expiry') from None
    return cookie.get('session', expires < 0) or expires > now


def _unpartitioned(cookie):
    return not cookie.get('partitionKeyOpaque') and not cookie.get('partitionKey')


def cookies_for_url(records, target_url):
    _url_parts(target_url)
    result = {}
    for raw in records:
        cookie = normalize_cookie(raw)
        if not _applies(cookie, target_url, time.time()):
            continue
        if not _unpartitioned(cookie):
            raise CookieExportError('A partitioned cookie cannot be used in a plain HTTP cookie dictionary')
        if cookie['name'] in result:
            raise CookieExportError('Ambiguous cookie domain or path; a new login is required')
        result[cookie['name']] = cookie['value']
    return result


def cookie_parameters(records):
    fields = ('name', 'value', 'domain', 'path', 'secure', 'httpOnly', 'sameSite',
              'priority', 'sourceScheme', 'sourcePort', 'partitionKey')
    result = []
    for raw in records:
        if not isinstance(raw, dict) or not isinstance(raw.get('name'), str) or not isinstance(raw.get('value'), str):
            raise CookieExportError('Invalid saved browser cookie')
        cookie = dict(raw)
        if cookie.get('partitionKeyOpaque'):
            continue
        if not cookie.get('session', cookie.get('expires', -1) < 0) and cookie.get('expires', -1) <= time.time():
            continue
        param = {key: cookie[key] for key in fields if key in cookie}
        if not cookie.get('session', cookie.get('expires', -1) < 0):
            param['expires'] = cookie['expires']
        result.append(param)
    return result


@dataclass(frozen=True)
class CookieExport:
    source_origins: tuple[str, ...]
    source_paths: tuple[str, ...]
    targets: tuple[str, ...] | None = None
    names: frozenset[str] | None = None
    observation_only: bool = False

    def matches(self, url):
        try:
            _, _, path, origin = _url_parts(url)
            return origin in self.source_origins and path in self.source_paths
        except CookieExportError:
            return False

    def select(self, rawcookies, source_url):
        if not self.matches(source_url) or not isinstance(rawcookies, list):
            raise CookieExportError('Cookie export source is not allowed')
        _, _, path, origin = _url_parts(source_url)
        targets = self.targets if self.targets is not None else (origin + path,)
        records = []
        for raw in rawcookies:
            if not isinstance(raw, dict):
                raise CookieExportError('Invalid cookie record')
            if self.names is not None and raw.get('name') not in self.names:
                continue
            if not any(_applies(raw, target, time.time()) for target in targets):
                continue
            cookie = normalize_cookie(raw)
            if not self.observation_only and not _unpartitioned(cookie):
                raise CookieExportError('Partitioned credentials cannot leave their browser partition')
            cookie.update(source_origin=origin, source_path=path)
            records.append(cookie)
        if not self.observation_only:
            for target in targets:
                cookies_for_url(records, target)
        return records
