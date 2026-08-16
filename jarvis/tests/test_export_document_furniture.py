"""Tests for ``jarvis.tools._export.document.furniture.render_pdf`` (the rich-PDF
render core: security flags + a real subprocess render timeout) and its
letterhead resolution (default lookup, read-perm, image inlining).

wkhtmltopdf is ABSENT in local dev (brew removed it), so this suite NEVER runs a
real render - that is the Frappe Cloud smoke gate. Instead ``subprocess.run`` is
mocked and ``_resolve_binary`` is stubbed to a fake path, and the assertions
cover:
  * the UNCONDITIONAL security/fidelity arg list (parametrized; drop a flag ->
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

The real ``get_letter_head`` + real render are gated (``skipif`` a site) and
belong to the FC smoke gate.
"""

import os
import subprocess

import frappe
import pytest

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


@pytest.fixture
def fake_wk(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
	"""Stub the binary resolver + ``subprocess.run`` so ``render_pdf`` runs its
	full logic (temp files, arg build, cleanup) without a real wkhtmltopdf."""
	recorder = _Recorder()
	monkeypatch.setattr(furniture, "_resolve_binary", lambda: "/usr/bin/wkhtmltopdf")
	monkeypatch.setattr(furniture.subprocess, "run", recorder)
	return recorder


# --- arg list: unconditional security/fidelity flags ------------------------


@pytest.mark.parametrize("flag", _REQUIRED_FLAGS)
def test_required_flag_always_present(flag: str, fake_wk: _Recorder) -> None:
	# No header/footer/watermark, page numbers off: the leanest render still
	# carries every security/fidelity flag. Dropping any from _build_args fails
	# exactly this case (the mutation target).
	render_pdf("<p>hi</p>", page_numbers=False)
	assert flag in fake_wk.args


def test_encoding_flag_value(fake_wk: _Recorder) -> None:
	render_pdf("<p>hi</p>", page_numbers=False)
	i = fake_wk.args.index("--encoding")
	assert fake_wk.args[i + 1] == "utf-8"


def test_page_size_default_and_custom(fake_wk: _Recorder) -> None:
	render_pdf("<p>hi</p>", page_numbers=False)
	assert fake_wk.args[fake_wk.args.index("--page-size") + 1] == "A4"
	render_pdf("<p>hi</p>", page_size="letter", page_numbers=False)
	# canonicalized spelling
	assert fake_wk.args[fake_wk.args.index("--page-size") + 1] == "Letter"


def test_page_size_invalid_raises(fake_wk: _Recorder) -> None:
	with pytest.raises(InvalidArgumentError):
		render_pdf("<p>hi</p>", page_size="A2", page_numbers=False)


def test_orientation_portrait_default(fake_wk: _Recorder) -> None:
	render_pdf("<p>hi</p>", page_numbers=False)
	assert fake_wk.args[fake_wk.args.index("--orientation") + 1] == "Portrait"


def test_orientation_landscape(fake_wk: _Recorder) -> None:
	render_pdf("<p>hi</p>", orientation="landscape", page_numbers=False)
	assert fake_wk.args[fake_wk.args.index("--orientation") + 1] == "Landscape"


def test_orientation_invalid_raises(fake_wk: _Recorder) -> None:
	with pytest.raises(InvalidArgumentError):
		render_pdf("<p>hi</p>", orientation="sideways")


def test_margins_all_four_sides(fake_wk: _Recorder) -> None:
	render_pdf("<p>hi</p>", margins_mm=20, page_numbers=False)
	for side in ("--margin-top", "--margin-bottom", "--margin-left", "--margin-right"):
		assert fake_wk.args[fake_wk.args.index(side) + 1] == "20mm"


def test_margins_negative_raises(fake_wk: _Recorder) -> None:
	# A negative margin would produce a "-5mm" token wkhtmltopdf could mis-parse
	# as a flag; reject it app-side like orientation.
	with pytest.raises(InvalidArgumentError):
		render_pdf("<p>hi</p>", margins_mm=-5)


def test_margins_too_large_raises(fake_wk: _Recorder) -> None:
	with pytest.raises(InvalidArgumentError):
		render_pdf("<p>hi</p>", margins_mm=500)


def test_reads_stdin_writes_stdout(fake_wk: _Recorder) -> None:
	render_pdf("<p>hi</p>", page_numbers=False)
	assert fake_wk.args[-2:] == ["-", "-"]


def test_body_passed_as_input_bytes(fake_wk: _Recorder) -> None:
	render_pdf("<p>UNIQUEBODYMARKER</p>", page_numbers=False)
	assert isinstance(fake_wk.input, bytes)
	assert b"UNIQUEBODYMARKER" in fake_wk.input


def test_timeout_passed_through_to_subprocess(fake_wk: _Recorder) -> None:
	render_pdf("<p>hi</p>", timeout=13, page_numbers=False)
	assert fake_wk.timeout == 13


def test_oversized_furniture_rejected(fake_wk: _Recorder) -> None:
	# Header/footer/watermark are agent-influenced; an unbounded one is refused
	# before the pre-render sanitize (the body is capped upstream).
	with pytest.raises(InvalidArgumentError):
		render_pdf("<p>hi</p>", header_html="x" * 20_001)


# --- header/footer/watermark are sanitized before hitting the temp file ------


def test_header_is_sanitized(fake_wk: _Recorder) -> None:
	render_pdf("<p>body</p>", header_html='<img src="http://evil"><script>x</script><b>keepme</b>')
	content = fake_wk.file_contents["--header-html"]
	low = content.lower()
	assert "<img" not in low
	assert "evil" not in low
	assert "<script" not in low
	assert "keepme" in content


def test_footer_is_sanitized(fake_wk: _Recorder) -> None:
	render_pdf("<p>body</p>", footer_html='<img src="http://evil"><script>x</script><b>footkeep</b>')
	content = fake_wk.file_contents["--footer-html"]
	low = content.lower()
	assert "<img" not in low
	assert "evil" not in low
	assert "<script" not in low
	assert "footkeep" in content


def test_watermark_is_sanitized_and_rotated_in_header(fake_wk: _Recorder) -> None:
	render_pdf("<p>body</p>", watermark='DRAFT<img src="http://evil"><script>x</script>')
	content = fake_wk.file_contents["--header-html"]
	low = content.lower()
	assert "<img" not in low
	assert "evil" not in low
	assert "<script" not in low
	assert "jv-watermark" in content
	assert "rotate(-45deg)" in content
	assert "DRAFT" in content


# --- temp-file lifecycle: created, then removed (success + failures) ---------


def test_temp_files_exist_during_call_then_removed(fake_wk: _Recorder) -> None:
	render_pdf("<p>body</p>", header_html="<b>H</b>", footer_html="<b>F</b>")
	for flag in ("--header-html", "--footer-html"):
		path = fake_wk.path_for(flag)
		assert fake_wk.files_existed[path] is True, f"{flag} temp missing during call"
		assert not os.path.exists(path), f"{flag} temp not cleaned up after return"


def test_timeout_cleans_temp_and_raises_clean_error(fake_wk: _Recorder) -> None:
	fake_wk.raise_timeout = True
	with pytest.raises(InvalidArgumentError) as excinfo:
		render_pdf("<p>body</p>", header_html="<b>H</b>", footer_html="<b>F</b>", timeout=7)
	assert "7s" in str(excinfo.value)
	assert not isinstance(excinfo.value, subprocess.TimeoutExpired)
	for flag in ("--header-html", "--footer-html"):
		assert not os.path.exists(fake_wk.path_for(flag))


def test_oserror_from_exec_is_clean_and_cleans_temp(fake_wk: _Recorder) -> None:
	# The resolved binary fails to EXECUTE (OSError, not TimeoutExpired) → clean
	# InvalidArgumentError, never a raw OSError, and temp files are cleaned.
	fake_wk.raise_oserror = True
	with pytest.raises(InvalidArgumentError) as excinfo:
		render_pdf("<p>body</p>", header_html="<b>H</b>")
	assert not isinstance(excinfo.value, OSError)
	assert not os.path.exists(fake_wk.path_for("--header-html"))


def test_render_exception_still_cleans_temp(fake_wk: _Recorder) -> None:
	fake_wk.returncode = 1
	fake_wk.stderr = b"boom: qt render blew up"
	with pytest.raises(InvalidArgumentError):
		render_pdf("<p>body</p>", header_html="<b>H</b>")
	# temp cleaned even on a non-zero exit (this test's point)
	assert not os.path.exists(fake_wk.path_for("--header-html"))


def test_first_temp_cleaned_when_second_write_fails(
	fake_wk: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
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

	monkeypatch.setattr(furniture, "_write_temp", _wt)
	with pytest.raises(OSError):
		render_pdf("<p>hi</p>", header_html="<b>H</b>", footer_html="<b>F</b>")
	assert created, "first temp was never created"
	assert not os.path.exists(created[0]), "first temp leaked when the second write failed"


def test_write_temp_removes_partial_on_write_failure(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
	class _BadFile:
		def __enter__(self):
			return self

		def __exit__(self, *_a):
			return False

		def write(self, _data):
			raise OSError("disk full mid-write")

	removed: list[str] = []
	monkeypatch.setattr(furniture, "open", lambda *_a, **_k: _BadFile(), raising=False)
	monkeypatch.setattr(furniture, "_remove_quietly", lambda p: removed.append(p))
	with pytest.raises(OSError):
		furniture._write_temp(str(tmp_path), "content")
	assert removed, "partial file was not cleaned up on write failure"


# --- distinct temp names across calls (generate_hash uniqueness) -------------


def test_two_calls_use_distinct_temp_names(fake_wk: _Recorder) -> None:
	render_pdf("<p>a</p>", header_html="<b>H</b>")
	first = fake_wk.path_for("--header-html")
	render_pdf("<p>b</p>", header_html="<b>H</b>")
	second = fake_wk.path_for("--header-html")
	assert first != second


# --- output guards -----------------------------------------------------------


def test_returns_pdf_bytes_on_success(fake_wk: _Recorder) -> None:
	out = render_pdf("<p>hi</p>", page_numbers=False)
	assert out == b"%PDF-1.4 fake pdf bytes"


def test_empty_output_raises(fake_wk: _Recorder) -> None:
	fake_wk.stdout = b""
	with pytest.raises(InvalidArgumentError):
		render_pdf("<p>hi</p>", page_numbers=False)


def test_nonpdf_output_raises(fake_wk: _Recorder) -> None:
	# Non-empty but not a PDF (an error page slipping past exit 0) is rejected.
	fake_wk.stdout = b"<html><body>gateway error</body></html>"
	with pytest.raises(InvalidArgumentError):
		render_pdf("<p>hi</p>", page_numbers=False)


def test_nonzero_exit_logs_stderr_but_keeps_it_out_of_user_message(
	fake_wk: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
	# stderr can carry the /tmp/jv-pdf-<hash>.html temp path — it goes to the
	# Error Log, NOT the user-facing message (which stays clean).
	logged: list[dict] = []
	monkeypatch.setattr(furniture.frappe, "log_error", lambda **k: logged.append(k))
	fake_wk.returncode = 3
	fake_wk.stderr = b"unique-stderr-signature /tmp/jv-pdf-abc.html"
	with pytest.raises(InvalidArgumentError) as excinfo:
		render_pdf("<p>hi</p>", page_numbers=False)
	assert "unique-stderr-signature" not in str(excinfo.value)
	assert "3" in str(excinfo.value)  # the exit code is fine to surface
	assert any("unique-stderr-signature" in str(k.get("message", "")) for k in logged)


def test_infra_failure_is_logged(fake_wk: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
	# A working binary misbehaving (non-zero exit) must hit the Error Log so a
	# systemic regression is visible to operators, not only surfaced to the user.
	logged: list[dict] = []
	monkeypatch.setattr(furniture.frappe, "log_error", lambda **k: logged.append(k))
	fake_wk.returncode = 2
	fake_wk.stderr = b"err"
	with pytest.raises(InvalidArgumentError):
		render_pdf("<p>hi</p>", page_numbers=False)
	assert logged, "infra render failure was not logged"


# --- binary resolution -------------------------------------------------------


def test_binary_not_found_raises_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
	def _no_config(*_a, **_k):
		raise OSError("No wkhtmltopdf executable found")

	monkeypatch.setattr(furniture.pdfkit, "configuration", _no_config)
	monkeypatch.setattr(furniture.shutil, "which", lambda _name: None)
	with pytest.raises(InvalidArgumentError):
		render_pdf("<p>hi</p>")


# --- page numbering ----------------------------------------------------------


def test_page_numbers_footer_uses_bracket_vars(fake_wk: _Recorder) -> None:
	render_pdf("<p>hi</p>", page_numbers=True)
	assert "--footer-html" in fake_wk.args
	content = fake_wk.file_contents["--footer-html"]
	assert "[page]" in content
	assert "[topage]" in content


def test_no_footer_when_page_numbers_off_and_no_footer(fake_wk: _Recorder) -> None:
	render_pdf("<p>hi</p>", page_numbers=False)
	assert "--footer-html" not in fake_wk.args


def test_no_header_when_nothing_to_render(fake_wk: _Recorder) -> None:
	render_pdf("<p>hi</p>", page_numbers=False)
	assert "--header-html" not in fake_wk.args


def test_explicit_footer_suppresses_auto_page_numbers(fake_wk: _Recorder) -> None:
	render_pdf("<p>hi</p>", footer_html="<b>my-footer</b>", page_numbers=True)
	content = fake_wk.file_contents["--footer-html"]
	assert "my-footer" in content
	assert "[page]" not in content


# --- letterhead: folding (pre-resolved, trusted HTML) -----------------------


def test_letterhead_header_footer_folded(fake_wk: _Recorder) -> None:
	# render_pdf receives ALREADY-RESOLVED, safe letterhead HTML and folds it in
	# as-is (it is not re-sanitized - the images were neutralised in resolve).
	render_pdf(
		"<p>hi</p>",
		letterhead_header="<div>LH-HEADER-MARK</div>",
		letterhead_footer="<div>LH-FOOTER-MARK</div>",
		page_numbers=False,
	)
	assert "LH-HEADER-MARK" in fake_wk.file_contents["--header-html"]
	assert "LH-FOOTER-MARK" in fake_wk.file_contents["--footer-html"]


# --- letterhead: image inlining (the SSRF fix), site-free via stubs ----------


def test_inline_keeps_data_uri() -> None:
	src = '<img src="data:image/png;base64,AAAA">'
	assert furniture._inline_letterhead_images(src) == src


def test_inline_drops_remote_image() -> None:
	out = furniture._inline_letterhead_images('<p>x</p><img src="http://evil/logo.png"><p>y</p>')
	assert "<img" not in out.lower()
	assert "evil" not in out.lower()
	assert "x" in out and "y" in out


def test_inline_drops_protocol_relative_image() -> None:
	out = furniture._inline_letterhead_images('<img src="//evil/logo.png">')
	assert "<img" not in out.lower()
	assert "evil" not in out.lower()


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


def test_inline_same_site_to_base64_with_perm(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(furniture.frappe, "db", _FakeDB(_file_gv()))
	monkeypatch.setattr(furniture.frappe, "has_permission", lambda *_a, **_k: True)
	monkeypatch.setattr(furniture.frappe, "get_doc", lambda *_a, **_k: _PngFile())
	out = furniture._inline_letterhead_images('<img src="/files/logo.png">')
	assert "data:image/png;base64," in out
	assert "http" not in out.lower()


def test_inline_same_site_dropped_without_perm(monkeypatch: pytest.MonkeyPatch) -> None:
	# get_doc is mocked to SUCCEED so the drop is attributable ONLY to
	# has_permission=False — if the perm check were removed, this would LEAK a
	# base64 image and the test would fail (mutation-catching).
	monkeypatch.setattr(furniture.frappe, "db", _FakeDB(_file_gv()))
	monkeypatch.setattr(furniture.frappe, "has_permission", lambda *_a, **_k: False)
	monkeypatch.setattr(furniture.frappe, "get_doc", lambda *_a, **_k: _PngFile())
	out = furniture._inline_letterhead_images('<img src="/files/logo.png">')
	assert "<img" not in out.lower()
	assert "base64" not in out.lower()


def test_inline_unknown_file_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(furniture.frappe, "db", _FakeDB(_file_gv(name=None)))
	monkeypatch.setattr(furniture.frappe, "has_permission", lambda *_a, **_k: True)
	monkeypatch.setattr(furniture.frappe, "get_doc", lambda *_a, **_k: _PngFile())
	out = furniture._inline_letterhead_images('<img src="/files/missing.png">')
	assert "<img" not in out.lower()


def test_inline_oversized_logo_dropped_before_read(monkeypatch: pytest.MonkeyPatch) -> None:
	# An oversized File is rejected on its file_size BEFORE get_content is called.
	monkeypatch.setattr(furniture.frappe, "db", _FakeDB(_file_gv(size=furniture._MAX_LOGO_BYTES + 1)))
	monkeypatch.setattr(furniture.frappe, "has_permission", lambda *_a, **_k: True)

	def _boom(*_a, **_k):
		raise AssertionError("get_content must not be called for an oversized logo")

	monkeypatch.setattr(furniture.frappe, "get_doc", _boom)
	out = furniture._inline_letterhead_images('<img src="/files/huge.png">')
	assert "<img" not in out.lower()


def test_inline_nonimage_bytes_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
	# A file whose bytes are not an image (mislabeled *.png) is not inlined.
	monkeypatch.setattr(furniture.frappe, "db", _FakeDB(_file_gv()))
	monkeypatch.setattr(furniture.frappe, "has_permission", lambda *_a, **_k: True)

	class _NotImg:
		def get_content(self):
			return b"<html>not an image</html>"

	monkeypatch.setattr(furniture.frappe, "get_doc", lambda *_a, **_k: _NotImg())
	out = furniture._inline_letterhead_images('<img src="/files/fake.png">')
	assert "<img" not in out.lower()
	assert "base64" not in out.lower()


# --- letterhead: resolution + degrade note, site-free via stubs --------------


def _stub_letterhead(monkeypatch, *, default=None, docs=None, perms=True):
	docs = docs or {}

	def _get_value(_dt, filt, _field=None, **_k):
		if isinstance(filt, dict):  # the default (is_default=1) lookup
			return default
		return docs.get(filt)  # named/default-name doc lookup

	monkeypatch.setattr(furniture.frappe, "db", _FakeDB(_get_value))
	monkeypatch.setattr(furniture.frappe, "has_permission", lambda *_a, **_k: perms)


def test_resolve_letterhead_default_used_when_unnamed(monkeypatch: pytest.MonkeyPatch) -> None:
	_stub_letterhead(
		monkeypatch,
		default="Std",
		docs={"Std": {"content": "<div>HDR</div>", "footer": "<div>FTR</div>"}},
	)
	header, footer, note = resolve_letterhead(None)
	assert "HDR" in header and "FTR" in footer
	assert note is None


def test_resolve_letterhead_named(monkeypatch: pytest.MonkeyPatch) -> None:
	_stub_letterhead(monkeypatch, docs={"Acme": {"content": "<div>ACME</div>", "footer": ""}})
	header, _footer, note = resolve_letterhead("Acme")
	assert "ACME" in header
	assert note is None


def test_resolve_letterhead_no_default_no_note(monkeypatch: pytest.MonkeyPatch) -> None:
	_stub_letterhead(monkeypatch, default=None)
	header, footer, note = resolve_letterhead(None)
	assert header == "" and footer == ""
	assert note is None  # no default configured is a normal unbranded render


def test_resolve_letterhead_named_not_found_notes(monkeypatch: pytest.MonkeyPatch) -> None:
	_stub_letterhead(monkeypatch, docs={})  # named lookup returns None
	header, footer, note = resolve_letterhead("Nope")
	assert header == "" and footer == ""
	assert note and "Nope" in note


def test_resolve_letterhead_no_read_perm_notes(monkeypatch: pytest.MonkeyPatch) -> None:
	_stub_letterhead(monkeypatch, docs={"Acme": {"content": "<div>ACME</div>", "footer": ""}}, perms=False)
	header, _footer, note = resolve_letterhead("Acme")
	assert header == ""
	assert note and "Acme" in note


def test_resolve_letterhead_strips_remote_image_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
	# The F1 SSRF gate wired through resolve_letterhead: a Letter Head whose content
	# carries a remote <img>/<link> must come back with NO remote URL (the gate runs
	# after inlining), while benign text survives.
	_stub_letterhead(
		monkeypatch,
		docs={
			"Evil": {
				"content": '<div>Acme<img src="http://169.254.169.254/x">'
				'<link href="http://evil/x.css"></div>',
				"footer": "",
			}
		},
	)
	header, _footer, note = resolve_letterhead("Evil")
	low = header.lower()
	assert "169.254" not in low and "evil" not in low
	assert "<link" not in low
	assert "Acme" in header
	assert note is None


def test_remove_quietly_suppresses_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
	# A non-FileNotFound OSError (e.g. EPERM) during cleanup must be swallowed so
	# the finally loop can't abort (leaking the other temp) or mask the real error.
	def _raise(_p):
		raise PermissionError("EPERM")

	monkeypatch.setattr(furniture.os, "remove", _raise)
	furniture._remove_quietly("/tmp/whatever")  # must NOT raise


@pytest.mark.skipif(not _HAS_SITE, reason="needs a bench site (get_letter_head / File)")
def test_resolve_letterhead_real_default() -> None:
	header, footer, note = resolve_letterhead(None)
	assert isinstance(header, str) and isinstance(footer, str)
	assert note is None
