"""Bench → openclaw container WebSocket ping helper.

Single use: the `Test Agent Connection` diagnostic button on Jarvis
Settings (via `jarvis.diagnostics.ping_openclaw`) opens a WS to the
customer's container, completes the operator-role connect handshake,
and closes. No secrets.reload, no restart.

In production the WS endpoint is the customer's `agent_url`
(`wss://<slug>.jarvis.aerele.in`), which the bench can reach directly
without going through admin. In local-dev the same code paths apply -
the WS happens to terminate on a local container, but the bench's role
is the same.
"""

import json
import time
import uuid

import websocket
from websocket import create_connection

from jarvis.exceptions import OpenclawUnreachableError

PING_TIMEOUT_SECONDS = 10


def ping(gateway_url: str, gateway_token: str) -> None:
	"""Open WS to openclaw and complete the connect handshake only.

	Raises ``OpenclawUnreachableError`` if the socket can't open or the
	handshake is rejected.
	"""
	try:
		ws = create_connection(gateway_url, timeout=PING_TIMEOUT_SECONDS)
	except (websocket.WebSocketException, OSError) as e:
		raise OpenclawUnreachableError(f"connect failed: {e}") from e

	deadline = time.monotonic() + PING_TIMEOUT_SECONDS
	try:
		connect_id = str(uuid.uuid4())
		ws.send(
			json.dumps(
				{
					"type": "req",
					"id": connect_id,
					"method": "connect",
					"params": {
						"minProtocol": 3,
						"maxProtocol": 4,
						"role": "operator",
						"client": {
							"id": "gateway-client",
							"version": "0.1.0",
							"platform": "linux",
							"mode": "backend",
						},
						"scopes": ["operator.admin"],
						"auth": {"token": gateway_token},
					},
				}
			)
		)
		connect_res = _await_response(ws, connect_id, deadline)
		if not connect_res.get("ok"):
			err = connect_res.get("error") or {}
			raise OpenclawUnreachableError(
				f"connect rejected: {err.get('code', '?')}: {err.get('message', '')}"
			)
	except (websocket.WebSocketTimeoutException, TimeoutError) as e:
		raise OpenclawUnreachableError(f"timeout: {e}") from e
	except websocket.WebSocketException as e:
		raise OpenclawUnreachableError(f"ws error: {e}") from e
	finally:
		try:
			ws.close()
		except Exception:
			pass


def _await_response(ws, request_id: str, deadline: float) -> dict:
	"""Read frames until a `res` frame with matching id arrives. Other
	frames (events, challenges) are skipped.

	Timeout here surfaces as ``OpenclawUnreachableError`` - the module
	docstring is explicit that this is a connect-only ping (no
	secrets.reload, no restart), so the previous
	``OpenclawReloadFailedError`` was the wrong category and confused
	the diagnostics UI's branching on the exception type. Punch-list
	item from the 2026-06-16 review.
	"""
	while True:
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			raise OpenclawUnreachableError("timeout waiting for response")
		ws.settimeout(remaining)
		raw = ws.recv()
		if not raw:
			raise OpenclawUnreachableError("ws closed unexpectedly")
		try:
			frame = json.loads(raw)
		except json.JSONDecodeError:
			continue
		if frame.get("type") == "res" and frame.get("id") == request_id:
			return frame
