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
"""Jupyter server extension to add support for HTTP-over-websocket transport."""

from jupyter_http_over_ws import handlers
from notebook import utils

__all__ = ['load_jupyter_server_extension', 'handlers']

__version__ = str(handlers.HANDLER_VERSION)


def _jupyter_server_extension_paths():
  return [{
      'module': 'jupyter_http_over_ws',
  }]


def _handler_rule(app, handler_class):
  return (utils.url_path_join(app.settings['base_url'],
                              handler_class.PATH), handler_class)


def load_jupyter_server_extension(nb_server_app):
  """Called by Jupyter when this module is loaded as a server extension."""
  app = nb_server_app.web_app
  host_pattern = '.*$'

  app.add_handlers(host_pattern, [
      _handler_rule(app, handlers.HttpOverWebSocketHandler),
      _handler_rule(app, handlers.HttpOverWebSocketDiagnosticHandler),
      _handler_rule(app, handlers.ProxiedSocketHandler),
  ])
  print('jupyter_http_over_ws extension initialized. Listening on '
        '/http_over_websocket')
