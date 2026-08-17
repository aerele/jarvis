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
import shutil
import subprocess
import tempfile
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

	def test_footer_is_sanitized(self) -> None:
		render_pdf("<p>body</p>", footer_html='<img src="http://evil"><script>x</script><b>footkeep</b>')
		content = self.fake_wk.file_contents["--footer-html"]
		low = content.lower()
		self.assertNotIn("<img", low)
		self.assertNotIn("evil", low)
		self.assertNotIn("<script", low)
		self.assertIn("footkeep", content)

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
	def test_temp_files_exist_during_call_then_removed(self) -> None:
		render_pdf("<p>body</p>", header_html="<b>H</b>", footer_html="<b>F</b>")
		for flag in ("--header-html", "--footer-html"):
			path = self.fake_wk.path_for(flag)
			self.assertTrue(self.fake_wk.files_existed[path], f"{flag} temp missing during call")
			self.assertFalse(os.path.exists(path), f"{flag} temp not cleaned up after return")

	def test_timeout_cleans_temp_and_raises_clean_error(self) -> None:
		self.fake_wk.raise_timeout = True
		with self.assertRaises(InvalidArgumentError) as cm:
			render_pdf("<p>body</p>", header_html="<b>H</b>", footer_html="<b>F</b>", timeout=7)
		self.assertIn("7s", str(cm.exception))
		self.assertFalse(isinstance(cm.exception, subprocess.TimeoutExpired))
		for flag in ("--header-html", "--footer-html"):
			self.assertFalse(os.path.exists(self.fake_wk.path_for(flag)))

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

	def test_first_temp_cleaned_when_second_write_fails(self) -> None:
		# ENOSPC / fd-exhaustion on the SECOND temp write must not orphan the FIRST -
		# temp creation is inside the try so the finally still cleans it.
		real_write = furniture._write_temp
		created: list[str] = []
		calls = {"n": 0}

		def _wt(tmp_dir: str, content: str) -> str:
			calls["n"] += 1
			if calls["n"] == 2:
				raise OSError("disk full")
			path = real_write(tmp_dir, content)
			created.append(path)
			return path

		patch.object(furniture, "_write_temp", _wt).start()
		with self.assertRaises(OSError):
			render_pdf("<p>hi</p>", header_html="<b>H</b>", footer_html="<b>F</b>")
		self.assertTrue(created, "first temp was never created")
		self.assertFalse(os.path.exists(created[0]), "first temp leaked when the second write failed")

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
	def test_page_numbers_use_text_footer_not_html(self) -> None:
		# Page numbers go through the TEXT footer (--footer-center), which
		# substitutes [page]/[topage] in wkhtmltopdf's C++ layer and so works under
		# --disable-javascript. An HTML footer's bracket substitution needs JS, so
		# it would print a literal "[page]" (the staging-smoke bug).
		render_pdf("<p>hi</p>", page_numbers=True)
		self.assertNotIn("--footer-html", self.fake_wk.args)
		self.assertIn("--footer-center", self.fake_wk.args)
		i = self.fake_wk.args.index("--footer-center")
		self.assertEqual(self.fake_wk.args[i + 1], "Page [page] of [topage]")

	def test_no_footer_when_page_numbers_off_and_no_footer(self) -> None:
		render_pdf("<p>hi</p>", page_numbers=False)
		self.assertNotIn("--footer-html", self.fake_wk.args)
		self.assertNotIn("--footer-center", self.fake_wk.args)

	def test_no_header_when_nothing_to_render(self) -> None:
		render_pdf("<p>hi</p>", page_numbers=False)
		self.assertNotIn("--header-html", self.fake_wk.args)

	def test_html_footer_wins_over_text_page_numbers(self) -> None:
		# An explicit footer (or a letterhead footer) needs --footer-html; it and
		# the text footer are mutually exclusive, so auto page numbers are omitted.
		render_pdf("<p>hi</p>", footer_html="<b>my-footer</b>", page_numbers=True)
		self.assertIn("--footer-html", self.fake_wk.args)
		self.assertNotIn("--footer-center", self.fake_wk.args)
		content = self.fake_wk.file_contents["--footer-html"]
		self.assertIn("my-footer", content)

	def test_letterhead_footer_uses_html_footer(self) -> None:
		render_pdf("<p>hi</p>", letterhead_footer="<div>LH-FOOT</div>", page_numbers=True)
		self.assertIn("--footer-html", self.fake_wk.args)
		self.assertNotIn("--footer-center", self.fake_wk.args)
		self.assertIn("LH-FOOT", self.fake_wk.file_contents["--footer-html"])


# --- letterhead: folding (pre-resolved, trusted HTML) -----------------------


class TestLetterheadFolding(_FurnitureRenderBase):
	def test_letterhead_header_footer_folded(self) -> None:
		# render_pdf receives ALREADY-RESOLVED, safe letterhead HTML and folds it in
		# as-is (it is not re-sanitized - the images were neutralised in resolve).
		render_pdf(
			"<p>hi</p>",
			letterhead_header="<div>LH-HEADER-MARK</div>",
			letterhead_footer="<div>LH-FOOTER-MARK</div>",
			page_numbers=False,
		)
		self.assertIn("LH-HEADER-MARK", self.fake_wk.file_contents["--header-html"])
		self.assertIn("LH-FOOTER-MARK", self.fake_wk.file_contents["--footer-html"])


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
	def test_resolve_letterhead_default_used_when_unnamed(self) -> None:
		_stub_letterhead(
			default="Std",
			docs={"Std": {"content": "<div>HDR</div>", "footer": "<div>FTR</div>"}},
		)
		self.addCleanup(patch.stopall)
		header, footer, note = resolve_letterhead(None)
		self.assertTrue("HDR" in header and "FTR" in footer)
		self.assertIsNone(note)

	def test_resolve_letterhead_named(self) -> None:
		_stub_letterhead(docs={"Acme": {"content": "<div>ACME</div>", "footer": ""}})
		self.addCleanup(patch.stopall)
		header, _footer, note = resolve_letterhead("Acme")
		self.assertIn("ACME", header)
		self.assertIsNone(note)

	def test_resolve_letterhead_jinja_never_leaks_raw(self) -> None:
		# The staging-smoke bug: a Jinja Letter Head folded in raw leaked
		# {% if %}/{{ }} into the PDF. Even in the worst case (render leaves the
		# tags), the residual scrub must remove them; static text survives.
		_stub_letterhead(
			default="Std",
			docs={"Std": {"content": "<div>{% if x %}{{ x }}{% endif %}Acme Ltd</div>", "footer": ""}},
		)
		self.addCleanup(patch.stopall)
		with patch.object(furniture.frappe, "render_template", lambda tpl, ctx: tpl):
			header, _footer, _note = resolve_letterhead(None)
		self.assertNotIn("{%", header)
		self.assertNotIn("{{", header)
		self.assertIn("Acme Ltd", header)

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
		# The F1 SSRF gate wired through resolve_letterhead: a Letter Head whose content
		# carries a remote <img>/<link> must come back with NO remote URL (the gate runs
		# after inlining), while benign text survives.
		_stub_letterhead(
			docs={
				"Evil": {
					"content": '<div>Acme<img src="http://169.254.169.254/x">'
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
		self.assertIn("Acme", header)
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


class TestRenderLetterheadTemplate(unittest.TestCase):
	"""_render_letterhead: render a Letter Head's Jinja, then scrub residual tags."""

	def test_non_jinja_passthrough_does_not_render(self) -> None:
		called: list = []

		def _spy(*_a, **_k):
			called.append(1)
			return ""

		patch.object(furniture.frappe, "render_template", _spy).start()
		self.addCleanup(patch.stopall)
		out = furniture._render_letterhead("<div>plain logo</div>", {})
		self.assertEqual(out, "<div>plain logo</div>")
		self.assertEqual(called, [])  # no template markers → render_template skipped

	def test_renders_then_scrubs_residual(self) -> None:
		patch.object(
			furniture.frappe, "render_template", lambda t, c: "<b>Acme</b>{% stray %}keep{{ x }}"
		).start()
		self.addCleanup(patch.stopall)
		out = furniture._render_letterhead("<b>{{ company }}</b>", {})
		self.assertNotIn("{%", out)
		self.assertNotIn("{{", out)
		self.assertIn("Acme", out)
		self.assertIn("keep", out)

	def test_render_error_falls_back_to_stripped_raw(self) -> None:
		def _boom(*_a, **_k):
			raise RuntimeError("no doc context")

		patch.object(furniture.frappe, "render_template", _boom).start()
		patch.object(furniture.frappe, "clear_last_message", lambda: None).start()
		self.addCleanup(patch.stopall)
		out = furniture._render_letterhead('<img src="data:x"> {% if y %}{{ y }}{% endif %}', {})
		self.assertNotIn("{%", out)
		self.assertNotIn("{{", out)
		self.assertIn("<img", out)  # static logo survives the fallback

	def test_oversized_letterhead_dropped_without_hang(self) -> None:
		# A pathological unclosed-{{ run must be dropped on size BEFORE the
		# superlinear residual scrub (it runs pre-render, outside the render
		# timeout). Guard: this returns quickly, not in O(n^2).
		import time

		called: list = []
		patch.object(furniture.frappe, "render_template", lambda *_a, **_k: called.append(1) or "").start()
		self.addCleanup(patch.stopall)
		huge = "{{" * (furniture._MAX_FURNITURE_CHARS)  # >> the cap
		start = time.monotonic()
		out = furniture._render_letterhead(huge, {})
		self.assertEqual(out, "")
		self.assertEqual(called, [])  # never even reached render_template / scrub
		self.assertLess(time.monotonic() - start, 1.0)

	def test_render_error_clears_leaked_message(self) -> None:
		# render_template's error path appends its traceback to message_log before
		# raising; _render_letterhead must clear it so it can't leak to the caller.
		cleared: list = []

		def _boom(*_a, **_k):
			raise RuntimeError("Jinja Template Error")

		patch.object(furniture.frappe, "render_template", _boom).start()
		patch.object(furniture.frappe, "clear_last_message", lambda: cleared.append(1)).start()
		self.addCleanup(patch.stopall)
		furniture._render_letterhead("<b>{{ x }}</b>", {})
		self.assertEqual(cleared, [1])
