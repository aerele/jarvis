"""Jarvis Personalise Question DocType controller.

One row per question Jarvis wants to ask a specific user (``user``) - the
Personalise-tab analogue of a Jarvis Learned Pattern row: instead of sitting
on the (admin-only) Review board, findings that name an identifiable person
materialize here, in that person's own question bank (DESIGN.md section 2.1;
Wave B1's ``jarvis.learning.questions`` router is the writer).

Soft-delete convention: ``delete_question`` (``jarvis.chat.personalise_api``,
Wave B1) NEVER hard-deletes a row - it flips ``status`` to "Deleted" (an
audit trail, and it suppresses the generators from ever re-minting the same
question). Every list/probe endpoint MUST exclude ``status="Deleted"`` rows
explicitly; this controller only allows the value to exist, it does not
filter it anywhere. The three user-facing states are Unanswered / Answered /
Ignored (DESIGN.md section 6); answering is legal from any of the three
(re-answering just replaces ``answer_note`` and flips back to Answered).

Owner-visibility: permissions use ``if_owner`` (the "All" role) so the
target user can read/write/delete their own questions, but Frappe's
if_owner matches ``doc.owner`` (the identity that performed the insert), not
an arbitrary field. Rows are routinely materialized by backend code running
as Administrator or the pattern-learning engine, not as the target user -
so ``before_insert`` stamps ``self.owner = self.user`` here, once, so
if_owner keeps working no matter which identity performs the insert.
Callers should NOT rely on ``frappe.set_user(target_user)`` around the
insert; this field stamp is the single source of truth.
"""

import frappe
from frappe import _
from frappe.model.document import Document

STATUSES = ("Unanswered", "Answered", "Ignored", "Deleted")
ORIGINS = (
	"Behavioural Learning",
	"From your organisation",
	"From your chat patterns",
	"From your reviewer",
)
MAX_QUESTION_LEN = 500


class JarvisPersonaliseQuestion(Document):
	def before_insert(self):
		# See module docstring: owner-visibility rides doc.owner, so it must
		# track the target user regardless of which identity performs the
		# insert (materializers commonly run as Administrator).
		if self.user:
			self.owner = self.user

	def validate(self):
		self._validate_user()
		self._validate_question()
		self._validate_status()
		self._validate_origin()

	def _validate_user(self):
		if not self.user:
			frappe.throw(_("A Personalise question must target a user."))

	def _validate_question(self):
		self.question = (self.question or "").strip()
		if not self.question:
			frappe.throw(_("Question text is required."))
		if len(self.question) > MAX_QUESTION_LEN:
			frappe.throw(_("Question must be at most {0} characters.").format(MAX_QUESTION_LEN))

	def _validate_status(self):
		# Self-heal a blank/None status to the default rather than relying on
		# a fresh doc always carrying the JSON default (programmatic inserts
		# via frappe.get_doc(dict) don't always apply field defaults).
		status = self.status or "Unanswered"
		if status not in STATUSES:
			frappe.throw(_("Unknown Personalise question status: {0}").format(status))
		self.status = status

	def _validate_origin(self):
		# origin is a reqd field (Frappe's mandatory check covers blank); this
		# is defense-in-depth against a value that bypasses the Select-option
		# check (e.g. a raw db write feeding a later ignore_permissions load).
		if self.origin and self.origin not in ORIGINS:
			frappe.throw(_("Unknown Personalise question origin: {0}").format(self.origin))
