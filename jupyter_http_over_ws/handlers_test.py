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
"""Tests for package jupyter_http_over_ws."""

import base64
import json

import jupyter_http_over_ws
from jupyter_http_over_ws import handlers

from tornado import concurrent
from tornado import gen
from tornado import httpclient
from tornado import testing
from tornado import web
from tornado import websocket

TEST_XSRF_HEADER = 'Propagated-Xsrf'


class FakeSessionsHandler(web.RequestHandler):

  SUPPORTED_METHODS = web.RequestHandler.SUPPORTED_METHODS + ('CUSTOMMETHOD',)

  # List of three-tuples containing:
  # 1) Query argument name
  # 2) Response code to return if query arg present
  # 3) Response text to send if query arg present
  # The list will be evaluated in order. If a query
  # argument is present in the request matching this,
  # then the response code and text will be set accordingly.
  # Otherwise, a 200 is returned.
  _CANNED_RESPONSES = [
      ('throw_500', 500, 'Server error'),
  ]

  def get(self):
    for query_arg, response_code, response_text in self._CANNED_RESPONSES:
      if self.get_argument(query_arg, default=None):
        self.set_status(response_code, response_text)
        return

    self.write('ok')

  def custommethod(self):
    self.write('ok')

  def post(self):
    # Proxied requests send _xsrf token in the URL since cookies can't be set
    # in messages on the websocket after it's been established.
    xsrf = self.get_query_argument('_xsrf', default=None)
    if xsrf:
      self.set_header(TEST_XSRF_HEADER, xsrf)
    self.write(self.request.body)


class FakeStreamedResponseHandler(web.RequestHandler):

  @gen.coroutine
  def get(self):
    self.write('first')
    yield self.flush()
    self.write('last')
    yield self.flush()
    self.finish()


class LargeStreamedResponseHandler(web.RequestHandler):

  @gen.coroutine
  def get(self):
    self.write('a' * 100000)
    yield self.flush()
    self.finish()


class FakeNotebookServer(object):

  def __init__(self, app):
    self.web_app = app


class AlwaysThrowingHTTPOverWebSocketHandler(handlers.HttpOverWebSocketHandler):

  class AlwaysThrowingHTTPClient(httpclient.AsyncHTTPClient):

    def fetch(self, request, *args, **kwargs):
      future = concurrent.Future()
      future.set_result(
          httpclient.HTTPResponse(
              request=request,
              code=500,
              error=ValueError('Expected programming error')))
      return future

  def _get_http_client(self):
    return AlwaysThrowingHTTPOverWebSocketHandler.AlwaysThrowingHTTPClient()


WHITELISTED_ORIGIN = 'http://www.examplewhitelistedorigin.com'


class HttpOverWebSocketHandlerTestBase(object):
  """Base class for all tests to exercise.

  Tornado only provides AsyncHTTPTestCase and AsyncHTTPSTestCase. We'd like to
  test in both modes. This class has all the test methods that should be run by
  either type of server. Specific implementations will derive from the
  appropriate Tornado test class. The only forks in behavior are around initial
  configuration.

  Subclasses are expected to implement the get_config and
  get_ws_connection_request methods.
  """

  def get_app(self):
    """Setup code required by testing.AsyncHTTP[S]TestCase."""
    settings = {
        'base_url': '/',
        'local_hostnames': ['localhost'],
        # This flag controls which domains cross-origin requests are allowed
        # for.
        'allow_origin': WHITELISTED_ORIGIN,
    }
    config = self.get_config()
    if config is not None:
      settings['config'] = config
    app = web.Application([
        (r'/api/sessions', FakeSessionsHandler),
        (r'/api/streamedresponse', FakeStreamedResponseHandler),
        (r'/api/largeresponse', LargeStreamedResponseHandler),
        (r'/always_throwing_http_over_ws',
         AlwaysThrowingHTTPOverWebSocketHandler),
    ], **settings)

    nb_server_app = FakeNotebookServer(app)
    jupyter_http_over_ws.load_jupyter_server_extension(nb_server_app)

    # For HTTPS servers, we disable certificate validation.
    def _modify_proxy_request_test_only(request):
      request.validate_cert = False

    handlers._modify_proxy_request_test_only = _modify_proxy_request_test_only

    return app

  def get_config(self):
    """Initialize any Tornado-specific config when server is started."""
    raise NotImplementedError()

  def get_ws_connection_request(self, http_over_ws_url='http_over_websocket'):
    """Returns an HTTPRequest used to connect to the Tornado server."""
    raise NotImplementedError()

  def get_request_json(self, path, message_id, method='GET', body=None):
    request = {
        'path': path,
        'message_id': message_id,
        'method': method,
    }
    if body is not None:
      request['body'] = body
    return json.dumps(request)

  @testing.gen_test
  def test_proxied_get(self):
    client = yield websocket.websocket_connect(self.get_ws_connection_request())
    client.write_message(self.get_request_json('/api/sessions', '1234'))

    response_body = yield client.read_message()
    response = json.loads(response_body)
    self.assertEqual(200, response['status'])
    self.assertEqual('ok', base64.b64decode(response['data']).decode('utf-8'))
    self.assertEqual('1234', response['message_id'])
    self.assertTrue(response['done'])

  @testing.gen_test
  def test_proxied_get_empty_body(self):
    client = yield websocket.websocket_connect(self.get_ws_connection_request())
    client.write_message(
        self.get_request_json('/api/sessions', '1234', body=''))

    response_body = yield client.read_message()
    response = json.loads(response_body)
    self.assertEqual(200, response['status'])
    self.assertEqual('ok', base64.b64decode(response['data']).decode('utf-8'))
    self.assertEqual('1234', response['message_id'])
    self.assertTrue(response['done'])

  @testing.gen_test
  def test_proxied_post_no_body(self):
    client = yield websocket.websocket_connect(self.get_ws_connection_request())
    client.write_message(
        self.get_request_json('/api/sessions', '1234', body=None))

    response_body = yield client.read_message()
    response = json.loads(response_body)
    self.assertEqual(200, response['status'])
    self.assertEqual('ok', base64.b64decode(response['data']).decode('utf-8'))
    self.assertEqual('1234', response['message_id'])
    self.assertTrue(response['done'])

  @testing.gen_test
  def test_proxied_nonstandard_method(self):
    client = yield websocket.websocket_connect(self.get_ws_connection_request())
    client.write_message(
        self.get_request_json(
            '/api/sessions', '1234', method='CUSTOMMETHOD', body=None))

    response_body = yield client.read_message()
    response = json.loads(response_body)
    self.assertEqual(200, response['status'])
    self.assertEqual('ok', base64.b64decode(response['data']).decode('utf-8'))
    self.assertEqual('1234', response['message_id'])
    self.assertTrue(response['done'])

  @testing.gen_test
  def test_streamed_response(self):
    client = yield websocket.websocket_connect(self.get_ws_connection_request())
    client.write_message(self.get_request_json('/api/streamedresponse', '1234'))

    # The /api/streamedresponse handler yields a body split over two response.
    # The first will contain "first" as the body and contain metadata such as
    # status and headers. The second will contain "last" as the body and have
    # the done field set to true.
    first_response_body = yield client.read_message()
    first_response = json.loads(first_response_body)
    self.assertEqual(200, first_response['status'])
    self.assertEqual('1234', first_response['message_id'])
    self.assertEqual('first',
                     base64.b64decode(first_response['data']).decode('utf-8'))
    self.assertFalse(first_response['done'])

    second_response_body = yield client.read_message()
    second_response = json.loads(second_response_body)
    self.assertNotIn('status', second_response)
    self.assertNotIn('statusText', second_response)
    self.assertNotIn('headers', second_response)
    self.assertEqual('1234', second_response['message_id'])
    self.assertEqual('last',
                     base64.b64decode(second_response['data']).decode('utf-8'))
    self.assertTrue(second_response['done'])

  @testing.gen_test
  def test_request_for_invalid_url(self):
    client = yield websocket.websocket_connect(self.get_ws_connection_request())
    client.write_message(self.get_request_json('/invalid/path', '1234'))

    response_body = yield client.read_message()
    response = json.loads(response_body)
    self.assertEqual(404, response['status'])
    self.assertEqual('1234', response['message_id'])
    self.assertTrue(response['done'])

  @testing.gen_test
  def test_proxied_endpoint_has_error(self):
    client = yield websocket.websocket_connect(self.get_ws_connection_request())
    # Force a 500 response. See _CANNED_RESPONSES
    client.write_message(
        self.get_request_json('/api/sessions?throw_500=1', '1234'))

    response_body = yield client.read_message()
    response = json.loads(response_body)
    self.assertEqual(500, response['status'])
    self.assertEqual('Server error', response['statusText'])
    self.assertEqual('1234', response['message_id'])
    self.assertTrue(response['done'])

  @testing.gen_test
  def test_unwhitelisted_cross_domain_origin(self):
    request = self.get_ws_connection_request()
    request.headers.add('Origin', 'http://www.example.com')
    with self.assertRaises(httpclient.HTTPError) as e:
      yield websocket.websocket_connect(request)

    self.assertEquals(403, e.exception.code)

  @testing.gen_test
  def test_whitelisted_cross_domain_origin(self):
    request = self.get_ws_connection_request()
    request.headers.add('Origin', WHITELISTED_ORIGIN)
    client = yield websocket.websocket_connect(request)
    self.assertNotEqual(None, client)

  @testing.gen_test
  def test_propagates_body_text_and_xsrf(self):
    request = self.get_ws_connection_request()
    request.headers.add('Cookie', '_xsrf=5678')
    client = yield websocket.websocket_connect(request)
    client.write_message(
        self.get_request_json(
            '/api/sessions', '1234', method='POST', body='somedata'))

    response_body = yield client.read_message()
    response = json.loads(response_body)
    self.assertEqual(200, response['status'])
    self.assertEqual('somedata',
                     base64.b64decode(response['data']).decode('utf-8'))
    self.assertEqual('1234', response['message_id'])
    self.assertIn(TEST_XSRF_HEADER, response['headers'])
    self.assertEqual('5678', response['headers'][TEST_XSRF_HEADER])
    self.assertTrue(response['done'])

  @testing.gen_test
  def test_max_chunk_size_64k(self):
    # Ensure that the size of the data propagated to the client is below 64K.
    # This size was chosen to ensure that Javascript JSON deserialization occurs
    # in a bounded amount of time.
    # Tornado itself does not make this parameter configurable, so add a
    # regression test here to ensure the default does not change. Reference:
    # https://github.com/tornadoweb/tornado/blob/master/tornado/http1connection.py#L76
    client = yield websocket.websocket_connect(self.get_ws_connection_request())
    client.write_message(self.get_request_json('/api/largeresponse', '1234'))

    # Read all responses and ensure that they are smaller than the 64K limit.
    # HTTPS connections split the body into even smaller chunks.
    tornado_chunk_size = 65536
    responses = []
    while True:
      response_body = yield client.read_message()
      response = json.loads(response_body)
      responses.append(response)

      self.assertEqual('1234', response['message_id'])
      self.assertTrue(
          len(base64.b64decode(response['data'])) <= tornado_chunk_size)

      if response.get('done'):
        break

  @testing.gen_test
  def test_bad_json(self):
    client = yield websocket.websocket_connect(self.get_ws_connection_request())
    client.write_message('invalid json')

    response_body = yield client.read_message()
    response = json.loads(response_body)
    self.assertEqual(400, response['status'])
    self.assertEqual('JSON input is required.', response['statusText'])
    self.assertTrue(response['done'])

  @testing.gen_test
  def test_missing_param(self):
    client = yield websocket.websocket_connect(self.get_ws_connection_request())
    # Missing the 'method' parameter.
    client.write_message('{"path": "/api/sessions", "method": "GET"}')

    response_body = yield client.read_message()
    response = json.loads(response_body)
    self.assertEqual(400, response['status'])
    self.assertTrue('body must contain' in response['statusText'])
    self.assertTrue(response['done'])

  @testing.gen_test
  def test_invalid_protocol_version_requested(self):
    request = self.get_ws_connection_request()
    request.url += '?min_version=abc'

    client = yield websocket.websocket_connect(request)
    msg = yield client.read_message()
    # Message of None indicates that the connection has been closed.
    self.assertIsNone(msg)
    self.assertEqual(400, client.close_code)
    self.assertEqual('Invalid "min_version" provided: abc', client.close_reason)

  @testing.gen_test
  def test_newer_protocol_version_requested(self):
    request = self.get_ws_connection_request()
    request.url += '?min_version=9.0.0'

    with testing.ExpectLog(
        'tornado.application',
        'Rejecting connection:.*Please upgrade',
        required=True):
      client = yield websocket.websocket_connect(request)
      msg = yield client.read_message()

    # Message of None indicates that the connection has been closed.
    self.assertIsNone(msg)
    self.assertEqual(400, client.close_code)
    self.assertIn('Please upgrade', client.close_reason)

  @testing.gen_test
  def test_valid_version_requested(self):
    request = self.get_ws_connection_request()
    request.url += '?min_version=0.0.1a0'

    client = yield websocket.websocket_connect(request)
    client.write_message('abc')

    # Receiving a non-None response indicates that the connection is alive, even
    # if the response itself is an error.
    response_body = yield client.read_message()
    # Message of None indicates that the connection has been closed.
    self.assertIsNotNone(response_body)

    response = json.loads(response_body)
    self.assertEqual(400, response['status'])

  @testing.gen_test
  def test_current_version_requested(self):
    request = self.get_ws_connection_request()
    request.url += '?min_version=0.0.1a3'

    client = yield websocket.websocket_connect(request)
    client.write_message('abc')

    # Receiving a non-None response indicates that the connection is alive, even
    # if the response itself is an error.
    response_body = yield client.read_message()
    # Message of None indicates that the connection has been closed.
    self.assertIsNotNone(response_body)

    response = json.loads(response_body)
    self.assertEqual(400, response['status'])

  @testing.gen_test
  def test_programming_error_propagates(self):
    # Ensure that any programming errors from how the proxy is implemented
    # (i.e. malformed requests) are properly logged.

    # Ideally, there aren't any programming errors in the current
    # implementation. Should one exist, it would be better to fix it rather than
    # use it as a test case here.
    ws_request = self.get_ws_connection_request('always_throwing_http_over_ws')
    client = yield websocket.websocket_connect(ws_request)

    with testing.ExpectLog(
        'tornado.application',
        'Uncaught error when proxying request',
        required=True) as expect_log:
      client.write_message(self.get_request_json('/api/sessions', '1234'))

      response_body = yield client.read_message()
      # Message of None indicates that the connection has been closed.
      self.assertIsNotNone(response_body)

      response = json.loads(response_body)
      self.assertEqual(500, response['status'])
      self.assertTrue(expect_log.logged_stack)

  @testing.gen_test
  def test_diagnostic_handler_unwhitelisted_cross_domain_origin(self):
    request = self.get_ws_connection_request(
        http_over_ws_url='http_over_websocket/diagnose')
    request.headers.add('Origin', 'http://www.example.com')
    with self.assertRaises(httpclient.HTTPError) as e:
      yield websocket.websocket_connect(request)

    self.assertEquals(403, e.exception.code)

  @testing.gen_test
  def test_diagnostic_handler_no_problems_request(self):
    request = self.get_ws_connection_request(
        http_over_ws_url='http_over_websocket/diagnose')
    request.url += '?min_version=0.0.3'
    request.headers.add('Origin', WHITELISTED_ORIGIN)
    request.headers.add('Cookie', '_xsrf=5678')

    client = yield websocket.websocket_connect(request)
    self.assertNotEqual(None, client)
    client.write_message('1')

    response_body = yield client.read_message()
    response = json.loads(response_body)

    self.assertEqual({
        'message_id': '1',
        'extension_version': '0.0.3',
        'has_authentication_cookie': True,
        'is_outdated_extension': False
    }, response)

  @testing.gen_test
  def test_diagnostic_handler_missing_xsrf_cookie(self):
    request = self.get_ws_connection_request(
        http_over_ws_url='http_over_websocket/diagnose')
    request.headers.add('Origin', WHITELISTED_ORIGIN)

    client = yield websocket.websocket_connect(request)
    client.write_message('1')

    response_body = yield client.read_message()
    response = json.loads(response_body)

    self.assertEqual({
        'message_id': '1',
        'extension_version': '0.0.3',
        'has_authentication_cookie': False,
        'is_outdated_extension': False
    }, response)

  @testing.gen_test
  def test_diagnostic_handler_newer_protocol_version_requested(self):
    request = self.get_ws_connection_request(
        http_over_ws_url='http_over_websocket/diagnose')
    request.url += '?min_version=0.0.4'
    request.headers.add('Origin', WHITELISTED_ORIGIN)
    request.headers.add('Cookie', '_xsrf=5678')

    client = yield websocket.websocket_connect(request)
    client.write_message('1')

    response_body = yield client.read_message()
    response = json.loads(response_body)

    self.assertEqual({
        'message_id': '1',
        'extension_version': '0.0.3',
        'has_authentication_cookie': True,
        'is_outdated_extension': True
    }, response)


class HttpOverWebSocketHandlerHttpTest(HttpOverWebSocketHandlerTestBase,
                                       testing.AsyncHTTPTestCase):

  def get_config(self):
    return None

  def get_ws_connection_request(self, http_over_ws_url='http_over_websocket'):
    ws_url = 'ws://localhost:{}/{}'.format(self.get_http_port(),
                                           http_over_ws_url)
    return httpclient.HTTPRequest(url=ws_url)


class HttpOverWebSocketHandlerHttpsTest(HttpOverWebSocketHandlerTestBase,
                                        testing.AsyncHTTPSTestCase):

  def get_config(self):
    # AsyncHTTPSTestCase provides a self-signed cert. The proxy client used by
    # our handler uses the NotebookApp.certfile setting to establish what
    # certificate authorities are trusted.
    return {'NotebookApp': {'certfile': self.get_ssl_options()['certfile'],}}

  def get_ws_connection_request(self, http_over_ws_url='http_over_websocket'):
    ws_url = 'wss://localhost:{}/{}'.format(self.get_http_port(),
                                            http_over_ws_url)
    return httpclient.HTTPRequest(url=ws_url, validate_cert=False)
