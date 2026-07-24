"""Jarvis Dashboard DocType controller.

A dashboard is one self-contained HTML document (``html``) plus zero or more
declared data sources (``sources`` child rows). All user-facing validation
lives here so it runs on every insert/save whether the write came from the SPA
API (``jarvis.chat.dashboards_api``), the Desk, chat's doc tools, or a test —
mirroring ``Jarvis Trigger``.

Enforced here (NOT in the has_permission hook — "create" reaches the hook
without a doc, so a scope gate there would be bypassable on insert):

  * scope normalization + target field consistency (User -> target_user
    defaults to the owner; Role -> target_role required; Org -> both cleared).
  * SCOPE-WIDENING GATE: Org/Role scope needs the Jarvis Admin tier, and a
    Role scope must target a role the author may manage
    (``dashboard_permissions.manageable_roles``).
  * caps: html size, source count, per-source spec size.
  * per-source validation (name charset/uniqueness here; tool + spec shape via
    ``dashboards_api._validate_source_row``).
  * ``dashboard_type`` derivation: sources present -> Connected, else Static.

Read/write visibility is the "grant + deny-hook" shape used by Jarvis Trigger:
the doctype rows grant Jarvis User r/w/c/d broadly and
``jarvis.chat.dashboard_permissions`` denies at the ORM (non-owner writes,
out-of-scope reads).
"""

import re

import frappe
from frappe import _
from frappe.model.document import Document

from jarvis.permissions import has_jarvis_admin_access

MAX_TITLE_LEN = 140
MAX_HTML_CHARS = 1_000_000
MAX_SOURCES = 12
MAX_SPEC_CHARS = 32_000

_SCOPES = ("Org", "Role", "User")
# "Custom" is the bespoke escape hatch: the save-time theme lint relaxes its
# design rules (palette / font / light-dark) but keeps the safety bans.
_THEMES = ("Jarvis", "Insight", "Claude", "Graphite", "Custom")
_SOURCE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class ThemeStandardError(frappe.ValidationError):
	"""A save rejected because the canvas HTML does not follow the selected
	theme. A distinct type so the builder can offer 'Ask the assistant to fix
	these' (surfaced as ``exc_type`` to the SPA) rather than a dead-end error."""


class JarvisDashboard(Document):
	def validate(self):
		self._validate_title()
		self._validate_theme()
		self._validate_scope()
		self._validate_caps()
		self._validate_sources()
		self._validate_theme_standard()
		# Derived, never author-set: the html contract is "declared sources ->
		# live data at view time", so the type simply reflects the sources table.
		self.dashboard_type = "Connected" if (self.sources or []) else "Static"

	# ------------------------------------------------------------------ #
	# validate
	# ------------------------------------------------------------------ #
	def _validate_title(self):
		self.dashboard_title = (self.dashboard_title or "").strip()
		if not self.dashboard_title:
			frappe.throw(_("Dashboard title is required."))
		if len(self.dashboard_title) > MAX_TITLE_LEN:
			frappe.throw(_("Dashboard title must be at most {0} characters.").format(MAX_TITLE_LEN))

	def _validate_theme(self):
		self.theme = (self.theme or "").strip() or "Jarvis"
		if self.theme not in _THEMES:
			frappe.throw(_("Theme must be one of {0}.").format(", ".join(_THEMES)))

	def _validate_theme_standard(self):
		"""Deterministic theme-standard lint on the canvas HTML (colors/fonts/
		light-dark bans + the always-on safety bans). Runs when the html OR the
		theme changed (or on insert), so switching a saved dashboard's theme
		re-lints it against the NEW palette — a doc that conformed under one
		theme's literals must re-conform (via tokens or a re-gen) under the next
		(F7 / the owner's exact complaint). A pure metadata edit that touches
		NEITHER html nor theme (rename, re-scope) never re-validates the body —
		the grandfather rule for legacy (unstamped) docs. On pass, stamp the
		schema version; docs saved before this landed carry 0/None and are legacy
		(never retro-enforced on read). No auto-repair: a rejected save surfaces
		the reasons and the builder's chat loop regenerates."""
		if not (self.is_new() or self.has_value_changed("html") or self.has_value_changed("theme")):
			return
		from jarvis.dashboards.theme_validator import (
			THEME_SCHEMA_VERSION,
			format_violations,
			validate_dashboard_html,
		)

		violations = validate_dashboard_html(self.html or "", self.theme or "Jarvis")
		if violations:
			frappe.throw(
				_(
					"This dashboard's HTML does not follow the selected theme "
					"('{0}'). Fix these, then regenerate:\n{1}"
				).format(self.theme or "Jarvis", format_violations(violations)),
				ThemeStandardError,
				title=_("Dashboard does not match its theme"),
			)
		self.theme_schema_version = THEME_SCHEMA_VERSION

	def _validate_scope(self):
		"""Scope normalization + the SCOPE-WIDENING GATE.

		The gate must live here, not in the has_permission hook: "create" has
		no doc when the hook runs, so a hook-side gate could never see the
		scope being created. Runs as the SESSION user — exactly the actor
		performing the write."""
		self.scope = (self.scope or "").strip() or "User"
		if self.scope not in _SCOPES:
			frappe.throw(_("Scope must be one of Org, Role or User."))
		if self.scope == "User":
			self.target_role = None
			# Pin to the owner — NEVER honor a client-supplied target_user. The SPA
			# never sends it, but a direct REST/Desk write could otherwise set
			# target_user to an arbitrary victim, pushing this dashboard into that
			# user's visible list (visible_scope_condition matches target_user).
			self.target_user = self.owner or frappe.session.user
		elif self.scope == "Role":
			self.target_user = None
			if not self.target_role:
				frappe.throw(_("Target role is required for a Role-scoped dashboard."))
		else:  # Org
			self.target_role = None
			self.target_user = None

		if self.scope in ("Org", "Role") and not has_jarvis_admin_access():
			frappe.throw(
				_(
					"You need the Jarvis Admin or System Manager role to share a "
					"dashboard org-wide or with a role."
				),
				frappe.PermissionError,
			)
		if self.scope == "Role":
			from jarvis.chat.dashboard_permissions import manageable_roles

			if self.target_role not in manageable_roles():
				frappe.throw(
					_("You cannot target the role '{0}' with a dashboard.").format(
						self.target_role
					)
				)

	def _validate_caps(self):
		if len(self.html or "") > MAX_HTML_CHARS:
			frappe.throw(
				_("Dashboard HTML must be at most {0} characters.").format(MAX_HTML_CHARS)
			)
		if len(self.sources or []) > MAX_SOURCES:
			frappe.throw(
				_("A dashboard can declare at most {0} data sources.").format(MAX_SOURCES)
			)

	def _validate_sources(self):
		"""Name charset + uniqueness here; tool/spec shape per row via the API
		module's ``_validate_source_row`` (single source of truth shared with
		``save_dashboard``). Lazy import — the API module imports nothing from
		this controller, but keeping the import call-time avoids any future
		module-load cycle."""
		from jarvis.chat.dashboards_api import _validate_source_row

		seen: set = set()
		for row in self.sources or []:
			row.source_name = (row.source_name or "").strip()
			if not _SOURCE_NAME_RE.match(row.source_name):
				frappe.throw(
					_(
						"Invalid source name '{0}': use 1-64 letters, digits, "
						"underscores or hyphens."
					).format(row.source_name)
				)
			if row.source_name in seen:
				frappe.throw(_("Duplicate source name: {0}").format(row.source_name))
			seen.add(row.source_name)
			if len(row.spec or "") > MAX_SPEC_CHARS:
				frappe.throw(
					_("Source '{0}': spec must be at most {1} characters.").format(
						row.source_name, MAX_SPEC_CHARS
					)
				)
			_validate_source_row(
				{"source_name": row.source_name, "tool": row.tool, "spec": row.spec}
			)
