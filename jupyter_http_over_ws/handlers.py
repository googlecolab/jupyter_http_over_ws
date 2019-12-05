# Copyright 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Jupyter server extension which supports HTTP-over-websocket communication."""

import base64
import collections
import json

from distutils import version
from enum import Enum

from notebook.base import handlers
from six.moves import http_cookies
from six.moves import urllib_parse as urlparse
from tornado import gen
from tornado import httpclient
from tornado import httputil
from tornado import websocket

# LINT.IfChange(handler_version)
HANDLER_VERSION = version.StrictVersion('0.0.7')
# LINT.ThenChange(pkg_files/setup.py:handler_version)

ExtensionVersionResult = collections.namedtuple('ExtensionVersionResult', [
    'error_reason',
    'requested_extension_version',
])


class ExtensionValidationError(Enum):
  UNPARSEABLE_REQUESTED_VERSION = 1
  OUTDATED_VERSION = 2


_PROTOCOL_MAP = {
    'http': 'ws',
    'https': 'wss',
}

_MIN_VERSION_QUERY_PARAM = 'min_version'
_AUTH_URL_QUERY_PARAM = 'jupyter_http_over_ws_auth_url'


class _WebSocketHandlerBase(websocket.WebSocketHandler,
                            handlers.IPythonHandler):
  """WebSocket handler to reuse IPythonHandler's authentication mechanisms."""

  def __init__(self, *args, **kwargs):
    websocket.WebSocketHandler.__init__(self, *args, **kwargs)
    handlers.IPythonHandler.__init__(self, *args, **kwargs)

    ca_certs = None
    if self.request.protocol == 'https':
      ca_certs = self.config.get('NotebookApp', {}).get('certfile')
      if not ca_certs:
        raise ValueError('HTTPS requires the NotebookApp.certfile setting to '
                         'be present.')
    self.ca_certs = ca_certs

  def check_origin(self, origin):
    # The IPythonHandler implementation of check_origin uses the
    # NotebookApp.allow_origin setting.
    return handlers.IPythonHandler.check_origin(self, origin)


class HttpOverWebSocketDiagnosticHandler(_WebSocketHandlerBase):
  """Socket handler that provides connection diagnostics."""

  PATH = '/http_over_websocket/diagnose'

  def on_message(self, message):
    extension_version_result = _validate_min_version(
        self.get_argument(_MIN_VERSION_QUERY_PARAM, None))

    self.write_message(
        json.dumps({
            'message_id':
                message,
            'extension_version':
                str(HANDLER_VERSION),
            'has_authentication_cookie':
                bool(self.get_cookie('_xsrf')),
            'is_outdated_extension': (
                extension_version_result.error_reason ==
                ExtensionValidationError.OUTDATED_VERSION),
        }))


def _validate_min_version(min_version):
  """Validates the extension version matches the requested version.

  Args:
    min_version: Minimum version passed as a query param when establishing the
      connection.

  Returns:
    An ExtensionVersionResult indicating validation status. If there is a
    problem, the error_reason field will be non-empty.
  """
  if min_version is not None:
    try:
      parsed_min_version = version.StrictVersion(min_version)
    except ValueError:
      return ExtensionVersionResult(
          error_reason=ExtensionValidationError.UNPARSEABLE_REQUESTED_VERSION,
          requested_extension_version=min_version)

    if parsed_min_version > HANDLER_VERSION:
      return ExtensionVersionResult(
          error_reason=ExtensionValidationError.OUTDATED_VERSION,
          requested_extension_version=str(parsed_min_version))

  return ExtensionVersionResult(
      error_reason=None, requested_extension_version=min_version)


class HttpOverWebSocketHandler(_WebSocketHandlerBase):
  """Socket handler that forwards requests via HTTP to the notebook server."""

  PATH = '/http_over_websocket'

  _REQUIRED_KEYS = {'path', 'method', 'message_id'}

  _REQUIRE_XSRF_FORWARDING_METHODS = {'DELETE', 'PATCH', 'POST', 'PUT'}

  def _write_error(self, msg, status=500, message_id=None):
    response = {
        'done': True,
        'status': status,
        'statusText': msg,
    }
    if message_id:
      response['message_id'] = message_id

    self.write_message(json.dumps(response))

  def open(self):
    min_version = self.get_argument(_MIN_VERSION_QUERY_PARAM, None)
    result = _validate_min_version(min_version)
    if (result.error_reason ==
        ExtensionValidationError.UNPARSEABLE_REQUESTED_VERSION):
      self.close(
          code=400,
          reason='Invalid "min_version" provided: {}'.format(min_version))
      return
    elif result.error_reason == ExtensionValidationError.OUTDATED_VERSION:
      reason = ('Requested version ({}) > Current version ({}). Please '
                'upgrade this package.').format(
                    result.requested_extension_version, HANDLER_VERSION)
      self.log.error('Rejecting connection: %s', reason)
      self.close(code=400, reason=reason)
      return

  def _get_http_client(self):
    """Test hook to allow a different HTTPClient implementation."""
    return httpclient.AsyncHTTPClient()

  @gen.coroutine
  def _attach_auth_cookies(self):
    auth_url = self.get_argument(_AUTH_URL_QUERY_PARAM, default='')
    if not auth_url:
      raise gen.Return()

    parsed_auth_url = urlparse.urlparse(auth_url)

    try:
      _validate_same_domain(self.request, parsed_auth_url)
      extra_cookies = yield _perform_request_and_extract_cookies(
          parsed_auth_url, self.ca_certs, self._get_http_client())
    except Exception:  # pylint:disable=broad-except
      self.log.exception('Uncaught error when proxying request')
      raise

    self.request.headers.update(extra_cookies)

  def _get_xsrf_cookie(self):
    """Parses the headers and extracts the _xsrf cookie value if it exists."""
    # Why not use Tornado's existing get_cookie method?
    # When the "jupyter_http_over_ws_auth_url" query param is set, handlers make
    # a proxied request and set the XSRF cookie based on the result. The
    # get_cookie method uses the headers processed as part of the initial
    # request and is thus insufficient.
    for cookie_header in self.request.headers.get_list('Cookie'):
      try:
        cookie_vals = httputil.parse_cookie(cookie_header)
        if '_xsrf' in cookie_vals:
          return cookie_vals['_xsrf']
      except Exception:  # pylint:disable=broad-except
        # Malformed cookie header, return empty.
        pass

    return None

  @gen.coroutine
  def on_message(self, message):
    try:
      contents = json.loads(message)
    except ValueError:
      self.log.debug('Bad JSON: %r', message)
      self.log.error("Couldn't parse JSON", exc_info=True)
      self._write_error(status=400, msg='JSON input is required.')
      raise gen.Return()

    if not set(contents.keys()).issuperset(self._REQUIRED_KEYS):
      msg = ('Invalid request. The body must contain the following keys: '
             '{required}. Got: {got}').format(
                 required=self._REQUIRED_KEYS, got=contents.keys())
      self._write_error(
          status=400, msg=msg, message_id=contents.get('message_id'))
      raise gen.Return()

    message_id = contents['message_id']
    try:
      yield self._attach_auth_cookies()
    except Exception:  # pylint:disable=broad-except
      self.log.error("Couldn't attach auth cookies")
      self._on_unhandled_exception(message_id)
      raise gen.Return()

    method = str(contents['method']).upper()
    query = ''
    if method in self._REQUIRE_XSRF_FORWARDING_METHODS:
      xsrf = self._get_xsrf_cookie()
      if xsrf:
        query += '_xsrf={}'.format(xsrf)

    path = urlparse.urlunsplit(  # pylint:disable=too-many-function-args
        urlparse.SplitResult(
            scheme=self.request.protocol,
            netloc=self.request.host,
            path=contents['path'],
            query=query,
            fragment=''))

    body = None
    if contents.get('body_base64'):
      body = base64.b64decode(contents.get('body_base64')).decode('utf-8')
    else:
      body = contents.get('body')

    emitter = _StreamingResponseEmitter(message_id, self.write_message)
    proxy_request = httpclient.HTTPRequest(
        url=path,
        method=method,
        headers=self.request.headers,
        body=body,
        ca_certs=self.ca_certs,
        header_callback=emitter.header_callback,
        streaming_callback=emitter.streaming_callback,
        allow_nonstandard_methods=True)
    _modify_proxy_request_test_only(proxy_request)

    http_client = self._get_http_client()
    # Since this channel represents a proxy, don't raise errors directly and
    # instead send them back in the response.
    # The response contents will normally be captured by
    # _StreamingResponseEmitter. However, if a programming error occurs with how
    # the proxy is set up, these callbacks will not be used.
    response = yield http_client.fetch(proxy_request, raise_error=False)
    if response.error and not isinstance(response.error, httpclient.HTTPError):
      try:
        response.rethrow()
      except Exception:  # pylint:disable=broad-except
        # Rethrow the exception to capture the stack trace and write
        # an error message.
        self.log.exception('Uncaught error when proxying request')

      self._on_unhandled_exception(message_id)
      raise gen.Return()

    emitter.done()

  def _on_unhandled_exception(self, message_id):
    self._write_error(
        status=500,
        msg=('Uncaught server-side exception. Check logs for '
             'additional details.'),
        message_id=message_id)


class _StreamingResponseEmitter(object):
  """Helper to support streaming responses.

  Tornado's HTTPRequest object allows callbacks to be installed upon receipt of
  headers as well as each body chunk. This provides hooks for these two
  callbacks and emits JSON messages on the websocket channel for each chunk. The
  final response in the stream will set the "done" field to True.

  Note: The caller is responsible for ensuring the done() method is called,
  which sends the last chunk of data and sets the "done" field to True.
  """

  def __init__(self, message_id, write_message_func):
    self._message_id = message_id
    self._write_message_func = write_message_func
    self._response_status = None
    self._response_headers = httputil.HTTPHeaders()
    self._last_response = None

  def header_callback(self, headers):
    # The header callback will be called multiple times, once for the initial
    # HTTP status line and once for each received header.
    header_lines = headers
    if self._response_status is None:
      status_line, _, header_lines = headers.partition('\r\n')
      self._response_status = httputil.parse_response_start_line(status_line)

    self._response_headers.update(httputil.HTTPHeaders.parse(header_lines))

  def streaming_callback(self, body_part):
    """Handles a streaming chunk of the response.

    The streaming_response callback gives no indication about whether the
    received chunk is the last in the stream. The "last_response" instance
    variable allows us to keep track of the last received chunk of the
    response. Each time this is called, the previous chunk is emitted. The
    done() method is expected to be called after the response completes to
    ensure that the last piece of data is sent.

    Args:
      body_part: A chunk of the streaming response.
    """
    b64_body_string = base64.b64encode(body_part).decode('utf-8')

    response = {
        'message_id': self._message_id,
        'data': b64_body_string,
    }
    if self._last_response is None:
      # This represents the first chunk of data to be streamed to the caller.
      # Attach status and header information to this item.
      response.update(self._generate_metadata_body())
    else:
      self._last_response['done'] = False
      self._write_message_func(self._last_response)

    self._last_response = response

  def done(self):
    response = self._last_response
    if response is None:
      response = {'message_id': self._message_id}
      response.update(self._generate_metadata_body())

    response['done'] = True
    self._write_message_func(response)

  def _generate_metadata_body(self):
    # The write_message method expects an object that is JSON serializable and
    # Tornado's HTTPHeaders class does not support this. Make a copy of the
    # headers.
    headers = dict(self._response_headers)

    return {
        'status': self._response_status.code,
        'statusText': self._response_status.reason,
        'headers': headers,
    }


class ProxiedSocketHandler(_WebSocketHandlerBase):
  """Socket handler that proxies a websocket connection.

  This creates a remote websocket connection and does bidirectional forwarding
  of messages to the remote URL. This intermediary allows attaching additional
  headers and/or cookies to the proxied connection if desired.

  Note: There are two WebSockets being referenced in this implementation. The
  "server WS" refers to the WebSocket serviced by this handler and connected to
  from a browser. The "proxied WS" refers to the local WebSocket connection that
  this server forwards requests to.
  """

  _PATH_PREFIX = '/http_over_websocket/proxied_ws/'
  PATH = _PATH_PREFIX + '.+'

  def __init__(self, *args, **kwargs):
    super(ProxiedSocketHandler, self).__init__(*args, **kwargs)
    self._proxied_ws_future = None

  def _get_http_client(self):
    """Test hook to allow a different HTTPClient implementation."""
    return httpclient.AsyncHTTPClient()

  @gen.coroutine
  def _attach_auth_cookies(self):
    auth_url = self.get_argument(_AUTH_URL_QUERY_PARAM, default='')
    if not auth_url:
      raise gen.Return({})

    parsed_auth_url = urlparse.urlparse(auth_url)

    try:
      _validate_same_domain(self.request, parsed_auth_url)
      extra_cookies = yield _perform_request_and_extract_cookies(
          parsed_auth_url, self.ca_certs, self._get_http_client())
    except Exception as e:  # pylint:disable=broad-except
      self._on_unhandled_exception(e)
      raise

    self.request.headers.update(extra_cookies)

  @gen.coroutine
  def open(self):
    self._proxied_ws_future = self._on_open()
    raise gen.Return(self._get_proxied_ws())

  @gen.coroutine
  def _on_open(self):
    # Only proxy local connections.
    proxy_path = self.request.uri.replace(self._PATH_PREFIX, '/')
    proxy_url = '{}://{}{}'.format(_PROTOCOL_MAP[self.request.protocol],
                                   self.request.host, proxy_path)
    yield self._attach_auth_cookies()

    self.log.info('proxying WebSocket connection to: {}'.format(proxy_url))
    proxy_request = httpclient.HTTPRequest(
        url=proxy_url,
        method='GET',
        headers=self.request.headers,
        body=None,
        ca_certs=self.ca_certs)
    _modify_proxy_request_test_only(proxy_request)

    client = yield websocket.websocket_connect(
        proxy_request, on_message_callback=self._on_proxied_message)
    raise gen.Return(client)

  @gen.coroutine
  def _get_proxied_ws(self):
    if not self._proxied_ws_future:
      raise gen.Return()

    try:
      client = yield self._proxied_ws_future
    except Exception as e:  # pylint:disable=broad-except
      self._on_unhandled_exception(e)
    else:
      raise gen.Return(client)

  @gen.coroutine
  def _on_proxied_message(self, message):
    # A message was received from the proxied WebSocket. Write this the server
    # WebSocket.
    if not message:
      # Proxied WebSocket connection is closed. Close the server's connection as
      # well.
      proxied_ws = yield self._get_proxied_ws()
      self.close(proxied_ws.close_code if proxied_ws else 500,
                 proxied_ws.close_reason if proxied_ws else 'Unknown')
      raise gen.Return()

    self.write_message(message)

  @gen.coroutine
  def on_close(self):
    # Server's WebSocket connection was closed. Attempt to close the proxied
    # WebSocket.
    proxied_ws = yield self._get_proxied_ws()
    if proxied_ws:
      proxied_ws.close(self.close_code or 500, self.close_reason or 'Unknown')

  @gen.coroutine
  def on_message(self, message):
    # Received message from our server's WebSocket - forward this to the proxied
    # WebSocket.
    proxied_ws = yield self._get_proxied_ws()
    if proxied_ws:
      proxied_ws.write_message(message)
      raise gen.Return()

    # If the proxied WebSocket has been closed or hasn't yet been established,
    # there's nothing to do but terminate the server's connection.
    self.close(proxied_ws.close_code if proxied_ws else 500,
               proxied_ws.close_reason if proxied_ws else 'Unknown')

  def _on_unhandled_exception(self, e):
    self.log.exception('Uncaught error when proxying request')
    code = e.code if isinstance(e, httpclient.HTTPError) else 500
    self.close(code, 'Uncaught error when proxying request')


def _modify_proxy_request_test_only(unused_request):
  """Hook for modifying the request before making a fetch (test-only)."""


def _validate_same_domain(request, url):
  handler_domain = urlparse.urlparse('{}://{}'.format(request.protocol,
                                                      request.host))
  if (handler_domain.scheme, handler_domain.netloc) != (url.scheme, url.netloc):
    raise ValueError('Invalid cross-domain request from {} to {}'.format(
        handler_domain.geturl(), url.geturl()))


@gen.coroutine
def _perform_request_and_extract_cookies(request_url, ca_certs, http_client):
  """Fetches URL and returns headers that should be used in subsequent requests.

  This is achieved by requesting the URL and parsing any "Set-Cookie" headers in
  the response. These values are then assembled into an appropriate "Cookie"
  header that can be attached to subsequent responses.

  Args:
    request_url: The URL
    ca_certs: Filename of any CA certificates to be used for making the proxied
      request.
    http_client: A httpclient.AsyncHttpClient to be used when fetching the given
      URL.

  Yields:
    A dictionary of headers that should be sent in any subsequent requests.
  """

  proxy_request = httpclient.HTTPRequest(
      url=request_url.geturl(),
      method='GET',
      headers=None,
      body=None,
      ca_certs=ca_certs)
  _modify_proxy_request_test_only(proxy_request)

  response = yield http_client.fetch(proxy_request, raise_error=False)
  if response.error:
    raise response.error

  cookie_fragments = []
  for cookie_header in response.headers.get_list('Set-Cookie'):
    parsed_cookie = http_cookies.SimpleCookie(cookie_header)
    cookie_fragments.append(parsed_cookie.output(attrs=[], header=''))

  headers = {}
  if cookie_fragments:
    headers['Cookie'] = '; '.join(cookie_fragments)
  raise gen.Return(headers)
