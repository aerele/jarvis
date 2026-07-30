"""The suite must be incapable of touching a live tenant -- and must not break the many
tests that mock their transport and were never unsafe.

Both halves matter. The first cut of this guard sat in admin_client._do_post and
OpenclawSession.connect, and fired even when the caller had already patched
requests.post -- breaking dozens of pre-existing tests that mock the transport in order
to exercise the function under test. The block belongs at the TRANSPORT, the exact layer
a mock replaces. See jarvis/tests/__init__.py.
"""

import unittest
from unittest.mock import patch

import requests
from frappe.tests.utils import FrappeTestCase


class TestRealNetworkIsBlocked(FrappeTestCase):
	"""Only calls that actually reach the wire."""

	def test_a_real_http_call_is_blocked(self):
		with self.assertRaises(requests.ConnectionError) as ctx:
			requests.post(
				"http://jarvis.admin:8002/api/method/push_pool",
				json={"models": ["FIXTURE gpt-4o"]},
				timeout=2,
			)
		self.assertIn("BLOCKED", str(ctx.exception))

	def test_the_destructive_path_specifically_is_blocked(self):
		"""The exact call that destroyed a tenant on 2026-07-14: on_update -> admin sync
		-> fleet-agent -> rewrite the pool, delete the OAuth blob."""
		from jarvis import admin_client
		from jarvis.exceptions import AdminUnreachableError

		# admin_client turns a ConnectionError into AdminUnreachableError -- exactly as it
		# already does when admin is down. Local now behaves as CI always has.
		with self.assertRaises(AdminUnreachableError):
			admin_client._do_post(
				"http://jarvis.admin:8002/api/method/x",
				{},
				{},
				5,
				"http://jarvis.admin:8002",
			)

	def test_a_real_websocket_is_blocked(self):
		"""openclaw's gateway. A test that reached it would mutate live session state via
		sessions.patch and burn real tokens from the customer's LLM quota."""
		import websocket

		with self.assertRaises(ConnectionRefusedError) as ctx:
			websocket.create_connection("ws://localhost:19300")
		self.assertIn("BLOCKED", str(ctx.exception))

	def test_a_raw_urllib3_fetch_is_blocked(self):
		"""link_fetch pins the resolved IP and drives a pool directly, bypassing requests
		entirely -- so blocking requests alone would leave it wide open."""
		import urllib3

		pool = urllib3.HTTPConnectionPool(host="example.com", port=80, retries=False)
		with self.assertRaises(ConnectionRefusedError) as ctx:
			pool.urlopen("GET", "/")
		self.assertIn("BLOCKED", str(ctx.exception))


class TestMockedTestsAreUntouched(FrappeTestCase):
	"""The half that the first attempt got wrong. A test that patches its transport never
	reaches the wire, is therefore already safe, and must run exactly as before."""

	def test_a_patched_requests_post_still_works(self):
		with patch("jarvis.admin_client.requests.post") as post:
			post.return_value.status_code = 200
			post.return_value.json.return_value = {"message": {"ok": True}}
			r = requests_post_via_admin_client()
		self.assertEqual(r, {"ok": True})
		post.assert_called_once()

	def test_a_patched_websocket_still_works(self):
		"""test_chat_agent_client calls OpenclawSession.connect DIRECTLY -- connect is
		the unit under test, so it cannot mock itself. It mocks create_connection instead.
		A guard inside connect() would have broken that whole file."""
		import websocket

		with patch.object(websocket, "create_connection", return_value="fake-socket") as cc:
			self.assertEqual(websocket.create_connection("ws://t"), "fake-socket")
		cc.assert_called_once()


def requests_post_via_admin_client():
	from jarvis import admin_client

	return admin_client._do_post("http://x/y", {}, {}, 5, "http://x")


if __name__ == "__main__":
	unittest.main()
