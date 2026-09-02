"""Marketplace catalog sync: registry.json -> Jarvis Agent Listing rows.

The catalog is a BUNDLED deploy artifact (``jarvis/agents/registry.json``),
NEVER fetched from anywhere at runtime — a poisoned catalog bundle is
third-party prompt-as-code with the user's data access, so bundles are treated
as reviewed code and shipped in the app (adversarial finding S2).

``sync_agent_listings`` upserts one ``Jarvis Agent Listing`` per registry agent
(keyed by ``agent_slug`` — the doc name, via ``naming_rule: By fieldname``, so a
re-sync is idempotent) and marks any listing no longer in the registry as
``Deprecated``.

Every shipped agent is ``delivery: "delegate"``: the listing is a STUB — every
catalog field EXCEPT the SKILL body, which must NEVER enter the customer DB (A2).
The bench emits only an enablement signal; admin looks the SKILL body up from the
private bundle store keyed by slug (Phase 2C) and pushes it to fleet.

This is the mirror image of ``jarvis.chat.custom_skills.build_push_payload``
(registry -> DB here; DB -> container payload there).
"""

import json
import os

import frappe

from jarvis.chat.agent_installability import reconcile_installations

LISTING = "Jarvis Agent Listing"

_AGENTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "agents")
_REGISTRY_PATH = os.path.join(_AGENTS_DIR, "registry.json")


# --------------------------------------------------------------------------- #
# registry loading (bundled only — never a network fetch)
# --------------------------------------------------------------------------- #
def _load_registry() -> dict:
	if not os.path.isfile(_REGISTRY_PATH):
		frappe.log_error(
			title="jarvis agent catalog: registry.json missing",
			message=f"expected bundled registry at {_REGISTRY_PATH}",
		)
		return {"agents": []}
	with open(_REGISTRY_PATH) as fh:
		return json.load(fh)


# --------------------------------------------------------------------------- #
# sync
# --------------------------------------------------------------------------- #
def sync_agent_listings() -> dict:
	"""Upsert Jarvis Agent Listing rows from the bundled registry. Idempotent.

	Returns ``{created, updated, deprecated, total}`` for logging / the migrate
	summary. Never fetches remotely (security)."""
	reg = _load_registry()
	agents = reg.get("agents") or []
	seen_slugs = set()
	created = updated = 0

	for a in agents:
		slug = (a.get("agent_slug") or "").strip()
		if not slug:
			continue
		seen_slugs.add(slug)

		# All shipped agents are delegate (A2): the listing is a body-free STUB —
		# every catalog field EXCEPT the SKILL body, which must NEVER enter the
		# customer DB. The bench emits only an enablement signal; admin resolves
		# the body from the private bundle store by slug and pushes it to fleet.
		delivery = "delegate"

		# NOTE: ``allowed_roles`` is deliberately ABSENT — it is bench-admin
		# state (set via agents_api.set_agent_roles), not registry state. A
		# re-sync must never clobber an admin's role restrictions: doc.update()
		# only touches the keys given here, so the loaded child rows survive
		# the save untouched.
		values = {
			"agent_slug": slug,
			"title": a.get("title") or slug,
			"description": a.get("description") or "",
			"category": a.get("domain") or a.get("category") or "",
			"nature": (a.get("nature") or "").strip().title() or "Auditor",
			"delivery": delivery,
			"version": a.get("version") or "",
			"publisher": a.get("publisher") or reg.get("publisher") or "",
			"status": a.get("status") or "Draft",
			"tools_required": frappe.as_json(a.get("tools_required") or []),
			# A12: DocTypes the run-as user must hold read on for the agent's
			# aggregates to be numerically correct — a sibling of tools_required,
			# checkable at install/validate without leaking rule shape. For
			# delegate agents this comes from the bundle-store manifest.
			"doctypes_required": frappe.as_json(a.get("doctypes_required") or []),
			# A2/A16: the delegate auditor's OPAQUE rule-token set, id-only. The
			# bench needs it to validate a finding's rule in record_agent_run
			# without ever holding a rule body/threshold. Empty for operators /
			# legacy agents. Mirrors the bundle store's rules.ids.json.
			"rule_tokens": frappe.as_json(a.get("rule_tokens") or []),
			"min_apps": frappe.as_json(a.get("min_apps") or []),
			# R5-J9: the declarative operator write contract (manifest.writes[] —
			# non-IP {doctype, mode} metadata the exporter emits). create_doc/
			# update_doc bind a delegate call to it. Tolerant of an OLD registry
			# that predates the field (``.get`` -> empty list): an operator whose
			# registry carries no writes stays refused every write (fail-closed),
			# an auditor's is always empty. NEVER a rule body/threshold.
			"writes": frappe.as_json(a.get("writes") or []),
			# R5-J11(c): the canonical pack-membership id (a NAME, never the as-coded
			# predicate) the reviewer two-pack activation gate counts DISTINCT non-empty
			# values of. Tolerant of an old registry that predates it (``.get`` -> "");
			# an empty pack id contributes nothing to the distinct-pack count.
			"rule_pack": (a.get("rule_pack") or "").strip(),
			# A2: NEVER write a SKILL body into the customer DB — the proprietary
			# playbook lives only in the private admin bundle store. Every listing
			# is a body-free stub.
			"skill_bundle": frappe.as_json([]),
			"default_schedule": frappe.as_json(a.get("default_schedule") or {}),
			"validated_for_fy": a.get("validated_for_fy") or "",
		}

		if frappe.db.exists(LISTING, slug):
			doc = frappe.get_doc(LISTING, slug)
			doc.update(values)
			doc.flags.ignore_permissions = True
			doc.save()
			updated += 1
		else:
			doc = frappe.get_doc({"doctype": LISTING, **values})
			# Role restriction seed (INSERT branch ONLY): a manifest may declare
			# ``default_allowed_roles`` to ship its install/run restriction ON BY
			# DEFAULT (the Custom App Learning scribe seeds System Manager + Jarvis
			# Admin, so it is admin-only out of the box). Seeded ONLY on first sync
			# — the UPDATE branch above never touches ``allowed_roles``, so a
			# re-sync can NEVER clobber an admin's later role edits (the
			# bench-admin-state invariant, agent_catalog.py note above). Enforcement
			# is unchanged (server-side at install AND run); this only sets the
			# default. Unknown roles are skipped so a seed can never fail a migrate.
			seed_roles = a.get("default_allowed_roles") or []
			if isinstance(seed_roles, list):
				rows = [
					{"role": r}
					for r in seed_roles
					if isinstance(r, str) and r.strip() and frappe.db.exists("Role", r.strip())
				]
				if rows:
					doc.set("allowed_roles", rows)
			doc.flags.ignore_permissions = True
			doc.insert()
			created += 1

	# Any listing not in the current registry is retired to Deprecated (never
	# hard-deleted — installs may still reference it).
	deprecated = 0
	for name in frappe.get_all(LISTING, pluck="name"):
		if name not in seen_slugs:
			cur = frappe.db.get_value(LISTING, name, "status")
			if cur != "Deprecated":
				frappe.db.set_value(LISTING, name, "status", "Deprecated", update_modified=False)
				deprecated += 1

	frappe.db.commit()
	return {"created": created, "updated": updated, "deprecated": deprecated, "total": len(seen_slugs)}


# --------------------------------------------------------------------------- #
# DB -> container push payload (the mirror of build_push_payload for skills)
# --------------------------------------------------------------------------- #
# The container skill dir for an installed agent, namespaced ``agent-<slug>`` so
# it lives in the SEPARATE agent_skills reconcile namespace (adversarial S4:
# never let it evict the customer's own custom skills).
AGENT_PREFIX = "agent-"


def build_agent_push_payload(owner: str | None = None) -> list[dict]:
	"""Collect the container's agent roster into the fleet push payload.

	Every entry is a body-free DELEGATE ENABLEMENT SIGNAL (A2): ``{slug,
	delivery:"delegate", tools_allow, model, timeout_s, nature}`` where ``slug``
	is ``agent-<agent_slug>``. The proprietary SKILL never transits the customer
	bench; admin looks the body up from the private bundle store keyed by slug and
	pushes it to fleet (Phase 2C). ``tools_allow``/``timeout_s``/``nature``/``model``
	echo the BUNDLED registry (metadata, not IP) so admin can render the delegate.

	The roster is the UNION of two legs, deduped by slug:

	  1. ALLOWED LISTINGS (jarvis#1062) — every Published, installable listing an
	     admin has granted to at least one role or named user. This is the leg that
	     makes the product work: under deny-by-default an admin ALLOWS an agent and
	     applies once (the tenant-wide restart is the admin's cost to accept), and
	     the allowed users can then self-install and run it WITHOUT each install
	     triggering another restart of everyone's workspace.
	  2. ENABLED INSTALLS — the historical leg, kept so that nothing running today
	     stops running the moment this ships. See ``legacy_empty_allows`` below.

	Bench-global by design (one bench == one customer == one container). ``owner``
	scopes the payload to one owner's enabled installs and is used only by tests;
	the allowed-listings leg is skipped when it is set, because "this owner's
	roster" is not a statement about listings nobody installed. An empty list is a
	valid "remove all agent skills" reconcile.

	RBAC (defense in depth): an enabled install whose RUN-AS user may no longer use
	the agent is EXCLUDED from the push — the scheduler / run-now gates already
	refuse to run it, but its enablement signal must not reach the container
	either. The run-as user, not the owner, is the identity that decides (#457): it
	is the one every dispatch gate applies, so gating the push on anything else
	advertises a roster the bench will not honour. Identity (CX1-1, the same
	reasoning): an enabled install with a BLANK run-as user is EXCLUDED too —
	R1-F3 refuses it at every dispatch path.

	That install-leg gate runs in ``legacy_empty_allows`` mode: a listing with NO
	roles and NO users still admits its enabled installs here. This is the
	grandfather guarantee, and it is deliberately WIDER than what dispatch will
	honour after the inversion. The accepted consequence: on a tenant that upgrades
	before ``v2_18_agent_access_grandfather`` runs — or where an admin clears both
	allow lists on an agent that still has enabled installs — the delegate stays in
	the container roster while ``run_agent_now`` / ``_sweep_one`` refuse it. A
	roster entry nobody can dispatch is inert; the opposite error (dropping a
	working customer's agent mid-upgrade) is a live outage.

	Installs are per-(owner, agent) but the payload is bench-global and keyed by
	SLUG, so two users each enabling the SAME agent are ONE entry, not two —
	emitted with UNION semantics (the slug ships if ANY enabled install clears the
	gates)."""
	# Lazy import — agents_api imports build_agent_push_payload from this module
	# at module level, so a top-level back-import would be circular.
	from jarvis.chat.agents_api import _user_allowed_for_agent

	# Delegate metadata (tools_allow / timeout_s / nature / model) lives in the
	# BUNDLED registry, never the customer DB; the enablement signal echoes it so
	# admin can render the delegate without the body transiting the bench. Indexed
	# once per build.
	reg_by_slug = {
		(a.get("agent_slug") or "").strip(): a
		for a in (_load_registry().get("agents") or [])
		if (a.get("agent_slug") or "").strip()
	}

	filters = {"enabled": 1}
	if owner:
		filters["owner"] = owner
	installs = frappe.get_all(
		"Jarvis Agent Installation",
		filters=filters,
		fields=["agent", "owner", "installable", "run_as_user"],
		# ``agent`` alone is not a total order once two owners install the same
		# agent; the ``name`` tiebreak makes the iteration — and therefore which
		# install wins the de-dupe below — stable across runs. The payload is a
		# FULL RECONCILE that admin/fleet diffs against the container's current
		# roster, so a wobbling order would read as a change.
		order_by="agent asc, name asc",
	)
	payload = []
	# De-dupe key: the LISTING DOCNAME (``row.agent``), not the emitted slug, so the
	# short-circuit below can run BEFORE any query. The two are the same key —
	# ``Jarvis Agent Listing`` is named ``field:agent_slug`` and ``agent_slug`` is
	# ``unique: 1`` (a DB index), so docname <-> agent_slug is a bijection, and
	# frappe re-pins the field to the name on every ORM save
	# (``base_document._sync_autoname_field``, called from ``_validate``). Deduping
	# on either therefore ships exactly the same set of slugs.
	seen_agents: set[str] = set()
	for row in installs:
		# De-dupe by AGENT. An install is per-(owner, agent) but the container's
		# agent_skills namespace is per-slug, so two users who each install AND
		# enable the same agent produce two rows for ONE slug. Admin REJECTS a
		# payload carrying a duplicate slug outright ("invalid: duplicate agent
		# skill slug '<x>'"), which kills EVERY agent push from the bench — not just
		# the duplicated agent.
		#
		# Checked FIRST, before the listing lookup and the RBAC gate: once an agent
		# has been emitted the outcome cannot change — no later row can add or
		# remove it — so the work those gates do for a second row of the same agent
		# is spent purely to reach a `continue`. That work is a ``get_value`` on the
		# listing PLUS ``_user_allowed_for_agent``, which is itself an N+1 on the
		# allowed-role child table; and this function runs TWICE per Apply (the
		# endpoint and again in the background job), so at 20 owners x 20 installs
		# the short-circuit saves ~800 round-trips.
		#
		# UNION semantics are unaffected: ``seen_agents`` is only added to AFTER
		# every gate has passed, so a row blocked by one of them leaves its agent
		# unseen and the next candidate is still evaluated in full. The slug ships
		# as soon as ANY enabled install clears the gates.
		if row.agent in seen_agents:
			continue

		# R5-J8: a reconcile marks an install ``installable=0`` (never deletes) when
		# a min_apps dependency vanished AFTER install while it was still enabled.
		# Its enablement signal must not reach the container — the run gates already
		# refuse it, and pushing it would install a bundle whose data is absent.
		if not frappe.utils.cint(row.installable):
			continue

		# CX1-1: an enabled install with a BLANK run-as user has no executing
		# identity, so R1-F3 makes every dispatch path refuse it (the scheduler, the
		# manual run-now, and ``_launch_audit`` — the choke point both funnel
		# through). Its enablement signal must not reach the container either: the
		# push would provision the delegate, seat it in the container roster and the
		# tenant's agent_roster, and so advertise an agent this bench will NEVER run.
		# Same reasoning as the installability gate above and the RBAC gate below —
		# schema relaxation (``run_as_user`` is no longer ``reqd``, so a legacy row
		# stays disableable) must not leave a misconfigured row eligible for the
		# fleet reconcile just because it is still flagged enabled.
		#
		# A PER-ROW gate, like every other one here: it disqualifies THIS row only
		# and ``continue``s before ``seen_agents.add``, so UNION semantics hold in
		# both directions — a blank row never suppresses a valid install of the same
		# agent by another owner, and a blank row alone never ships the slug.
		if not (row.run_as_user or "").strip():
			continue

		listing = frappe.db.get_value(
			LISTING,
			row.agent,
			["agent_slug", "description", "status"],
			as_dict=True,
		)
		if not listing or listing.status != "Published":
			continue
		# #457: gated on the RUN-AS user, not the row owner. The push and the two
		# dispatch gates (``agent_scheduler.run_due_agent_audits`` and
		# ``agents_api.run_agent_now``) must agree on WHICH identity decides, or the
		# roster describes a bench that does not exist. Gotcha #8 settles which one:
		# the EXECUTING identity is gated, not the triggerer, and the run-as user is
		# the identity whose permissions every ``jarvis__*`` read is bounded by.
		# Owner-gating produced both errors — an install whose owner lost the role
		# while the run-as user kept it was dropped from the roster yet still
		# dispatched (the reported phantom run), and the mirror case advertised a
		# delegate the bench would refuse to run at every cadence.
		# ``legacy_empty_allows``: the GRANDFATHER leg. A listing with neither an
		# allowed role nor an allowed user is closed everywhere else, but an install
		# that was already enabled here keeps its roster entry — see the docstring
		# for the divergence this consciously accepts.
		if not _user_allowed_for_agent(row.agent, row.run_as_user, legacy_empty_allows=True):
			continue

		# Every gate has passed, so this agent is emitted and no later row for it can
		# change that: mark it seen. Collapsing the rows is safe because the entry
		# below is derived purely from the listing row plus the bundled registry —
		# it carries NO per-install data — so every row for an agent would yield a
		# byte-identical entry.
		seen_agents.add(row.agent)
		payload.append(_enablement_signal(listing.agent_slug, reg_by_slug))

	# ── Leg 1: ALLOWED LISTINGS (jarvis#1062) ──────────────────────────────────
	# Appended AFTER the install leg so ``seen_agents`` already holds everything
	# that leg emitted and this one only adds slugs it did not. Skipped entirely
	# for an owner-scoped build (see the docstring) — a listing nobody installed
	# belongs to no owner.
	if not owner:
		from jarvis.chat.agent_installability import evaluate_installability

		allowed_listings = frappe.get_all(
			LISTING,
			filters={"status": "Published"},
			fields=["name", "agent_slug"],
			order_by="name asc",
		)
		granted = _listings_with_any_grant()
		for lst in allowed_listings:
			if lst.name in seen_agents or lst.name not in granted:
				continue
			# Same reasoning as the install leg's ``installable`` check: an agent
			# whose min_apps / required DocTypes are absent has no data to evaluate,
			# so advertising its delegate would seat a roster entry every dispatch
			# path refuses. No install row exists here, so the site is evaluated
			# directly rather than read off a reconciled per-install flag.
			if not evaluate_installability(lst.name)[0]:
				continue
			seen_agents.add(lst.name)
			payload.append(_enablement_signal(lst.agent_slug, reg_by_slug))

	# Stable order regardless of which leg contributed an entry: the payload is a
	# FULL RECONCILE that admin/fleet diffs against the container's current roster,
	# and an entry moving between legs must not read as a change.
	payload.sort(key=lambda e: e["slug"])
	return payload


def _enablement_signal(agent_slug: str, reg_by_slug: dict) -> dict:
	"""The A2 enablement signal for one agent — body-free.

	Admin resolves the SKILL from the private bundle store by slug (Phase 2C); the
	body NEVER leaves it. Derived purely from the slug plus the BUNDLED registry,
	which is why both legs of the roster can share it and why de-duping by slug is
	safe: two sources for the same agent yield a byte-identical entry."""
	meta = reg_by_slug.get(agent_slug) or {}
	return {
		"slug": f"{AGENT_PREFIX}{agent_slug}",
		"delivery": "delegate",
		"tools_allow": meta.get("tools_allow") or [],
		"model": meta.get("model") or None,
		"timeout_s": meta.get("timeout_s"),
		"nature": (meta.get("nature") or "").strip().lower(),
	}


def _listings_with_any_grant() -> set[str]:
	"""Listing docnames carrying at least one allowed role OR one allowed user.

	Two queries for the whole catalog rather than ``_user_allowed_for_agent`` per
	listing, which is an N+1 over both child tables and runs twice per Apply."""
	granted: set[str] = set()
	for doctype, parentfield in (
		("Jarvis Agent Allowed Role", "allowed_roles"),
		("Jarvis Agent Allowed User", "allowed_users"),
	):
		granted.update(
			frappe.get_all(
				doctype,
				filters={"parenttype": LISTING, "parentfield": parentfield},
				pluck="parent",
			)
		)
	return granted


def registry_timeout_s(agent_slug: str, default: int = 600) -> int:
	"""The delegate's per-run wall-clock budget (seconds) from the BUNDLED
	registry.

	Delegate audits run 20-40 min, so the run verb needs the agent's declared
	budget, not the chat default. The ``Jarvis Agent Listing`` doctype carries no
	``timeout_s`` field, so the scheduler's dispatch tail sources it here — the
	same bundled metadata ``build_agent_push_payload`` echoes into the enablement
	signal (never the customer DB). Clamped to the fleet-agent's accepted range
	[60, 5400]; falls back to ``default`` for a legacy agent / missing / bad
	value."""
	slug = (agent_slug or "").strip()
	for a in _load_registry().get("agents") or []:
		if (a.get("agent_slug") or "").strip() == slug:
			try:
				n = int(a.get("timeout_s") or 0)
			except (TypeError, ValueError):
				n = 0
			return n if 60 <= n <= 5400 else default
	return default


def registry_tools_allow(agent_slug: str) -> list[str]:
	"""The agent's DECLARED tool surface from the BUNDLED registry, verbatim.

	The same metadata ``build_agent_push_payload`` echoes into the container's
	enablement signal — the manifest's ``tools_allow``, agent-facing ids
	(``jarvis__get_doc``) plus the container-side tools (``exec``/``canvas``/
	``message``) the bench never serves. JF-017 snapshots this onto the run at
	launch and enforces it bench-side, so it is no longer just the container's
	configuration.

	Returns ``[]`` for an unknown slug or a malformed entry — under a ``snapshot``
	contract that authorises NOTHING, which is the intended fail-closed outcome for
	a run whose bundle the bench cannot describe."""
	slug = (agent_slug or "").strip()
	if not slug:
		return []
	for a in _load_registry().get("agents") or []:
		if (a.get("agent_slug") or "").strip() == slug:
			declared = a.get("tools_allow")
			if not isinstance(declared, list):
				return []
			return [str(t).strip() for t in declared if str(t or "").strip()]
	return []


def after_migrate() -> None:
	"""hooks.after_migrate entry: keep the catalog in lockstep with the bundled
	registry on every migrate. Best-effort — a catalog hiccup must never fail a
	migration."""
	try:
		result = sync_agent_listings()
		frappe.logger("jarvis").info(f"agent catalog synced: {result}")
	except Exception:
		frappe.log_error(
			title="jarvis agent catalog: after_migrate sync failed",
			message=frappe.get_traceback(),
		)
	# R5-J8: an app install/uninstall runs a migrate, so this is the reconcile
	# point — re-mark every installation installable/not against the current site.
	# Independent try/except: a reconcile hiccup must never fail a migration and
	# must run even if the catalog sync above raised.
	try:
		rec = reconcile_installations()
		frappe.logger("jarvis").info(f"agent installability reconciled: {rec}")
	except Exception:
		frappe.log_error(
			title="jarvis agent catalog: after_migrate installability reconcile failed",
			message=frappe.get_traceback(),
		)
