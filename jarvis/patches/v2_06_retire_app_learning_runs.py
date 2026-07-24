"""Retire non-terminal legacy ``Jarvis App Learning Run`` rows.

The chat-batch custom-app learning pipeline has been REPLACED by the Custom App
Learning *scribe* delegate: its ``*/10`` scheduler tick is disabled and the
public ``schedule_app_learning`` endpoint now REFUSES, so no tick will ever run
again. A run that was Queued (or mid-flight: Zipping / Analyzing / Ingesting)
before this upgrade would otherwise sit non-terminal FOREVER — nothing advances
it, and the frontend surfaces that could cancel it were removed, so it is
undiscoverable.

Terminalize every such row to ``Cancelled`` with an explicit retirement reason.
Cancelled (not Failed) is the honest state: nothing failed — the pipeline was
retired out from under the run. Any partial wiki/skill writes an Ingesting row
already landed are left as-is (harmless, and un-scribed pages are simply legacy
content the scribe may later refresh). Historical terminal rows (Completed /
Failed / Cancelled) are untouched.

Idempotent: after one run no non-terminal row remains, so a re-run is a no-op.
"""

import frappe

_REASON = "retired: chat-batch app-learning pipeline replaced by the Custom App Learning agent"


def execute():
	frappe.db.sql(
		"""
		UPDATE `tabJarvis App Learning Run`
		SET status = 'Cancelled',
		    finished_at = COALESCE(finished_at, %(now)s),
		    error = COALESCE(NULLIF(error, ''), %(reason)s)
		WHERE status IN ('Queued', 'Zipping', 'Analyzing', 'Ingesting')
		""",
		{"now": frappe.utils.now(), "reason": _REASON},
	)
