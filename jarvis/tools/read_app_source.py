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
     budget, so a looping delegate cannot exhaust the run reading source. The
     counter is DURABLE (``Jarvis Agent Run.source_bytes_read``, read + advanced
     under the run-row lock) and FAILS CLOSED (CX5-3).
  4. Sensitive-filename deny (CX5-4) — a basename advertising credentials
     (``*secret*``, ``*credential*``, ``*password*``, ``.env``, ssh keys) is never
     served; ``.pem``/``.key`` are already outside the extension allowlist.
  5. File-level guards (in ``app_source.read_source_file``): relative-path only,
     extension allowlist, realpath-containment + symlink skipping (the secret-
     exfil guard — a symlinked ``common_site_config.json`` is refused before it
     is opened), and the per-file 200 KB cap.

The served text is then REDACTED (``source_guard.redact_secrets`` — CX5-4: a
committed key/password/PEM block never reaches the model provider verbatim) and
fenced with ``turn_handler._fence_untrusted`` so a crafted file cannot break out
of the fence and be misread as instructions.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.learning import app_source, source_guard
from jarvis.tools import _app_learning_ctx as ctx

RUN = "Jarvis Agent Run"


def read_app_source(app: str, path: str) -> dict:
	"""Read one custom-app source file (fenced as untrusted data)."""
	run = ctx.resolve_scribe_run()  # self-gate: scribe run + admin-tier or raise
	run_name = run["name"]

	app = str(app or "").strip()
	path = str(path or "").strip()
	if not app or not path:
		raise InvalidArgumentError("both app and path are required")
	ctx.assert_custom_app(app, run_name)  # allowlist + this run's authorized selection
	# CX5-4: a file whose NAME advertises credentials is never served (and never
	# listed). Refused before the budget lock, so a probe costs the run nothing.
	if source_guard.is_sensitive_path(path):
		raise InvalidArgumentError(
			"this file is excluded from learning because its name indicates credentials "
			"(secret / credential / password / key files are never served)"
		)

	# CX5-3 — the per-run byte budget is enforced against the DURABLE counter on the
	# run row, read UNDER a row lock: read → check → serve → increment, all in one
	# transaction. The cache is no longer the authority (an outage used to reset the
	# budget to zero and hand a looping delegate a whole fresh 5 MB), and the read
	# FAILS CLOSED — an unreadable row raises rather than serving on an unknown
	# budget. The per-file 200 KB cap bounds the overshoot from the file that tips it
	# over.
	row = ctx.lock_run_budget(run_name, ["source_bytes_read"])
	used_before = int(row.get("source_bytes_read") or 0)
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

	used = used_before + size
	# Advance the durable counter while STILL holding the row lock, so a concurrent
	# read for the same run cannot spend the same remaining budget. The budget is
	# charged the file's REAL size, before redaction — the guard is not a discount.
	frappe.db.set_value(RUN, run_name, "source_bytes_read", used, update_modified=False)

	# CX5-4: redact credential-shaped values BEFORE the text is fenced (and therefore
	# before it can reach the model provider). Committed secrets are a real pattern in
	# customer apps, and everything the delegate reads leaves the bench.
	text, redactions = source_guard.redact_secrets(text)

	# Source is DATA: fence it so a crafted comment/docstring/string cannot break
	# out and be misread as an instruction to the delegate.
	from jarvis.chat.turn_handler import _fence_untrusted

	out = {
		"app": app,
		"path": path,
		"size": size,
		"bytes_used": used,
		"bytes_budget": ctx.PER_RUN_SOURCE_BYTES_BUDGET,
		"redactions": redactions,
		"content": _fence_untrusted(text, f"{app} source file: {path}"),
	}
	if redactions:
		# Disclose per file, so the delegate can note the gap instead of inventing
		# meaning for a [REDACTED-SECRET] placeholder.
		out["note"] = (
			f"{redactions} credential-shaped value(s) in this file were redacted before "
			"it was served. Do not speculate about the redacted values."
		)
	return out
