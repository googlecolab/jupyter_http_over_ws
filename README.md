# Jupyter HTTP-over-WebSocket

This Jupyter server extension allows running Jupyter notebooks that use a
WebSocket to proxy HTTP traffic. Browsers do not allow cross-domain
communication to localhost via HTTP, but do support cross-domain communication
to localhost via WebSocket.

## Installation and Setup

Run the following commands in a shell:

``` shell
pip install jupyter_http_over_ws
# Optional: Install the extension to run every time the notebook server starts.
# Adds a /http_over_websocket endpoint to the Tornado notebook server.
jupyter serverextension enable --py jupyter_http_over_ws
```

## Usage

New notebook servers are started normally, though you will need to set a flag to
explicitly trust WebSocket connections from the host communicating via
HTTP-over-WebSocket.

``` shell
jupyter notebook \
  --NotebookApp.allow_origin='https://www.example.com' \
  --port=8081
```

Note: Before requests will be accepted by your Jupyter notebook, make sure to
open the browser window specified in the command-line when the notebook server
starts up. This will set an auth cookie that is required for allowing requests
(http://jupyter-notebook.readthedocs.io/en/stable/security.html).

## Troubleshooting

### Receiving 403 errors when attempting connection

If the auth cookie isn't present when a connection is attempted, you may see a
403 error. To help prevent these types of issues, consider starting your Jupyter
server using the `--no-browser` flag and open the provided link that appears in
the terminal from the same browser that you would like to connect from:

``` shell
jupyter notebook \
  --NotebookApp.allow_origin='https://www.example.com' \
  --port=8081
  --no-browser
```

If you still see issues, consider retrying the above steps from an incognito
window which will prevent issues related to browser extensions.

## Contributing

If you have a problem, or see something that could be improved, please file an
issue. However, we don't have the bandwidth to support review of external
contributions, and we don't want user PRs to languish, so we aren't accepting
any external contributions right now.
