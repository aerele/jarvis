"""Jarvis Connector - a configured MCP gateway (GitHub / Atlassian / Linear /
Stripe / Custom URL) a tenant's agent may call through the bench broker
(``jarvis/connectors/*``, owned by a parallel change - not this file).

Two scopes, resolved personal-wins-over-shared by the broker:

  * Personal - owned by one user, invisible to everyone else. Any Jarvis User
    may create their own.
  * Shared   - one connector for the whole tenant, credential set by an admin,
    readable (not writable) by every tenant user. Creating or widening INTO
    Shared requires the admin tier (System Manager / Jarvis Admin) - see
    ``_guard_shared_scope`` below. Row-level read/write scoping itself lives in
    ``jarvis/chat/connector_permissions.py`` (mirrors
    ``jarvis/chat/dashboard_permissions.py``); this controller only guards the
    scope-widening edge that a doc-less "create" has_permission call cannot see.

Uniqueness (checked here, not at the DB level, since it spans two fields plus
an owner condition MySQL unique keys can't express directly):
  * (scope=Personal, owner, key) - one Personal connector per key per user.
  * (scope=Shared, key)          - one Shared connector per key, tenant-wide.

The credential is a Password field; it is never read directly here and must
only be read server-side via ``doc.get_password("credential", raise_exception=False)``.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.model.document import Document

from jarvis.permissions import has_jarvis_admin_access

MAX_KEY = 64
MAX_LABEL = 140
MAX_BASE_URL = 500

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_ALLOWED_URL_SCHEMES = ("http", "https")


class JarvisConnector(Document):
	def validate(self) -> None:
		self._normalize_key()
		self._normalize_label()
		self._validate_base_url()
		self._guard_shared_scope()
		self._enforce_uniqueness()

	def _normalize_key(self) -> None:
		key = (self.key or "").strip().lower()[:MAX_KEY]
		if not key or not _SLUG_RE.match(key):
			frappe.throw(
				_(
					"Connector Key must be lowercase letters, digits, hyphens or "
					'underscores, starting with a letter or digit (e.g. "github").'
				)
			)
		self.key = key

	def _normalize_label(self) -> None:
		label = (self.label or "").strip()[:MAX_LABEL]
		if not label:
			frappe.throw(_("Connector Label is required."))
		self.label = label

	def _validate_base_url(self) -> None:
		base_url = (self.base_url or "").strip()[:MAX_BASE_URL]
		parsed = urlparse(base_url)
		if parsed.scheme not in _ALLOWED_URL_SCHEMES or not parsed.netloc:
			frappe.throw(_("Base URL must be a valid http:// or https:// address."))
		self.base_url = base_url

	def _guard_shared_scope(self) -> None:
		"""Creating a NEW Shared connector, or widening an existing Personal one
		to Shared, requires the admin tier. ``has_permission`` cannot see this on
		"create" (it is called doc-less), so the gate has to live here - the same
		reason ``Jarvis Dashboard`` puts its scope-widening gate in its
		controller rather than in ``dashboard_permissions.py``. Server-side
		writes done under ``ignore_permissions`` (broker/sync code acting as
		Administrator) skip this - they are not a user-initiated widen."""
		if self.flags.ignore_permissions:
			return
		if self.scope != "Shared":
			return
		user = frappe.session.user
		if has_jarvis_admin_access(user):
			return
		if self.is_new():
			frappe.throw(
				_("Only a System Manager or Jarvis Admin may create a Shared connector."),
				frappe.PermissionError,
			)
		previous_scope = frappe.db.get_value("Jarvis Connector", self.name, "scope")
		if previous_scope != "Shared":
			frappe.throw(
				_("Only a System Manager or Jarvis Admin may share a connector tenant-wide."),
				frappe.PermissionError,
			)

	def _enforce_uniqueness(self) -> None:
		filters: dict = {"key": self.key, "scope": self.scope, "name": ("!=", self.name or "")}
		if self.scope == "Personal":
			filters["owner"] = self.owner or frappe.session.user
		if frappe.db.exists("Jarvis Connector", filters):
			if self.scope == "Personal":
				frappe.throw(_('You already have a Personal connector with key "{0}".').format(self.key))
			frappe.throw(_('A Shared connector with key "{0}" already exists.').format(self.key))
