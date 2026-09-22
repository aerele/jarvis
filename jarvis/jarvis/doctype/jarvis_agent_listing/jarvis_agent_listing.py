"""Jarvis Agent Listing DocType controller.

One row per marketplace agent, synced from the BUNDLED
``jarvis/agents/registry.json`` by ``jarvis.chat.agent_catalog.sync_agent_listings``
(never fetched at runtime — bundles are reviewed deploy artifacts, S2).

A read-only catalog to customers (``All`` role has read only; ``System Manager``
writes via the sync). The controller is intentionally thin: all upsert logic
lives in the sync so a re-sync is idempotent.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class JarvisAgentListing(Document):
	def validate(self):
		self._guard_operator_visibility_authority()

	def _guard_operator_visibility_authority(self):
		"""``operator_visibility`` is OPERATOR-GLOBAL state: only a System Manager may
		change it (the ``set_operator_visibility`` endpoint runs as SM; the catalog sync
		runs as Administrator). A tenant ``Jarvis Admin`` has generic write on this doctype
		(needed for ``set_agent_access``), so WITHOUT this guard they could flip visibility
		via Desk / ``frappe.client.set_value`` / REST and override the operator's catalogue
		decision — un-masking a teaser or un-hiding a hidden agent tenant-wide. Guard the
		FIELD CHANGE only, so legitimate Jarvis-Admin edits (allowed_roles) still save."""
		if self.is_new():
			return  # a fresh listing is born from the sync (Administrator); defaults to 'available'
		before = self.get_doc_before_save()
		if before is None:
			return
		if (self.operator_visibility or "available") == (before.operator_visibility or "available"):
			return
		if "System Manager" not in frappe.get_roles(frappe.session.user):
			frappe.throw(
				_("Only a System Manager can change an agent's catalogue visibility."),
				frappe.PermissionError,
			)
