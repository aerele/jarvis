# Copyright (c) 2026, Aerele and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, now_datetime


class JarvisImportAnnouncement(Document):
	"""Tracks one staged Data Import so its completion can be announced back into
	the originating chat exactly once (Slice B). Backend-only: no role permissions,
	so it is never readable by a user (every import-runner is a System Manager - a
	read grant would be a cross-user side channel, PC-4). Admin visibility is the
	``import_announce_stats`` diagnostics endpoint only.

	Lifecycle: created ``announced=0`` at run_import confirm-time; the ``*/2`` poll
	(``jarvis.chat.import_announce``) and the ``after_job`` fast-path both classify
	it and, once the import is genuinely done/blocked/stuck/dead, post the completion
	message and flip ``announced=1`` (or abort + flip with a ``reason``). It never
	sits ``announced=0`` forever."""

	@staticmethod
	def clear_old_logs(days=30):
		"""Retention: reap only TERMINAL rows (``announced=1`` = the poll/hook is
		finished with them, whether a message was posted or the row was aborted).
		An ``announced=0`` row is still live work and must NEVER be swept - a bare
		age cutoff would delete un-announced imports and lose the completion. Called
		by core log-clearing via ``default_log_clearing_doctypes`` (hooks.py)."""
		cutoff = add_days(now_datetime(), -days)
		frappe.db.delete(
			"Jarvis Import Announcement",
			{"announced": 1, "modified": ["<", cutoff]},
		)
