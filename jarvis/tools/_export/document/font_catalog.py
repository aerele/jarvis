"""Bundled font catalog for custom PDF templates: a fixed, curated set of open
(SIL OFL) fonts an admin picks from — no uploads. Each font must render IDENTICALLY
in the settings-pane HTML preview (Chrome) and the real PDF (wkhtmltopdf), which
need OPPOSITE mechanisms (proven — see the ``wkhtmltopdf-custom-fonts`` note):

  * PREVIEW / standalone HTML (Chrome): a base64 ``@font-face`` block. -> ``font_face_css``.
  * PDF / PNG (wkhtmltopdf 0.12.6 QtWebKit): a base64 ``@font-face`` of any real size
    BLANKS the page; the font must be registered with fontconfig and referenced BY
    FAMILY NAME. -> ``staged_fontconfig`` stages the bundled files into a temp
    fontconfig tree and yields a ``FONTCONFIG_FILE`` for the wkhtmltopdf subprocess
    env. The PDF CSS carries the family stack ONLY, never an ``@font-face``.

PURE module (no ``import frappe``): the catalog is static and the faces are bundled
files under ``fonts/`` (static Regular + Bold, subset to Latin, built by the repo's
font build step), so both helpers are plain file I/O and unit-testable without a
bench. The families are TRUSTED constants here, so nothing user-authored ever reaches
a ``font-family`` — the injection surface the uploaded-font design had is gone.

``sans``/``serif`` are the two system-stack defaults (no bundled file); every other
key is a bundled family with a ``regular`` and ``bold`` face."""

from __future__ import annotations

import base64
import contextlib
import os
import shutil
import tempfile

from .theme import FONT_STACKS

_FONTS_DIR = os.path.join(os.path.dirname(__file__), "fonts")
_SANS = FONT_STACKS["sans"]
_SERIF = FONT_STACKS["serif"]
_FALLBACK = {"sans": _SANS, "serif": _SERIF}

# key -> (label, category, family). family=None means a system-stack default (no
# bundled file). Order here is the order shown in the picker (defaults, then sans,
# then serif). Bundled keys have `<key>-regular.ttf` + `<key>-bold.ttf` in fonts/.
_CATALOG: dict[str, dict] = {
	"sans": {"label": "System Sans", "category": "sans", "family": None},
	"serif": {"label": "System Serif", "category": "serif", "family": None},
	"inter": {"label": "Inter", "category": "sans", "family": "Inter"},
	"lato": {"label": "Lato", "category": "sans", "family": "Lato"},
	"worksans": {"label": "Work Sans", "category": "sans", "family": "Work Sans"},
	"lora": {"label": "Lora", "category": "serif", "family": "Lora"},
	"merriweather": {"label": "Merriweather", "category": "serif", "family": "Merriweather"},
	"ptserif": {"label": "PT Serif", "category": "serif", "family": "PT Serif"},
}

DEFAULT_KEY = "sans"

# Every bundled family ships these two static faces (headings/titles need a REAL
# bold, not a synthesized one). If a family is missing one, that is surfaced as a
# degrade note rather than silently synthesizing bold.
_WEIGHTS = ("regular", "bold")

# A bundled face's base64 ``@font-face`` rule, memoized by absolute path. The files
# are static and read-only, so caching the encode is safe and spares the interactive
# preview path (called on every edit) from re-reading + re-encoding ~hundreds of KB.
_FACE_RULE_CACHE: dict[str, str] = {}


def is_valid(key: str | None) -> bool:
	"""True if ``key`` names a catalog entry."""
	return key in _CATALOG


def is_bundled(key: str | None) -> bool:
	"""True if ``key`` is a bundled font (has faces to embed/stage), not a default."""
	entry = _CATALOG.get(key or "")
	return bool(entry and entry["family"])


def family_for(key: str | None) -> str:
	"""The family name for a bundled key, else "" (a system-stack default)."""
	entry = _CATALOG.get(key or "")
	return (entry["family"] or "") if entry else ""


def stack_for(key: str | None) -> str | None:
	"""The CSS ``font-family`` stack for ``key``: a bundled font's family before its
	category fallback (``"Inter", <sans stack>``), or the plain system stack for a
	default. ``None`` for an unknown key (the caller then leaves the token unset so it
	falls back to classic)."""
	entry = _CATALOG.get(key or "")
	if not entry:
		return None
	fallback = _FALLBACK[entry["category"]]
	fam = entry["family"]
	return f'"{fam}", {fallback}' if fam else fallback


def font_options() -> list[dict]:
	"""``[{key, label, category}]`` for the API / SPA picker, in catalog order."""
	return [{"key": k, "label": v["label"], "category": v["category"]} for k, v in _CATALOG.items()]


def _faces(key: str) -> list[tuple[str, str, str]]:
	"""``[(family, "regular"|"bold", abspath)]`` for a bundled key whose files exist."""
	entry = _CATALOG.get(key)
	if not entry or not entry["family"]:
		return []
	out = []
	for weight in _WEIGHTS:
		path = os.path.join(_FONTS_DIR, f"{key}-{weight}.ttf")
		if os.path.isfile(path):
			out.append((entry["family"], weight, path))
	return out


def _face_note(key: str, faces: list) -> str | None:
	"""A degrade note when a bundled key is missing face files, else ``None``: no face
	at all (falls back entirely), or one of the expected weights missing (the other
	still renders, but bold would be synthesized — surface it rather than pretend)."""
	if not faces:
		return f"font {key!r} could not be applied (files missing)"
	if len(faces) < len(_WEIGHTS):
		present = {w for _f, w, _p in faces}
		missing = ", ".join(w for w in _WEIGHTS if w not in present)
		return f"font {key!r} is missing its {missing} face; that weight may be synthesized"
	return None


def _bundled_keys(keys) -> list[str]:
	"""De-duplicated bundled keys from ``keys`` (drops defaults and unknowns)."""
	return [k for k in dict.fromkeys(keys or []) if is_bundled(k)]


def _face_rule(family: str, weight: str, path: str) -> str:
	"""The base64 ``@font-face`` rule for one bundled face, memoized by path (the file
	is static + read-only). May raise ``OSError`` if the file vanished after the
	``_faces`` existence check — the caller degrades to a note."""
	cached = _FACE_RULE_CACHE.get(path)
	if cached is not None:
		return cached
	with open(path, "rb") as fh:
		b64 = base64.b64encode(fh.read()).decode()
	css_weight = "700" if weight == "bold" else "400"
	rule = (
		f'@font-face{{font-family:"{family}";'
		f'src:url(data:font/ttf;base64,{b64}) format("truetype");'
		f"font-weight:{css_weight};font-style:normal;font-display:swap;}}"
	)
	_FACE_RULE_CACHE[path] = rule
	return rule


def font_face_css(keys) -> tuple[str, list[str]]:
	"""``(css, notes)`` — base64 ``@font-face`` rules (Regular + Bold) for the bundled
	fonts in ``keys``, for the PREVIEW / standalone-HTML path only. NEVER inject this
	into the PDF path (a real-sized base64 face blanks wkhtmltopdf). A missing bundled
	file is skipped with a note (a build/packaging error, not user input)."""
	rules: list[str] = []
	notes: list[str] = []
	for key in _bundled_keys(keys):
		faces = _faces(key)
		note = _face_note(key, faces)
		if note:
			notes.append(note)
		for family, weight, path in faces:
			try:
				rules.append(_face_rule(family, weight, path))
			except OSError:
				notes.append(f"font {key!r} could not be applied (files missing)")
	return "".join(rules), notes


@contextlib.contextmanager
def staged_fontconfig(keys):
	"""Yield ``(fontconfig_file_path_or_None, notes)`` for the PDF/PNG path.

	Copies the bundled faces for ``keys`` into a private temp fontconfig tree and
	writes a ``fonts.conf`` that INHERITS the system config (so fallbacks resolve) and
	adds the staged dir + a writable cachedir. The caller puts the yielded path in
	``FONTCONFIG_FILE`` on the wkhtmltopdf subprocess env and references the fonts BY
	FAMILY NAME (no ``@font-face``). The temp tree is removed on exit (success, error,
	or timeout). Yields ``(None, notes)`` when nothing is stage-able."""
	bundled = _bundled_keys(keys)
	notes: list[str] = []
	staged = 0
	if not bundled:
		yield None, notes
		return
	tmp = tempfile.mkdtemp(prefix="jv-fc-")
	try:
		fonts_dir = os.path.join(tmp, "fonts")
		cache_dir = os.path.join(tmp, "cache")
		os.makedirs(fonts_dir)
		os.makedirs(cache_dir)
		for key in bundled:
			faces = _faces(key)
			note = _face_note(key, faces)
			if note:
				notes.append(note)
			for _family, weight, path in faces:
				try:
					shutil.copyfile(path, os.path.join(fonts_dir, f"{key}-{weight}.ttf"))
					staged += 1
				except OSError:
					# Matches the HTML path: a face that vanished after the _faces check
					# is skipped with a note, never aborts the whole export.
					notes.append(f"font {key!r} could not be applied (files missing)")
		if not staged:
			yield None, notes
			return
		conf_path = os.path.join(tmp, "fonts.conf")
		with open(conf_path, "w", encoding="utf-8") as fh:
			fh.write(
				'<?xml version="1.0"?>\n'
				'<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
				"<fontconfig>\n"
				'  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>\n'
				f"  <dir>{_xml_escape(fonts_dir)}</dir>\n"
				f"  <cachedir>{_xml_escape(cache_dir)}</cachedir>\n"
				"</fontconfig>\n"
			)
		yield conf_path, notes
	finally:
		shutil.rmtree(tmp, ignore_errors=True)


def _xml_escape(value: str) -> str:
	return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
