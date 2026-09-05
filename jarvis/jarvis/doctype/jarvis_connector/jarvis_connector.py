"""Jarvis Connector - a configured MCP gateway a tenant's agent may call through
the bench broker (``jarvis/connectors/*``, owned by a parallel change - not this
file). ``preset`` names one of the providers in ``jarvis.connectors.catalog``,
or ``Custom URL`` for a caller-supplied address.

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
		self._pin_preset_base_url()
		self._validate_base_url()
		self._guard_shared_scope()
		self._guard_oauth_fields()
		self._enforce_uniqueness()

	def _pin_preset_base_url(self) -> None:
		"""A catalog preset's endpoint is pinned server-side on EVERY write path.
		The connectors API already ignores a caller's base_url for a preset, but a
		raw DocType write (a Jarvis User has create/write here) could otherwise aim
		a preset row at any public host and have the user's credential or token
		sent there. Custom URL is the only preset whose base_url is caller-chosen.
		Disabled catalog entries still resolve so existing rows keep saving."""
		from jarvis.connectors import catalog

		if self.preset and self.preset != catalog.CUSTOM_URL:
			pinned = catalog.base_urls().get(self.preset)
			if pinned:
				self.base_url = pinned

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

	def _guard_oauth_fields(self) -> None:
		"""Two OAuth engines back this DocType, a row may belong to exactly one of
		them, and the in-app provider catalog decides which. ``connected_app`` is
		allowed ONLY on a ``connected_app``-class preset (Frappe's Connected App,
		the shipped GitHub tier); ``mcp_oauth_client`` is allowed ONLY on a Custom
		URL row or a ``dcr``/``static`` preset (the discovery-driven engine). A
		``token``/``open`` preset has no sign-in at all, so ``auth_method="OAuth"``
		on one is refused outright, as it is in ``connectors_api.add_connector`` -
		this is the defense-in-depth copy of that rule. A key row carries neither
		link.

		A Jarvis User has raw write/create on this DocType, so without this guard
		they could aim either link wherever they liked, or route a key-only app
		into the sign-in engine - the API's server-side pinning lives only in
		``connectors_api``. Server writes under ``ignore_permissions`` (the API
		already resolved the link itself) skip the check.

		The pin is enforced ONLY when a link (or ``auth_method``) is being set or
		changed - never on an unchanged resave. Re-deriving every save would 403 a
		legitimately created row the moment the preset resolves elsewhere (e.g. a
		second Connected App with the same ``provider_name`` later wins the
		``get_all(limit=1)`` lookup), locking the row against even a disable or
		relabel. Steering still cannot slip through: aiming a row at another app
		requires setting or changing the field, which this catches.

		WHY A NEW DISCOVERY-ENGINE ROW MAY CARRY NEITHER LINK: an ``MCP OAuth
		Client`` links back to its connector, so it cannot exist until the connector
		does. ``add_connector`` therefore inserts the row first and writes the link
		immediately after. That window is safe because a client naming THIS
		connector cannot exist yet, so any ``mcp_oauth_client`` present on a new row
		is by definition foreign - which the ownership check below rejects.

		``catalog`` is frappe-free and safe to import lazily here; the Connected App
		resolver is imported lazily too, to avoid a load-time cycle between this
		controller and ``connectors_api``."""
		from jarvis.connectors import catalog

		if self.flags.ignore_permissions:
			return
		if (self.auth_method or "") != "OAuth":
			# A non-OAuth (key) connector must never carry either engine's link.
			self.connected_app = None
			self.mcp_oauth_client = None
			return
		if not (
			self.is_new()
			or self.has_value_changed("connected_app")
			or self.has_value_changed("mcp_oauth_client")
			or self.has_value_changed("auth_method")
		):
			return
		if self.connected_app and self.mcp_oauth_client:
			frappe.throw(_("This app cannot use two sign-in methods at once."), frappe.PermissionError)
		preset = self.preset or ""
		auth_class = catalog.auth_of(preset)
		if preset == catalog.CUSTOM_URL or auth_class in (catalog.AUTH_DCR, catalog.AUTH_STATIC):
			self._guard_discovery_oauth()
		elif auth_class == catalog.AUTH_CONNECTED_APP:
			self._guard_preset_oauth()
		else:
			# A key-only or no-credential app, or a preset the catalog does not carry
			# at all. Neither has a sign-in, so neither may claim one.
			frappe.throw(_("This app connects with a key, not a sign-in."), frappe.PermissionError)

	def _guard_discovery_oauth(self) -> None:
		"""Discovery engine only (a Custom URL row, or a ``dcr``/``static`` catalog
		preset): never the Connected App link. The client must be the one created
		FOR this connector - checked by reading the client's own ``connector`` field
		rather than trusting the link's direction, so a user cannot borrow another
		connector's client (and with it another tenant's discovered endpoints)."""
		if self.connected_app:
			frappe.throw(_("This app is not set up for sign-in."), frappe.PermissionError)
		if not self.mcp_oauth_client:
			return
		owner_connector = frappe.db.get_value("MCP OAuth Client", self.mcp_oauth_client, "connector")
		if owner_connector != self.name:
			frappe.throw(_("This app is not set up for sign-in."), frappe.PermissionError)

	def _guard_preset_oauth(self) -> None:
		"""A ``connected_app``-class preset + OAuth: the Connected App path only,
		pinned to the app the preset resolves to server-side."""
		from jarvis.chat.connectors_api import _resolve_connected_app_for_preset

		if self.mcp_oauth_client:
			frappe.throw(_("This app is not set up for sign-in."), frappe.PermissionError)
		expected = _resolve_connected_app_for_preset(self.preset)
		if not self.connected_app or self.connected_app != expected:
			frappe.throw(_("This app is not set up for sign-in."), frappe.PermissionError)

	def on_trash(self) -> None:
		"""Drop this connector's sign-in internals with it. Both DocTypes link BACK
		to this row, so without this Frappe's link check refuses the delete
		outright; and letting either survive would orphan live tokens for a
		connector that no longer exists."""
		from jarvis.connectors import mcp_oauth_store

		mcp_oauth_store.purge_connector(self.name)

	def _enforce_uniqueness(self) -> None:
		filters: dict = {"key": self.key, "scope": self.scope, "name": ("!=", self.name or "")}
		if self.scope == "Personal":
			filters["owner"] = self.owner or frappe.session.user
		if frappe.db.exists("Jarvis Connector", filters):
			if self.scope == "Personal":
				frappe.throw(_('You already have a Personal connector with key "{0}".').format(self.key))
			frappe.throw(_('A Shared connector with key "{0}" already exists.').format(self.key))
