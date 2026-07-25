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
	"""Collect the ENABLED installed agents into the fleet push payload.

	Every entry is a body-free DELEGATE ENABLEMENT SIGNAL (A2): ``{slug,
	delivery:"delegate", tools_allow, model, timeout_s, nature}`` where ``slug``
	is ``agent-<agent_slug>``. The proprietary SKILL never transits the customer
	bench; admin looks the body up from the private bundle store keyed by slug and
	pushes it to fleet (Phase 2C). ``tools_allow``/``timeout_s``/``nature``/``model``
	echo the BUNDLED registry (metadata, not IP) so admin can render the delegate.

	Bench-global by design (one bench == one customer == one container), so all
	enabled installs on the site are pushed; ``owner`` is accepted only to scope
	tests. An empty list is a valid "remove all agent skills" reconcile.

	RBAC (defense in depth): an enabled install whose OWNER's roles no longer
	permit the agent is EXCLUDED from the push — the scheduler / run-now gates
	already refuse to run it, but its enablement signal must not reach the
	container either.

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
		fields=["agent", "owner", "installable"],
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
		listing = frappe.db.get_value(
			LISTING,
			row.agent,
			["agent_slug", "description", "status"],
			as_dict=True,
		)
		if not listing or listing.status != "Published":
			continue
		if not _user_allowed_for_agent(row.agent, row.owner):
			continue

		# Every gate has passed, so this agent is emitted and no later row for it can
		# change that: mark it seen. Collapsing the rows is safe because the entry
		# below is derived purely from the listing row plus the bundled registry —
		# it carries NO per-install data — so every row for an agent would yield a
		# byte-identical entry.
		seen_agents.add(row.agent)
		slug = f"{AGENT_PREFIX}{listing.agent_slug}"

		# A2 enablement signal — body-free. Admin resolves the SKILL from the
		# private bundle store by slug (Phase 2C). The body NEVER leaves it.
		meta = reg_by_slug.get(listing.agent_slug) or {}
		payload.append(
			{
				"slug": slug,
				"delivery": "delegate",
				"tools_allow": meta.get("tools_allow") or [],
				"model": meta.get("model") or None,
				"timeout_s": meta.get("timeout_s"),
				"nature": (meta.get("nature") or "").strip().lower(),
			}
		)
	return payload


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
