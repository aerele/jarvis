"""Unit tests for the native-media (``MEDIA:`` marker) consumer in
``jarvis.chat.generated_media`` — detection, stripping, the size-bounded
gateway fetch, and the seed/dedup logic. The R2 wiring into the three chat
transports is covered by the worker / pump / recovery tests.
"""

import unittest
from unittest.mock import Mock, patch

from jarvis.chat import generated_media as gm

# Import the root rather than re-hardcoding it — a Q2 rename that updates the
# production constant must not leave the tests validating the old path.
_ROOT = gm._MEDIA_ROOT
_IMG = _ROOT + "tool-image-generation/black-hole---4de239c0-8d05-41e5-9f52-e404ee9f0b21.png"
_IMG_REL = _IMG[len(_ROOT) :]


class TestDetectMediaPaths(unittest.TestCase):
	def test_local_image_marker(self):
		self.assertEqual(gm.detect_media_paths(f"Here you go:\nMEDIA:{_IMG}"), [_IMG])

	def test_case_insensitive(self):
		self.assertEqual(gm.detect_media_paths(f"media:{_IMG}"), [_IMG])
		self.assertEqual(gm.detect_media_paths(f"Media:{_IMG}"), [_IMG])

	def test_surrounding_backticks(self):
		self.assertEqual(gm.detect_media_paths(f"MEDIA:`{_IMG}`"), [_IMG])

	def test_multiple_markers(self):
		a = _ROOT + "tool-image-generation/a.png"
		b = _ROOT + "tool-image-generation/b.jpg"
		self.assertEqual(gm.detect_media_paths(f"MEDIA:{a}\nMEDIA:{b}"), [a, b])

	def test_unicode_filename(self):
		u = _ROOT + "tool-image-generation/图片---abcd1234.png"
		self.assertEqual(gm.detect_media_paths(f"MEDIA:{u}"), [u])

	def test_fenced_marker_is_consumed(self):
		# Fence-UNAWARE by design (D2 leak-safety): a marker inside a balanced fence
		# is detected too. Preserving a fenced example is a nicety we forgo so an
		# UNbalanced fence can never swallow a real trailing marker.
		self.assertEqual(gm.detect_media_paths(f"```\nMEDIA:{_IMG}\n```"), [_IMG])
		self.assertEqual(gm.detect_media_paths(f"~~~\nMEDIA:{_IMG}\n~~~"), [_IMG])

	def test_unbalanced_fence_does_not_swallow_real_marker(self):
		# THE critical case: an odd number of fence lines before the real trailing
		# marker. A fence-toggle scanner would be "in fence" and miss it -> leak +
		# no image. Fence-unaware detection finds it.
		reply = f"Here's the API:\n```python\nimport foo\nMEDIA:{_IMG}\n"
		self.assertEqual(gm.detect_media_paths(reply), [_IMG])

	def test_unicode_whitespace_indent_detected(self):
		# the agent runtime's trimStart() strips NBSP / ideographic space; \s* matches.
		self.assertEqual(gm.detect_media_paths(f" MEDIA:{_IMG}"), [_IMG])
		self.assertEqual(gm.detect_media_paths(f"　MEDIA:{_IMG}"), [_IMG])

	def test_ignored_mid_line(self):
		# A "MEDIA:" NOT at the start of the line is never a dedicated protocol
		# marker (the runtime's own contract requires line-start) - but as of the
		# embedded-path detector (TestEmbeddedMediaPaths), the bare qualifying
		# path itself is STILL found, since it now matches on ANY line. The two
		# detectors are independent: this only proves the marker path finds
		# nothing (there is no line-start "MEDIA:" line here).
		text = f"see the file MEDIA:{_IMG} inline"
		self.assertEqual([p for _, p in gm._media_lines(text)], [])

	def test_external_url_excluded(self):
		self.assertEqual(gm.detect_media_paths("MEDIA:https://example.com/x.png"), [])

	def test_outside_media_root_excluded(self):
		self.assertEqual(gm.detect_media_paths("MEDIA:/home/node/.openclaw/credentials/x.png"), [])
		self.assertEqual(gm.detect_media_paths("MEDIA:/etc/passwd.png"), [])

	def test_traversal_excluded(self):
		self.assertEqual(gm.detect_media_paths(f"MEDIA:{_ROOT}../../etc/passwd.png"), [])

	def test_non_image_excluded(self):
		self.assertEqual(gm.detect_media_paths(f"MEDIA:{_ROOT}tool-image-generation/report.pdf"), [])
		# .svg is deliberately NOT in the raster-only allowlist
		self.assertEqual(gm.detect_media_paths(f"MEDIA:{_ROOT}tool-image-generation/chart.svg"), [])

	def test_empty_rel_excluded(self):
		self.assertEqual(gm.detect_media_paths(f"MEDIA:{_ROOT}"), [])

	def test_cap_per_turn(self):
		many = "\n".join(f"MEDIA:{_ROOT}tool-image-generation/c{i}.png" for i in range(12))
		self.assertEqual(len(gm.detect_media_paths(many)), gm._MAX_MEDIA_PER_TURN)

	def test_empty_and_none(self):
		self.assertEqual(gm.detect_media_paths(""), [])
		self.assertEqual(gm.detect_media_paths(None), [])

	def test_has_media_marker(self):
		# True for any MEDIA: line (qualifying OR not); False otherwise.
		self.assertTrue(gm.has_media_marker(f"MEDIA:{_IMG}"))
		self.assertTrue(gm.has_media_marker(f"MEDIA:{_ROOT}tool-image-generation/report.pdf"))  # non-raster
		self.assertTrue(gm.has_media_marker("MEDIA:https://example.com/x.png"))  # external
		self.assertFalse(gm.has_media_marker("a plain reply"))
		self.assertFalse(gm.has_media_marker(""))
		self.assertFalse(gm.has_media_marker(None))


class TestStripMediaLines(unittest.TestCase):
	def test_strips_handled_marker(self):
		self.assertEqual(gm.strip_media_lines(f"Here:\nMEDIA:{_IMG}"), "Here:")

	def test_strips_external_and_malformed(self):
		self.assertEqual(gm.strip_media_lines("MEDIA:https://example.com/x.png\nok"), "ok")
		# D2: a prose line that merely starts "MEDIA:" is removed (broader than the runtime)
		self.assertEqual(gm.strip_media_lines("MEDIA: coverage was extensive\ndone"), "done")

	def test_fenced_marker_is_stripped(self):
		# Fence-UNAWARE (D2): even a fenced MEDIA line is stripped. The fence markers
		# themselves remain.
		out = gm.strip_media_lines(f"```\nMEDIA:{_IMG}\n```")
		self.assertNotIn("MEDIA:", out)
		self.assertNotIn("/home/node", out)

	def test_unbalanced_fence_marker_is_stripped(self):
		# THE critical leak case: a real trailing marker after an unbalanced fence
		# must still be removed (no raw path survives).
		out = gm.strip_media_lines(f"See:\n```python\nx=1\nMEDIA:{_IMG}\n")
		self.assertNotIn("MEDIA:", out)
		self.assertNotIn("/home/node", out)

	def test_interior_backtick_line_is_stripped(self):
		# An interior backtick must not let the line evade the strip (leak-safety).
		out = gm.strip_media_lines(f"MEDIA: {_ROOT}tool-image-generation/foo`bar.png\nok")
		self.assertNotIn("/home/node", out)
		self.assertEqual(out, "ok")

	def test_collapses_blank_runs(self):
		text = f"Line one\n\nMEDIA:{_IMG}\n\nLine two"
		self.assertEqual(gm.strip_media_lines(text), "Line one\n\nLine two")

	def test_no_marker_returns_verbatim(self):
		text = "A normal reply.\n\nWith paragraphs.\n"
		self.assertEqual(gm.strip_media_lines(text), text)

	def test_empty(self):
		self.assertEqual(gm.strip_media_lines(""), "")
		self.assertIsNone(gm.strip_media_lines(None))


class TestEmbeddedMediaPaths(unittest.TestCase):
	"""the agent runtime's image/video/music generation tools unconditionally detach into
	a background task; ~25-30s later the deferred reply NAMES the generated
	file's path in free text (Attachment: / path="..." / a markdown image),
	never a ``MEDIA:`` marker line. Unlike a marker (stripped unconditionally,
	leak-safety-first), an embedded path is only touched when it FULLY
	qualifies — anything else is ordinary prose and must survive intact."""

	def test_attachment_line_detected_and_stripped(self):
		text = f"Here's the bicycle.\nAttachment: {_IMG}"
		self.assertEqual(gm.detect_media_paths(text), [_IMG])
		self.assertEqual(gm.strip_media_lines(text), "Here's the bicycle.")
		self.assertTrue(gm.has_embedded_media_path(text))
		# has_media_marker stays MEDIA:-only - it must NOT see this embedded path.
		self.assertFalse(gm.has_media_marker(text))

	def test_path_kwarg_quoted_detected_and_stripped(self):
		text = f'1. type=image name="pic" mimeType=image/png path="{_IMG}"\nEnjoy!'
		self.assertEqual(gm.detect_media_paths(text), [_IMG])
		out = gm.strip_media_lines(text)
		self.assertEqual(out, "Enjoy!")
		self.assertNotIn("/home/node", out)

	def test_markdown_image_detected_and_stripped(self):
		text = f"Here you go.\n![the bicycle]({_IMG})\nEnjoy!"
		self.assertEqual(gm.detect_media_paths(text), [_IMG])
		self.assertEqual(gm.strip_media_lines(text), "Here you go.\nEnjoy!")

	def test_outside_media_root_untouched(self):
		text = "Attachment: /etc/passwd.png"
		self.assertEqual(gm.detect_media_paths(text), [])
		self.assertEqual(gm.strip_media_lines(text), text)
		self.assertFalse(gm.has_embedded_media_path(text))

	def test_traversal_untouched(self):
		text = f"Attachment: {_ROOT}../../etc/passwd.png"
		self.assertEqual(gm.detect_media_paths(text), [])
		self.assertEqual(gm.strip_media_lines(text), text)

	def test_non_image_extension_untouched(self):
		text = f"Attachment: {_ROOT}tool-image-generation/report.pdf"
		self.assertEqual(gm.detect_media_paths(text), [])
		self.assertEqual(gm.strip_media_lines(text), text)
		self.assertFalse(gm.has_embedded_media_path(text))

	def test_has_embedded_media_path_stays_off_plain_prose(self):
		self.assertFalse(gm.has_embedded_media_path("a plain reply"))
		self.assertFalse(gm.has_embedded_media_path(""))
		self.assertFalse(gm.has_embedded_media_path(None))

	def test_media_marker_behaviour_is_unchanged(self):
		# The MEDIA: marker path stays leak-safety-first (unconditional strip)
		# even though the new embedded-path detector is more conservative.
		pdf = _ROOT + "tool-image-generation/report.pdf"
		self.assertTrue(gm.has_media_marker(f"MEDIA:{pdf}"))
		self.assertEqual(gm.strip_media_lines(f"MEDIA:{pdf}\nok"), "ok")

	def test_media_line_not_double_counted_as_embedded(self):
		# A MEDIA: marker line is claimed by the marker detector; the embedded
		# scanner must skip it (no double strip / double cap consumption).
		text = f"MEDIA:{_IMG}"
		self.assertEqual(gm.detect_media_paths(text), [_IMG])

	def test_cap_shared_across_marker_and_embedded(self):
		marker_lines = [f"MEDIA:{_ROOT}tool-image-generation/m{i}.png" for i in range(5)]
		embedded_lines = [f"Attachment: {_ROOT}tool-image-generation/e{i}.png" for i in range(5)]
		text = "\n".join(marker_lines + embedded_lines)
		self.assertEqual(len(gm.detect_media_paths(text)), gm._MAX_MEDIA_PER_TURN)

	def test_only_first_valid_path_per_line(self):
		a = _ROOT + "tool-image-generation/a.png"
		b = _ROOT + "tool-image-generation/b.png"
		text = f"Attachment: {a} (also see {b})"
		self.assertEqual(gm.detect_media_paths(text), [a])

	def test_no_path_untouched(self):
		text = "Attachment: nothing generated this turn."
		self.assertEqual(gm.detect_media_paths(text), [])
		self.assertEqual(gm.strip_media_lines(text), text)


def _streamed_response(status=200, body=b"PNGDATA", chunk=64 * 1024):
	"""A Mock mimicking requests' streamed response context manager."""
	resp = Mock()
	resp.status_code = status
	resp.iter_content = Mock(return_value=[body[i : i + chunk] for i in range(0, len(body), chunk)] or [b""])
	cm = Mock()
	cm.__enter__ = Mock(return_value=resp)
	cm.__exit__ = Mock(return_value=False)
	return cm


class TestFetchMedia(unittest.TestCase):
	def test_url_header_and_ws_to_http(self):
		with patch("requests.get", return_value=_streamed_response()) as rget:
			out = gm.fetch_media("ws://agent.host:9000", "tok123", _IMG)
		self.assertEqual(out, b"PNGDATA")
		args, kwargs = rget.call_args
		url = args[0]
		self.assertTrue(url.startswith("http://agent.host:9000/__openclaw__/assistant-media?source="))
		# the path is URL-encoded (slashes -> %2F), not raw
		self.assertNotIn("/home/node", url.split("source=", 1)[1])
		self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok123")
		self.assertFalse(kwargs["allow_redirects"])
		self.assertTrue(kwargs["stream"])

	def test_wss_to_https(self):
		with patch("requests.get", return_value=_streamed_response()) as rget:
			gm.fetch_media("wss://agent.host:9000", "t", _IMG)
		self.assertTrue(rget.call_args[0][0].startswith("https://agent.host:9000/"))

	def test_non_200_returns_none(self):
		for code in (302, 404, 500):
			with patch("requests.get", return_value=_streamed_response(status=code)):
				self.assertIsNone(gm.fetch_media("ws://h:1", "t", _IMG))

	def test_oversize_aborts(self):
		big = b"x" * (gm._MAX_MEDIA_BYTES + 1)
		with patch("requests.get", return_value=_streamed_response(body=big)):
			self.assertIsNone(gm.fetch_media("ws://h:1", "t", _IMG))

	def test_oversize_stops_reading_early(self):
		# Guards the "streamed, do NOT buffer whole" property: a lazy body that
		# raises if read past the cap must NOT be fully consumed.
		chunk = 64 * 1024

		def gen():
			for _ in range((gm._MAX_MEDIA_BYTES // chunk) + 1):
				yield b"x" * chunk
			raise AssertionError("fetch_media kept reading past the size cap")

		resp = Mock()
		resp.status_code = 200
		resp.iter_content = Mock(return_value=gen())
		cm = Mock()
		cm.__enter__ = Mock(return_value=resp)
		cm.__exit__ = Mock(return_value=False)
		with patch("requests.get", return_value=cm):
			self.assertIsNone(gm.fetch_media("ws://h:1", "t", _IMG))

	def test_missing_base_or_token(self):
		self.assertIsNone(gm.fetch_media("", "t", _IMG))
		self.assertIsNone(gm.fetch_media("ws://h:1", "", _IMG))

	def test_request_exception_returns_none(self):
		with patch("requests.get", side_effect=RuntimeError("boom")):
			self.assertIsNone(gm.fetch_media("ws://h:1", "t", _IMG))


class TestSeedMedia(unittest.TestCase):
	def _patch_db(self, existing_canvas=None):
		"""Patch frappe DB + save_file; capture the final set_value payload."""
		self.saved = []
		self.set_values = []

		def fake_save_file(name, content, dt, dn, is_private=1):
			self.saved.append(name)
			return Mock(file_url=f"/private/files/{name}")

		def fake_get_value(dt, dn, field):
			return existing_canvas

		def fake_set_value(dt, dn, field, value):
			self.set_values.append(value)

		p1 = patch("frappe.utils.file_manager.save_file", side_effect=fake_save_file)
		p2 = patch("frappe.db.get_value", side_effect=fake_get_value)
		p3 = patch("frappe.db.set_value", side_effect=fake_set_value)
		p4 = patch("frappe.db.commit")
		return p1, p2, p3, p4

	def test_seeds_and_builds_item(self):
		a = _ROOT + "tool-image-generation/black-hole---abcd1234.png"
		ps = self._patch_db(existing_canvas=None)
		with ps[0], ps[1], ps[2], ps[3], patch.object(gm, "fetch_media", return_value=b"PNG"):
			items = gm.seed_media("MSG-1", "ws://h:1", "tok", [a])
		self.assertEqual(len(items), 1)
		it = items[0]
		self.assertEqual(it["type"], "image")
		# source is the RELATIVE path (dedup key), NOT the brand/path-bearing absolute
		self.assertEqual(it["source"], a[len(_ROOT) :])
		self.assertNotIn("/home/node", it["source"])
		self.assertEqual(it["title"], "Black Hole")  # uuid + ext stripped
		self.assertTrue(it["file_url"].startswith("/private/files/jarvis-media-"))

	def test_skips_failed_fetch_and_logs(self):
		ps = self._patch_db(existing_canvas=None)
		with (
			ps[0],
			ps[1],
			ps[2],
			ps[3],
			patch.object(gm, "fetch_media", return_value=None),
			patch("frappe.log_error") as log,
		):
			items = gm.seed_media("MSG-1", "ws://h:1", "tok", [_IMG])
		self.assertEqual(items, [])
		self.assertEqual(self.set_values, [])  # nothing written
		log.assert_called_once()  # a silent fleet-wide degradation stays visible

	def test_dedups_by_source(self):
		import json

		# existing canvas stores the RELATIVE source key (what seed_media writes)
		existing = json.dumps([{"name": "x", "type": "image", "source": _IMG_REL}])
		ps = self._patch_db(existing_canvas=existing)
		with ps[0], ps[1], ps[2], ps[3], patch.object(gm, "fetch_media", return_value=b"PNG") as fm:
			items = gm.seed_media("MSG-1", "ws://h:1", "tok", [_IMG])
		self.assertEqual(items, [])
		fm.assert_not_called()  # already present (by rel key) -> never fetched

	def test_empty_sources(self):
		with patch.object(gm, "fetch_media") as fm:
			self.assertEqual(gm.seed_media("MSG-1", "ws://h:1", "tok", []), [])
			fm.assert_not_called()


_URL_ITEM = {
	"url": "/api/chat/media/outgoing/sk-enc/12345678-1234-1234-1234-123456789012/full.png",
	"mime_type": "image/png",
}


class TestSeedMediaUrls(unittest.TestCase):
	"""Gateway-attached content blocks (image/video/audio/document on the final
	chat message itself) - a distinct delivery mechanism from seed_media's
	native MEDIA:/embedded-path markers, but the same idempotent-dedup shape."""

	def _patch_db(self, existing_canvas=None):
		self.saved = []
		self.set_values = []

		def fake_save_file(name, content, dt, dn, is_private=1):
			self.saved.append(name)
			return Mock(file_url=f"/private/files/{name}")

		def fake_get_value(dt, dn, field):
			return existing_canvas

		def fake_set_value(dt, dn, field, value):
			self.set_values.append(value)

		p1 = patch("frappe.utils.file_manager.save_file", side_effect=fake_save_file)
		p2 = patch("frappe.db.get_value", side_effect=fake_get_value)
		p3 = patch("frappe.db.set_value", side_effect=fake_set_value)
		p4 = patch("frappe.db.commit")
		return p1, p2, p3, p4

	def test_seeds_and_builds_item(self):
		ps = self._patch_db(existing_canvas=None)
		with ps[0], ps[1], ps[2], ps[3], patch.object(gm, "fetch_media_url", return_value=b"PNG") as fm:
			items = gm.seed_media_urls("MSG-1", "ws://h:1", "tok", [_URL_ITEM])
		fm.assert_called_once_with("ws://h:1", "tok", _URL_ITEM["url"], mime_type="image/png")
		self.assertEqual(len(items), 1)
		it = items[0]
		self.assertEqual(it["type"], "image")
		self.assertEqual(it["source"], "12345678-1234-1234-1234-123456789012")  # attachment id only
		self.assertNotIn("sk-enc", it["source"])  # never the session-bearing url
		self.assertTrue(it["file_url"].startswith("/private/files/jarvis-media-"))

	def test_skips_failed_fetch_and_logs(self):
		ps = self._patch_db(existing_canvas=None)
		with (
			ps[0],
			ps[1],
			ps[2],
			ps[3],
			patch.object(gm, "fetch_media_url", return_value=None),
			patch("frappe.log_error") as log,
		):
			items = gm.seed_media_urls("MSG-1", "ws://h:1", "tok", [_URL_ITEM])
		self.assertEqual(items, [])
		log.assert_called_once()

	def test_dedups_by_attachment_id(self):
		import json

		existing = json.dumps(
			[{"name": "x", "type": "image", "source": "12345678-1234-1234-1234-123456789012"}]
		)
		ps = self._patch_db(existing_canvas=existing)
		with ps[0], ps[1], ps[2], ps[3], patch.object(gm, "fetch_media_url", return_value=b"PNG") as fm:
			items = gm.seed_media_urls("MSG-1", "ws://h:1", "tok", [_URL_ITEM])
		self.assertEqual(items, [])
		fm.assert_not_called()

	def test_empty_items(self):
		with patch.object(gm, "fetch_media_url") as fm:
			self.assertEqual(gm.seed_media_urls("MSG-1", "ws://h:1", "tok", []), [])
			fm.assert_not_called()

	def test_type_derived_from_mime(self):
		video_item = {"url": _URL_ITEM["url"].replace(".png", ".mp4"), "mime_type": "video/mp4"}
		ps = self._patch_db(existing_canvas=None)
		with ps[0], ps[1], ps[2], ps[3], patch.object(gm, "fetch_media_url", return_value=b"VID"):
			items = gm.seed_media_urls("MSG-1", "ws://h:1", "tok", [video_item])
		self.assertEqual(items[0]["type"], "video")


def _streamed_media_url_response(status=200, body=b"PNGDATA", content_type="image/png"):
	cm = _streamed_response(status=status, body=body)
	cm.__enter__.return_value.headers = {"Content-Type": content_type}
	return cm


class TestFetchMediaUrl(unittest.TestCase):
	def test_url_and_header(self):
		with patch("requests.get", return_value=_streamed_media_url_response()) as rget:
			out = gm.fetch_media_url(
				"ws://agent.host:9000", "tok123", _URL_ITEM["url"], mime_type="image/png"
			)
		self.assertEqual(out, b"PNGDATA")
		args, kwargs = rget.call_args
		self.assertEqual(args[0], "http://agent.host:9000" + _URL_ITEM["url"])
		self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok123")
		self.assertFalse(kwargs["allow_redirects"])

	def test_content_type_mismatch_rejected(self):
		with patch("requests.get", return_value=_streamed_media_url_response(content_type="text/html")):
			out = gm.fetch_media_url("ws://h:1", "t", _URL_ITEM["url"], mime_type="image/png")
		self.assertIsNone(out)

	def test_declared_mime_type_accepted_even_when_not_image(self):
		with patch(
			"requests.get",
			return_value=_streamed_media_url_response(content_type="application/pdf"),
		):
			out = gm.fetch_media_url("ws://h:1", "t", _URL_ITEM["url"], mime_type="application/pdf")
		self.assertEqual(out, b"PNGDATA")

	def test_missing_base_or_token(self):
		self.assertIsNone(gm.fetch_media_url("", "t", _URL_ITEM["url"]))
		self.assertIsNone(gm.fetch_media_url("ws://h:1", "", _URL_ITEM["url"]))

	def test_non_200_returns_none(self):
		with patch("requests.get", return_value=_streamed_media_url_response(status=404)):
			self.assertIsNone(gm.fetch_media_url("ws://h:1", "t", _URL_ITEM["url"], mime_type="image/png"))


class TestTitleAndFilename(unittest.TestCase):
	def test_pretty_title_strips_uuid(self):
		self.assertEqual(
			gm._pretty_media_title("black-hole---4de239c0-8d05-41e5-9f52-e404ee9f0b21.png"),
			"Black Hole",
		)
		self.assertEqual(gm._pretty_media_title("sales_report.png"), "Sales Report")

	def test_pretty_title_length_capped(self):
		self.assertLessEqual(len(gm._pretty_media_title("a" * 500 + ".png")), 80)

	def test_safe_filename(self):
		self.assertTrue(gm._safe_media_filename("a.png").startswith("jarvis-media-"))
		self.assertTrue(gm._safe_media_filename("a.png").endswith(".png"))
		# non-image ext -> forced .png
		self.assertTrue(gm._safe_media_filename("weird.bin").endswith(".png"))


class TestRedactFinalWithMedia(unittest.TestCase):
	"""The shared terminal helper: consume the marker BEFORE egress, return
	(redacted-and-stripped text, media_rels)."""

	def test_strips_before_redact_and_returns_rels(self):
		from jarvis.chat import egress_rules

		# Identity redact so we isolate the strip+detect behaviour from DB rules.
		with patch("jarvis.chat.egress_rules.redact_and_flag", side_effect=lambda t, **k: t):
			text, rels, marked = egress_rules.redact_final_with_media(
				f"Here you go:\nMEDIA:{_IMG}", run_id="R1"
			)
		self.assertEqual(text, "Here you go:")  # MEDIA line stripped before redact
		self.assertEqual(rels, [_IMG])
		self.assertTrue(marked)

	def test_non_raster_marker_stripped_flag_without_rels(self):
		from jarvis.chat import egress_rules

		# A .pdf marker under the media root: stripped (marker_stripped True) but NOT
		# a fetchable image (rels empty) -> the gate still forces the content clear.
		pdf = _ROOT + "tool-image-generation/report.pdf"
		with patch("jarvis.chat.egress_rules.redact_and_flag", side_effect=lambda t, **k: t):
			text, rels, marked = egress_rules.redact_final_with_media(f"MEDIA:{pdf}")
		self.assertEqual(text, "")
		self.assertEqual(rels, [])
		self.assertTrue(marked)

	def test_embedded_attachment_line_strips_and_marks(self):
		# has_media_marker alone stays MEDIA:-only (a separate leak-safety gate) -
		# redact_final_with_media is the caller that ALSO considers embedded
		# paths, so an Attachment:-only reply still forces the content overwrite.
		from jarvis.chat import egress_rules

		with patch("jarvis.chat.egress_rules.redact_and_flag", side_effect=lambda t, **k: t):
			text, rels, marked = egress_rules.redact_final_with_media(f"Attachment: {_IMG}", run_id="R1")
		self.assertEqual(text, "")
		self.assertEqual(rels, [_IMG])
		self.assertTrue(marked)

	def test_no_marker_returns_empty_rels(self):
		from jarvis.chat import egress_rules

		with patch("jarvis.chat.egress_rules.redact_and_flag", side_effect=lambda t, **k: t):
			text, rels, marked = egress_rules.redact_final_with_media("A plain reply.", run_id="R1")
		self.assertEqual(text, "A plain reply.")
		self.assertEqual(rels, [])
		self.assertFalse(marked)

	def test_none_text(self):
		from jarvis.chat import egress_rules

		with patch("jarvis.chat.egress_rules.redact_and_flag", side_effect=lambda t, **k: t):
			text, rels, marked = egress_rules.redact_final_with_media(None)
		self.assertIsNone(text)
		self.assertEqual(rels, [])
		self.assertFalse(marked)


class TestPumpThreading(unittest.TestCase):
	"""finalize._effect_rich_outputs must re-read media_rels off the Turn row and
	pass it to persist_rich_outputs — the pump (default) transport delivery path
	that a direct-relay-only wiring would silently drop."""

	def test_effect_reads_media_rels_from_turn_row(self):
		import json

		from jarvis.chat import finalize

		ctx = Mock()
		ctx.turn = {"assistant_message": "MSG-1"}
		ctx.conversation = "C1"
		ctx.owner = "u@x"
		ctx.run_id = "R1"

		payload_json = json.dumps({"text": "clean reply", "media_rels": [_IMG]})

		def fake_get_value(dt, dn, field):
			if field == "terminal_payload":
				return payload_json
			return "2026-09-10 00:00:00"  # dispatching_at for _turn_start_ms

		with (
			patch("frappe.db.get_value", side_effect=fake_get_value),
			patch("jarvis.chat.turn_handler.persist_rich_outputs") as pro,
		):
			finalize._effect_rich_outputs(ctx)

		pro.assert_called_once()
		self.assertEqual(pro.call_args.kwargs.get("media_rels"), [_IMG])

	def test_effect_no_media_rels_passes_none(self):
		import json

		from jarvis.chat import finalize

		ctx = Mock()
		ctx.turn = {"assistant_message": "MSG-1"}
		ctx.conversation = "C1"
		ctx.owner = "u@x"
		ctx.run_id = "R1"

		def fake_get_value(dt, dn, field):
			if field == "terminal_payload":
				return json.dumps({"text": "clean reply"})
			return "2026-09-10 00:00:00"

		with (
			patch("frappe.db.get_value", side_effect=fake_get_value),
			patch("jarvis.chat.turn_handler.persist_rich_outputs") as pro,
		):
			finalize._effect_rich_outputs(ctx)

		self.assertIsNone(pro.call_args.kwargs.get("media_rels"))


class TestSeedBlockWiring(unittest.TestCase):
	"""persist_rich_outputs' third block: when media_rels is present it must call
	seed_media with the right args and publish a cumulative 'canvas' event."""

	def test_media_block_seeds_and_publishes_cumulative(self):
		import json

		from jarvis.chat import turn_handler

		settings = Mock(agent_url="ws://h:1")
		settings.get_password = Mock(return_value="tok")

		def fake_get_value(dt, dn, field):
			if field == "content":
				return ""  # no canvas refs, no "imagegen" -> first two blocks no-op
			if field == "canvas":
				return json.dumps([{"name": "chart", "type": "svg"}, {"name": "img", "type": "image"}])
			return None

		media_item = {"name": "img", "type": "image", "source": _IMG_REL}
		published = []
		with (
			patch("frappe.get_single", return_value=settings),
			patch("frappe.db.get_value", side_effect=fake_get_value),
			patch("jarvis.chat.canvas.persist_canvases", return_value=[]),
			patch("jarvis.chat.generated_media.seed_media", return_value=[media_item]) as sm,
			patch("jarvis.chat.turn_handler._publish_to_user", side_effect=lambda u, p: published.append(p)),
		):
			turn_handler.persist_rich_outputs("MSG-1", "C1", "u@x", "run1", 0, media_rels=[_IMG])

		sm.assert_called_once_with("MSG-1", "ws://h:1", "tok", [_IMG])
		canvas_pubs = [p for p in published if p.get("kind") == "canvas"]
		self.assertEqual(len(canvas_pubs), 1)
		# cumulative: the published items include the pre-existing chart, not just media
		self.assertEqual(len(canvas_pubs[0]["items"]), 2)

	def test_no_media_rels_skips_seed(self):
		from jarvis.chat import turn_handler

		settings = Mock(agent_url="ws://h:1")
		settings.get_password = Mock(return_value="tok")
		with (
			patch("frappe.get_single", return_value=settings),
			patch("frappe.db.get_value", return_value=""),
			patch("jarvis.chat.canvas.persist_canvases", return_value=[]),
			patch("jarvis.chat.generated_media.seed_media") as sm,
			patch("jarvis.chat.turn_handler._publish_to_user"),
		):
			turn_handler.persist_rich_outputs("MSG-1", "C1", "u@x", "run1", 0, media_rels=None)
		sm.assert_not_called()

	def test_no_media_rels_but_not_yield_continuation_skips_harvest(self):
		from jarvis.chat import turn_handler

		with patch.object(turn_handler, "_harvest_post_yield_media") as harvest:
			with (
				patch("frappe.get_single", return_value=Mock(agent_url="ws://h:1")),
				patch("frappe.db.get_value", return_value=""),
				patch("jarvis.chat.canvas.persist_canvases", return_value=[]),
				patch("jarvis.chat.turn_handler._publish_to_user"),
			):
				turn_handler.persist_rich_outputs("MSG-1", "C1", "u@x", "run1", 0)
		harvest.assert_not_called()

	def test_yield_continuation_with_media_already_present_skips_harvest(self):
		from jarvis.chat import turn_handler

		with patch.object(turn_handler, "_harvest_post_yield_media") as harvest:
			with (
				patch("frappe.get_single", return_value=Mock(agent_url="ws://h:1")),
				patch("frappe.db.get_value", return_value=""),
				patch("jarvis.chat.canvas.persist_canvases", return_value=[]),
				patch("jarvis.chat.generated_media.seed_media", return_value=[]),
				patch("jarvis.chat.turn_handler._publish_to_user"),
			):
				turn_handler.persist_rich_outputs(
					"MSG-1", "C1", "u@x", "run1", 0, media_rels=[_IMG], yield_continuation=True
				)
		harvest.assert_not_called()

	def test_yield_continuation_with_no_media_harvests_and_seeds(self):
		from jarvis.chat import turn_handler

		settings = Mock(agent_url="ws://h:1")
		settings.get_password = Mock(return_value="tok")
		with patch.object(turn_handler, "_harvest_post_yield_media", return_value=([_IMG], [])) as harvest:
			with (
				patch("frappe.get_single", return_value=settings),
				patch("frappe.db.get_value", return_value=""),
				patch("jarvis.chat.canvas.persist_canvases", return_value=[]),
				patch("jarvis.chat.generated_media.seed_media", return_value=[]) as sm,
				patch("jarvis.chat.turn_handler._publish_to_user"),
			):
				turn_handler.persist_rich_outputs("MSG-1", "C1", "u@x", "run1", 123, yield_continuation=True)
		harvest.assert_called_once_with("C1", 123)
		sm.assert_called_once_with("MSG-1", "ws://h:1", "tok", [_IMG])


class TestRecoveryStrip(unittest.TestCase):
	"""Recovery (D1): _latest_assistant_text strips the marker at each return
	(string / block-list / text), leaking no raw path, delivering no image."""

	def test_string_content_marker_stripped(self):
		from jarvis.chat import turn_recovery

		out = turn_recovery._latest_assistant_text([{"role": "assistant", "content": f"done\nMEDIA:{_IMG}"}])
		self.assertNotIn("MEDIA:", out)
		self.assertNotIn("/home/node", out)

	def test_block_list_content_marker_stripped(self):
		from jarvis.chat import turn_recovery

		msg = {"role": "assistant", "content": [{"type": "text", "text": f"ok\nMEDIA:{_IMG}"}]}
		out = turn_recovery._latest_assistant_text([msg])
		self.assertNotIn("MEDIA:", out)
		self.assertNotIn("/home/node", out)


_URL_MSG_IMG = "/api/chat/media/outgoing/sk/12345678-1234-1234-1234-123456789012/full.png"


class TestScanHistoryForMedia(unittest.TestCase):
	"""The deferred-reply yield's actual media arrives as SEPARATE transcript
	messages after the continuation's own final - _scan_history_for_media is
	the pure extractor _harvest_post_yield_media polls chat.history with."""

	def test_media_line_extracted(self):
		from jarvis.chat.turn_handler import _scan_history_for_media

		msg = {"role": "assistant", "content": f"here\nMEDIA:{_IMG}", "timestamp": 5000}
		rels, urls = _scan_history_for_media([msg], turn_start_ms=1000)
		self.assertEqual(rels, [_IMG])
		self.assertEqual(urls, [])

	def test_image_block_extracted(self):
		from jarvis.chat.turn_handler import _scan_history_for_media

		msg = {
			"role": "assistant",
			"content": [{"type": "image", "url": _URL_MSG_IMG, "mimeType": "image/png"}],
			"timestamp": 5000,
		}
		rels, urls = _scan_history_for_media([msg], turn_start_ms=1000)
		self.assertEqual(rels, [])
		self.assertEqual(urls, [{"url": _URL_MSG_IMG, "mime_type": "image/png"}])

	def test_timestamp_filter_excludes_older_messages(self):
		from jarvis.chat.turn_handler import _scan_history_for_media

		old = {"role": "assistant", "content": f"MEDIA:{_IMG}", "timestamp": 500}
		rels, urls = _scan_history_for_media([old], turn_start_ms=1000)
		self.assertEqual((rels, urls), ([], []))

	def test_non_assistant_role_skipped(self):
		from jarvis.chat.turn_handler import _scan_history_for_media

		msg = {"role": "user", "content": f"MEDIA:{_IMG}", "timestamp": 5000}
		rels, urls = _scan_history_for_media([msg], turn_start_ms=1000)
		self.assertEqual((rels, urls), ([], []))

	def test_media_line_preferred_over_image_block(self):
		# The live capture shows the runtime posting the SAME image both ways -
		# a MEDIA: text message AND an image-block message. Only one must seed.
		from jarvis.chat.turn_handler import _scan_history_for_media

		media_msg = {"role": "assistant", "content": f"MEDIA:{_IMG}", "timestamp": 5000}
		block_msg = {
			"role": "assistant",
			"content": [{"type": "image", "url": _URL_MSG_IMG, "mimeType": "image/png"}],
			"timestamp": 5001,
		}
		rels, urls = _scan_history_for_media([media_msg, block_msg], turn_start_ms=1000)
		self.assertEqual(rels, [_IMG])
		self.assertEqual(urls, [])

	def test_no_timestamp_field_is_not_filtered_out(self):
		from jarvis.chat.turn_handler import _scan_history_for_media

		msg = {"role": "assistant", "content": f"MEDIA:{_IMG}"}
		rels, urls = _scan_history_for_media([msg], turn_start_ms=1000)
		self.assertEqual(rels, [_IMG])

	def test_nothing_found_returns_empty(self):
		from jarvis.chat.turn_handler import _scan_history_for_media

		msg = {"role": "assistant", "content": "just a normal reply", "timestamp": 5000}
		self.assertEqual(_scan_history_for_media([msg], turn_start_ms=1000), ([], []))
		self.assertEqual(_scan_history_for_media([], turn_start_ms=1000), ([], []))


class TestHarvestPostYieldMediaPolling(unittest.TestCase):
	"""_harvest_post_yield_media: polls chat.history on the pooled gateway
	connection every _YIELD_MEDIA_POLL_INTERVAL_S, stops on the first hit,
	gives up after _YIELD_MEDIA_POLL_TIMEOUT_S."""

	def _fake_checkout(self, sess):
		import contextlib

		@contextlib.contextmanager
		def checkout(url):
			yield sess

		return checkout

	def test_stops_polling_on_first_hit(self):
		from jarvis.chat import turn_handler

		calls = {"n": 0}

		def fake_get_history(sk, limit=20):
			calls["n"] += 1
			if calls["n"] < 3:
				return {"messages": []}
			return {"messages": [{"role": "assistant", "content": f"MEDIA:{_IMG}", "timestamp": 999999}]}

		sess = Mock()
		sess.get_history = Mock(side_effect=fake_get_history)
		settings = Mock(agent_url="ws://h:1")

		with (
			patch("frappe.db.get_value", return_value="sk-1"),
			patch("frappe.get_single", return_value=settings),
			patch("jarvis.chat.agent_session_pool.checkout", self._fake_checkout(sess)),
			patch("time.sleep", return_value=None),
		):
			rels, urls = turn_handler._harvest_post_yield_media("C1", turn_start_ms=0)
		self.assertEqual(rels, [_IMG])
		self.assertEqual(calls["n"], 3)

	def test_gives_up_after_the_cap(self):
		from jarvis.chat import turn_handler

		calls = {"n": 0}

		def fake_get_history(sk, limit=20):
			calls["n"] += 1
			return {"messages": []}

		sess = Mock()
		sess.get_history = Mock(side_effect=fake_get_history)
		settings = Mock(agent_url="ws://h:1")

		clock = {"now": 0.0}

		def fake_monotonic():
			return clock["now"]

		def fake_sleep(s):
			clock["now"] += s

		with (
			patch("frappe.db.get_value", return_value="sk-1"),
			patch("frappe.get_single", return_value=settings),
			patch("jarvis.chat.agent_session_pool.checkout", self._fake_checkout(sess)),
			patch("time.sleep", side_effect=fake_sleep),
			patch("time.monotonic", side_effect=fake_monotonic),
		):
			rels, urls = turn_handler._harvest_post_yield_media("C1", turn_start_ms=0)
		self.assertEqual((rels, urls), ([], []))
		self.assertGreaterEqual(calls["n"], 1)

	def test_missing_session_key_short_circuits(self):
		from jarvis.chat import turn_handler

		with (
			patch("frappe.db.get_value", return_value=None),
			patch("frappe.get_single", return_value=Mock(agent_url="ws://h:1")),
		):
			self.assertEqual(turn_handler._harvest_post_yield_media("C1", turn_start_ms=0), ([], []))


if __name__ == "__main__":
	unittest.main()
