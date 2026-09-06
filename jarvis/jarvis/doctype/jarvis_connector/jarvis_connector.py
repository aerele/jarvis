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
		self._guard_custom_url_policy()
		self._guard_shared_scope()
		self._guard_oauth_fields()
		self._enforce_uniqueness()

	def _guard_custom_url_policy(self) -> None:
		"""A Custom URL row may only be created, or re-pointed, by a plain user while
		the workspace policy allows custom addresses. The API already refuses; this
		closes the raw DocType write around it, which matters because a later sign-in
		self-heal discovers whatever address the row carries. The admin tier and
		server-side writes are exempt, as for the Shared-scope guard."""
		from jarvis.connectors import catalog

		if self.flags.ignore_permissions or self.preset != catalog.CUSTOM_URL:
			return
		if not (self.is_new() or self.has_value_changed("base_url")):
			return
		if has_jarvis_admin_access(frappe.session.user):
			return
		from jarvis.chat.connectors_api import connector_flags

		if not connector_flags()["allow_custom_urls"]:
			frappe.throw(
				_("Custom URL connectors are turned off. Ask an administrator to enable them."),
				frappe.PermissionError,
			)

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
		"""One sign-in engine backs this DocType, and the in-app provider catalog
		decides whether a preset may use it. ``mcp_oauth_client`` is allowed ONLY on
		a Custom URL row or a ``dcr``/``static`` preset. A ``token``/``open`` preset
		has no sign-in at all, so ``auth_method="OAuth"`` on one is refused outright,
		as it is in ``connectors_api.add_connector`` - this is the defense-in-depth
		copy of that rule. A key row carries no link.

		A Jarvis User has raw write/create on this DocType, so without this guard
		they could aim the link wherever they liked, or route a key-only app into the
		sign-in engine - the API's server-side pinning lives only in
		``connectors_api``. Server writes under ``ignore_permissions`` (the API
		already resolved the link itself) skip the check.

		The pin is enforced ONLY when the link (or ``auth_method``, or ``preset``) is
		being set or changed - never on an unchanged resave, so a legitimately
		created row is never locked against a later disable or relabel. Steering
		still cannot slip through: aiming a row at another app requires setting or
		changing the field, which this catches.

		WHY A NEW OAUTH ROW MAY CARRY NO LINK: an ``MCP OAuth Client`` links back to
		its connector, so it cannot exist until the connector does. ``add_connector``
		therefore inserts the row first and writes the link immediately after. That
		window is safe because a client naming THIS connector cannot exist yet, so
		any ``mcp_oauth_client`` present on a new row is by definition foreign - which
		the ownership check below rejects.

		``catalog`` is frappe-free and safe to import lazily here."""
		from jarvis.connectors import catalog

		if self.flags.ignore_permissions:
			return
		if (self.auth_method or "") != "OAuth":
			# A non-OAuth (key) connector must never carry the engine's link.
			self.mcp_oauth_client = None
			return
		if not (
			self.is_new()
			or self.has_value_changed("mcp_oauth_client")
			or self.has_value_changed("auth_method")
			# A preset change re-decides WHICH app this row is for, so it has to be
			# re-checked even when the link sits still: without it a raw write could
			# move a row onto another preset while keeping the first one's client (and
			# its registered credentials).
			or self.has_value_changed("preset")
		):
			return
		preset = self.preset or ""
		auth_class = catalog.auth_of(preset)
		if preset == catalog.CUSTOM_URL or auth_class in (catalog.AUTH_DCR, catalog.AUTH_STATIC):
			self._guard_discovery_oauth()
		else:
			# A key-only or no-credential app, or a preset the catalog does not carry
			# at all. Neither has a sign-in, so neither may claim one.
			frappe.throw(_("This app connects with a key, not a sign-in."), frappe.PermissionError)

	def _guard_discovery_oauth(self) -> None:
		"""Sign-in engine only (a Custom URL row, or a ``dcr``/``static`` catalog
		preset). The client must be the one created FOR this connector - checked by
		reading the client's own ``connector`` field rather than trusting the link's
		direction, so a user cannot borrow another connector's client (and with it
		another tenant's discovered endpoints).

		Changing the PRESET of a row that already has a client is refused outright.
		The ownership check below would pass (the client does name this connector),
		but the client holds endpoints and credentials registered with ONE app,
		and the preset change has already re-pinned this row at a different one -
		so the row would present another provider's registration, exactly the
		re-point ``update_connector`` refuses for a Custom URL address."""
		if not self.mcp_oauth_client:
			return
		if not self.is_new() and self.has_value_changed("preset"):
			frappe.throw(_("Add a new connector to use a different app."), frappe.PermissionError)
		owner_connector, client_resource = frappe.db.get_value(
			"MCP OAuth Client", self.mcp_oauth_client, ["connector", "resource"]
		) or (None, None)
		if owner_connector != self.name:
			frappe.throw(_("This app is not set up for sign-in."), frappe.PermissionError)
		# The client was discovered FOR one address. It must still be this row's
		# address, whatever route the row took to get here (a preset flip split across
		# two saves re-pins base_url on the first and re-attaches the client on the
		# second, with "preset" unchanged in that second save).
		from jarvis.connectors.mcp_oauth import canonical_resource

		try:
			same_address = canonical_resource(client_resource or "") == canonical_resource(
				self.base_url or ""
			)
		except ValueError:
			same_address = False
		if not same_address:
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
