"""SPA-facing CRUD + apply + run controls for the Agents Marketplace.

Mirrors ``jarvis.chat.custom_skills_api``: owner-scoped CRUD over ``Jarvis Agent
Installation`` rows, an explicit Apply that pushes the ENABLED installed bundles
to the container via a deduped redis-locked worker (admin -> fleet -> restart),
and read endpoints for the catalog / runs / findings.

Security (adversarial S3 — HARD REQ): every MUTATION resolves the row via
``frappe.get_doc`` + ``doc.check_permission(...)`` (owner-gate — ``get_doc``
alone does NOT enforce ``if_owner``), NEVER ``frappe.db.set_value`` by a
user-supplied bare name. A non-owner cannot mutate another owner's installation.
Enable / schedule are pure DB writes (no container restart — O6); only Apply
(install/uninstall/update reconcile) restarts the container.
"""

import frappe
from frappe import _

from jarvis._session import authenticated_user, impersonate
from jarvis.chat import coverage_reasons as cr
from jarvis.chat.agent_activity import log_activity
from jarvis.chat.agent_catalog import build_agent_push_payload
from jarvis.chat.filebox import _clamp_page, _lk
from jarvis.chat.macro_scheduler import compute_next_run
from jarvis.permissions import (
	has_jarvis_admin_access,
	require_jarvis_admin,
	require_jarvis_user,
)

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
FINDING = "Jarvis Agent Finding"
ACTIVITY = "Jarvis Agent Activity"
DASHBOARD = "Jarvis Dashboard"
PROVENANCE = "Jarvis Agent Provenance Event"
ALLOWED_ROLE = "Jarvis Agent Allowed Role"
_SETTINGS = "Jarvis Settings"

# PP-6 — the stage-maximum global activation ceiling. The initial ceiling is 1
# (every customer starts with a single live module); a Jarvis Admin may raise it
# to 2 (this maximum) with a recorded reviewer-capacity justification. No path to
# 3+ exists at this stage.
_ACTIVATION_CEILING_MAX = 2
_PUSH_JOB_ID = "jarvis_agent_skills_push"
_LOCK_NAME = "jarvis_agent_skills_push"

_FREQUENCIES = ("daily", "weekly", "monthly")
# Statuses a bench admin may set via set_listing_status (Draft is registry-only).
_ADMIN_STATUSES = ("Published", "Coming Soon", "Deprecated")
# Never meaningful as an agent restriction ("All" == unrestricted; the other two
# are identities, not grantable roles) and never offered in the admin picker.
_NON_SELECTABLE_ROLES = ("Administrator", "Guest", "All")


# --------------------------------------------------------------------------- #
# role-based access (RBAC)
# --------------------------------------------------------------------------- #
def _user_allowed_for_agent(listing, user: str | None = None) -> bool:
	"""True iff ``user`` may install / run the agent.

	Allowed iff the listing has NO ``allowed_roles`` rows (empty = unrestricted)
	OR the user's roles intersect them. System Manager is ALWAYS allowed.
	``listing`` may be a Jarvis Agent Listing doc or its name (agent_slug).
	Fail-closed on the restricted side: an unknown user has no roles beyond
	Guest/All, which never satisfy a restriction.
	"""
	user = user or frappe.session.user
	if isinstance(listing, str):
		allowed = frappe.get_all(
			ALLOWED_ROLE,
			filters={"parenttype": LISTING, "parent": listing},
			pluck="role",
		)
	else:
		allowed = [row.role for row in (listing.get("allowed_roles") or [])]
	if not allowed:
		return True
	# PART 4 REVISED, TASK 49: admin parity — a Jarvis Admin / System Manager is
	# ALWAYS allowed, in lockstep with the widened ``allowed`` display flag so an
	# admin never sees an agent as installable but 403s on install.
	if has_jarvis_admin_access(user):
		return True
	roles = set(frappe.get_roles(user))
	return bool(roles.intersection(allowed))


def _allowed_roles_map() -> dict[str, list[str]]:
	"""All listings' allowed_roles child rows in ONE query: {listing_name: [role, ...]}."""
	out: dict[str, list[str]] = {}
	for row in frappe.get_all(
		ALLOWED_ROLE,
		filters={"parenttype": LISTING, "parentfield": "allowed_roles"},
		fields=["parent", "role"],
		order_by="parent asc, idx asc",
	):
		out.setdefault(row.parent, []).append(row.role)
	return out


# --------------------------------------------------------------------------- #
# catalog + install state (read)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def list_agents() -> list[dict]:
	"""The full catalog plus THIS owner's install/enable/schedule state per
	agent. Read-only — the catalog is visible to every logged-in user. Each row
	also carries ``allowed_roles`` (empty = unrestricted) and ``allowed`` (0/1
	for the CURRENT user; System Manager is always 1) — display state only, the
	real gate is server-side in install_agent / run_agent_now / the scheduler."""
	return _enriched_catalog()


def _enriched_catalog() -> list[dict]:
	"""The shared per-row enrichment behind ``list_agents`` AND
	``list_agents_page`` — one implementation so the paginated SPA list can
	never drift from the legacy full list."""
	me = frappe.session.user
	roles_map = _allowed_roles_map()
	my_roles = set(frappe.get_roles(me))
	# PART 4 REVISED, TASK 49(d): admin display parity — a Jarvis Admin sees every
	# agent card as ``allowed`` (and _user_allowed_for_agent lets them install).
	is_sm = has_jarvis_admin_access(me)
	listings = frappe.get_all(
		LISTING,
		fields=[
			"name",
			"agent_slug",
			"title",
			"description",
			"category",
			"nature",
			"version",
			"publisher",
			"status",
			"rule_pack",
			"default_schedule",
			"validated_for_fy",
			"tools_required",
			"modified",
		],
		order_by="status asc, title asc",
	)
	installs = {
		i.agent: i
		for i in frappe.get_all(
			INSTALLATION,
			filters={"owner": me},
			fields=[
				"name",
				"agent",
				"enabled",
				"installed_version",
				"sync_status",
				"schedule_enabled",
				"schedule_frequency",
				"schedule_time",
				"next_run_at",
				"last_run_at",
			],
		)
	}
	# All owners' install counts in one grouped query (DESIGN-V3 §14 F5 —
	# additive; feeds the "Featured" strip + the "N installs" hero stat).
	install_counts = {
		r.agent: r.n
		for r in frappe.db.sql(
			"SELECT agent, COUNT(*) AS n FROM `tabJarvis Agent Installation` GROUP BY agent",
			as_dict=True,
		)
	}
	out = []
	for lst in listings:
		inst = installs.get(lst.name)
		lst["installed"] = 1 if inst else 0
		lst["installation"] = inst.name if inst else None
		lst["enabled"] = int(inst.enabled) if inst else 0
		lst["installed_version"] = inst.installed_version if inst else None
		lst["schedule_enabled"] = int(inst.schedule_enabled) if inst else 0
		lst["schedule_frequency"] = inst.schedule_frequency if inst else None
		lst["schedule_time"] = str(inst.schedule_time) if (inst and inst.schedule_time) else None
		lst["next_run_at"] = str(inst.next_run_at) if (inst and inst.next_run_at) else None
		lst["update_available"] = (
			1 if inst and inst.installed_version and inst.installed_version != lst.version else 0
		)
		allowed_roles = roles_map.get(lst.name, [])
		lst["allowed_roles"] = allowed_roles
		lst["allowed"] = 1 if (is_sm or not allowed_roles or my_roles.intersection(allowed_roles)) else 0
		lst["install_count"] = install_counts.get(lst.name, 0)
		out.append(lst)
	return out


@frappe.whitelist()
@require_jarvis_user
def list_agents_page(
	tab: str = "available",
	category: str | None = None,
	sort: str = "installs",
	search: str | None = None,
	start: int = 0,
	page_length: int = 20,
) -> dict:
	"""Paginated catalog for the SPA (envelope ``{rows, total, has_more, start,
	page_length}``). ADDITIVE — ``list_agents`` stays. Reuses the exact per-row
	enrichment of ``list_agents`` (``_enriched_catalog``) and filters/sorts/
	slices in Python: the catalog is a bundled registry of at most a few dozen
	rows, so the enriched-list-then-slice approach is both simplest and correct
	(``total``/``has_more`` are computed on the active tab's filtered set).

	Tabs (AgentsList.vue semantics): ``featured`` = Published only;
	``available`` = everything except Deprecated (a Deprecated listing shows
	only if the CALLER still has it installed); ``installed`` = the caller's
	installs, any status. Sort: ``installs`` (install_count desc, title asc —
	also the Featured strip's order), ``updated`` (modified desc), ``name``
	(title asc). Search is case-insensitive over
	title/description/category/agent_slug."""
	if tab not in ("featured", "available", "installed"):
		frappe.throw(_("Invalid tab."))
	start, pl = _clamp_page(start, page_length)
	rows = _enriched_catalog()

	if tab == "featured":
		rows = [r for r in rows if r.status == "Published"]
	elif tab == "installed":
		rows = [r for r in rows if r.installed]
	else:  # available
		rows = [r for r in rows if r.status != "Deprecated" or r.installed]

	if category:
		rows = [r for r in rows if (r.category or "") == category]

	q = (search or "").strip().lower()
	if q:
		rows = [
			r
			for r in rows
			if any(
				q in str(r.get(k) or "").lower() for k in ("title", "description", "category", "agent_slug")
			)
		]

	if sort == "updated":
		rows.sort(key=lambda r: str(r.get("modified") or ""), reverse=True)
	elif sort == "name":
		rows.sort(key=lambda r: (r.get("title") or "").lower())
	else:  # installs (default)
		rows.sort(key=lambda r: (-(r.get("install_count") or 0), (r.get("title") or "").lower()))

	total = len(rows)
	page = rows[start : start + pl]
	return {
		"rows": page,
		"total": total,
		"has_more": start + len(page) < total,
		"start": start,
		"page_length": pl,
	}


@frappe.whitelist()
@require_jarvis_user
def get_agent(agent_slug: str) -> dict:
	"""One listing + the CURRENT user's installation for the agent detail page
	(DESIGN-V3 §8.3 / D39). Any authenticated user may read (listing perms =
	All read); the ``installation`` block is the caller's own install or None.
	``all_roles`` rides along only for System Managers (Admin-tab roles editor)."""
	listing = frappe.get_doc(LISTING, agent_slug)  # All-role read; 404s if unknown
	me = frappe.session.user
	# PART 4 REVISED, TASK 49(d): the Admin-tab signal (all_roles rider) rides for
	# Jarvis Admins too — the SPA derives isSM from all_roles' presence.
	is_sm = has_jarvis_admin_access(me)

	out: dict = {
		"name": listing.name,
		"agent_slug": listing.agent_slug,
		"title": listing.title,
		"description": listing.description,
		"category": listing.category,
		"nature": listing.nature,
		"version": listing.version,
		"publisher": listing.publisher,
		"status": listing.status,
		"tools_required": listing.tools_required,
		"min_apps": listing.min_apps,
		"rule_pack": listing.rule_pack,
		# skill_bundle deliberately omitted (PART 3 TASK 33): it is proprietary
		# vendor IP (the full agent SKILL.md rule-pack) and is now permlevel-1
		# (SM-only). A normal Jarvis User's detail page never needs it; SM / the
		# engine read it via generic REST / get_doc.
		"default_schedule": listing.default_schedule,
		"validated_for_fy": listing.validated_for_fy,
		"allowed_roles": [row.role for row in (listing.allowed_roles or [])],
		"allowed": 1 if _user_allowed_for_agent(listing, me) else 0,
		"install_count": frappe.db.count(INSTALLATION, {"agent": listing.name}),
		"installation": None,
	}

	inst = frappe.get_all(
		INSTALLATION,
		filters={"owner": me, "agent": listing.name},
		fields=[
			"name",
			"enabled",
			"installed_version",
			"installed_at",
			"config",
			"sync_status",
			"synced_at",
			"schedule_enabled",
			"schedule_frequency",
			"schedule_time",
			"next_run_at",
			"last_run_at",
		],
		limit=1,
	)
	if inst:
		i = inst[0]
		i["enabled"] = int(i.enabled or 0)
		i["schedule_enabled"] = int(i.schedule_enabled or 0)
		i["schedule_time"] = str(i.schedule_time) if i.schedule_time else None
		i["next_run_at"] = str(i.next_run_at) if i.next_run_at else None
		i["last_run_at"] = str(i.last_run_at) if i.last_run_at else None
		out["installation"] = i

	if is_sm:
		out["all_roles"] = [
			r
			for r in frappe.get_all(
				"Role",
				filters={"disabled": 0, "desk_access": 1},
				order_by="name asc",
				pluck="name",
			)
			if r not in _NON_SELECTABLE_ROLES
		]
	return out


@frappe.whitelist()
@require_jarvis_user
def get_installations() -> list[dict]:
	"""This owner's installations, with the linked listing title/nature/status."""
	me = frappe.session.user
	rows = frappe.get_all(
		INSTALLATION,
		filters={"owner": me},
		fields=[
			"name",
			"agent",
			"enabled",
			# PP-4: the activation state + named reviewer + promotion stamp so the SPA
			# can surface a customer's SHADOW installations as a distinct set and wire the
			# promote/demote actions (the reviewer's "one clear action") to them.
			"activation_state",
			"reviewer",
			"promoted_by",
			"promoted_at",
			"installed_version",
			"installed_at",
			"config",
			"sync_status",
			"synced_at",
			"schedule_enabled",
			"schedule_frequency",
			"schedule_time",
			"next_run_at",
			"last_run_at",
		],
		order_by="modified desc",
	)
	for r in rows:
		meta = (
			frappe.db.get_value(LISTING, r.agent, ["title", "nature", "status", "version"], as_dict=True)
			or {}
		)
		r["title"] = meta.get("title")
		r["nature"] = meta.get("nature")
		r["listing_status"] = meta.get("status")
		r["latest_version"] = meta.get("version")
	return rows


# --------------------------------------------------------------------------- #
# admin surface (Jarvis Admin / System Manager — every check server-side)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def set_agent_roles(agent_slug: str, roles: str | list | None = None) -> dict:
	"""Restrict an agent listing to a set of Roles. Jarvis Admin / System Manager
	(PART 4 REVISED, TASK 45; needs the Jarvis Admin write:1 row on Jarvis Agent
	Listing — TASK 47 — for the perm-checked save).

	``roles`` is a JSON array of Role names; ``[]`` clears the restriction
	(unrestricted). Roles are validated against the Role doctype; the
	non-grantable Administrator/Guest/All are rejected (All would silently mean
	unrestricted — force the explicit empty list instead)."""
	require_jarvis_admin()
	parsed = roles
	if isinstance(parsed, str):
		try:
			parsed = frappe.parse_json(parsed)
		except Exception:
			frappe.throw(_("roles must be a JSON array of Role names."))
	if parsed is None:
		parsed = []
	if not isinstance(parsed, list):
		frappe.throw(_("roles must be a JSON array of Role names."))

	clean: list[str] = []
	for r in parsed:
		if not isinstance(r, str) or not r.strip():
			frappe.throw(_("roles must be a JSON array of Role names."))
		r = r.strip()
		if r in _NON_SELECTABLE_ROLES:
			frappe.throw(_("Role {0} cannot be used as an agent restriction.").format(r))
		if not frappe.db.exists("Role", r):
			frappe.throw(_("Role {0} does not exist.").format(r))
		if r not in clean:
			clean.append(r)

	doc = frappe.get_doc(LISTING, agent_slug)
	doc.check_permission("write")
	doc.set("allowed_roles", [{"role": r} for r in clean])
	doc.save()
	frappe.db.commit()
	return {"ok": True, "allowed_roles": [row.role for row in doc.allowed_roles]}


@frappe.whitelist()
def set_listing_status(agent_slug: str, status: str) -> dict:
	"""Set a listing's marketplace status. Jarvis Admin / System Manager (PART 4
	REVISED, TASK 45). Only the admin-meaningful statuses are settable (Draft
	stays registry-controlled)."""
	require_jarvis_admin()
	if status not in _ADMIN_STATUSES:
		frappe.throw(_("Status must be one of: {0}.").format(", ".join(_ADMIN_STATUSES)))
	doc = frappe.get_doc(LISTING, agent_slug)
	doc.check_permission("write")
	doc.status = status
	doc.save()
	frappe.db.commit()
	return {"ok": True, "status": doc.status}


@frappe.whitelist()
def get_agent_admin_overview() -> dict:
	"""Bench-admin overview: the selectable Roles + every listing with its
	allowed_roles and ALL owners' installs. Jarvis Admin / System Manager (PART 4
	REVISED, TASK 45; the cross-owner install read needs the Jarvis Admin read row
	on Jarvis Agent Installation — TASK 47). The SPA probes this endpoint and hides
	the Admin tab when it throws PermissionError."""
	require_jarvis_admin()

	roles = [
		r
		for r in frappe.get_all(
			"Role",
			filters={"disabled": 0, "desk_access": 1},
			order_by="name asc",
			pluck="name",
		)
		if r not in _NON_SELECTABLE_ROLES
	]

	roles_map = _allowed_roles_map()
	installs_by_agent: dict[str, list[dict]] = {}
	for i in frappe.get_all(
		INSTALLATION,
		fields=[
			"name",
			"agent",
			"owner",
			"run_as_user",
			"enabled",
			"schedule_enabled",
			"schedule_frequency",
			"next_run_at",
			"last_run_at",
			"sync_status",
		],
		order_by="owner asc, creation asc",
	):
		installs_by_agent.setdefault(i.agent, []).append(
			{
				"installation": i.name,
				"owner": i.owner,
				# R1-S2: the EXECUTING identity, distinct from the row owner. A blank
				# one is a legacy/misconfigured row — the dispatch paths refuse to run
				# it (R1-F3) — so an admin reading an Apply or run failure needs to see
				# WHICH row it is here, rather than having to open raw Desk.
				"run_as_user": i.run_as_user or None,
				"enabled": int(i.enabled or 0),
				"schedule_enabled": int(i.schedule_enabled or 0),
				"schedule_frequency": i.schedule_frequency,
				"next_run_at": str(i.next_run_at) if i.next_run_at else None,
				"last_run_at": str(i.last_run_at) if i.last_run_at else None,
				"sync_status": i.sync_status,
			}
		)

	listings = frappe.get_all(
		LISTING,
		fields=[
			"name",
			"agent_slug",
			"title",
			"nature",
			"category",
			"status",
			"version",
			"validated_for_fy",
		],
		order_by="status asc, title asc",
	)
	for lst in listings:
		lst["allowed_roles"] = roles_map.get(lst.name, [])
		lst["installs"] = installs_by_agent.get(lst.name, [])

	return {"roles": roles, "listings": listings}


# --------------------------------------------------------------------------- #
# install / enable / schedule / uninstall (mutations — all owner-gated)
# --------------------------------------------------------------------------- #
def _mark_catalog_dirty() -> None:
	"""Flag that the container-pushed ENABLED set changed since the last Apply
	(install / uninstall / enable-disable). Cleared only on a SUCCESSFUL push
	inside ``_enqueued_push_agent_skills``; surfaced to the SPA as ``dirty`` in
	``get_agents_sync_status``. Also bumps ``agent_catalog_version`` — the
	optimistic-concurrency stamp the push worker snapshots before building its
	payload, so a mutation landing MID-push can never have its dirty flag
	cleared by that push (TOCTOU). Best-effort — the flag must never break the
	mutation it annotates."""
	try:
		frappe.db.set_single_value(_SETTINGS, "agent_catalog_dirty", 1)
		_bump_catalog_version()
	except Exception:
		frappe.log_error(title="Jarvis: agent catalog dirty flag failed", message=frappe.get_traceback())


def _bump_catalog_version() -> None:
	"""Increment ``agent_catalog_version`` in ONE statement (#458).

	The obvious read-modify-write (``get_single_value`` + ``set_single_value``)
	loses updates: two mutations racing both read V and both write V+1, so the
	second mutation leaves the version looking untouched. The push worker's TOCTOU
	recheck compares against that version, so a lost increment is exactly the case
	where the worker clears the dirty flag for a change that never made the
	payload.

	Frappe stores one Single field as one ``tabSingles`` row, so a single
	``UPDATE ... SET value = value + 1`` runs under that row's write lock and
	cannot lose an increment however the two writers interleave.
	``set_single_value`` survives only as the SEED path: a Single field that was
	never written has no ``tabSingles`` row at all, and the UPDATE above would
	silently match nothing.
	"""
	params = {"dt": _SETTINGS, "field": "agent_catalog_version"}
	frappe.db.sql(
		"""UPDATE `tabSingles`
		SET `value` = CAST(COALESCE(NULLIF(`value`, ''), '0') AS UNSIGNED) + 1
		WHERE `doctype` = %(dt)s AND `field` = %(field)s""",
		params,
	)
	if not frappe.db.sql(
		"""SELECT 1 FROM `tabSingles` WHERE `doctype` = %(dt)s AND `field` = %(field)s LIMIT 1""",
		params,
	):
		frappe.db.set_single_value(_SETTINGS, "agent_catalog_version", 1)
	# Raw SQL bypasses both the Single's redis document cache and
	# ``frappe.db.value_cache``, which would otherwise keep serving the
	# pre-increment value for the rest of this request.
	frappe.clear_document_cache(_SETTINGS, _SETTINGS)


@frappe.whitelist()
@require_jarvis_user
def install_agent(agent_slug: str) -> dict:
	"""Install a Published agent for the current user. The doctype validate()
	enforces the per-owner cap + (owner, agent) uniqueness. Role-gated: a user
	whose roles do not intersect the listing's allowed_roles is refused
	server-side (System Manager always allowed)."""
	listing = frappe.get_doc(LISTING, agent_slug)  # All-role read
	me = frappe.session.user
	# FIX 11: an agent runs AS a named user (run_as_user defaults to the installer),
	# and the identity guard fail-closes on Administrator/Guest. Catch that here with
	# an actionable message instead of letting doc.insert() surface the raw validation
	# throw ("Run-as user must be an existing, enabled, non-system user").
	if me in ("Administrator", "Guest"):
		frappe.throw(_("Log in as a named user, or map a run-as user — agents cannot run as Administrator."))
	if not _user_allowed_for_agent(listing, me):
		frappe.throw(_("Your roles do not permit installing this agent."), frappe.PermissionError)
	if listing.status != "Published":
		frappe.throw(_("This agent is not available to install."))
	if frappe.db.exists(INSTALLATION, {"owner": me, "agent": listing.name}):
		frappe.throw(_("You have already installed this agent."))
	# R5-J8: refuse when the listing's min_apps are not all installed / a required
	# DocType is absent (typed reason app_absent_or_ineligible). The controller
	# validate() re-enforces this on every surface; catch it here for a clean,
	# pre-insert message. A non-installable capability produces no install row.
	from jarvis.chat.agent_installability import assert_installable

	assert_installable(listing.name)

	sched = {}
	try:
		sched = frappe.parse_json(listing.default_schedule) or {}
	except Exception:
		sched = {}
	freq = str(sched.get("schedule_frequency") or "daily").strip().lower()
	if freq not in _FREQUENCIES:
		freq = "daily"

	doc = frappe.get_doc(
		{
			"doctype": INSTALLATION,
			"agent": listing.name,
			"enabled": 0,
			# Phase 1 identity: the agent runs AS this user (every jarvis__* read is
			# permission-bounded to them). Defaults to the installer — a self-map,
			# which the controller's escalation guard always permits. An admin may
			# retarget it later via set_run_as_user.
			"run_as_user": me,
			# PP-4: every capability activates per customer in preview/SHADOW first,
			# with the installer as the initial named reviewer (retargetable by an
			# admin). The controller also defaults reviewer + forbids a non-shadow
			# birth, but set it explicitly here so the install intent is on the record.
			"activation_state": "shadow",
			"reviewer": me,
			"installed_version": listing.version,
			"installed_at": frappe.utils.now(),
			"schedule_enabled": int(sched.get("schedule_enabled") or 0),
			"schedule_frequency": freq,
		}
	)
	doc.insert()  # owner = me; validate() runs the cap/uniqueness/run-as checks
	# No _mark_catalog_dirty(): installs start enabled=0, so the container's
	# ENABLED set is unchanged — only enable/disable (and uninstalling an
	# ENABLED install) make an Apply pending.
	log_activity(
		agent=listing.name,
		agent_title=listing.title,
		installation=doc.name,
		action="installed",
		detail=f"v{listing.version}" if listing.version else None,
	)
	frappe.db.commit()
	return {"ok": True, "data": {"name": doc.name, "agent": listing.name}}


@frappe.whitelist()
def set_enabled(installation: str, enabled: int) -> dict:
	"""Enable/disable an installed agent — a pure DB write (O6: NO restart; the
	bundle only reaches the container on the next Apply)."""
	doc = frappe.get_doc(INSTALLATION, installation)
	doc.check_permission("write")  # S3 owner-gate
	# R5-J8: never enable a non-installable capability (a min_apps dependency
	# absent at install, or one that vanished after install and was reconciled to
	# installable=0). Disabling is always allowed.
	if int(enabled or 0):
		from jarvis.chat.agent_installability import assert_installable

		assert_installable(doc.agent)
	doc.enabled = int(enabled or 0)
	doc.save()
	_mark_catalog_dirty()
	log_activity(
		agent=doc.agent,
		agent_title=frappe.db.get_value(LISTING, doc.agent, "title"),
		installation=doc.name,
		action="enabled" if doc.enabled else "disabled",
	)
	frappe.db.commit()
	return {"ok": True, "data": {"name": doc.name, "enabled": doc.enabled}}


@frappe.whitelist()
def set_schedule(
	installation: str,
	schedule_enabled: int | None = None,
	schedule_frequency: str | None = None,
	schedule_time: str | None = None,
) -> dict:
	"""Set an installed agent's audit schedule — pure DB write (O6: no restart).
	Recomputes ``next_run_at`` when the schedule is enabled."""
	doc = frappe.get_doc(INSTALLATION, installation)
	doc.check_permission("write")  # S3 owner-gate
	# R5-J8: turning a schedule ON is a run commitment — refuse it for a
	# non-installable capability (a scheduled run would only fail its preflight).
	if int(schedule_enabled or 0):
		from jarvis.chat.agent_installability import assert_installable

		assert_installable(doc.agent)
	if schedule_enabled is not None:
		doc.schedule_enabled = int(schedule_enabled or 0)
	if schedule_frequency is not None:
		freq = str(schedule_frequency).strip().lower()
		if freq not in _FREQUENCIES:
			frappe.throw(_("Frequency must be daily, weekly or monthly."))
		doc.schedule_frequency = freq
	if schedule_time is not None:
		doc.schedule_time = schedule_time or None

	if doc.schedule_enabled:
		doc.next_run_at = compute_next_run(doc.schedule_frequency, doc.schedule_time)
	doc.save()
	log_activity(
		agent=doc.agent,
		agent_title=frappe.db.get_value(LISTING, doc.agent, "title"),
		installation=doc.name,
		action="schedule_changed",
		detail=(
			f"{doc.schedule_frequency} at {doc.schedule_time or '09:00'}"
			if doc.schedule_enabled
			else "schedule off"
		),
	)
	frappe.db.commit()
	return {
		"ok": True,
		"data": {"name": doc.name, "next_run_at": str(doc.next_run_at or "")},
	}


@frappe.whitelist()
def set_config(installation: str, config: str) -> dict:
	"""Persist an installed agent's engagement config JSON — a pure DB write
	(O6: no restart; the delegate reads it on its installation on the next run).
	Owner-gated (S3). Validates the payload is a JSON object (keys: benchmark_value,
	percentage, engagement_risk_level, rounding_step, company, …)."""
	doc = frappe.get_doc(INSTALLATION, installation)
	doc.check_permission("write")  # S3 owner-gate
	try:
		parsed = frappe.parse_json(config) if config else {}
	except Exception:
		frappe.throw(_("Config must be valid JSON."))
	if not isinstance(parsed, dict):
		frappe.throw(_("Config must be a JSON object."))
	doc.config = frappe.as_json(parsed)
	doc.save()
	log_activity(
		agent=doc.agent,
		agent_title=frappe.db.get_value(LISTING, doc.agent, "title"),
		installation=doc.name,
		action="config_changed",
		# Key names only — engagement config VALUES stay out of the feed.
		detail=", ".join(sorted(parsed)) or None,
	)
	frappe.db.commit()
	return {"ok": True, "data": {"name": doc.name}}


@frappe.whitelist()
def set_run_as_user(installation: str, user: str) -> dict:
	"""Map an installed agent's RUN-AS identity — the user every ``jarvis__*``
	ERP read is permission-bounded to (Phase 1 identity). Owner-gated (S3:
	``check_permission("write")``) for WHO may touch the row; the ESCALATION guard
	(who may map WHICH user) lives in the controller ``validate()`` so it holds on
	Desk/test writes too — a non-admin may only map to themselves, any cross-user
	mapping needs Jarvis Admin, and binding to a System Manager needs a System
	Manager. Mirrors ``set_config``: check_permission, set, then save so
	validate() enforces the guard. Pure DB write (no restart)."""
	doc = frappe.get_doc(INSTALLATION, installation)
	doc.check_permission("write")  # S3 owner-gate
	doc.run_as_user = user
	# validate() runs the A4 escalation + A12 permission guard; on_update() writes the
	# cross-user-mapping audit row (FIX 10 — the audit now lives in the controller so
	# it fires on EVERY write surface, Desk / import / bulk / direct save, not just
	# this SPA endpoint; a self-map is correctly not audited).
	doc.save()
	frappe.db.commit()
	return {
		"ok": True,
		"data": {
			"name": doc.name,
			"run_as_user": doc.run_as_user,
			"scoped_visibility": int(doc.scoped_visibility or 0),
		},
	}


def _installation_finding_names(run_names: list) -> set:
	"""#455 — the findings an uninstall cascade may destroy: ONLY those this
	installation's own runs CREATED.

	Membership is read off the finding's creation stamps, ``run`` and
	``first_seen_run``. Both are written once at insert
	(``agent_runs.record_delegate_run``) and are frozen by the finding controller
	thereafter, so they are the one durable statement of which installation
	produced the row.

	``last_seen_run`` is deliberately NOT a membership signal even though it too
	points at a run. It is the only run pointer the engine ever re-points, and the
	recurrence bump that re-points it matches on ``(owner, agent, fingerprint)``.
	Under PP-4 a shadow installation's findings are re-homed to the REVIEWER, so a
	reviewer who also runs their own installation of the same agent can have a
	DIFFERENT owner's finding bumped onto one of their runs (#454). Treating that
	as membership would re-open exactly the cross-owner sweep this function
	exists to close: a bumped row was, by construction, created by somebody else's
	installation.

	The old ``{"agent": ..., "owner": ["in", [owner, reviewer]]}`` filter is gone
	entirely. Nothing constrains a reviewer to a single installation (PP-6
	institutionalises the opposite, see ``_verify_reviewer_two_pack_capacity``), so
	that clause matched — and ``force=True`` destroyed — live findings belonging to
	other customers.

	Findings with NO surviving run pointer (a pre-run-tracking row, or one whose
	runs were already deleted) are left ALONE. There is no evidence tying such a row
	to this installation rather than to any other install of the same agent, and
	guessing is what caused #455. An orphan is visible and removable from Desk;
	another customer's destroyed audit history is neither.
	"""
	if not run_names:
		return set()
	names = set()
	for field in ("run", "first_seen_run"):
		names.update(
			frappe.get_all(FINDING, filters={field: ["in", run_names]}, pluck="name", ignore_permissions=True)
		)
	return names


def _detach_last_seen_run(run_names: list) -> None:
	"""Re-point any SURVIVING finding whose ``last_seen_run`` lands in the runs this
	cascade is about to force-delete.

	A survivor here is by definition another installation's row that the #454
	recurrence bump attached to one of our runs. Leaving the pointer would dangle
	it: ``get_findings``' run drill-down INNER JOINs ``last_seen_run``, so the row
	would vanish from its real owner's history, and Frappe's link validation would
	reject that owner's next acknowledge/resolve save outright. Falling back to the
	row's own ``first_seen_run`` (which belongs to ITS installation and survives, or
	is empty) keeps the recurrence pointer truthful and the row usable.

	Raw ``db.set_value`` — ``last_seen_run`` is a frozen audit field, so this must
	bypass the finding controller exactly as the recurrence bump does.
	"""
	if not run_names:
		return
	for row in frappe.get_all(
		FINDING,
		filters={"last_seen_run": ["in", run_names]},
		fields=["name", "first_seen_run"],
		ignore_permissions=True,
	):
		frappe.db.set_value(
			FINDING, row.name, "last_seen_run", row.first_seen_run or None, update_modified=False
		)


@frappe.whitelist()
def uninstall_agent(installation: str) -> dict:
	"""Delete an installation (owner-gated) plus its run + finding history —
	bottom-up, mirroring ``macros_api.delete_macro``: findings link runs (via
	``run`` / ``first_seen_run`` / ``last_seen_run``) and runs link the
	installation. The cascade is scoped STRICTLY BY INSTALLATION MEMBERSHIP, the
	way ``_rehome_installation_outputs`` scopes it, never by a broad
	``(agent, owner)`` match (#455 — see ``_installation_finding_names``). The
	``uninstalled`` activity row is written FIRST — it is Link-free by design, so
	the history survives the cascade. The bundle leaves the container on the next
	Apply (the fleet endpoint does a full reconcile); the dirty flag records that
	an Apply is now pending — but only when the install was ENABLED (a disabled
	install was never in the pushed set, so removing it changes nothing)."""
	doc = frappe.get_doc(INSTALLATION, installation)
	doc.check_permission("delete")  # S3 owner-gate before touching linked rows
	log_activity(
		agent=doc.agent,
		agent_title=frappe.db.get_value(LISTING, doc.agent, "title"),
		installation=doc.name,
		action="uninstalled",
	)
	# PP-4: a shadow installation's runs/findings are re-homed to the REVIEWER (not
	# the installer owner), so the owner-scoped permission-query hook would hide them
	# from this owner-initiated cascade and leave orphans that block the delete.
	# ignore_permissions here finds every row regardless of its current visibility
	# owner; the delete itself is already ignore_permissions + owner-gated above.
	run_names = frappe.get_all(RUN, filters={"installation": doc.name}, pluck="name", ignore_permissions=True)
	for name in _installation_finding_names(run_names):
		# The guard flag rides through ``delete_doc``'s ``flags`` argument, which is
		# applied to the fetched doc BEFORE ``on_trash`` runs — so the controller can
		# re-derive membership itself and refuse a row this cascade has no claim on.
		frappe.delete_doc(
			FINDING,
			name,
			ignore_permissions=True,
			force=True,
			flags={"jarvis_uninstall_installation": doc.name},
		)
	_detach_last_seen_run(run_names)
	for name in run_names:
		frappe.delete_doc(RUN, name, ignore_permissions=True, force=True)
	frappe.delete_doc(INSTALLATION, installation)  # honors if_owner
	if doc.enabled:
		_mark_catalog_dirty()
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist()
def run_agent_now(installation: str, options: str | dict | None = None) -> dict:
	"""Manual trigger: enqueue an audit turn NOW via the SAME code path the
	scheduler uses (``agent_scheduler._launch_audit``), executed UNDER THE
	INSTALLATION's RUN-AS USER identity — never the triggering user's.

	``options`` is a small per-launch payload (JSON string or dict). The only key
	today is ``source_apps`` — the list of custom apps this run may read source
	from (CX5-2). It is REQUIRED for the ``custom-app-learning`` agent (whose whole
	job is shipping customer source to the configured model provider) and every
	name is validated against the installed learnable custom apps before launch:
	"installed" is not consent, so an admin authorises the exact apps, per run. The
	validated selection is stamped server-side into the run's ``scope_json`` (the
	tools' authorization) and PERSISTED on the installation
	(``source_apps_json``) so a scheduled re-learn reuses the same explicit
	selection instead of running unbounded.

	``check_permission`` gates WHO may trigger (owner, or a System Manager);
	but the audit's ``jarvis__*`` tool calls must always be scoped to the
	installation's ``run_as_user`` permissions, so a System Manager triggering
	another owner's audit cannot run ERP reads with elevated rights. This mirrors
	the scheduler's S1 identity hinge on the manual path. Run/finding ROW
	ownership stays the human owner (``_launch_audit``); only the ERP-read
	identity is the run-as user; and the TRIGGERING human is recorded separately as
	the run's immutable ``initiating_human`` (JF-021) — three distinct identities."""
	doc = frappe.get_doc(INSTALLATION, installation)
	doc.check_permission("write")  # S3: who may trigger
	# R1-F3: no ``or doc.owner`` fallback. A blank run-as user is a MISCONFIGURED
	# install, and defaulting to the row owner would run this audit's ERP reads as
	# an identity the A4 escalation guard never litigated — a privilege grant
	# nobody reviewed. Refuse explicitly (and before the RBAC helper, which would
	# otherwise be asked to rule on an empty user) so the operator sees WHICH row
	# is wrong instead of getting a silent owner-privileged run.
	run_as = (doc.run_as_user or "").strip()
	if not run_as:
		frappe.throw(
			_(
				"This agent has no run-as user, so there is no identity to run it as. Set a "
				"run-as user on the installation, or disable it."
			)
		)
	# RBAC: the audit executes AS the RUN-AS user, so it is THAT identity's roles
	# that must permit the agent (gotcha #8 — the executing identity is gated, not
	# the triggerer). SM run-as users pass via the System Manager bypass inside the
	# helper.
	if not _user_allowed_for_agent(doc.agent, run_as):
		frappe.throw(
			_("The run-as user's roles do not permit running this agent."),
			frappe.PermissionError,
		)
	if not doc.enabled:
		frappe.throw(_("Enable the agent before running it."))
	# R5-J8: refuse an on-demand run for a non-installable capability — a required
	# app/DocType that was absent at install or vanished afterward means the run
	# has no data to evaluate (typed reason app_absent_or_ineligible).
	from jarvis.chat.agent_installability import assert_installable

	assert_installable(doc.agent)
	nature = frappe.db.get_value(LISTING, doc.agent, "nature")
	if nature not in ("Auditor", "Scribe"):
		frappe.throw(
			_("Only auditor and scribe agents run on demand; operators draft through the Approval Board.")
		)
	# CX5-5: a SHADOW installation means "run it, but its output is not live yet" —
	# an auditor's findings sit in a shadow set for a reviewer. A scribe has no such
	# holding pen: it writes the LIVE Org wiki directly, with no confirmation gate,
	# so letting one run in shadow would make the shadow state a lie. Refuse until
	# the install is promoted.
	if nature == "Scribe" and (doc.activation_state or "shadow") == "shadow":
		frappe.throw(
			_(
				"This agent writes to the Org wiki directly, so it cannot run while the "
				"installation is in shadow. Promote it to live to run it."
			)
		)
	from jarvis.chat.agent_scheduler import (
		_is_app_learning,
		_launch_audit,
		_over_run_budget,
		_valid_owner,
	)

	# CX5-2: the Custom App Learning agent's explicit, per-run app authorization.
	# Refuse the launch outright when the admin named no app — a run with an empty
	# selection would reach the container and read nothing, wasting a run and a
	# budget slot while looking like a silent failure.
	source_apps = None
	if _is_app_learning(doc.agent):
		from jarvis.learning import app_source

		selection = (frappe.parse_json(options) if isinstance(options, str) else options) or {}
		if not isinstance(selection, dict):
			selection = {}
		try:
			source_apps = app_source.validate_source_apps(selection.get("source_apps") or [])
		except ValueError as e:
			frappe.throw(
				_(
					"Select which custom apps this run may read before starting it — {0}. "
					"Their source code is sent to the configured AI model provider."
				).format(str(e))
			)
		# Persist the validated selection so the SCHEDULED path has a durable, explicit
		# authorization to reuse (it has no human to ask) instead of running unbounded.
		frappe.db.set_value(
			INSTALLATION, doc.name, "source_apps_json", frappe.as_json(source_apps), update_modified=False
		)

	# A14: the manual path shares the SAME per-installation + per-tenant monthly
	# budget as the scheduler (counting manual + scheduled runs together), so a
	# manual "Run now" loop cannot drain the subscription the scheduler is capped
	# against. Checked before dispatch; a plain COUNT, identity-agnostic.
	over, why = _over_run_budget(installation)
	if over:
		frappe.throw(
			_(
				"Monthly agent-run budget reached ({0}). Runs resume next month, or an "
				"admin can raise the budget in Jarvis Settings."
			).format(why)
		)

	# Fail-closed identity guard: refuse to run an audit AS Administrator / Guest /
	# a disabled RUN-AS user ON SOMEONE ELSE'S behalf (the escalation a System
	# Manager could otherwise cause, and the unattended risk the scheduler faces).
	# A user running their OWN self-mapped install manually is attended +
	# same-identity, so it is allowed — this is how a single-admin dev box runs
	# audits at all.
	if not _valid_owner(run_as) and run_as != frappe.session.user:
		frappe.throw(_("Cannot run this audit as its run-as user (identity guard)."))

	original_user = frappe.session.user
	# JF-021: the run's IMMUTABLE launch provenance identity, captured HERE — before
	# the impersonation below — and passed EXPLICITLY into the launch. _launch_audit
	# used to read frappe.session.user, which by then is the RUN-AS user: every manual
	# run a System Manager triggered on someone else's install permanently recorded the
	# wrong person in a field no correction can reach. This is never a client argument
	# (run_agent_now takes no such parameter) and the launch re-derives + verifies it
	# against the authenticated session user, so it can only confirm, never forge.
	triggering_human = authenticated_user()
	# impersonate is session-safe (a bare frappe.set_user in this HTTP path
	# would gut the caller's cookie session and log them out) and no-ops when
	# the run-as user IS the caller (self-mapped manual run). get_doc does NOT
	# enforce read perms, so the re-fetch under the run-as user is safe even when
	# run_as is not the (if_owner) row owner.
	with impersonate(run_as if run_as != original_user else None):
		if run_as != original_user:
			doc = frappe.get_doc(INSTALLATION, installation)  # re-fetch under run_as
		result = _launch_audit(
			doc, trigger="manual", source_apps=source_apps, initiating_human=triggering_human
		)
	return {"ok": True, "data": result}


# --------------------------------------------------------------------------- #
# PP-4 shadow activation + PP-6 global activation budget
# --------------------------------------------------------------------------- #
def _has_ceiling_grant(customer: str) -> bool:
	"""PP-6 — True iff a Jarvis Admin has recorded an ``activation_ceiling_raised``
	provenance grant BOUND TO THIS CUSTOMER. The grant is stored on the append-only
	ledger, keyed to the customer via ``result_link_doctype='User' / result_link_name
	=<owner>`` — never a global singleton — so a raise justified by ONE customer's
	reviewer can never silently unlock a second live module for every other customer
	on the site (the exact detachment the global-singleton read caused)."""
	if not customer:
		return False
	return bool(
		frappe.db.exists(
			PROVENANCE,
			{
				"event_type": "activation_ceiling_raised",
				"result_link_doctype": "User",
				"result_link_name": customer,
			},
		)
	)


def _activation_ceiling(customer: str) -> int:
	"""PP-6 — the LIVE-module ceiling FOR THIS CUSTOMER. Base 1 (every customer starts
	with a single live module); raised to the stage maximum only for a customer who
	has a recorded per-customer grant (``_has_ceiling_grant``). Per-customer, never a
	site-wide singleton — the budget is a per-customer ceiling (Round-4 condition 2),
	so its raise must bind to the customer it was justified for."""
	return _ACTIVATION_CEILING_MAX if _has_ceiling_grant(customer) else 1


def _verify_reviewer_two_pack_capacity(customer: str) -> str:
	"""PP-6 reviewer-capacity gate (system-verified, not free text): a second live
	module needs a named reviewer who can own it, so require this customer to have a
	single named ``reviewer`` who is the reviewer-of-record across installations
	spanning at least TWO DISTINCT packs. Returns that reviewer; throws if none
	qualifies.

	R5-J11(c): pack identity is the listing's CANONICAL ``rule_pack`` (a curated
	pack-membership name synced from the registry) and NOTHING ELSE — the former
	agent-slug fallback is gone. Two agents in the SAME pack (or two agents whose
	listings declare NO pack) therefore no longer masquerade as two packs: an empty/
	missing pack id contributes nothing, so competency is never inferred from agent
	count (codex R5-P1-02)."""
	rows = frappe.get_all(INSTALLATION, filters={"owner": customer}, fields=["reviewer", "agent"])
	packs_by_reviewer: dict[str, set] = {}
	for r in rows:
		if not r.reviewer:
			continue
		pack = (frappe.db.get_value(LISTING, r.agent, "rule_pack") or "").strip()
		if not pack:
			continue  # no canonical pack -> contributes nothing (never the slug)
		packs_by_reviewer.setdefault(r.reviewer, set()).add(pack)
	for reviewer, packs in packs_by_reviewer.items():
		if len(packs) >= 2:
			return reviewer
	frappe.throw(
		_(
			"No named reviewer for this customer covers two packs. A second live module "
			"needs a reviewer who is the reviewer-of-record on installations spanning at "
			"least two distinct packs before the activation ceiling may be raised."
		)
	)


def _append_provenance_event(**fields) -> str:
	"""PP-5 — append ONE immutable provenance event. The controller enforces
	append-only + stamps ``occurred_at``; this is the only ledger writer Phase C
	needs (``agent_promoted_to_live`` on promotion, ``activation_ceiling_raised`` on
	a budget raise). ignore_permissions — trusted server infrastructure, exactly
	like the finding/run inserts (the ledger perm grants create to System Manager
	only, but a reviewer/admin action legitimately records one)."""
	doc = frappe.get_doc({"doctype": PROVENANCE, **fields})
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return doc.name


def _rehome_installation_outputs(inst, to_owner: str) -> None:
	"""PP-4 — move the VISIBILITY ownership of this installation's already-persisted
	runs / findings / dashboards / activity to ``to_owner``: the reviewer while
	shadow, the installer once promoted to live. The agent permission-query hooks
	and the dashboard scope condition gate reads on ``owner``/``target_user``, so
	re-homing is how promotion OPENS the owner surface (and demotion re-closes it).
	Raw ``db.set_value`` bypasses the finding/run immutability controllers. Rows are
	located precisely by installation membership (never a broad owner+agent match
	that could sweep a different install of the same agent)."""
	run_names = frappe.get_all(
		RUN, filters={"installation": inst.name}, pluck="name", ignore_permissions=True
	)
	dash_names, finding_names = set(), set()
	if run_names:
		for r in frappe.get_all(
			RUN, filters={"name": ["in", run_names]}, fields=["dashboard"], ignore_permissions=True
		):
			if r.dashboard:
				dash_names.add(r.dashboard)
		for field in ("run", "first_seen_run", "last_seen_run"):
			finding_names.update(
				frappe.get_all(
					FINDING, filters={field: ["in", run_names]}, pluck="name", ignore_permissions=True
				)
			)
	for rn in run_names:
		frappe.db.set_value(RUN, rn, "owner", to_owner, update_modified=False)
	for fn in finding_names:
		frappe.db.set_value(FINDING, fn, "owner", to_owner, update_modified=False)
	for dn in dash_names:
		frappe.db.set_value(
			DASHBOARD, dn, {"owner": to_owner, "target_user": to_owner}, update_modified=False
		)
	for an in frappe.get_all(
		ACTIVITY, filters={"installation": inst.name}, pluck="name", ignore_permissions=True
	):
		frappe.db.set_value(ACTIVITY, an, "owner", to_owner, update_modified=False)


@frappe.whitelist()
def promote_installation(installation: str, justification: str | None = None) -> dict:
	"""PP-4 — promote a shadow installation to LIVE by the named reviewer's explicit
	sign-off. This is the SINGLE choke point PP-4 (calibration) and PP-6 (budget)
	both enforce:

	  * Authority: the named ``reviewer`` (their sign-off), or an authorised approver
	    (Jarvis Admin / System Manager). NOT the owner surface — ``check_permission``
	    gates on ``if_owner`` and the reviewer may not be the owner, so authority is
	    checked explicitly here.
	  * PP-6 budget: refuse when this customer already has ``activation_module_ceiling``
	    live modules (global across all packs, per customer; default 1).
	  * Records who/when (``promoted_by``/``promoted_at``, track_changes audited) and
	    writes an append-only ``agent_promoted_to_live`` provenance event.
	  * Re-homes the installation's runs/findings/dashboards to the owner so the
	    owner surface + attestation become available only AFTER sign-off."""
	from jarvis._redis_lock import redis_lock

	doc = frappe.get_doc(INSTALLATION, installation)
	me = frappe.session.user
	if me != doc.reviewer and not has_jarvis_admin_access(me):
		frappe.throw(
			_("Only the named reviewer or a Jarvis Admin may promote this installation to live."),
			frappe.PermissionError,
		)
	if doc.activation_state == "live":
		frappe.throw(_("This installation is already live."))

	# PP-6 activation-budget enforcement must be RACE-FREE: the read-check-flip below
	# (count live -> compare to ceiling -> save live) is a TOCTOU window. Two concurrent
	# promotions for the same customer would both read live_count=0 against ceiling 1 and
	# both flip to live, exceeding the per-customer ceiling. Serialize every activation
	# change for a customer on a redis lock keyed on the OWNER, and — inside the lock —
	# force a fresh transaction snapshot (``commit`` ends the REPEATABLE-READ snapshot the
	# earlier ``get_doc`` opened, so the count reflects any promotion another request has
	# committed) before re-reading state and re-checking the ceiling.
	owner = doc.owner
	with redis_lock(f"jarvis_agent_activation:{owner}", timeout_s=30, blocking_timeout_s=10.0) as acquired:
		if not acquired:
			frappe.throw(
				_("Another activation change for this customer is in progress — please retry in a moment.")
			)
		frappe.db.commit()  # fresh snapshot under the lock (defeats stale REPEATABLE-READ count)
		doc = frappe.get_doc(INSTALLATION, installation)  # re-read the row under the lock
		if doc.activation_state == "live":
			frappe.throw(_("This installation is already live."))

		ceiling = _activation_ceiling(owner)
		live_count = frappe.db.count(INSTALLATION, {"owner": owner, "activation_state": "live"})
		if live_count >= ceiling:
			frappe.throw(
				_(
					"Activation budget reached: this customer already has {0} live module(s) and the "
					"activation ceiling is {1}. The initial budget is a single live module; a Jarvis Admin "
					"can raise the ceiling to {2} once the reviewer covers a second pack."
				).format(live_count, ceiling, _ACTIVATION_CEILING_MAX)
			)

		doc.activation_state = "live"
		doc.promoted_by = me
		doc.promoted_at = frappe.utils.now()
		doc.flags.promoting = True  # authorises the shadow->live transition guard
		doc.save(ignore_permissions=True)

		_rehome_installation_outputs(doc, owner)
		event = _append_provenance_event(
			event_type="agent_promoted_to_live",
			agent=doc.agent,
			installation=doc.name,
			preparation_mode="live",
			reviewing_human=me,
			detail=((justification or "").strip()[:500] or None),
		)
		log_activity(
			agent=doc.agent,
			agent_title=frappe.db.get_value(LISTING, doc.agent, "title"),
			installation=doc.name,
			action="promoted_to_live",
			detail=f"signed off by {me}",
			owner=owner,
		)
		frappe.db.commit()
	return {
		"ok": True,
		"data": {
			"name": doc.name,
			"activation_state": "live",
			"promoted_by": me,
			"promoted_at": str(doc.promoted_at),
			"provenance_event": event,
		},
	}


@frappe.whitelist()
def demote_installation(installation: str, reason: str | None = None) -> dict:
	"""PP-4 — the demotion / kill path: send a live installation back to SHADOW
	(re-closing the owner surface, freeing a global activation-budget slot). Same
	authority as promotion (named reviewer or a Jarvis Admin). Clears the promotion
	stamp and re-homes outputs back to the reviewer-only surface; audited via
	track_changes + the activity feed."""
	doc = frappe.get_doc(INSTALLATION, installation)
	me = frappe.session.user
	if me != doc.reviewer and not has_jarvis_admin_access(me):
		frappe.throw(
			_("Only the named reviewer or a Jarvis Admin may demote this installation."),
			frappe.PermissionError,
		)
	if doc.activation_state != "live":
		frappe.throw(_("This installation is not live."))
	doc.activation_state = "shadow"
	doc.promoted_by = None
	doc.promoted_at = None
	doc.flags.demoting = True  # authorises the live->shadow transition guard
	doc.save(ignore_permissions=True)
	_rehome_installation_outputs(doc, doc.reviewer or doc.owner)
	log_activity(
		agent=doc.agent,
		agent_title=frappe.db.get_value(LISTING, doc.agent, "title"),
		installation=doc.name,
		action="demoted_to_shadow",
		detail=((reason or "").strip()[:140] or f"by {me}"),
		owner=doc.owner,
	)
	frappe.db.commit()
	return {"ok": True, "data": {"name": doc.name, "activation_state": "shadow"}}


@frappe.whitelist()
def raise_activation_ceiling(customer: str, justification: str, new_ceiling: int = 2) -> dict:
	"""PP-6 — raise the activation ceiling from 1 to 2 (the stage maximum) FOR ONE
	NAMED CUSTOMER. Restricted to a Jarvis Admin. The raise is per-customer, never a
	site-wide singleton: it must name the ``customer`` (owner) being raised, is granted
	only when that customer's named reviewer is system-verified to cover two packs'
	competency (``_verify_reviewer_two_pack_capacity`` — the reviewer-capacity signal,
	not a free-text claim), and is recorded as an append-only ``activation_ceiling_raised``
	provenance event bound to the customer (who / when / customer / reviewer /
	justification / new ceiling). Any value above 2 is rejected — no path to 3+ here."""
	require_jarvis_admin()
	just = (justification or "").strip()
	if not just:
		frappe.throw(_("A reviewer-capacity justification is required to raise the activation ceiling."))
	nc = frappe.utils.cint(new_ceiling)
	if nc != _ACTIVATION_CEILING_MAX:
		frappe.throw(
			_("The activation ceiling may be raised only to {0} (the stage maximum).").format(
				_ACTIVATION_CEILING_MAX
			)
		)
	customer = (customer or "").strip()
	if not customer or not frappe.db.exists("User", customer):
		frappe.throw(_("Name the customer (installation owner) whose activation ceiling is being raised."))
	# System-verified reviewer-capacity gate (not just a free-text justification): a
	# second live module needs a named reviewer who can own it.
	reviewer = _verify_reviewer_two_pack_capacity(customer)
	me = frappe.session.user
	# The grant lives ONLY on the append-only ledger, bound to this customer via
	# result_link_doctype/name — no global singleton is written, so the raise cannot
	# leak a second live module to any other customer. Idempotent: one grant per
	# customer already lifts _activation_ceiling(customer) to the maximum.
	event = _append_provenance_event(
		event_type="activation_ceiling_raised",
		initiating_human=me,
		reviewing_human=reviewer,
		result_link_doctype="User",
		result_link_name=customer,
		detail=f"activation_module_ceiling -> {nc} for {customer}; reviewer {reviewer}; {just}"[:500],
	)
	frappe.db.commit()
	return {
		"ok": True,
		"data": {
			"customer": customer,
			"reviewer": reviewer,
			"activation_module_ceiling": nc,
			"provenance_event": event,
		},
	}


# --------------------------------------------------------------------------- #
# runs + findings (read)
# --------------------------------------------------------------------------- #
def _count(doctype: str, filters: dict, or_filters: list | None = None) -> int:
	"""Server-side COUNT for the paginated envelopes. The common (no-search) path
	uses ``frappe.db.count`` — a true ``COUNT(*)``. ``frappe.db.count`` cannot
	express ``or_filters`` and newer Frappe rejects raw SQL functions in
	``fields``, so the search path plucks names — bounded because it is already
	owner-scoped AND search-narrowed."""
	if or_filters:
		return len(frappe.get_all(doctype, filters=filters, or_filters=or_filters, pluck="name"))
	return frappe.db.count(doctype, filters=filters)


_RUN_LIST_FIELDS = [
	"name",
	"agent",
	"installation",
	"trigger",
	"status",
	"started_at",
	"finished_at",
	"conversation",
	"findings_count",
	"blocker_count",
	"error",
	"coverage_note",
	# Custom App Learning scribe runs render a pages tally (+ links) instead of a
	# findings count; empty/0 for auditor/operator runs.
	"pages_written",
	"pages_json",
]


def _stamp_run_nature(rows: list[dict]) -> list[dict]:
	"""Stamp each run row with its agent's ``nature`` (one query for the whole
	page) so the SPA can render a scribe run's pages tally instead of a findings
	count without an N+1."""
	agents = {r.get("agent") for r in rows if r.get("agent")}
	if not agents:
		return rows
	natures = {
		d.name: d.nature
		for d in frappe.get_all(LISTING, filters={"name": ["in", list(agents)]}, fields=["name", "nature"])
	}
	for r in rows:
		r["nature"] = natures.get(r.get("agent"))
	return rows


@frappe.whitelist()
@require_jarvis_user
def list_runs(agent: str | None = None, limit: int = 50) -> list[dict]:
	"""This owner's run history (optionally filtered to one agent)."""
	me = frappe.session.user
	filters = {"owner": me}
	if agent:
		filters["agent"] = agent
	rows = frappe.get_all(
		RUN,
		filters=filters,
		fields=list(_RUN_LIST_FIELDS),
		order_by="creation desc",
		limit=int(limit or 50),
	)
	return _stamp_run_nature(rows)


@frappe.whitelist()
@require_jarvis_user
def list_runs_page(
	agent: str | None = None,
	status: str | None = None,
	search: str | None = None,
	sort: str = "recent",
	start: int = 0,
	page_length: int = 20,
) -> dict:
	"""This owner's run history, paginated (envelope ``{rows, total, has_more,
	start, page_length}``). ADDITIVE — ``list_runs`` stays. ``sort="recent"``
	(the only order today; the param is forward-compat) is ``started_at desc``
	— MariaDB sorts NULLs LAST on DESC, so a not-yet-started row sinks to the
	bottom; ``creation desc`` breaks ties. Optional ``status`` filter; search
	matches name/status (LIKE-escaped)."""
	me = frappe.session.user
	start, pl = _clamp_page(start, page_length)
	filters: dict = {"owner": me}
	if agent:
		filters["agent"] = agent
	if status:
		if status not in ("running", "completed", "partial", "failed"):
			frappe.throw(_("Invalid status filter."))
		filters["status"] = status
	or_filters = []
	if search and search.strip():
		q = f"%{_lk(search.strip())}%"
		or_filters = [["name", "like", q], ["status", "like", q]]

	total = _count(RUN, filters, or_filters)
	rows = frappe.get_all(
		RUN,
		filters=filters,
		or_filters=or_filters,
		fields=list(_RUN_LIST_FIELDS),
		order_by="started_at desc, creation desc",
		limit_start=start,
		limit_page_length=pl,
	)
	_stamp_run_nature(rows)
	return {
		"rows": rows,
		"total": total,
		"has_more": start + len(rows) < total,
		"start": start,
		"page_length": pl,
	}


@frappe.whitelist()
@require_jarvis_user
def list_findings(
	run: str | None = None,
	state: str | None = None,
	start: int = 0,
	page_length: int = 50,
) -> dict:
	"""This owner's persisted findings (optionally filtered by run and/or state),
	paginated. Envelope ``{rows, total, has_more, start, page_length,
	severity_counts}`` — ``total`` counts ALL matching findings and
	``severity_counts`` (``{blocker, warning, note}``) are the TRUE per-severity
	totals across the whole matching set, NOT just the page, so the SPA's group
	headers stay honest at scale.

	``run`` means "the findings that run OBSERVED", not "rows whose ``run`` field
	is that run": ``record_delegate_run`` dedupes re-detections into the EXISTING
	Finding row (bumping ``last_seen_run``), so filtering on the ``run`` column
	alone returns rows only for the FIRST run that discovered each finding while
	the newer Run's ``findings_count`` still counts them. Dedupe only ever bumps
	``last_seen_run`` while a finding stays open, and a finding NOT seen by a
	recording run is auto-resolved (a later re-detection starts a NEW row), so a
	row's observed runs are exactly the recording runs of its (owner, agent)
	whose creation falls inside the ``[first_seen_run, last_seen_run]`` span —
	which keeps this drill-down consistent with the Runs-table counts."""
	me = frappe.session.user
	start, pl = _clamp_page(start, page_length)

	def _empty() -> dict:
		return {
			"rows": [],
			"total": 0,
			"has_more": False,
			"start": start,
			"page_length": pl,
			"severity_counts": {"blocker": 0, "warning": 0, "note": 0},
		}

	filters = {"owner": me}
	if run:
		# TASK 32 (AGENTS-4): fetch owner alongside and gate it — this raw
		# get_value bypasses perms, so without the owner check a foreign run id is
		# an existence/metadata oracle. A run the caller does not own returns empty
		# (identical to an unknown run), never leaking that it exists.
		run_row = frappe.db.get_value(RUN, run, ["agent", "creation", "status", "owner"], as_dict=True)
		if not run_row or run_row.owner != me:
			return _empty()
		if run_row.status not in ("completed", "partial"):
			# unknown / failed / still-running runs recorded no findings snapshot
			# (findings_count is 0 there too — the drill-down must match).
			return _empty()
		observed = frappe.db.sql(
			"""SELECT f.name FROM `tabJarvis Agent Finding` f
			JOIN `tabJarvis Agent Run` fr ON fr.name = f.first_seen_run
			JOIN `tabJarvis Agent Run` lr ON lr.name = f.last_seen_run
			WHERE f.owner = %(me)s AND f.agent = %(agent)s
			  AND fr.creation <= %(rc)s AND lr.creation >= %(rc)s""",
			{"me": me, "agent": run_row.agent, "rc": run_row.creation},
			pluck=True,
		)
		if not observed:
			return _empty()
		filters["name"] = ["in", observed]
	if state:
		filters["state"] = state

	# TRUE totals over the WHOLE matching set (one grouped COUNT — never the
	# page): total + per-severity counts for the UI group headers.
	severity_counts = {"blocker": 0, "warning": 0, "note": 0}
	# Real COUNT(*)s via frappe.db.count — newer Frappe rejects raw
	# "count(name)" SQL-function strings in get_all fields (see filebox.py).
	total = frappe.db.count(FINDING, filters=filters)
	for sev in severity_counts:
		severity_counts[sev] = frappe.db.count(FINDING, filters={**filters, "severity": sev})

	rows = frappe.get_all(
		FINDING,
		filters=filters,
		fields=[
			"name",
			"run",
			"agent",
			"rule_id",
			"severity",
			# PP-1: the immutable result class + its class-conditional metadata ride on
			# EVERY read row so the SPA can label the class beside the amount and mark a
			# derived_candidate / legal_scenario as unconfirmed — a candidate must never
			# render indistinguishable from an observed_fact on the primary triage surface.
			"result_class",
			"confidence",
			"match_basis",
			"false_positive_path",
			"confirmation_status",
			"rule_version",
			"assumptions",
			"known_exceptions",
			"source",
			"reviewer",
			"outcome_provenance",
			"title",
			"detail_md",
			"section",
			"effective_date",
			"disclaimer",
			"ref_doctype",
			"ref_name",
			"amount",
			"state",
			"first_seen_run",
			"last_seen_run",
			"modified",
		],
		order_by="modified desc",
		limit_start=start,
		limit_page_length=pl,
	)
	# Derived recurrence label: dedupe only ever bumps ``last_seen_run`` while a
	# finding stays open, so a span wider than one run means it recurred.
	for r in rows:
		# PP-1 strong-verb gate on the READ path (angle-6): the stored authored
		# ``title``/``detail_md`` is served through the SAME shared helper the fallback
		# dashboard uses, so a "saved/recovered/prevented" token on any row that is NOT a
		# confirmed_outcome with a resolving provenance link is neutralised server-side —
		# no read surface (this list, FindingsPanel's v-html) can emit an unearned strong
		# verb, and the guard holds by construction, not author discipline.
		_rc = r.get("result_class")
		_op = r.get("outcome_provenance")
		if r.get("title"):
			r["title"] = cr.render_value_text(r["title"], _rc, outcome_provenance=_op)
		if r.get("detail_md"):
			r["detail_md"] = cr.render_value_text(r["detail_md"], _rc, outcome_provenance=_op)
		if r.state == "resolved":
			r["recurrence"] = "resolved"
		elif r.first_seen_run and r.first_seen_run != r.last_seen_run:
			r["recurrence"] = "recurring"
		else:
			r["recurrence"] = "new"
	return {
		"rows": rows,
		"total": total,
		"has_more": start + len(rows) < total,
		"start": start,
		"page_length": pl,
		"severity_counts": severity_counts,
	}


@frappe.whitelist()
def set_finding_state(finding: str, state: str) -> dict:
	"""Move a finding to open/acknowledged/resolved. Owner-gated (S3)."""
	if state not in ("open", "acknowledged", "resolved"):
		frappe.throw(_("Invalid finding state."))
	doc = frappe.get_doc(FINDING, finding)
	doc.check_permission("write")  # S3 owner-gate
	doc.state = state
	doc.save()
	frappe.db.commit()
	return {"ok": True, "data": {"name": doc.name, "state": state}}


@frappe.whitelist()
@require_jarvis_user
def list_agent_activity_page(
	agent: str | None = None,
	action: str | None = None,
	search: str | None = None,
	start: int = 0,
	page_length: int = 20,
) -> dict:
	"""This owner's agent activity feed, newest first, paginated (envelope
	``{rows, total, has_more, start, page_length}``). Activity rows are
	Link-free Data snapshots, so the feed survives the uninstall cascade —
	``agent`` filters on the slug snapshot, ``action`` on the lifecycle verb.
	Search matches agent_title/detail (LIKE-escaped)."""
	me = frappe.session.user
	start, pl = _clamp_page(start, page_length)
	filters: dict = {"owner": me}
	if agent:
		filters["agent"] = agent
	if action:
		filters["action"] = action
	or_filters = []
	if search and search.strip():
		q = f"%{_lk(search.strip())}%"
		or_filters = [["agent_title", "like", q], ["detail", "like", q]]

	total = _count(ACTIVITY, filters, or_filters)
	rows = frappe.get_all(
		ACTIVITY,
		filters=filters,
		or_filters=or_filters,
		fields=[
			"name",
			"agent",
			"agent_title",
			"installation",
			"action",
			"run",
			"detail",
			"creation",
		],
		order_by="creation desc",
		limit_start=start,
		limit_page_length=pl,
	)
	return {
		"rows": rows,
		"total": total,
		"has_more": start + len(rows) < total,
		"start": start,
		"page_length": pl,
	}


@frappe.whitelist()
def take_finding_to_chat(finding: str) -> dict:
	"""Open a NEW conversation seeded with a finding's recorded facts so the
	user can act on it with Jarvis. Owner-gated via ``check_permission("read")``
	(the finding owner via ``if_owner``, or a System Manager). The seed hands
	over ONLY what the run persisted — rule id, statement, severity, referenced
	document, amount, statutory section — and asks for help; it never fabricates
	a remediation. Dispatched as a normal FOREGROUND turn (no ``background``
	flag — unlike ``filebox.drop_file``'s unattended drop, the user lands in
	the live chat), mirroring ``approvals_api.decide``'s resume send."""
	doc = frappe.get_doc(FINDING, finding)
	doc.check_permission("read")  # S3 owner-gate (owner via if_owner, or SM)

	from jarvis.chat.api import send_message

	title = (doc.title or doc.rule_id or "finding").strip()
	conv = frappe.get_doc(
		{
			"doctype": "Jarvis Conversation",
			"title": f"Finding: {title}"[:140],
			"status": "Active",
		}
	)
	conv.insert()  # owned by the current user; respects perms
	frappe.db.commit()

	parts = [
		f"I want to act on audit finding {doc.name} (rule {doc.rule_id}, severity: {doc.severity}).",
		f"Finding: {title}",
	]
	if doc.ref_doctype and doc.ref_name:
		parts.append(f"Referenced document: {doc.ref_doctype} {doc.ref_name}")
	if doc.amount:
		parts.append(f"Amount: {doc.amount}")
	if doc.section:
		eff = f" (effective {doc.effective_date})" if doc.effective_date else ""
		parts.append(f"Statutory section: {doc.section}{eff}")
	if doc.detail_md:
		parts.append(f"Detail: {doc.detail_md[:500]}")
	parts.append(
		"Help me review and act on this finding. Start from the referenced "
		"document and the recorded facts above; do not invent numbers, "
		"documents or remediation steps the data does not support."
	)
	res = send_message(conversation=conv.name, message="\n".join(parts))
	return {
		"ok": bool(res.get("ok")),
		"conversation": conv.name,
		"run_id": res.get("run_id"),
		"reason": res.get("reason"),
	}


# --------------------------------------------------------------------------- #
# Apply (explicit push to the container, via admin -> fleet) + status poller
# --------------------------------------------------------------------------- #
@frappe.whitelist()
@require_jarvis_user
def get_agents_sync_status() -> dict:
	"""Lightweight poller mirroring get_custom_skills_sync_status."""
	s = frappe.get_single(_SETTINGS)
	status = s.get("agent_skills_sync_status") or ""
	return {
		"last_sync_at": str(s.get("agent_skills_synced_at") or ""),
		"last_sync_status": status,
		"pending": status.startswith("pending:"),
		# The enabled set changed since the last successful Apply (install /
		# uninstall / enable-disable) — the SPA shows "Apply pending".
		"dirty": bool(frappe.utils.cint(s.get("agent_catalog_dirty"))),
	}


@frappe.whitelist()
@require_jarvis_user
def get_agents_caps() -> dict:
	"""Lightweight capability probe for the Agents SPA (PART 3 remediation).

	``review`` is the skill-reviewer set (Jarvis Skill Reviewer / Jarvis Admin /
	System Manager) — exactly what ``apply_agents`` requires — so it drives the
	Apply-catalog button's visibility, decoupled from the SM-only cross-owner
	``get_agent_admin_overview``. ``admin`` stays the System-Manager gate for the
	admin-only roles editor / cross-owner data. Any Jarvis User may call this and
	simply gets ``{review: False, admin: False}`` (no button)."""
	from jarvis.permissions import is_skill_reviewer

	return {
		"review": bool(is_skill_reviewer()),
		# PART 4 REVISED, TASK 49(d): the admin roles editor / cross-owner overview
		# is now Jarvis Admin | System Manager (get_agent_admin_overview widened).
		"admin": has_jarvis_admin_access(),
	}


@frappe.whitelist()
def apply_agents() -> dict:
	"""Push all ENABLED installed agent bundles to the container (one restart).
	Explicit action. Builds the payload synchronously (surfaces size/cap errors
	immediately), marks pending, then enqueues the deduped redis-locked worker —
	mirrors ``custom_skills_api.apply_custom_skills``.

	Reviewer/admin-gated (security review PART 3 TASK 30): a bench-wide push
	reconciles + RESTARTS the shared container for EVERY user and builds a payload
	of EVERY owner's enabled agent bundles, so a plain Jarvis User (which every
	backfilled user holds) must not be able to trigger it (DoS). Gated with the
	skill-reviewer set (Jarvis Skill Reviewer / Jarvis Admin / System Manager),
	mirroring ``apply_custom_skills`` — deliberately NOT stacked under
	@require_jarvis_user, since a reviewer/admin may hold neither Jarvis User nor
	System Manager."""
	from jarvis.permissions import require_skill_reviewer

	require_skill_reviewer()
	_rate_limit_apply()
	payload = build_agent_push_payload()
	frappe.db.set_single_value(_SETTINGS, "agent_skills_sync_status", "pending: applying agents")
	frappe.db.commit()
	run_inline = bool(frappe.flags.in_test or frappe.flags.run_admin_sync_inline)
	frappe.enqueue(
		"jarvis.chat.agents_api._enqueued_push_agent_skills",
		queue="long",
		timeout=180,
		enqueue_after_commit=not run_inline,
		now=run_inline,
		job_id=_PUSH_JOB_ID,
		deduplicate=True,
	)
	return {
		"ok": True,
		"agent_skills_sync_status": "pending: applying agents",
		"count": len(payload),
	}


def _rate_limit_apply() -> None:
	"""Simple per-user redis guard so a double-click / script can't storm the
	admin -> fleet -> restart chain (S3). The deduped enqueue already coalesces;
	this rejects the second call outright within a short window."""
	if frappe.flags.in_test:
		return
	me = frappe.session.user
	key = f"jarvis_apply_agents_rl:{me}"
	if frappe.cache().get_value(key):
		frappe.throw(_("An apply is already in progress — please wait a moment."))
	frappe.cache().set_value(key, "1", expires_in_sec=5)


def _enqueued_push_agent_skills() -> None:
	"""Background worker: push the enabled agent bundles via admin -> fleet ->
	container. Re-builds the payload fresh (never trust a payload across the
	queue boundary) and mirrors ``_enqueued_push_custom_skills``'s
	try/except/finally so the status never stays ``pending:`` forever."""
	from jarvis import admin_client
	from jarvis._redis_lock import redis_lock

	with redis_lock(_LOCK_NAME, timeout_s=180, blocking_timeout_s=60.0) as acquired:
		if not acquired:
			frappe.db.set_single_value(
				_SETTINGS, "agent_skills_sync_status", "failed: skipped (concurrent sync)"
			)
			frappe.db.commit()
			return

		terminal_written = False
		try:
			# TOCTOU guard: snapshot the catalog version BEFORE building the
			# payload (inside the lock). A mutation landing mid-push bumps it
			# (``_mark_catalog_dirty``), and we then refuse to clear the dirty
			# flag below — the change missed this payload; a later Apply
			# reconciles it.
			version = frappe.utils.cint(
				frappe.db.get_single_value(_SETTINGS, "agent_catalog_version", cache=False)
			)
			payload = build_agent_push_payload()
			admin_client.post_push_agent_skills(agent_skills=payload)
			# #458: END THIS WORKER'S TRANSACTION before the recheck, or the recheck
			# cannot see a mutation at all and the guard above is inert. Two layers
			# hid it: ``get_single_value`` defaults to ``cache=True`` and serves
			# ``frappe.db.value_cache``, which is invalidated ONLY by commit/rollback;
			# and under REPEATABLE READ every plain SELECT in this transaction reads
			# the snapshot taken before the push, so even ``cache=False`` would
			# re-read the snapshot value. Committing clears both. Exactly the reason
			# ``promote_installation`` commits before ITS re-read.
			#
			# SAFE HERE and nowhere earlier: at this point the worker has written
			# NOTHING (the version read and ``build_agent_push_payload`` are pure
			# reads, and ``admin_client`` is pure HTTP), so this commits an empty
			# transaction and can leave no half-written state. It also sits AFTER the
			# push, so it can never commit a status implying a push that did not
			# happen. The terminal write below is a single ``set_value`` in the fresh
			# transaction, still covered by the try/except/finally and the trailing
			# commit, so the "status is never left pending" invariant is unchanged.
			frappe.db.commit()
			values = {
				"agent_skills_synced_at": frappe.utils.now(),
				"agent_skills_sync_status": f"ok (applied {len(payload)} via admin)",
			}
			# The container now matches the DB — clear the dirty flag ONLY on a
			# successful push whose payload saw every mutation (version
			# unchanged); failures and mid-push mutations leave it set.
			fresh = frappe.utils.cint(
				frappe.db.get_single_value(_SETTINGS, "agent_catalog_version", cache=False)
			)
			if fresh == version:
				values["agent_catalog_dirty"] = 0
			frappe.db.set_value(_SETTINGS, _SETTINGS, values)
			terminal_written = True
		except admin_client.AdminAuthError as e:
			_fail(f"failed: auth: {e}")
			terminal_written = True
			frappe.log_error(title="Jarvis: agent-skills admin auth failed", message=frappe.get_traceback())
		except admin_client.AdminUnreachableError as e:
			_fail(f"failed: admin unreachable: {e}")
			terminal_written = True
			frappe.log_error(title="Jarvis: agent-skills admin unreachable", message=frappe.get_traceback())
		except admin_client.AdminRateLimitedError as e:
			retry = getattr(e, "retry_after_seconds", 0) or 0
			retry_str = f"retry_after={retry}s" if retry > 0 else "retry shortly"
			_fail(f"failed: rate-limited; {retry_str}")
			terminal_written = True
		except admin_client.AdminValidationError as e:
			_fail(f"failed: invalid: {e}")
			terminal_written = True
		except Exception:
			_fail("failed: unexpected error; see Error Log")
			terminal_written = True
			frappe.log_error(title="Jarvis: agent-skills push failed", message=frappe.get_traceback())
		finally:
			if not terminal_written:
				try:
					_fail("failed: unexpected error; see Error Log")
				except Exception:
					pass
		frappe.db.commit()


def _fail(status: str) -> None:
	frappe.db.set_value(
		_SETTINGS,
		_SETTINGS,
		{"agent_skills_synced_at": frappe.utils.now(), "agent_skills_sync_status": status},
	)
