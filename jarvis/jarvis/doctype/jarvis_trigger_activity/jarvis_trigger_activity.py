"""Jarvis Trigger Activity DocType controller.

One append-only row per trigger firing (Success / Failed / Blocked / Skipped; intake rows also Queued / Running / Needs Review),
written server-side by ``jarvis.triggers.engine._insert_activity``. The
``trigger`` reference is a Data SNAPSHOT — deliberately NOT a Link — so
deleting a trigger never blocks on LinkExistsError and the log outlives the
trigger it narrates. Doctype perms are System Manager read+delete, plus role All read
``if_owner`` narrowed by ``jarvis.triggers.activity_permissions`` to the
user's own ``Intake`` rows. Every other user sees visibility-filtered rows via
``jarvis.chat.triggers_api.list_activity_page`` (read access on the target
doc is the axis). Reaped after 90 days via hooks'
``default_log_clearing_doctypes`` (mirrors core's WebhookRequestLog).
"""

import frappe
from frappe.model.document import Document

from jarvis.permissions import NotRenamable

# Intake rows still moving through (or waiting on a human) stay past the reap age.
OPEN_STATUSES = ("Queued", "Running", "Needs Review")


class JarvisTriggerActivity(NotRenamable, Document):
	def onload(self):
		"""Tell the desk form whether to show Retry: the same rule File Box uses, so the two
		never disagree (duplicates, permanent skips and rows with a live draft hide it)."""
		if self.action_type != "Intake":
			return
		from jarvis.chat import filebox
		from jarvis.chat.triggers_api import _can_manage

		row = self.as_dict()
		me = frappe.session.user
		self.set_onload(
			"can_retry_intake", filebox.can_retry_row(row, me, _can_manage(me), filebox.live_drafts([row]))
		)

	@staticmethod
	def clear_old_logs(days=90):
		from frappe.query_builder import Interval
		from frappe.query_builder.functions import Now

		table = frappe.qb.DocType("Jarvis Trigger Activity")
		# Open intake jobs are live state, not history: never reap them by age.
		frappe.db.delete(
			table,
			filters=(table.creation < (Now() - Interval(days=days))) & table.status.notin(OPEN_STATUSES),
		)
