"""Reconcile message fences for direct upgrades and mixed-version workers.

This neutral patch identity intentionally runs once on previously upgraded sites.
Reconciliation is idempotent, retains the larger fence in both columns, and also
runs after each normal migration. Existing Patch Log entries remain untouched.
"""


def execute():
	from jarvis.chat.seq_watermark import reconcile_watermarks

	reconcile_watermarks()
