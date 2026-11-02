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

from jarvis.permissions import NotRenamable


class JarvisAgentListing(NotRenamable, Document):
	def validate(self):
		# ``name`` is immutable and is therefore the authority even if a crafted
		# save tries to spoof agent_slug before Frappe restores it.
		from jarvis.chat.agent_closure import assert_listing_status

		assert_listing_status(self.name or self.agent_slug, self.status)
		self._guard_registry_runtime_status()
		self._guard_operator_visibility_authority()

	def _guard_registry_runtime_status(self):
		"""Keep a reviewed no-runtime teaser at its fixed marketplace status.

		The listing is tenant-mutable, but the bundle metadata is reviewed deploy
		code. A Desk, REST, or API write must not publish a bundle that declares no
		runtime surface.
		"""
		from jarvis.chat.agent_catalog import registry_is_coming_soon_no_runtime

		if registry_is_coming_soon_no_runtime(self.agent_slug or self.name) and self.status != "Coming Soon":
			frappe.throw(
				_("This agent is a Coming Soon teaser and cannot be published."),
				frappe.PermissionError,
			)

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
		# A slug the operator governs is fleet-owned: read-only to the tenant on EVERY write
		# surface, even a System Manager. The control-plane pull is the only authoritative
		# writer (it writes via db.set_value, bypassing validate()), so no tenant-side edit —
		# and hence no "flip governed->available then install in the window" race — is possible.
		from jarvis import catalogue_visibility

		if catalogue_visibility.is_operator_governed(self.name):
			frappe.throw(
				_("This agent's catalogue visibility is set by the operator and can't be changed here."),
				frappe.PermissionError,
			)
		if "System Manager" not in frappe.get_roles(frappe.session.user):
			frappe.throw(
				_("Only a System Manager can change an agent's catalogue visibility."),
				frappe.PermissionError,
			)
