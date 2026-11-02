"""Canvas fetches never follow redirects."""

import unittest
from unittest.mock import Mock, patch

from jarvis.chat import canvas
from jarvis.tests._gateway_fixtures import install_synthetic_runtime_profile
from jarvis.tests._plugin_route_fixtures import patch_signer


def setUpModule():
	install_synthetic_runtime_profile()


class TestCanvasNoRedirects(unittest.TestCase):
	def setUp(self):
		patcher = patch_signer()
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_fetch_passes_allow_redirects_false(self):
		resp = Mock(status_code=200, content=b"<html></html>")
		with patch("requests.request", return_value=resp) as get:
			out = canvas.fetch_canvas("ws://127.0.0.1:19000", "tok", "documents/a/index.html")
		self.assertIsNotNone(out)
		self.assertIs(get.call_args.kwargs.get("allow_redirects"), False)

	def test_fetch_treats_redirect_as_failed(self):
		resp = Mock(status_code=302, content=b"moved")
		with (
			patch("requests.request", return_value=resp),
			patch("jarvis.chat.canvas.frappe.log_error") as log,
		):
			out = canvas.fetch_canvas("ws://127.0.0.1:19000", "tok", "documents/a/index.html")
		self.assertIsNone(out)
		log.assert_called_once()

	def test_probe_passes_allow_redirects_false(self):
		with patch("requests.request", return_value=Mock(status_code=404)) as get:
			canvas._gateway_fakes_missing_canvas_as_ok("ws://127.0.0.1:19000", "tok")
		self.assertIs(get.call_args.kwargs.get("allow_redirects"), False)
