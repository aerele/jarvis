"""Jarvis Wiki Promotion Request DocType controller.

One row per "promote my personal wiki page to Role/Org visibility" ask
(Skills-area rework part 3, DESIGN.md section 2.4). Created by
``jarvis.chat.wiki.request_wiki_promotion`` (Wave B1) when a desk user asks
to widen a User-scope Jarvis Wiki Page they own; decided by a reviewer
(``jarvis.chat.learned_api.decide_promotion``, Wave B1), who approves or
rejects. On approve, the reviewer's code merges ``body_snapshot`` into the
Role/Org target page (audience-suffix slug rules respected, per
jarvis_wiki_page.py) via ``ignore_permissions`` - the "All" role only ever
gets read/create here (no write), so a requester can never self-approve or
edit a decision after the fact.

Promotion only ever WIDENS visibility: ``to_scope`` is restricted to
Role/Org (never back down to User, never sideways). ``from_scope`` is a
point-in-time snapshot of the source page's scope, not independently
re-checked against the live page (the page could in principle be re-scoped
between request and decision - out of scope for v1; the reviewer sees the
frozen body_snapshot either way).
"""

import frappe
from frappe import _
from frappe.model.document import Document

FROM_SCOPES = ("Org", "Role", "User")
TO_SCOPES = ("Role", "Org")


class JarvisWikiPromotionRequest(Document):
	def before_insert(self):
		# Security review PART 2 TASK 14: this row's before_insert back-fills
		# body_snapshot from the source page with a permission-bypassing
		# db.get_value. Because the doctype used to grant `All: create`, a System
		# User could frappe.client.insert a request naming ANOTHER user's private
		# (User-scope) wiki page and read the victim's body_md back through the row
		# they own (if_owner). Assert the caller can actually READ the page BEFORE
		# snapshotting — closes the generic-REST disclosure at the controller so it
		# holds no matter which door the insert comes through (the bare `All:
		# create` grant is also dropped from the JSON, leaving the ownership-checked
		# request_wiki_promotion endpoint as the only creation path).
		self._guard_page_readable()
		# Freeze the diff basis at request time (DESIGN.md 2.4): if the caller
		# didn't already snapshot the body, pull it from the live page once,
		# here, so a later edit to the source page can never retroactively
		# change what the reviewer is diffing against.
		if not self.body_snapshot and self.page:
			self.body_snapshot = frappe.db.get_value("Jarvis Wiki Page", self.page, "body_md") or ""

	def _guard_page_readable(self):
		"""The requester must be able to READ the source page before it is
		snapshotted into a row they own. Runs for EVERY insert path (REST
		included); Administrator bypasses frappe.has_permission natively. The
		wiki's own has_permission hook resolves User-scope pages to their
		target_user, so this subsumes an explicit owner check."""
		if not self.page:
			return
		if not frappe.has_permission("Jarvis Wiki Page", "read", self.page):
			frappe.throw(
				_("You do not have access to this wiki page."),
				frappe.PermissionError,
			)

	def validate(self):
		self._validate_page()
		self._validate_scopes()

	def _validate_page(self):
		if not self.page:
			frappe.throw(_("A promotion request must reference a wiki page."))

	def _validate_scopes(self):
		self.from_scope = (self.from_scope or "").strip()
		if self.from_scope and self.from_scope not in FROM_SCOPES:
			frappe.throw(_("From Scope must be one of {0}.").format(", ".join(FROM_SCOPES)))
		self.to_scope = (self.to_scope or "").strip()
		if self.to_scope not in TO_SCOPES:
			frappe.throw(_("To Scope must be one of {0}.").format(", ".join(TO_SCOPES)))
		if self.to_scope == "Role" and not self.target_role:
			frappe.throw(_("Promoting to Role scope needs a Target Role."))
