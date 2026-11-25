"""Bounded retry and safe, compact diagnostics for read-only Chaoxing calls."""
import asyncio
import random
import socket
import ssl
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx


class ReadIssue(RuntimeError):
    def __init__(self, code, endpoint, status=None, page=None):
        super().__init__(code)
        self.code, self.endpoint, self.status, self.page = code, endpoint, status, page


LOGIN_HINTS = ('请重新登录', '请先登录', '登录已失效', '未登录', '登录过期')
KNOWN = {
    'material_folder_limit', 'material_response_unverified', 'material_pagination_not_advancing',
    'material_page_limit', 'notice_detail_unverified', 'untrusted_attachment_host',
    'non_public_attachment_address', 'too_many_source_redirects', 'too_many_attachment_redirects',
    'download_returned_html', 'attachment_too_large', 'attachment_archive_failed',
    'unsupported_attachment_reference', 'attachment_download_forbidden', 'download_api_rejected',
    'download_empty_body', 'download_size_mismatch', 'dns_lookup_failed', 'tls_handshake_error',
    'connection_failed', 'remote_protocol_error', 'resource_not_found',
}
TRANSIENT_CODES = {
    'rate_limited', 'network_error', 'network_timeout', 'dns_lookup_failed',
    'tls_handshake_error', 'connection_failed', 'remote_protocol_error', 'upstream_server_error',
}


def check_login(response, endpoint, data=None, page=None):
    host = response.url.host or ''
    redirected = host in {'passport2.chaoxing.com', 'passport2-api.chaoxing.com'} and 'login' in response.url.path.lower()
    messages = [data.get(k) for k in ('msg', 'message', 'errorMsg')] if isinstance(data, dict) else []
    expired = any(marker in msg for msg in messages if isinstance(msg, str) for marker in LOGIN_HINTS)
    if response.status_code == 401 or redirected or expired:
        raise ReadIssue('login_required', endpoint, response.status_code, page)


def diagnostic(exc, stage, endpoint):
    status = exc.status if isinstance(exc, ReadIssue) else (
        exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None)
    if isinstance(exc, ReadIssue):
        code, endpoint = exc.code, exc.endpoint
    elif status == 429:
        code = 'rate_limited'
    elif status == 403:
        code = 'access_denied'
    elif status in (404, 410):
        code = 'resource_not_found'
    elif status is not None and status >= 500:
        code = 'upstream_server_error'
    elif status is not None:
        code = 'upstream_client_error'
    elif isinstance(exc, httpx.TimeoutException):
        code = 'network_timeout'
    elif isinstance(exc, socket.gaierror):
        code = 'dns_lookup_failed'
    elif isinstance(exc, ssl.SSLError):
        code = 'tls_handshake_error'
    elif isinstance(exc, httpx.ConnectError):
        code = 'connection_failed'
    elif isinstance(exc, httpx.RemoteProtocolError):
        code = 'remote_protocol_error'
    elif isinstance(exc, httpx.TransportError):
        code = 'network_error'
    elif type(exc) is ValueError and exc.args and isinstance(exc.args[0], str) and exc.args[0] in KNOWN:
        code = exc.args[0]
    else:
        code = 'collector_error'

    action = ('scan_login_qr' if code == 'login_required' else
              'manual_check_in_app' if code == 'attachment_download_forbidden' else
              'retry_later' if code in TRANSIENT_CODES or (status or 0) >= 500 else
              'check_collector')
    result = {'stage': stage, 'endpoint': endpoint, 'code': code, 'action': action}
    if status is not None:
        result['http_status'] = status
    if isinstance(exc, ReadIssue) and exc.page is not None:
        result['page'] = exc.page
    return result


def retry_delay(value):
    if not value:
        return 0.5 + random.uniform(0, 0.2)
    try:
        return max(0, int(value))
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


async def read_request(client, method, url, endpoint, *, page=None, **kwargs):
    """Only call for read operations, including the notification-list POST."""
    for attempt in range(2):
        try:
            response = await client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError,
                socket.gaierror, ssl.SSLError) as exc:
            if attempt:
                raise ReadIssue(diagnostic(exc, '', endpoint)['code'], endpoint, page=page) from None
            await asyncio.sleep(retry_delay(None))
            continue
        check_login(response, endpoint, page=page)
        status = response.status_code
        if status in (429, 502, 503, 504) and not attempt:
            delay = retry_delay(response.headers.get('Retry-After'))
            if delay is not None and delay <= 2:
                await asyncio.sleep(delay)
                continue
        if status >= 400:
            code = ('rate_limited' if status == 429 else
                    'access_denied' if status == 403 else
                    'resource_not_found' if status in (404, 410) else
                    'upstream_server_error' if status >= 500 else
                    'upstream_client_error')
            raise ReadIssue(code, endpoint, status, page)
        return response


def json_response(response, endpoint, *, page=None, code='response_invalid'):
    check_login(response, endpoint, page=page)
    try:
        data = response.json()
    except ValueError:
        raise ReadIssue(code, endpoint, response.status_code, page) from None
    check_login(response, endpoint, data, page)
    if not isinstance(data, dict):
        raise ReadIssue(code, endpoint, response.status_code, page)
    return data
