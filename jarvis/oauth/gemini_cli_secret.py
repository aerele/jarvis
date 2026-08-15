"""Locate the Google Gemini OAuth client_secret bundled in the
``@google/gemini-cli`` npm package, when the operator hasn't supplied
``JARVIS_GEMINI_CLI_OAUTH_CLIENT_SECRET`` directly.

Extracted from ``jarvis/hooks.py`` in the Sprint-5 cleanup pass. Moving
this out of hooks.py shrinks the perf hazard surface on every Google
OAuth call (hooks.py is import-cached but the function used to be wired
into every secret-resolve), narrows the previously broad ``except
Exception`` to the specific filesystem errors we actually expect, and
gives the module a place to grow its own unit tests.
"""

from __future__ import annotations

import os
import re

import frappe

PACKAGE_REL_PATH = ("node_modules", "@google", "gemini-cli")
SECRET_PATTERN = re.compile(rb'"(GOCSPX-[A-Za-z0-9_-]+)"')


def _app_root() -> str:
	"""Return the customer-bench app root (``apps/jarvis/``).

	This file is ``apps/jarvis/jarvis/oauth/gemini_cli_secret.py``, so the app
	root that holds ``package.json`` + ``node_modules`` is THREE levels up
	(oauth -> jarvis module -> apps/jarvis), not two. Walking up only two
	levels pointed pkg_root at a non-existent ``apps/jarvis/jarvis/node_modules``
	and made the extractor silently return "".
	"""
	return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def extract_gemini_cli_secret(app_root: str | None = None) -> str:
	"""Best-effort scan of the bundled gemini-cli package for its OAuth
	``client_secret`` literal. Returns the matching ``GOCSPX-...`` value
	on first hit, or ``""`` when:

	* the package isn't installed alongside this app
	* the bundle doesn't carry an embedded secret in the expected shape
	* any filesystem error trips during the walk

	The gemini-cli bundle ships chunks up to ~16 MB each, so the scan
	iterates line-by-line instead of slurping. The secret appears as a
	single-line literal (``var OAUTH_CLIENT_SECRET = "GOCSPX-..."`` or
	``client_secret: "GOCSPX-..."``), so a per-line scan finds it
	without holding more than a line in memory.
	"""
	root = app_root if app_root is not None else _app_root()
	pkg_root = os.path.join(root, *PACKAGE_REL_PATH)
	if not os.path.isdir(pkg_root):
		return ""
	try:
		for dirpath, _dirs, files in os.walk(pkg_root):
			for name in files:
				if not name.endswith((".js", ".cjs", ".mjs", ".json")):
					continue
				path = os.path.join(dirpath, name)
				try:
					with open(path, "rb") as f:
						for line in f:
							m = SECRET_PATTERN.search(line)
							if m:
								return m.group(1).decode("utf-8", errors="ignore")
				except OSError:
					# Per-file IO error (permissions, vanished symlink,
					# bad block) - skip and keep walking. Quiet because
					# any single file is best-effort.
					continue
	except OSError as exc:
		# Walk-level failure (e.g. the package root itself becomes
		# unreadable mid-scan). Surface in Error Log so a misconfigured
		# install is visible, then fall through to "".
		frappe.log_error(
			title="extract_gemini_cli_secret: walk failed",
			message=f"pkg_root={pkg_root}: {exc!r}",
		)
	return ""
