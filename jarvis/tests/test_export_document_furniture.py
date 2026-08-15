"""Tests for ``jarvis.tools._export.document.furniture.render_pdf`` (the rich-PDF
render core: security flags + a real subprocess render timeout).

wkhtmltopdf is ABSENT in local dev (brew removed it), so this suite NEVER runs a
real render - that is the Frappe Cloud smoke gate. Instead ``subprocess.run`` is
mocked and ``_resolve_binary`` is stubbed to a fake path, and the assertions
cover:
  * the UNCONDITIONAL security/fidelity arg list (parametrized; drop a flag ->
    the matching case fails - the mutation target);
  * header/footer/watermark are run through ``sanitize_rich`` before the temp
    file is written (a mock reads each temp file WHILE it still exists);
  * temp-file lifecycle: created for the call, gone after return, AND removed on a
    ``TimeoutExpired`` (which surfaces as a clean ``InvalidArgumentError``);
  * distinct temp names across calls; empty-output guard; binary-not-found.

``fmt_amount``-style site-dependent behavior and the real ``get_letter_head`` /
real render are gated (skipped) here and belong to the FC smoke gate.
"""

import os
import subprocess

import pytest

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._export.document import furniture
from jarvis.tools._export.document.furniture import render_pdf

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
	# No header/footer/watermark/letterhead, page numbers off: the leanest render
	# still carries every security/fidelity flag. Dropping any from _build_args
	# fails exactly this case (the mutation target).
	render_pdf("<p>hi</p>", page_numbers=False)
	assert flag in fake_wk.args


def test_encoding_flag_value(fake_wk: _Recorder) -> None:
	render_pdf("<p>hi</p>", page_numbers=False)
	i = fake_wk.args.index("--encoding")
	assert fake_wk.args[i + 1] == "utf-8"


def test_page_size_default_and_custom(fake_wk: _Recorder) -> None:
	render_pdf("<p>hi</p>", page_numbers=False)
	assert fake_wk.args[fake_wk.args.index("--page-size") + 1] == "A4"
	render_pdf("<p>hi</p>", page_size="Letter", page_numbers=False)
	assert fake_wk.args[fake_wk.args.index("--page-size") + 1] == "Letter"


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
	# sanitized: no fetch-capable/active markup from the agent survives
	assert "<img" not in low
	assert "evil" not in low
	assert "<script" not in low
	# but the tool-controlled rotate block and the (escaped-safe) text remain
	assert "jv-watermark" in content
	assert "rotate(-45deg)" in content
	assert "DRAFT" in content


# --- temp-file lifecycle: created, then removed (success + timeout) ----------


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
	# clean, actionable message - NOT a raw TimeoutExpired
	assert "7s" in str(excinfo.value)
	assert not isinstance(excinfo.value, subprocess.TimeoutExpired)
	# finally-block removed the temp files even though the render "hung"
	for flag in ("--header-html", "--footer-html"):
		assert not os.path.exists(fake_wk.path_for(flag))


def test_render_exception_still_cleans_temp(fake_wk: _Recorder) -> None:
	# A non-zero exit is a render failure; temp files must still be cleaned.
	fake_wk.returncode = 1
	fake_wk.stderr = b"boom: qt render blew up"
	with pytest.raises(InvalidArgumentError) as excinfo:
		render_pdf("<p>body</p>", header_html="<b>H</b>")
	assert "boom" in str(excinfo.value)
	assert not os.path.exists(fake_wk.path_for("--header-html"))


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


def test_nonzero_exit_includes_stderr_tail(fake_wk: _Recorder) -> None:
	fake_wk.returncode = 3
	fake_wk.stderr = b"unique-stderr-signature"
	with pytest.raises(InvalidArgumentError) as excinfo:
		render_pdf("<p>hi</p>", page_numbers=False)
	assert "unique-stderr-signature" in str(excinfo.value)


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


# --- letterhead: fold logic (site-free via a stub); real call is gated -------


def test_letterhead_folded_into_header_and_footer(
	fake_wk: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
	# Stub the site-dependent Letter Head lookup; assert its header/footer HTML is
	# folded into the composed temp documents (trusted config, not re-sanitized).
	monkeypatch.setattr(
		furniture,
		"_letterhead_parts",
		lambda name: ("<div>LH-HEADER-MARK</div>", "<div>LH-FOOTER-MARK</div>"),
	)
	render_pdf("<p>hi</p>", letterhead="Standard", page_numbers=False)
	assert "LH-HEADER-MARK" in fake_wk.file_contents["--header-html"]
	assert "LH-FOOTER-MARK" in fake_wk.file_contents["--footer-html"]


@pytest.mark.skip(reason="needs bench site — FC smoke gate (real get_letter_head + real render)")
def test_letterhead_real_lookup() -> None:
	render_pdf("<p>hi</p>", letterhead="Standard")
