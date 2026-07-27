"""``jarvis__finish_app_learning_run`` — the Custom App Learning scribe's TERMINAL
run finalizer (audited, NOT gated).

A scribe writes wiki pages through ``jarvis__record_app_wiki`` (which has no
findings shape), so nothing in that path can flip ``Jarvis Agent Run.status`` off
``running`` — without this the run would sit ``running`` until the 3-hour stale
reaper marked a SUCCESSFUL run ``failed``. This tool is the scribe's analogue of
the auditor's ``record_agent_run`` finalization: the delegate calls it ONCE at the
end of its turn to reach TERMINAL SUCCESS (``completed``) on its own, carrying the
pages-written tally ``record_app_wiki`` accumulated on the run — never findings.

Like ``record_agent_run`` it resolves the run from the caller's session_key (never
a model id) and self-gates to a running app-learning **scribe** run of an
admin-tier identity. It is in ``_WRITE_TOOLS`` but EXCLUDED from ``_GATED_WRITES``
(a detached delegate has nobody to click a card).
"""

from __future__ import annotations

import frappe

from jarvis.tools import _app_learning_ctx as ctx
from jarvis.tools.record_app_wiki import RUN


def finish_app_learning_run() -> dict:
	"""Finalize the bound scribe run to ``completed`` with its pages-written tally.

	Takes no model argument: the run is resolved from the session_key and the
	tally (``pages_written`` + the written-page slugs) is read from what
	``record_app_wiki`` already stamped on the run — the count is server-authored,
	not model-trusted. Returns ``{run, status, pages_written}``.
	"""
	# allow_terminal: this is the IDEMPOTENT fast-path. Completion is now
	# SERVER-owned (a still-running scribe run that wrote pages is reconciled to
	# ``completed`` by the stale-run sweep even if the model never calls finish),
	# so a late/duplicate finish must report the terminal state, not error.
	run = ctx.resolve_scribe_run(allow_terminal=True)  # self-gate: scribe run + admin-tier or raise
	if run.get("status") != "running":
		row = frappe.db.get_value(RUN, run["name"], ["status", "pages_written"], as_dict=True) or {}
		return {
			"run": run["name"],
			"status": row.get("status") or "completed",
			"pages_written": int(row.get("pages_written") or 0),
		}
	row = frappe.db.get_value(RUN, run["name"], ["pages_written", "coverage_note"], as_dict=True) or {}
	pages_written = int(row.get("pages_written") or 0)
	values = {"status": "completed", "finished_at": frappe.utils.now()}
	# A completed scribe run that wrote nothing (e.g. no learnable custom app was
	# installed) is an honest success, not a failure — only stamp a coverage_note
	# already set by a truncated writeback; never invent a "0 findings" shape.
	frappe.db.set_value(RUN, run["name"], values, update_modified=False)
	# Zero-trace (A8): the per-run session bearer must not outlive the run — mirror
	# record_agent_run's teardown so the terminal reply cannot make a further call.
	from jarvis.chat.agent_runs import teardown_run_session

	teardown_run_session(run["session_key"])
	if not frappe.flags.in_test:
		frappe.db.commit()
	return {"run": run["name"], "status": "completed", "pages_written": pages_written}
