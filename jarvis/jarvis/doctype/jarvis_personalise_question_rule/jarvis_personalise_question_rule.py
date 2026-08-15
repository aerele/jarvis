"""Jarvis Personalise Question Rule DocType controller.

Admin-configured question templates, managed only from the Personalisation
Settings page (``jarvis.chat.personalise_api``, admin-gated: System Manager |
Administrator | Jarvis Admin - DocType-level permission stays System-Manager-
only below, matching Jarvis Learned Pattern's precedent; the Jarvis Admin
role reaches this DocType through the whitelisted API's own code-level guard
+ ``ignore_permissions``, never a direct DocType permission grant, per
DESIGN.md section 7b).

Each active rule is swept (daily + on-save; ``jarvis.learning.questions``,
Wave B1) into one Jarvis Personalise Question per in-scope user that doesn't
already have one from this rule (dedupe key: rule + user), origin "From your
organisation". Scope resolution mirrors Jarvis Wiki Page's Org/Role/User
model exactly (see jarvis_wiki_page.py): Org = every desk user, Role =
holders of target_role, User = target_user only.
"""

import frappe
from frappe import _
from frappe.model.document import Document

SCOPES = ("Org", "Role", "User")
MAX_QUESTION_LEN = 500


class JarvisPersonaliseQuestionRule(Document):
	def validate(self):
		self._validate_question()
		self._validate_scope()

	def _validate_question(self):
		self.question = (self.question or "").strip()
		if not self.question:
			frappe.throw(_("Question text is required."))
		if len(self.question) > MAX_QUESTION_LEN:
			frappe.throw(_("Question must be at most {0} characters.").format(MAX_QUESTION_LEN))

	def _validate_scope(self):
		self.scope = (self.scope or "").strip() or "Org"
		if self.scope not in SCOPES:
			frappe.throw(_("Scope must be one of {0}.").format(", ".join(SCOPES)))
		# Drop the off-scope target field rather than merely ignoring it, so
		# a stale target_role/target_user from a prior scope can never leak
		# into the sweep's in-scope-user resolution (jarvis_wiki_page.py
		# _normalize_scope precedent).
		if self.scope == "Role":
			if not self.target_role:
				frappe.throw(_("Role-scope rules need a Target Role."))
			self.target_user = None
		elif self.scope == "User":
			if not self.target_user:
				frappe.throw(_("User-scope rules need a Target User."))
			self.target_role = None
		else:
			self.target_role = None
			self.target_user = None
