"""Hosted-embed marker support in the chat canvas pipeline.

agent 2026.6+ teaches the model to publish rich HTML as hosted canvas
documents referenced by ``[embed ref="<id>" /]`` markers (or an explicit
``/__openclaw__/canvas/...`` url). These must resolve to the same gateway
fetch path as plain ``canvas/<path>.<ext>`` references, and the markers must
be stripped from the visible reply once the artifact is persisted.
"""

from frappe.tests.utils import FrappeTestCase

from jarvis.chat.canvas import detect_canvas_names, strip_canvas_refs


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
