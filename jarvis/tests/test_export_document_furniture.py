"""Tests for ``jarvis.tools._export.document.furniture.render_pdf`` (the rich-PDF
render core: security flags + a real subprocess render timeout) and its
letterhead resolution (default lookup, read-perm, image inlining).

wkhtmltopdf is ABSENT in local dev (brew removed it), so this suite NEVER runs a
real render - that is the Frappe Cloud smoke gate. Instead ``subprocess.run`` is
mocked and ``_resolve_binary`` is stubbed to a fake path, and the assertions
cover:
  * the UNCONDITIONAL security/fidelity arg list (looped; drop a flag ->
    the matching case fails - the mutation target);
  * page-geometry validation (orientation / page_size / margins);
  * header/footer/watermark are run through ``sanitize_rich`` before the temp
    file is written (a mock reads each temp file WHILE it still exists);
  * temp-file lifecycle: created for the call, gone after return, removed on a
    ``TimeoutExpired`` / ``OSError`` / non-zero exit, and the FIRST temp cleaned
    when the SECOND write fails;
  * output guards: empty output, non-zero exit (+stderr tail), non-PDF output,
    and that every infra failure is logged;
  * letterhead resolution: image inlining (data kept / remote dropped /
    same-site base64 with perm check) and the not-found degrade note.

The real ``get_letter_head`` + real render are gated (``skipUnless`` a site) and
belong to the FC smoke gate.
"""

import os
import re
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._export.document import furniture
from jarvis.tools._export.document.furniture import render_pdf, resolve_letterhead

_HAS_SITE = bool(getattr(frappe.local, "site", None))

# The security/fidelity flags that MUST appear on every render, unconditionally.
_REQUIRED_FLAGS = [
	"--disable-local-file-access",
	"--disable-javascript",
	"--background",
	"--images",
	"--print-media-type",
	"--disable-smart-shrinking",
	"--quiet",
]


class _Recorder:
	"""Stand-in for ``subprocess.run``. Records the arg list / stdin / timeout,
	and - crucially - reads any ``--header-html``/``--footer-html`` temp file
	WHILE the call is in flight (before the ``finally`` cleanup deletes it), so a
	test can assert both the file's sanitized content and that it existed at call
	time."""

	def __init__(self) -> None:
		self.args: list[str] | None = None
		self.input: bytes | None = None
		self.timeout: int | None = None
		self.file_contents: dict[str, str] = {}
		self.files_existed: dict[str, bool] = {}
		self.returncode = 0
		self.stdout = b"%PDF-1.4 fake pdf bytes"
		self.stderr = b""
		self.raise_timeout = False
		self.raise_oserror = False

	def __call__(self, args, input=None, capture_output=False, timeout=None):
		self.args = list(args)
		self.input = input
		self.timeout = timeout
		for flag in ("--header-html", "--footer-html"):
			if flag in self.args:
				path = self.args[self.args.index(flag) + 1]
				self.files_existed[path] = os.path.exists(path)
				with open(path, encoding="utf-8") as fh:
					self.file_contents[flag] = fh.read()
		if self.raise_timeout:
			raise subprocess.TimeoutExpired(args, timeout)
		if self.raise_oserror:
			raise OSError("wkhtmltopdf failed to execute")
		return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)

	def path_for(self, flag: str) -> str:
		assert self.args is not None
		return self.args[self.args.index(flag) + 1]


class _FurnitureRenderBase(unittest.TestCase):
	"""Stub the binary resolver + ``subprocess.run`` so ``render_pdf`` runs its
	full logic (temp files, arg build, cleanup) without a real wkhtmltopdf."""

	def setUp(self) -> None:
		self.fake_wk = _Recorder()
		patch.object(furniture, "_resolve_binary", lambda: "/usr/bin/wkhtmltopdf").start()
		patch.object(furniture.subprocess, "run", self.fake_wk).start()
		self.addCleanup(patch.stopall)


# --- arg list: unconditional security/fidelity flags ------------------------


class TestRequiredFlags(_FurnitureRenderBase):
	def test_required_flag_always_present(self) -> None:
		# No header/footer/watermark, page numbers off: the leanest render still
		# carries every security/fidelity flag. Dropping any from _build_args fails
		# exactly this case (the mutation target).
		for flag in _REQUIRED_FLAGS:
			with self.subTest(flag=flag):
				render_pdf("<p>hi</p>", page_numbers=False)
				self.assertIn(flag, self.fake_wk.args)

	def test_encoding_flag_value(self) -> None:
		render_pdf("<p>hi</p>", page_numbers=False)
		i = self.fake_wk.args.index("--encoding")
		self.assertEqual(self.fake_wk.args[i + 1], "utf-8")

	def test_page_size_default_and_custom(self) -> None:
		render_pdf("<p>hi</p>", page_numbers=False)
		self.assertEqual(self.fake_wk.args[self.fake_wk.args.index("--page-size") + 1], "A4")
		render_pdf("<p>hi</p>", page_size="letter", page_numbers=False)
		# canonicalized spelling
		self.assertEqual(self.fake_wk.args[self.fake_wk.args.index("--page-size") + 1], "Letter")

	def test_page_size_invalid_raises(self) -> None:
		with self.assertRaises(InvalidArgumentError):
			render_pdf("<p>hi</p>", page_size="A2", page_numbers=False)

	def test_orientation_portrait_default(self) -> None:
		render_pdf("<p>hi</p>", page_numbers=False)
		self.assertEqual(self.fake_wk.args[self.fake_wk.args.index("--orientation") + 1], "Portrait")

	def test_orientation_landscape(self) -> None:
		render_pdf("<p>hi</p>", orientation="landscape", page_numbers=False)
		self.assertEqual(self.fake_wk.args[self.fake_wk.args.index("--orientation") + 1], "Landscape")

	def test_orientation_invalid_raises(self) -> None:
		with self.assertRaises(InvalidArgumentError):
			render_pdf("<p>hi</p>", orientation="sideways")

	def test_margins_all_four_sides(self) -> None:
		render_pdf("<p>hi</p>", margins_mm=20, page_numbers=False)
		for side in ("--margin-top", "--margin-bottom", "--margin-left", "--margin-right"):
			self.assertEqual(self.fake_wk.args[self.fake_wk.args.index(side) + 1], "20mm")

	def test_margins_negative_raises(self) -> None:
		# A negative margin would produce a "-5mm" token wkhtmltopdf could mis-parse
		# as a flag; reject it app-side like orientation.
		with self.assertRaises(InvalidArgumentError):
			render_pdf("<p>hi</p>", margins_mm=-5)

	def test_margins_too_large_raises(self) -> None:
		with self.assertRaises(InvalidArgumentError):
			render_pdf("<p>hi</p>", margins_mm=500)

	def test_reads_stdin_writes_stdout(self) -> None:
		render_pdf("<p>hi</p>", page_numbers=False)
		self.assertEqual(self.fake_wk.args[-2:], ["-", "-"])

	def test_body_passed_as_input_bytes(self) -> None:
		render_pdf("<p>UNIQUEBODYMARKER</p>", page_numbers=False)
		self.assertTrue(isinstance(self.fake_wk.input, bytes))
		self.assertIn(b"UNIQUEBODYMARKER", self.fake_wk.input)

	def test_timeout_passed_through_to_subprocess(self) -> None:
		render_pdf("<p>hi</p>", timeout=13, page_numbers=False)
		self.assertEqual(self.fake_wk.timeout, 13)

	def test_oversized_furniture_rejected(self) -> None:
		# Header/footer/watermark are agent-influenced; an unbounded one is refused
		# before the pre-render sanitize (the body is capped upstream).
		with self.assertRaises(InvalidArgumentError):
			render_pdf("<p>hi</p>", header_html="x" * 20_001)


# --- header/footer/watermark are sanitized before hitting the temp file ------


class TestFurnitureSanitization(_FurnitureRenderBase):
	def test_header_is_sanitized(self) -> None:
		render_pdf("<p>body</p>", header_html='<img src="http://evil"><script>x</script><b>keepme</b>')
		content = self.fake_wk.file_contents["--header-html"]
		low = content.lower()
		self.assertNotIn("<img", low)
		self.assertNotIn("evil", low)
		self.assertNotIn("<script", low)
		self.assertIn("keepme", content)

	def test_footer_text_is_plain_in_text_zone(self) -> None:
		# The footer is now a TEXT zone (--footer-left), not an HTML doc: tags are
		# stripped to plain text so it coexists with the page-number zone, and no
		# HTML/img can reach a footer render at all.
		render_pdf("<p>body</p>", footer_text='<img src="http://evil"><b>footkeep</b>')
		self.assertNotIn("--footer-html", self.fake_wk.args)
		zone = self.fake_wk.args[self.fake_wk.args.index("--footer-left") + 1]
		self.assertNotIn("<", zone)
		self.assertNotIn("evil", zone)
		self.assertIn("footkeep", zone)

	def test_watermark_is_sanitized_and_rotated_in_header(self) -> None:
		render_pdf("<p>body</p>", watermark='DRAFT<img src="http://evil"><script>x</script>')
		content = self.fake_wk.file_contents["--header-html"]
		low = content.lower()
		self.assertNotIn("<img", low)
		self.assertNotIn("evil", low)
		self.assertNotIn("<script", low)
		self.assertIn("jv-watermark", content)
		self.assertIn("rotate(-45deg)", content)
		self.assertIn("DRAFT", content)


# --- temp-file lifecycle: created, then removed (success + failures) ---------


class TestTempFileLifecycle(_FurnitureRenderBase):
	def test_temp_file_exists_during_call_then_removed(self) -> None:
		# Only the running HEADER is a temp file now (the footer is text zones).
		render_pdf("<p>body</p>", header_html="<b>H</b>")
		path = self.fake_wk.path_for("--header-html")
		self.assertTrue(self.fake_wk.files_existed[path], "--header-html temp missing during call")
		self.assertFalse(os.path.exists(path), "--header-html temp not cleaned up after return")

	def test_timeout_cleans_temp_and_raises_clean_error(self) -> None:
		self.fake_wk.raise_timeout = True
		with self.assertRaises(InvalidArgumentError) as cm:
			render_pdf("<p>body</p>", header_html="<b>H</b>", timeout=7)
		self.assertIn("7s", str(cm.exception))
		self.assertFalse(isinstance(cm.exception, subprocess.TimeoutExpired))
		self.assertFalse(os.path.exists(self.fake_wk.path_for("--header-html")))

	def test_oserror_from_exec_is_clean_and_cleans_temp(self) -> None:
		# The resolved binary fails to EXECUTE (OSError, not TimeoutExpired) → clean
		# InvalidArgumentError, never a raw OSError, and temp files are cleaned.
		self.fake_wk.raise_oserror = True
		with self.assertRaises(InvalidArgumentError) as cm:
			render_pdf("<p>body</p>", header_html="<b>H</b>")
		self.assertFalse(isinstance(cm.exception, OSError))
		self.assertFalse(os.path.exists(self.fake_wk.path_for("--header-html")))

	def test_render_exception_still_cleans_temp(self) -> None:
		self.fake_wk.returncode = 1
		self.fake_wk.stderr = b"boom: qt render blew up"
		with self.assertRaises(InvalidArgumentError):
			render_pdf("<p>body</p>", header_html="<b>H</b>")
		# temp cleaned even on a non-zero exit (this test's point)
		self.assertFalse(os.path.exists(self.fake_wk.path_for("--header-html")))

	def test_two_calls_use_distinct_temp_names(self) -> None:
		render_pdf("<p>a</p>", header_html="<b>H</b>")
		first = self.fake_wk.path_for("--header-html")
		render_pdf("<p>b</p>", header_html="<b>H</b>")
		second = self.fake_wk.path_for("--header-html")
		self.assertNotEqual(first, second)


class TestWriteTempPartialCleanup(unittest.TestCase):
	def test_write_temp_removes_partial_on_write_failure(self) -> None:
		class _BadFile:
			def __enter__(self):
				return self

			def __exit__(self, *_a):
				return False

			def write(self, _data):
				raise OSError("disk full mid-write")

		removed: list[str] = []
		tmp_dir = tempfile.mkdtemp()
		self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
		patch.object(furniture, "open", lambda *_a, **_k: _BadFile(), create=True).start()
		patch.object(furniture, "_remove_quietly", lambda p: removed.append(p)).start()
		self.addCleanup(patch.stopall)
		with self.assertRaises(OSError):
			furniture._write_temp(tmp_dir, "content")
		self.assertTrue(removed, "partial file was not cleaned up on write failure")


# --- output guards -----------------------------------------------------------


class TestOutputGuards(_FurnitureRenderBase):
	def test_returns_pdf_bytes_on_success(self) -> None:
		out = render_pdf("<p>hi</p>", page_numbers=False)
		self.assertEqual(out, b"%PDF-1.4 fake pdf bytes")

	def test_empty_output_raises(self) -> None:
		self.fake_wk.stdout = b""
		with self.assertRaises(InvalidArgumentError):
			render_pdf("<p>hi</p>", page_numbers=False)

	def test_nonpdf_output_raises(self) -> None:
		# Non-empty but not a PDF (an error page slipping past exit 0) is rejected.
		self.fake_wk.stdout = b"<html><body>gateway error</body></html>"
		with self.assertRaises(InvalidArgumentError):
			render_pdf("<p>hi</p>", page_numbers=False)

	def test_nonzero_exit_logs_stderr_but_keeps_it_out_of_user_message(self) -> None:
		# stderr can carry the /tmp/jv-pdf-<hash>.html temp path — it goes to the
		# Error Log, NOT the user-facing message (which stays clean).
		logged: list[dict] = []
		patch.object(furniture.frappe, "log_error", lambda **k: logged.append(k)).start()
		self.fake_wk.returncode = 3
		self.fake_wk.stderr = b"unique-stderr-signature /tmp/jv-pdf-abc.html"
		with self.assertRaises(InvalidArgumentError) as cm:
			render_pdf("<p>hi</p>", page_numbers=False)
		self.assertNotIn("unique-stderr-signature", str(cm.exception))
		self.assertIn("3", str(cm.exception))  # the exit code is fine to surface
		self.assertTrue(any("unique-stderr-signature" in str(k.get("message", "")) for k in logged))

	def test_infra_failure_is_logged(self) -> None:
		# A working binary misbehaving (non-zero exit) must hit the Error Log so a
		# systemic regression is visible to operators, not only surfaced to the user.
		logged: list[dict] = []
		patch.object(furniture.frappe, "log_error", lambda **k: logged.append(k)).start()
		self.fake_wk.returncode = 2
		self.fake_wk.stderr = b"err"
		with self.assertRaises(InvalidArgumentError):
			render_pdf("<p>hi</p>", page_numbers=False)
		self.assertTrue(logged, "infra render failure was not logged")


# --- binary resolution -------------------------------------------------------


class TestBinaryResolution(unittest.TestCase):
	def test_binary_not_found_raises_clean_error(self) -> None:
		def _no_config(*_a, **_k):
			raise OSError("No wkhtmltopdf executable found")

		patch.object(furniture.pdfkit, "configuration", _no_config).start()
		patch.object(furniture.shutil, "which", lambda _name: None).start()
		self.addCleanup(patch.stopall)
		with self.assertRaises(InvalidArgumentError):
			render_pdf("<p>hi</p>")


# --- page numbering ----------------------------------------------------------


class TestPageNumbering(_FurnitureRenderBase):
	def test_page_numbers_use_text_footer_right_not_html(self) -> None:
		# Page numbers go through a TEXT footer zone (--footer-right), which
		# substitutes [page]/[topage] in wkhtmltopdf's C++ layer and so works under
		# --disable-javascript. An HTML footer's bracket substitution needs JS, so
		# it would print a literal "[page]" (the staging-smoke bug).
		render_pdf("<p>hi</p>", page_numbers=True)
		self.assertNotIn("--footer-html", self.fake_wk.args)
		i = self.fake_wk.args.index("--footer-right")
		self.assertEqual(self.fake_wk.args[i + 1], "Page [page] of [topage]")

	def test_no_footer_when_page_numbers_off_and_no_footer(self) -> None:
		render_pdf("<p>hi</p>", page_numbers=False)
		self.assertNotIn("--footer-html", self.fake_wk.args)
		self.assertNotIn("--footer-right", self.fake_wk.args)
		self.assertNotIn("--footer-left", self.fake_wk.args)

	def test_no_header_when_nothing_to_render(self) -> None:
		render_pdf("<p>hi</p>", page_numbers=False)
		self.assertNotIn("--header-html", self.fake_wk.args)

	def test_footer_text_and_page_numbers_coexist(self) -> None:
		# THE fix: a text footer and page numbers live on DIFFERENT edges, so a
		# branded/footed document KEEPS "Page X of Y" (the old --footer-html path
		# silently dropped it).
		render_pdf("<p>hi</p>", footer_text="Confidential", page_numbers=True)
		self.assertNotIn("--footer-html", self.fake_wk.args)
		self.assertEqual(self.fake_wk.args[self.fake_wk.args.index("--footer-left") + 1], "Confidential")
		self.assertEqual(
			self.fake_wk.args[self.fake_wk.args.index("--footer-right") + 1], "Page [page] of [topage]"
		)


# --- brand header folding + spacing (pre-built, trusted HTML) ---------------


class TestBrandHeaderFolding(_FurnitureRenderBase):
	def test_brand_header_folded_into_header_with_spacing(self) -> None:
		# render_pdf receives ALREADY-BUILT, safe brand-header HTML (a logo, else the
		# company name) and folds it into the running HTML header as-is.
		render_pdf(
			"<p>hi</p>", brand_header="<div class='jv-brand-header'>BRAND-MARK</div>", page_numbers=False
		)
		self.assertIn("BRAND-MARK", self.fake_wk.file_contents["--header-html"])
		self.assertIn("--header-spacing", self.fake_wk.args)

	def test_header_reserves_top_margin(self) -> None:
		# A header reserves extra top margin so a logo/brand line never overlaps body.
		render_pdf("<p>hi</p>", brand_header="<div>B</div>", margins_mm=15)
		ti = self.fake_wk.args.index("--margin-top")
		self.assertEqual(self.fake_wk.args[ti + 1], "29mm")  # 15 + 14 reserved

	def test_no_header_no_extra_margin_no_spacing(self) -> None:
		render_pdf("<p>hi</p>", margins_mm=15, page_numbers=False)
		self.assertNotIn("--header-spacing", self.fake_wk.args)
		ti = self.fake_wk.args.index("--margin-top")
		self.assertEqual(self.fake_wk.args[ti + 1], "15mm")


class TestWatermarkGeometry(_FurnitureRenderBase):
	def _wm_top(self, **kw) -> str:
		render_pdf("<p>hi</p>", watermark="DRAFT", **kw)
		m = re.search(r"top:\s*(\d+)pt", self.fake_wk.file_contents["--header-html"])
		self.assertTrue(m, "watermark top not found in header CSS")
		return m.group(1)

	def test_watermark_top_varies_by_page_size_and_orientation(self) -> None:
		a4 = self._wm_top(page_size="A4", orientation="portrait")
		letter = self._wm_top(page_size="Letter", orientation="portrait")
		landscape = self._wm_top(page_size="A4", orientation="landscape")
		self.assertNotEqual(a4, letter)  # not the old fixed 340pt
		self.assertNotEqual(a4, landscape)


# --- letterhead: image inlining (the SSRF fix), site-free via stubs ----------


class _FakeDB:
	"""Stand-in for the ``frappe.db`` LocalProxy, which is unbound (raises) without
	a site - so we replace the whole ``db`` object rather than a method on it."""

	def __init__(self, get_value):
		self._gv = get_value

	def get_value(self, *a, **k):
		return self._gv(*a, **k)


def _file_gv(name="FILE1", size=100):
	"""A field-aware File.get_value stub: the file_url→name lookup returns ``name``
	(or None to simulate 'no such File'), and the file_size lookup returns ``size``."""

	def gv(_dt, _filt, field=None, **_k):
		if field == "file_size":
			return size
		return name

	return gv


class _PngFile:
	def get_content(self):
		return b"\x89PNG\r\n\x1a\nDATA"  # valid PNG magic → passes the image sniff


class TestInlineLetterheadImages(unittest.TestCase):
	def test_inline_keeps_data_uri(self) -> None:
		src = '<img src="data:image/png;base64,AAAA">'
		self.assertEqual(furniture._inline_letterhead_images(src), src)

	def test_inline_drops_remote_image(self) -> None:
		out = furniture._inline_letterhead_images('<p>x</p><img src="http://evil/logo.png"><p>y</p>')
		self.assertNotIn("<img", out.lower())
		self.assertNotIn("evil", out.lower())
		self.assertTrue("x" in out and "y" in out)

	def test_inline_drops_protocol_relative_image(self) -> None:
		out = furniture._inline_letterhead_images('<img src="//evil/logo.png">')
		self.assertNotIn("<img", out.lower())
		self.assertNotIn("evil", out.lower())

	def test_inline_same_site_to_base64_with_perm(self) -> None:
		patch.object(furniture.frappe, "db", _FakeDB(_file_gv())).start()
		patch.object(furniture.frappe, "has_permission", lambda *_a, **_k: True).start()
		patch.object(furniture.frappe, "get_doc", lambda *_a, **_k: _PngFile()).start()
		self.addCleanup(patch.stopall)
		out = furniture._inline_letterhead_images('<img src="/files/logo.png">')
		self.assertIn("data:image/png;base64,", out)
		self.assertNotIn("http", out.lower())

	def test_inline_same_site_dropped_without_perm(self) -> None:
		# get_doc is mocked to SUCCEED so the drop is attributable ONLY to
		# has_permission=False — if the perm check were removed, this would LEAK a
		# base64 image and the test would fail (mutation-catching).
		patch.object(furniture.frappe, "db", _FakeDB(_file_gv())).start()
		patch.object(furniture.frappe, "has_permission", lambda *_a, **_k: False).start()
		patch.object(furniture.frappe, "get_doc", lambda *_a, **_k: _PngFile()).start()
		self.addCleanup(patch.stopall)
		out = furniture._inline_letterhead_images('<img src="/files/logo.png">')
		self.assertNotIn("<img", out.lower())
		self.assertNotIn("base64", out.lower())

	def test_inline_unknown_file_dropped(self) -> None:
		patch.object(furniture.frappe, "db", _FakeDB(_file_gv(name=None))).start()
		patch.object(furniture.frappe, "has_permission", lambda *_a, **_k: True).start()
		patch.object(furniture.frappe, "get_doc", lambda *_a, **_k: _PngFile()).start()
		self.addCleanup(patch.stopall)
		out = furniture._inline_letterhead_images('<img src="/files/missing.png">')
		self.assertNotIn("<img", out.lower())

	def test_inline_oversized_logo_dropped_before_read(self) -> None:
		# An oversized File is rejected on its file_size BEFORE get_content is called.
		patch.object(furniture.frappe, "db", _FakeDB(_file_gv(size=furniture._MAX_LOGO_BYTES + 1))).start()
		patch.object(furniture.frappe, "has_permission", lambda *_a, **_k: True).start()

		def _boom(*_a, **_k):
			raise AssertionError("get_content must not be called for an oversized logo")

		patch.object(furniture.frappe, "get_doc", _boom).start()
		self.addCleanup(patch.stopall)
		out = furniture._inline_letterhead_images('<img src="/files/huge.png">')
		self.assertNotIn("<img", out.lower())

	def test_inline_nonimage_bytes_dropped(self) -> None:
		# A file whose bytes are not an image (mislabeled *.png) is not inlined.
		patch.object(furniture.frappe, "db", _FakeDB(_file_gv())).start()
		patch.object(furniture.frappe, "has_permission", lambda *_a, **_k: True).start()

		class _NotImg:
			def get_content(self):
				return b"<html>not an image</html>"

		patch.object(furniture.frappe, "get_doc", lambda *_a, **_k: _NotImg()).start()
		self.addCleanup(patch.stopall)
		out = furniture._inline_letterhead_images('<img src="/files/fake.png">')
		self.assertNotIn("<img", out.lower())
		self.assertNotIn("base64", out.lower())

	def test_inline_svg_by_extension_refused(self) -> None:
		# SVG is refused (an inlined data:image/svg+xml is an opaque active-content blob
		# nh3 can't inspect). A *.svg File is dropped at the mime check, before read.
		patch.object(furniture.frappe, "db", _FakeDB(_file_gv())).start()
		patch.object(furniture.frappe, "has_permission", lambda *_a, **_k: True).start()

		def _boom(*_a, **_k):
			raise AssertionError("SVG must be refused before reading it")

		patch.object(furniture.frappe, "get_doc", _boom).start()
		self.addCleanup(patch.stopall)
		out = furniture._inline_letterhead_images('<img src="/files/logo.svg">')
		self.assertNotIn("<img", out.lower())

	def test_inline_svg_bytes_in_png_name_refused(self) -> None:
		# SVG bytes smuggled behind a *.png name are refused by the raster-only sniff.
		patch.object(furniture.frappe, "db", _FakeDB(_file_gv())).start()
		patch.object(furniture.frappe, "has_permission", lambda *_a, **_k: True).start()

		class _Svg:
			def get_content(self):
				return b'<svg xmlns="x"><image href="http://169.254.169.254/x"/></svg>'

		patch.object(furniture.frappe, "get_doc", lambda *_a, **_k: _Svg()).start()
		self.addCleanup(patch.stopall)
		out = furniture._inline_letterhead_images('<img src="/files/logo.png">')
		self.assertNotIn("<img", out.lower())
		self.assertNotIn("169.254", out)


# --- letterhead: resolution + degrade note, site-free via stubs --------------


def _stub_letterhead(*, default=None, docs=None, perms=True):
	"""Patch ``frappe.db``/``has_permission`` to site-free stubs. Patches are
	started but not stopped - the caller must ``self.addCleanup(patch.stopall)``."""
	docs = docs or {}

	def _get_value(_dt, filt, _field=None, **_k):
		if isinstance(filt, dict):  # the default (is_default=1) lookup
			return default
		return docs.get(filt)  # named/default-name doc lookup

	patch.object(furniture.frappe, "db", _FakeDB(_get_value)).start()
	patch.object(furniture.frappe, "has_permission", lambda *_a, **_k: perms).start()


class TestResolveLetterhead(unittest.TestCase):
	def test_resolve_letterhead_default_logo_kept_text_dropped(self) -> None:
		# Logo-only: a data: logo survives; the doc-bound text block is dropped.
		_stub_letterhead(
			default="Std",
			docs={
				"Std": {
					"content": '<div>HDR<img src="data:image/png;base64,AAAA"></div>',
					"footer": '<img src="data:image/png;base64,BBBB">FTR',
				}
			},
		)
		self.addCleanup(patch.stopall)
		header, footer, note = resolve_letterhead(None)
		self.assertIn("data:image/png;base64,AAAA", header)
		self.assertNotIn("HDR", header)  # doc-bound text dropped
		self.assertIn("data:image/png;base64,BBBB", footer)
		self.assertNotIn("FTR", footer)
		self.assertIsNone(note)

	def test_resolve_letterhead_named(self) -> None:
		_stub_letterhead(
			docs={"Acme": {"content": '<img src="data:image/png;base64,CCCC">ACME', "footer": ""}}
		)
		self.addCleanup(patch.stopall)
		header, _footer, note = resolve_letterhead("Acme")
		self.assertIn("data:image/png;base64,CCCC", header)
		self.assertNotIn("ACME", header)
		self.assertIsNone(note)

	def test_resolve_letterhead_jinja_never_leaks(self) -> None:
		# The staging-smoke bug: a doc-bound Jinja Letter Head produced raw {% %}
		# (folded raw) or "None"/junk (rendered without a doc). Logo-only drops the
		# entire Jinja text block — no tags, no rendered junk — keeping only a logo.
		_stub_letterhead(
			default="Std",
			docs={
				"Std": {
					"content": "<div>{% if x %}{{ company_address }}{% endif %}"
					'<img src="data:image/png;base64,DDDD"></div>',
					"footer": "",
				}
			},
		)
		self.addCleanup(patch.stopall)
		header, _footer, _note = resolve_letterhead(None)
		self.assertNotIn("{%", header)
		self.assertNotIn("{{", header)
		self.assertNotIn("None", header)
		self.assertNotIn("company_address", header)
		self.assertIn("data:image/png;base64,DDDD", header)  # logo survives

	def test_resolve_letterhead_no_default_no_note(self) -> None:
		_stub_letterhead(default=None)
		self.addCleanup(patch.stopall)
		header, footer, note = resolve_letterhead(None)
		self.assertTrue(header == "" and footer == "")
		self.assertIsNone(note)  # no default configured is a normal unbranded render

	def test_resolve_letterhead_named_not_found_notes(self) -> None:
		_stub_letterhead(docs={})  # named lookup returns None
		self.addCleanup(patch.stopall)
		header, footer, note = resolve_letterhead("Nope")
		self.assertTrue(header == "" and footer == "")
		self.assertTrue(note and "Nope" in note)

	def test_resolve_letterhead_no_read_perm_notes(self) -> None:
		_stub_letterhead(docs={"Acme": {"content": "<div>ACME</div>", "footer": ""}}, perms=False)
		self.addCleanup(patch.stopall)
		header, _footer, note = resolve_letterhead("Acme")
		self.assertEqual(header, "")
		self.assertTrue(note and "Acme" in note)

	def test_resolve_letterhead_strips_remote_image_end_to_end(self) -> None:
		# The F1 SSRF gate wired through resolve_letterhead: a Letter Head whose
		# content carries a REMOTE <img> (and a <link>) comes back with NO remote
		# URL — logo-only extracts the imgs, the remote one is dropped by the inline
		# step, the <link>/text never survive, and a data: logo is kept.
		_stub_letterhead(
			docs={
				"Evil": {
					"content": '<div>Acme<img src="http://169.254.169.254/x">'
					'<img src="data:image/png;base64,EEEE">'
					'<link href="http://evil/x.css"></div>',
					"footer": "",
				}
			},
		)
		self.addCleanup(patch.stopall)
		header, _footer, note = resolve_letterhead("Evil")
		low = header.lower()
		self.assertTrue("169.254" not in low and "evil" not in low)
		self.assertNotIn("<link", low)
		self.assertNotIn("Acme", header)  # doc-bound text dropped (logo-only)
		self.assertIn("data:image/png;base64,EEEE", header)  # the safe logo survives
		self.assertIsNone(note)

	def test_remove_quietly_suppresses_oserror(self) -> None:
		# A non-FileNotFound OSError (e.g. EPERM) during cleanup must be swallowed so
		# the finally loop can't abort (leaking the other temp) or mask the real error.
		def _raise(_p):
			raise PermissionError("EPERM")

		patch.object(furniture.os, "remove", _raise).start()
		self.addCleanup(patch.stopall)
		furniture._remove_quietly("/tmp/whatever")  # must NOT raise

	@unittest.skipUnless(_HAS_SITE, "needs a bench site (get_letter_head / File)")
	def test_resolve_letterhead_real_default(self) -> None:
		header, footer, note = resolve_letterhead(None)
		self.assertTrue(isinstance(header, str) and isinstance(footer, str))
		self.assertIsNone(note)


class TestLetterheadLogos(unittest.TestCase):
	"""_letterhead_logos: keep only <img> logos from a Letter Head, drop the
	doc-bound Jinja/text block (a composed report has no doc to render it cleanly,
	so rendering would leak raw tags or "None"/junk — the staging-smoke defect)."""

	def test_extracts_img_drops_text_and_jinja(self) -> None:
		out = furniture._letterhead_logos(
			"<div>{% if x %}{{ company_address }}{% endif %}Acme Ltd"
			'<img src="data:image/png;base64,AAAA"></div>'
		)
		self.assertIn('src="data:image/png;base64,AAAA"', out)
		self.assertNotIn("Acme", out)
		self.assertNotIn("{%", out)
		self.assertNotIn("{{", out)
		self.assertNotIn("company_address", out)

	def test_no_img_returns_empty(self) -> None:
		self.assertEqual(furniture._letterhead_logos("<div>{{ company }} Just text</div>"), "")
		self.assertEqual(furniture._letterhead_logos(""), "")

	def test_multiple_imgs_kept_text_between_dropped(self) -> None:
		out = furniture._letterhead_logos('<img src="/files/a.png"><span>x</span><img src="/files/b.png">')
		self.assertEqual(out.count("<img"), 2)
		self.assertNotIn("<span", out)

	def test_oversized_letterhead_dropped_without_hang(self) -> None:
		import time

		huge = "<img>" + "x" * (furniture._MAX_FURNITURE_CHARS + 10)
		start = time.monotonic()
		out = furniture._letterhead_logos(huge)
		self.assertEqual(out, "")  # over the cap → dropped
		self.assertLess(time.monotonic() - start, 1.0)


# --- tool-built masthead + running header (pure, no site) --------------------


class TestTitleBlock(unittest.TestCase):
	def test_title_is_escaped_and_no_raw_markdown_leaks(self) -> None:
		out = furniture.build_title_block("# ERP <x> & Co", subtitle="**sub**", meta="INR")
		# The tool builds a div from the param, so the raw markdown chars are escaped
		# TEXT, never rendered as a heading (the old <div class="cover">md bug).
		self.assertIn('<div class="doc-title"># ERP &lt;x&gt; &amp; Co</div>', out)
		self.assertIn('<div class="doc-subtitle">**sub**</div>', out)
		self.assertIn('<div class="doc-meta">INR</div>', out)
		self.assertNotIn("<h1", out)

	def test_default_is_title_block_full_cover_wraps(self) -> None:
		plain = furniture.build_title_block("T", full_cover=False)
		self.assertIn('<div class="doc-title-block">', plain)
		self.assertNotIn('class="cover"', plain)
		cover = furniture.build_title_block("T", full_cover=True)
		self.assertIn('<div class="cover"><div class="cover-inner">', cover)
		self.assertIn('<div class="doc-title-block">', cover)

	def test_empty_when_no_title_and_no_brand(self) -> None:
		self.assertEqual(furniture.build_title_block(None), "")

	def test_logo_wins_over_company_name(self) -> None:
		brand = {
			"logo_html": '<img src="data:image/png;base64,AAA">',
			"company": "Acme",
			"address_lines": ["1 St"],
		}
		out = furniture.build_title_block("T", brand=brand)
		self.assertIn('<div class="brand-logo"><img src="data:image/png;base64,AAA">', out)
		self.assertNotIn("Acme", out)

	def test_company_name_and_address_when_no_logo(self) -> None:
		brand = {"logo_html": "", "company": "Acme <Ltd>", "address_lines": ["1 St", "City 560001"]}
		out = furniture.build_title_block("T", brand=brand)
		self.assertIn('<div class="brand-name">Acme &lt;Ltd&gt;</div>', out)
		self.assertIn('<div class="brand-addr">1 St</div>', out)
		self.assertIn('<div class="brand-addr">City 560001</div>', out)


class TestBrandRunningHeader(unittest.TestCase):
	def test_logo_when_present(self) -> None:
		out = furniture.build_brand_header({"logo_html": "<img src='data:x'>", "company": "Acme"})
		self.assertIn("jv-brand-header", out)
		self.assertIn("<img", out)

	def test_company_name_when_no_logo(self) -> None:
		out = furniture.build_brand_header({"logo_html": "", "company": "Acme <Ltd>"})
		self.assertIn("jv-brand-name", out)
		self.assertIn("Acme &lt;Ltd&gt;", out)

	def test_empty_when_no_brand(self) -> None:
		self.assertEqual(furniture.build_brand_header({}), "")
		self.assertEqual(furniture.build_brand_header(None), "")


class TestResolveBrand(unittest.TestCase):
	def test_frappe_only_bench_no_company_is_empty_not_crash(self) -> None:
		# No Company doctype (frappe-only bench) => no company brand, no crash.
		with (
			patch.object(furniture, "resolve_letterhead", lambda _lh: ("", "", None)),
			patch.object(furniture, "frappe") as fk,
		):
			fk.db.exists.return_value = False
			brand = furniture.resolve_brand(None)
		self.assertEqual(brand["logo_html"], "")
		self.assertIsNone(brand["company"])
		self.assertEqual(brand["address_lines"], [])

	def test_logo_from_letterhead_passed_through(self) -> None:
		with (
			patch.object(furniture, "resolve_letterhead", lambda _lh: ("<img src='data:x'>", "", None)),
			patch.object(furniture, "_resolve_company_identity", lambda: (None, [], None)),
		):
			brand = furniture.resolve_brand(None)
		self.assertIn("<img", brand["logo_html"])

	def test_named_letterhead_note_and_company_fallback_propagate(self) -> None:
		with (
			patch.object(
				furniture,
				"resolve_letterhead",
				lambda _lh: ("", "", "letterhead 'X' not found — rendered without it"),
			),
			patch.object(furniture, "_resolve_company_identity", lambda: ("Acme", ["1 St"], None)),
		):
			brand = furniture.resolve_brand("X")
		self.assertIn("not found", brand["note"])
		self.assertEqual(brand["company"], "Acme")
		self.assertEqual(brand["address_lines"], ["1 St"])

	def test_footer_logo_used_when_header_logo_absent(self):
		# A Letter Head whose logo lives only in the FOOTER field is still used.
		with (
			patch.object(furniture, "resolve_letterhead", lambda _lh: ("", "<img src='data:foot'>", None)),
			patch.object(furniture, "_resolve_company_identity", lambda: (None, [], None)),
		):
			brand = furniture.resolve_brand(None)
		self.assertIn("data:foot", brand["logo_html"])

	def test_company_error_note_propagates(self):
		# A real company-resolution ERROR (not legitimate absence) surfaces a note.
		with (
			patch.object(furniture, "resolve_letterhead", lambda _lh: ("", "", None)),
			patch.object(
				furniture,
				"_resolve_company_identity",
				lambda: (None, [], "company branding could not be resolved — rendered without it"),
			),
		):
			brand = furniture.resolve_brand(None)
		self.assertIn("could not be resolved", brand["note"])


class TestCompanyIdentity(unittest.TestCase):
	"""The security-sensitive company/primary-address resolver (was untested).
	frappe is mocked wholesale so the branch logic is exercised without a site."""

	def test_address_primary_only_happy_path(self):
		with patch.object(furniture, "frappe") as fk:
			fk.get_all.side_effect = [["ADDR-1"], ["ADDR-1"]]  # Dynamic Link, then Address
			fk.has_permission.return_value = True
			fk.db.get_value.return_value = {
				"address_line1": "1 St",
				"address_line2": "",
				"city": "Bengaluru",
				"state": "KA",
				"pincode": "560001",
				"country": "India",
			}
			lines = furniture._resolve_company_address_lines("Acme")
		self.assertEqual(lines, ["1 St", "Bengaluru, KA, 560001", "India"])
		# the Address query MUST filter on the primary flag (never a shipping addr).
		addr_call = fk.get_all.call_args_list[1]
		self.assertEqual(addr_call.kwargs["filters"]["is_primary_address"], 1)
		self.assertEqual(addr_call.kwargs["filters"]["disabled"], 0)

	def test_address_perm_denied_returns_empty(self):
		with patch.object(furniture, "frappe") as fk:
			fk.get_all.side_effect = [["ADDR-1"], ["ADDR-1"]]
			fk.has_permission.return_value = False  # no Address read perm
			self.assertEqual(furniture._resolve_company_address_lines("Acme"), [])

	def test_no_primary_address_returns_empty(self):
		with patch.object(furniture, "frappe") as fk:
			fk.get_all.side_effect = [["ADDR-1"], []]  # linked, but none flagged primary
			self.assertEqual(furniture._resolve_company_address_lines("Acme"), [])

	def test_no_linked_address_returns_empty(self):
		with patch.object(furniture, "frappe") as fk:
			fk.get_all.side_effect = [[]]  # no Dynamic Link at all
			self.assertEqual(furniture._resolve_company_address_lines("Acme"), [])

	def test_partial_address_fields_drops_empties(self):
		with patch.object(furniture, "frappe") as fk:
			fk.get_all.side_effect = [["ADDR-1"], ["ADDR-1"]]
			fk.has_permission.return_value = True
			fk.db.get_value.return_value = {
				"address_line1": "1 St",
				"address_line2": None,
				"city": None,
				"state": None,
				"pincode": None,
				"country": None,
			}
			self.assertEqual(furniture._resolve_company_address_lines("Acme"), ["1 St"])

	def test_identity_no_company_doctype_absent_no_note(self):
		with patch.object(furniture, "frappe") as fk:
			fk.db.exists.return_value = False  # frappe-only bench
			self.assertEqual(furniture._resolve_company_identity(), (None, [], None))

	def test_identity_no_read_perm_is_absence_not_error(self):
		with patch.object(furniture, "frappe") as fk:
			fk.db.exists.return_value = True
			fk.defaults.get_user_default.return_value = "Acme"
			fk.has_permission.return_value = False
			self.assertEqual(furniture._resolve_company_identity(), (None, [], None))

	def test_identity_multi_company_no_default_no_pick(self):
		with patch.object(furniture, "frappe") as fk:
			fk.db.exists.return_value = True
			fk.defaults.get_user_default.return_value = None
			fk.defaults.get_global_default.return_value = None
			fk.get_all.return_value = [types.SimpleNamespace(name="A"), types.SimpleNamespace(name="B")]
			name, lines, note = furniture._resolve_company_identity()
		self.assertIsNone(name)  # >1 company, no default → no arbitrary pick

	def test_identity_error_sets_degrade_note(self):
		with patch.object(furniture, "frappe") as fk:
			fk.db.exists.return_value = True
			fk.defaults.get_user_default.side_effect = RuntimeError("db down")
			name, lines, note = furniture._resolve_company_identity()
		self.assertIsNone(name)
		self.assertIn("could not be resolved", note)  # ERROR degrades WITH a note

	def test_identity_address_error_keeps_name(self):
		with patch.object(furniture, "frappe") as fk:
			fk.db.exists.return_value = True
			fk.defaults.get_user_default.return_value = "Acme"
			fk.has_permission.return_value = True
			fk.db.get_value.return_value = "Acme Ltd"
			with patch.object(furniture, "_resolve_company_address_lines", side_effect=RuntimeError("boom")):
				name, lines, note = furniture._resolve_company_identity()
		self.assertEqual(name, "Acme Ltd")  # name kept despite the address failure
		self.assertEqual(lines, [])
		self.assertIsNone(note)


class TestTextOnly(unittest.TestCase):
	def test_strips_tags_collapses_whitespace_truncates(self):
		self.assertEqual(furniture._text_only("<b>Hi</b>   there"), "Hi there")
		self.assertEqual(furniture._text_only("a\n\n b\t c"), "a b c")
		self.assertEqual(len(furniture._text_only("x" * 500)), 200)  # 200-char cap
		self.assertEqual(furniture._text_only(None), "")
