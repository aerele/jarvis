"""Shared custom-app source-collection machinery (single source of truth).

The guards that make reading a customer's custom Frappe app source SAFE live
here once and are imported by both consumers:

  * the LEGACY chat pipeline (``jarvis.learning.app_analysis``) — snapshot zip +
    batched chat analysis; and
  * the DELEGATE source-read tools (``jarvis.tools.list_app_modules`` /
    ``jarvis.tools.read_app_source``) that back the Custom App Learning *scribe*
    agent — the delegate PULLS source on demand into its own container context.

What is shared (and why it must never be copy-pasted):
  * ``EXCLUDED_APPS`` — the custom-apps allowlist (NEVER serve frappe / erpnext /
    hrms / india_compliance / jarvis source);
  * ``EXT_ALLOWLIST`` + the excluded-dir sets + the per-file / file-count / zip
    caps;
  * ``_is_contained`` — the realpath-containment + symlink-skipping guard: a
    symlinked ``config.json -> ../../sites/common_site_config.json`` must never
    ship site secrets to the external LLM;
  * ``_priority`` — the analysis ordering (hooks.py, doctype schemas/controllers,
    reports, api, fixtures, then js/md);
  * ``_collect_files`` — the collection walk that applies all of the above.

Nothing here writes to the app source tree; every path is realpath-checked to
stay INSIDE the app dir before it is opened.
"""

from __future__ import annotations

import os

import frappe

# Core apps whose knowledge Jarvis already has (or must never mine). The
# custom-apps allowlist is ABSOLUTE: source of these apps is never served.
EXCLUDED_APPS = frozenset({"frappe", "erpnext", "hrms", "india_compliance", "jarvis"})

# --- snapshot / read caps ----------------------------------------------------
EXT_ALLOWLIST = frozenset(
	{
		".py",
		".js",
		".ts",
		".vue",
		".json",
		".md",
		".txt",
		".html",
		".css",
		".toml",
		".cfg",
		".yml",
		".yaml",
	}
)
_EXCLUDE_DIR_NAMES = frozenset(
	{
		".git",
		"node_modules",
		"dist",
		"build",
		"__pycache__",
		".venv",
		"env",
	}
)
_EXCLUDE_DIR_PATHS = frozenset({"public/frontend", "public/dist"})
PER_FILE_CAP_BYTES = 200 * 1024
FILE_CAP = 3000
ZIP_CAP_BYTES = 25 * 1024 * 1024
# Fast-probe bail for list_custom_apps (approx_files/approx_kb stop counting).
APPROX_FILE_BAIL = 3000


# --------------------------------------------------------------------------- #
# app discovery (factored so tests can patch)
# --------------------------------------------------------------------------- #
def _installed_custom_apps() -> list[str]:
	"""Installed apps eligible for learning (install order kept)."""
	return [a for a in frappe.get_installed_apps() if a not in EXCLUDED_APPS]


def _app_source_dir(app: str) -> str:
	"""Source dir of ``app`` on this bench. Factored (tests patch this)."""
	return os.path.join(frappe.utils.get_bench_path(), "apps", app)


def _resolve_app_root(app: str) -> str:
	"""Canonical, containment-validated source ROOT for ``app`` — the ONLY safe
	way to turn an app name into a directory to walk or read.

	``_is_contained`` / ``read_source_file`` reject a symlinked *file* whose target
	escapes the tree, but they TRUST the root they are handed. A symlinked *root*
	(``apps/<app> -> <bench>/sites``) defeats that: every regular file beneath the
	link passes containment AND ``os.path.islink`` (only the root is the link), so
	``common_site_config.json`` could be listed and shipped to the LLM. This closes
	that hole BEFORE any walk/read:

	  * the app name must be a bare directory component (no path separator, ``.``
	    or ``..``), so it can never resolve to a sibling of ``apps/``; and
	  * the resolved root must be EXACTLY ``<realpath(apps)>/<app>`` — a real
	    directory directly beneath the resolved apps root; a symlinked or relocated
	    root resolves elsewhere and is refused.

	Raises ``ValueError`` on any violation; returns the realpath on success. The
	apps root is taken from ``_app_source_dir``'s parent so the tests' source-dir
	seam still governs the layout."""
	name = (app or "").strip()
	if (
		not name
		or name in (os.curdir, os.pardir)
		or name != os.path.basename(name)
		or os.sep in name
		or (os.altsep and os.altsep in name)
	):
		raise ValueError("invalid app name (must be a bare app directory name)")
	candidate = _app_source_dir(name)
	apps_real = os.path.realpath(os.path.dirname(candidate))
	root_real = os.path.realpath(candidate)
	if root_real != os.path.join(apps_real, name) or not os.path.isdir(root_real):
		raise ValueError("app source root escapes the apps directory (symlinked or relocated root)")
	return root_real


def validate_source_apps(names) -> list[str]:
	"""Canonicalize + VALIDATE an admin's explicit app selection for one learning
	run (CX5-2). Returns the de-duplicated selection in the caller's order.

	Every name must be an INSTALLED learnable custom app with a containment-valid
	source root — the same two checks the per-call tool gate applies — so a bad
	selection is refused at LAUNCH (loudly, where a human can see it) rather than
	silently narrowing the run. Raises ``ValueError`` naming the offending app on
	an empty selection, a core app, an app that is not installed, or one whose
	source root fails ``_resolve_app_root``."""
	if isinstance(names, str):
		import json as _json

		try:
			names = _json.loads(names)
		except Exception:
			names = [n.strip() for n in names.split(",")]
	if not isinstance(names, list | tuple | set | frozenset):
		raise ValueError("select at least one custom app to learn from")
	installed = set(_installed_custom_apps())
	out: list[str] = []
	for raw in names:
		name = str(raw or "").strip()
		if not name or name in out:
			continue
		if name in EXCLUDED_APPS or name not in installed:
			raise ValueError(f"{name!r} is not a learnable custom app on this bench")
		_resolve_app_root(name)  # existence + symlinked/relocated-root rejection
		out.append(name)
	if not out:
		raise ValueError("select at least one custom app to learn from")
	return out


def _app_title(app: str) -> str:
	try:
		titles = frappe.get_hooks("app_title", app_name=app)
		return str(titles[0]) if titles else app
	except Exception:
		return app


def _app_version(app: str) -> str:
	try:
		return str(frappe.get_attr(f"{app}.__version__") or "")
	except Exception:
		return ""


def _approx_size(src_dir: str) -> tuple[int, int]:
	"""(approx eligible file count, approx KB) — fast walk with a bail at
	``APPROX_FILE_BAIL`` files so a huge tree can't stall the settings card."""
	count = 0
	total = 0
	src_real = os.path.realpath(src_dir)
	for root, dirs, files in os.walk(src_dir):
		rel_root = os.path.relpath(root, src_dir)
		dirs[:] = [
			d for d in dirs if not _is_excluded_dir(rel_root, d) and not os.path.islink(os.path.join(root, d))
		]
		for fname in files:
			if os.path.splitext(fname)[1].lower() not in EXT_ALLOWLIST:
				continue
			full = os.path.join(root, fname)
			# Mirror the snapshot's symlink-containment guard so the probe count
			# matches what will actually be read.
			if os.path.islink(full) or not _is_contained(full, src_real):
				continue
			count += 1
			try:
				total += os.path.getsize(full)
			except OSError:
				pass
			if count >= APPROX_FILE_BAIL:
				return count, total // 1024
	return count, total // 1024


# --------------------------------------------------------------------------- #
# collection walk (allowlist + excludes + caps + containment + priority)
# --------------------------------------------------------------------------- #
def _is_excluded_dir(rel_root: str, dirname: str) -> bool:
	if dirname in _EXCLUDE_DIR_NAMES:
		return True
	rel = os.path.join(rel_root, dirname) if rel_root not in (".", "") else dirname
	return rel.replace(os.sep, "/") in _EXCLUDE_DIR_PATHS


def _priority(rel_path: str) -> int:
	"""Manifest / subset priority (lower = analysed first / kept under the cap):
	hooks.py, doctype schemas, doctype controllers, report scripts, api-ish
	python, workflow/fixture JSON, other python, js/ts/vue, md, rest.

	Report scripts (``/report/<name>/*.py``) and fixture-style JSON
	(``fixtures/*.json``, plus Workflow / Property Setter / Custom Field / Print
	Format exports which often carry the real business logic in fixture-heavy
	apps) are lifted ABOVE js/vue/md so they aren't the first things dropped when
	a large app hits the file cap."""
	p = rel_path.replace(os.sep, "/")
	pp = f"/{p}"
	base = os.path.basename(p)
	if base == "hooks.py":
		return 0
	in_doctype = "/doctype/" in pp
	if in_doctype and p.endswith(".json"):
		return 1
	if in_doctype and p.endswith(".py") and base != "__init__.py":
		return 2
	if "/report/" in pp and p.endswith(".py") and base != "__init__.py":
		return 3
	if p.endswith(".py") and "api" in base:
		return 4
	# fixture-ish JSON: /fixtures/ exports, or workflow/print-format/custom-field
	# folders that store business config as JSON.
	if p.endswith(".json") and (
		"/fixtures/" in pp or "/workflow/" in pp or "/print_format/" in pp or "/custom/" in pp
	):
		return 5
	if p.endswith(".py"):
		return 6
	if p.endswith((".js", ".ts", ".vue")):
		return 7
	if p.endswith(".md"):
		return 8
	return 9


def _is_contained(full: str, root_real: str) -> bool:
	"""True iff ``full`` resolves to a path INSIDE ``root_real`` (already a
	realpath). Defends against symlinks in the app source that would otherwise
	make ``open()``/``zipfile.write`` follow the link and ship the TARGET's
	content — e.g. a ``config.json -> ../../sites/common_site_config.json`` link
	exfiltrating site secrets to the external LLM. os.walk itself does not
	descend symlinked dirs (followlinks=False), but a symlinked FILE is still
	opened by name, so every file is realpath-checked here."""
	try:
		real = os.path.realpath(full)
	except OSError:
		return False
	return real == root_real or real.startswith(root_real + os.sep)


def _collect_files(src_dir: str) -> tuple[list[str], dict]:
	"""Snapshot-eligible relative paths (sorted for determinism) + coverage
	notes. Applies the extension allowlist, dir excludes, the per-file size
	cap (skip + note), a symlink-containment guard (skip + note) and the total
	file cap (prioritized subset + note)."""
	rels: list[str] = []
	skipped_large = 0
	skipped_symlink = 0
	src_real = os.path.realpath(src_dir)
	for root, dirs, files in os.walk(src_dir):
		rel_root = os.path.relpath(root, src_dir)
		# Drop excluded AND symlinked directories (never descend a link out of
		# the tree — os.walk keeps followlinks off, but excluding here also keeps
		# such dirs out of the size probe / listing).
		dirs[:] = sorted(
			d for d in dirs if not _is_excluded_dir(rel_root, d) and not os.path.islink(os.path.join(root, d))
		)
		for fname in sorted(files):
			if os.path.splitext(fname)[1].lower() not in EXT_ALLOWLIST:
				continue
			full = os.path.join(root, fname)
			if os.path.islink(full) or not _is_contained(full, src_real):
				skipped_symlink += 1
				continue
			try:
				size = os.path.getsize(full)
			except OSError:
				continue
			if size > PER_FILE_CAP_BYTES:
				skipped_large += 1
				continue
			rel = fname if rel_root in (".", "") else os.path.join(rel_root, fname)
			rels.append(rel.replace(os.sep, "/"))
	dropped = 0
	if len(rels) > FILE_CAP:
		rels.sort(key=lambda r: (_priority(r), r))
		dropped = len(rels) - FILE_CAP
		rels = rels[:FILE_CAP]
	notes = {
		"skipped_large_files": skipped_large,
		"skipped_symlinks": skipped_symlink,
		"dropped_files": dropped,
	}
	return rels, notes


def coverage_caveats(notes: dict | None) -> list[str]:
	"""Human-readable coverage gaps (file-count / per-file-size / symlink) from a
	``_collect_files`` notes dict, so a delegate can disclose partial coverage on
	the overview wiki page. The batch-drop caveat is legacy-pipeline-only and is
	added by ``app_analysis._coverage_caveats``; this is the source-collection
	subset shared with the delegate tools."""
	notes = notes or {}
	out: list[str] = []
	dropped = int(notes.get("dropped_files") or 0)
	if dropped:
		out.append(f"{dropped} lowest-priority file(s) omitted (total-file cap)")
	large = int(notes.get("skipped_large_files") or 0)
	if large:
		out.append(f"{large} file(s) skipped for exceeding the per-file size cap")
	symlinks = int(notes.get("skipped_symlinks") or 0)
	if symlinks:
		out.append(f"{symlinks} symlinked file(s) skipped (not served for safety)")
	return out


def read_source_file(app: str, rel_path: str, src_dir: str | None = None) -> tuple[str, int]:
	"""Read ONE file's UTF-8 text from a custom app's source tree, applying the
	full guard stack: relative-path only, extension allowlist, realpath-
	containment + symlink skipping (the secret-exfil guard, incl. any escaping
	INTERMEDIATE symlink — ``os.path.realpath`` resolves the whole chain), and
	the per-file 200 KB cap. Returns ``(text, size_bytes)``; raises ``ValueError``
	on any guard rejection.

	The custom-apps allowlist, the per-run byte budget and the untrusted-data
	fence are the caller's job (``jarvis.tools.read_app_source``) — this function
	is the file-level guard only, shared with the legacy collection walk."""
	raw_rel = rel_path or ""
	if os.path.isabs(raw_rel):
		raise ValueError("path must be a relative path inside the app source tree")
	rel = raw_rel.strip().replace("\\", "/").lstrip("/")
	if not rel:
		raise ValueError("path must be a relative path inside the app source tree")
	if os.path.splitext(rel)[1].lower() not in EXT_ALLOWLIST:
		raise ValueError("file type not permitted for source reading")
	# Canonicalize + validate the trust ROOT before any read: a symlinked app root
	# (``apps/<app> -> <bench>/sites``) would otherwise make every regular file
	# beneath it pass the per-file containment guard and exfiltrate site secrets.
	src = src_dir or _resolve_app_root(app)
	src_real = os.path.realpath(src)
	full = os.path.join(src, rel)
	# The realpath-containment + symlink guard is the load-bearing secret-exfil
	# defense: a symlinked file (or any escaping intermediate link) whose target
	# is outside the app tree is refused before it is ever opened.
	if os.path.islink(full) or not _is_contained(full, src_real):
		raise ValueError("path escapes the app source tree (symlink or traversal)")
	if not os.path.isfile(full):
		raise ValueError("no such source file in the app")
	size = os.path.getsize(full)
	if size > PER_FILE_CAP_BYTES:
		raise ValueError(f"file exceeds the per-file {PER_FILE_CAP_BYTES // 1024} KB cap")
	with open(full, "rb") as fh:
		return fh.read().decode("utf-8", errors="replace"), size
