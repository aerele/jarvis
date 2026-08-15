"""Hosted-embed marker support in the chat canvas pipeline.

agent 2026.6+ teaches the model to publish rich HTML as hosted canvas
documents referenced by ``[embed ref="<id>" /]`` markers (or an explicit
``/__openclaw__/canvas/...`` url). These must resolve to the same gateway
fetch path as plain ``canvas/<path>.<ext>`` references, and the markers must
be stripped from the visible reply once the artifact is persisted.
"""

from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.canvas import detect_canvas_names, persist_canvases, strip_canvas_refs

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"


class TestCanvasEmbedDetection(FrappeTestCase):
	def test_embed_ref_maps_to_document_path(self):
		text = 'Done.\n\n[embed ref="sales-dash-abc123" title="Sales" height="720" /]'
		self.assertEqual(detect_canvas_names(text), ["documents/sales-dash-abc123/index.html"])

	def test_embed_url_form_detected_via_canvas_path(self):
		text = '[embed url="/__openclaw__/canvas/documents/cv_9/index.html" title="X" /]'
		self.assertEqual(detect_canvas_names(text), ["documents/cv_9/index.html"])

	def test_plain_canvas_path_still_detected_and_deduped(self):
		text = 'See canvas/charts/foo.html and again canvas/charts/foo.html plus [embed ref="bar" /]'
		self.assertEqual(
			detect_canvas_names(text),
			["charts/foo.html", "documents/bar/index.html"],
		)

	def test_single_quoted_ref(self):
		self.assertEqual(
			detect_canvas_names("[embed ref='q-1' /]"),
			["documents/q-1/index.html"],
		)

	def test_cap_still_applies(self):
		text = "\n".join(f'[embed ref="d{i}" /]' for i in range(12))
		self.assertEqual(len(detect_canvas_names(text)), 8)


class TestCanvasEmbedStripping(FrappeTestCase):
	def test_persisted_ref_marker_removed(self):
		text = 'Built it.\n\n[embed ref="sales-1" title="Sales" height="720" /]'
		out = strip_canvas_refs(text, ["documents/sales-1/index.html"])
		self.assertEqual(out, "Built it.")

	def test_unpersisted_marker_kept(self):
		text = 'Built it. [embed ref="other" /]'
		out = strip_canvas_refs(text, ["documents/sales-1/index.html"])
		self.assertIn('[embed ref="other" /]', out)

	def test_url_form_marker_removed_without_residue(self):
		text = 'Done [embed url="/__openclaw__/canvas/documents/cv_9/index.html" title="X" /] end'
		out = strip_canvas_refs(text, ["documents/cv_9/index.html"])
		self.assertNotIn("[embed", out)
		self.assertNotIn("cv_9", out)
		self.assertIn("Done", out)
		self.assertIn("end", out)

	def test_plain_path_strip_unchanged(self):
		text = "Chart at canvas/charts/foo.html for you"
		out = strip_canvas_refs(text, ["charts/foo.html"])
		self.assertNotIn("canvas/", out)


class TestHostClientStripping(FrappeTestCase):
	def test_host_socket_script_removed_others_kept(self):
		from jarvis.chat.canvas import _strip_host_client

		html = (
			"<html><body><script>renderChart()</script>"
			'<script>\nconst ws = new WebSocket("ws://" + location.host + "/__openclaw__/ws");\n</script>'
			"</body></html>"
		)
		out = _strip_host_client(html)
		self.assertIn("renderChart()", out)
		self.assertNotIn("__openclaw__/ws", out)
		self.assertNotIn("WebSocket", out)


class TestCanvasPathTraversal(FrappeTestCase):
	def test_dotdot_traversal_rejected(self):
		self.assertEqual(detect_canvas_names("see canvas/../../etc/passwd.html now"), [])

	def test_dotdot_mid_path_rejected(self):
		self.assertEqual(detect_canvas_names("canvas/charts/../../secret.html"), [])

	def test_legit_subdir_still_ok(self):
		self.assertEqual(detect_canvas_names("canvas/charts/sales.html"), ["charts/sales.html"])


class TestPersistCanvasesGatewayFallback(FrappeTestCase):
	"""End-to-end ``persist_canvases`` behavior when the gateway's canvas route
	is (and is not) actually serving the published document, driven entirely
	through a mocked ``requests.get`` — no live container needed.

	Live repro (2026-08-15, e2e.localhost): a main-chat dashboard build's
	``[embed ref=... /]`` resolved to the agent runtime's own web-UI shell
	(HTTP 200, wrong content) instead of the published document that genuinely
	existed on disk at that exact path. ``fetch_canvas`` only checked for a
	200 with a body, so that shell got saved and rendered as the dashboard,
	titled after the runtime's own product name."""

	def setUp(self):
		self.conv = frappe.get_doc({"doctype": CONV, "title": "canvas fallback test"}).insert(
			ignore_permissions=True
		)
		self.msg = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": self.conv.name,
				"seq": 1,
				"role": "assistant",
				"content": 'Built it.\n\n[embed ref="cv_abc123" title="Dash" height="720" /]',
			}
		).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.delete_doc(MSG, self.msg.name, force=True, ignore_permissions=True, delete_permanently=True)
		frappe.delete_doc(CONV, self.conv.name, force=True, ignore_permissions=True, delete_permanently=True)

	def test_broken_gateway_persists_nothing_and_leaves_content_untouched(self):
		"""Sentinel probe 200s (the runtime shell answering for everything) ->
		skip persistence entirely rather than save that shell as the canvas."""
		shell_resp = Mock(status_code=200, content=b"<html>runtime web UI shell</html>")
		original_content = self.msg.content

		with patch("requests.get", return_value=shell_resp), patch.object(frappe, "log_error") as log_error:
			items = persist_canvases(self.msg.name, self.msg.content, "ws://127.0.0.1:19000", "tok")

		self.assertEqual(items, [])
		log_error.assert_called_once()
		self.assertEqual(log_error.call_args.kwargs.get("title"), "chat canvas: gateway fallback detected")
		# Neither the embed marker nor the content was touched.
		stored = frappe.db.get_value(MSG, self.msg.name, ["content", "canvas"], as_dict=True)
		self.assertEqual(stored.content, original_content)
		self.assertIn('[embed ref="cv_abc123"', stored.content)
		self.assertFalse(stored.canvas)

	def test_healthy_gateway_persists_the_real_document(self):
		"""Sentinel probe 404s -> per-artifact fetch proceeds and persists as
		before."""
		real_doc = b"<!doctype html><html><head><title>Real Dashboard</title></head><body>ok</body></html>"

		def fake_get(url, headers=None, timeout=None):
			if "jarvis-canvas-probe-" in url:
				return Mock(status_code=404, content=b"not found")
			return Mock(status_code=200, content=real_doc)

		with patch("requests.get", side_effect=fake_get), patch.object(frappe, "log_error") as log_error:
			items = persist_canvases(self.msg.name, self.msg.content, "ws://127.0.0.1:19000", "tok")

		log_error.assert_not_called()
		self.assertEqual(len(items), 1)
		self.assertEqual(items[0]["name"], "documents/cv_abc123/index.html")
		self.assertEqual(items[0]["title"], "Real Dashboard")
		stored = frappe.db.get_value(MSG, self.msg.name, ["content", "canvas"], as_dict=True)
		self.assertNotIn("[embed", stored.content)
		self.assertTrue(stored.canvas)
