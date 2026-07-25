"""``jarvis__read_app_source`` — the Custom App Learning scribe's single-file
source read (self-gated, read-only, budgeted).

Returns ONE custom-app source file's UTF-8 text wrapped in an untrusted-data
fence (the source is DATA, never instructions). The guard stack, in order:

  1. Self-gate — resolve a running app-learning **scribe** ``Jarvis Agent Run``
     from the caller's session_key (never a model-supplied id) + admin-tier.
  2. Custom-apps allowlist + RUN SCOPE — ``app`` must be an installed non-core app
     (``EXCLUDED_APPS`` is absolute) AND one the launching admin explicitly
     selected for this run (CX5-2, ``scope_json.source_apps``).
  3. Per-run source-BYTES budget — refuse once the run has already read its
     budget, so a looping delegate cannot exhaust the run reading source.
  4. File-level guards (in ``app_source.read_source_file``): relative-path only,
     extension allowlist, realpath-containment + symlink skipping (the secret-
     exfil guard — a symlinked ``common_site_config.json`` is refused before it
     is opened), and the per-file 200 KB cap.

Then the returned text is fenced with ``turn_handler._fence_untrusted`` so a
crafted file cannot break out of the fence and be misread as instructions.
"""

from __future__ import annotations

from jarvis.exceptions import InvalidArgumentError
from jarvis.learning import app_source
from jarvis.tools import _app_learning_ctx as ctx


def read_app_source(app: str, path: str) -> dict:
	"""Read one custom-app source file (fenced as untrusted data)."""
	run = ctx.resolve_scribe_run()  # self-gate: scribe run + admin-tier or raise
	session_key = run["session_key"]

	app = str(app or "").strip()
	path = str(path or "").strip()
	if not app or not path:
		raise InvalidArgumentError("both app and path are required")
	ctx.assert_custom_app(app, run["name"])  # allowlist + this run's authorized selection

	# Per-run byte budget: refuse once the run has already read its budget. The
	# per-file 200 KB cap bounds the overshoot from the file that tips it over.
	used_before = ctx.source_bytes_used(session_key)
	if used_before >= ctx.PER_RUN_SOURCE_BYTES_BUDGET:
		raise InvalidArgumentError(
			"per-run source-read budget exhausted — compose your wiki pages from the "
			"source you have already read"
		)

	# File-level guards live in the shared module (identical to the legacy walk):
	# extension allowlist, realpath-containment + symlink skip, per-file 200 KB.
	try:
		text, size = app_source.read_source_file(app, path)
	except ValueError as e:
		raise InvalidArgumentError(str(e))

	used = ctx.add_source_bytes(session_key, size)

	# Source is DATA: fence it so a crafted comment/docstring/string cannot break
	# out and be misread as an instruction to the delegate.
	from jarvis.chat.turn_handler import _fence_untrusted

	return {
		"app": app,
		"path": path,
		"size": size,
		"bytes_used": used,
		"bytes_budget": ctx.PER_RUN_SOURCE_BYTES_BUDGET,
		"content": _fence_untrusted(text, f"{app} source file: {path}"),
	}
