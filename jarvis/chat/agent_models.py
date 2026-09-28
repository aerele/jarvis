"""Per-agent minimum model: eligibility, the tenant-wide choice row, and its gates.

A marketplace bundle declares ``min_model: {"tier": ...}`` and the admin catalog
serves an operator-curated ``capability_rank`` per model. Each agent runs on ONE
tenant-wide model (the container holds one delegate per slug), stored as a
``Jarvis Agent Model Choice`` row keyed by the agent.

Everything here is inert unless ``Jarvis Settings.enforce_agent_min_model`` is on:
with it off, no row is created, no push key is added and no run is gated.

Nothing in this module pushes or restarts the container. A change only calls
``agents_api._mark_catalog_dirty()``; the model reaches the fleet on the next
reviewer Apply, which stamps ``pushed_provider``/``pushed_model``.
"""

import hashlib
import json
import re

import frappe
from frappe import _

from jarvis.chat.agent_activity import log_activity
from jarvis.permissions import has_jarvis_admin_access, require_jarvis_admin, require_jarvis_user

CHOICE = "Jarvis Agent Model Choice"
LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
_SETTINGS = "Jarvis Settings"

# Mirrors admin provider_catalog.CAPABILITY_TIERS: rank = index + 1, 0 = uncurated.
TIERS = ("Standard", "Advanced", "Frontier")
MAX_FALLBACKS = 3
UNKNOWN = "unknown"
# States whose row pins the delegate (legacy is pushed unpinned; needs_model blocked).
_PINNED = ("auto", "chosen", "changed")
# Run model_source values that ran on a pinned delegate (pool_default / blank did not).
PINNED_SOURCES = ("choice", "fallback", "pending_apply")
# Same groups admin's agent_models._PROVIDER_ALIAS_GROUPS accepts as one provider.
_PROVIDER_ALIAS_GROUPS = (
	frozenset({"gemini", "google"}),
	frozenset({"moonshot", "kimi"}),
	frozenset({"anthropic", "anthropic_cli"}),
	frozenset({"openai_compat", "zai", "zai_coding"}),
)
# Admin's _MODEL_PROVIDER_RE: a provider outside it is sent blank (admin resolves by id).
_PROVIDER_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
# Fleet run-start refusals relayed through admin's 502 as a message prefix.
RUN_ERROR_TOKENS = (
	"delegate_blocked",
	"delegate_model_unresolved",
	"render_context_missing",
	"apply_in_progress",
)
PLATFORM_UPGRADE_PENDING = "platform_upgrade_pending"
_PLATFORM_NOTE = "Platform upgrade pending"
_REVALIDATE_JOB = "jarvis_agent_model_revalidate"
_FLAG_ON_JOB = "jarvis_agent_model_flag_on"


# --------------------------------------------------------------------------- #
# flag + requirement
# --------------------------------------------------------------------------- #
def is_enforced() -> bool:
	return bool(
		frappe.utils.cint(frappe.db.get_single_value(_SETTINGS, "enforce_agent_min_model", cache=False))
	)


def tier_rank(tier) -> int:
	try:
		return TIERS.index(tier) + 1
	except ValueError:
		return 0


def min_model(listing) -> dict | None:
	"""The listing's ``min_model`` dict (``{"tier": ...}``), or None when it declares none."""
	raw = (
		frappe.db.get_value(LISTING, listing, "min_model")
		if isinstance(listing, str)
		else listing.get("min_model")
	)
	if isinstance(raw, str):
		try:
			raw = json.loads(raw) if raw.strip() else None
		except ValueError:
			raw = None
	return raw if isinstance(raw, dict) and raw.get("tier") else None


def required_rank(listing) -> int:
	"""The rank a model needs for ``listing``. No requirement = any curated model; an
	unrecognised tier fails closed (nothing qualifies)."""
	mm = min_model(listing)
	if not mm:
		return 1
	return tier_rank(mm.get("tier")) or len(TIERS) + 1


def _tier_label(listing) -> str:
	mm = min_model(listing)
	return (mm or {}).get("tier") or TIERS[0]


def _tier_phrase(listing) -> str:
	"""``an Advanced-tier`` / ``a Standard-tier``."""
	tier = _tier_label(listing)
	return f"{'an' if tier[:1].lower() in 'aeiou' else 'a'} {tier}-tier"


# --------------------------------------------------------------------------- #
# eligibility
# --------------------------------------------------------------------------- #
def _provider_keys(*names) -> frozenset:
	ids = {(n or "").strip().lower() for n in names} - {""}
	for group in _PROVIDER_ALIAS_GROUPS:
		if ids & group:
			ids |= group
	return frozenset(ids)


def _catalog_index(catalog: list) -> list[tuple[frozenset, dict]]:
	"""Each catalog provider entry with every id a tenant row may carry for it: the
	provider id, the catalog id and the subscription upstream (``kimi`` for Moonshot)."""
	from jarvis.oauth.providers import agent_provider_for

	out = []
	for entry in catalog:
		if not isinstance(entry, dict):
			continue
		upstream = agent_provider_for(entry.get("subscription_label") or entry.get("label") or "")
		out.append((_provider_keys(entry.get("provider_id"), entry.get("catalog_id"), upstream), entry))
	return out


def _pooled_candidates(settings) -> list[dict]:
	"""Enabled pool rows in fleet order. ``provider`` is exactly what
	``build_pool_payload`` sends for the row. Behind the proxy a shared model id is
	served from its lowest-order row, so a later same-id row is shadowed; a Claude
	plan row is served natively and never collapses."""
	from jarvis.jarvis.pool_serialize import (
		_credential_type,
		_enabled_models,
		_field,
		_fleet_sort_key,
		_subscription_upstream,
		_wire_provider,
		compute_proxy_active,
		normalize_provider,
	)

	proxied = compute_proxy_active(settings)
	out, seen = [], set()
	for m in sorted(_enabled_models(settings), key=_fleet_sort_key):
		model = (_field(m, "model") or "").strip()
		if not model or "image" in model.lower():
			continue
		if _credential_type(m) == "subscription":
			key = _subscription_upstream(m)
			provider, lane = ("anthropic_cli", "plan") if key == "anthropic" else ("", "subscription")
		else:
			key = normalize_provider(_field(m, "provider"))
			provider, lane = _wire_provider(key), "api_key"
		ident = ("", model) if proxied and provider != "anthropic_cli" else (provider, model)
		if ident in seen:
			continue
		seen.add(ident)
		# An api key is its own provider's; only a subscription upstream needs aliases.
		out.append(
			{"provider": provider, "model": model, "lane": lane, "key": key, "exact": lane == "api_key"}
		)
	return out


def _direct_candidates(settings) -> list[dict]:
	"""A direct tenant's configured model only: the fleet renders nothing else for a
	direct or OAuth shape, so any other catalog model could never be pinned."""
	from jarvis.jarvis.pool_serialize import (
		_credential_type,
		_enabled_models,
		_field,
		_subscription_upstream,
		normalize_provider,
	)

	enabled = _enabled_models(settings)
	if enabled:
		m0 = enabled[0]
		sub = _credential_type(m0) == "subscription"
		key = _subscription_upstream(m0) if sub else normalize_provider(_field(m0, "provider"))
		own = (_field(m0, "model") or "").strip()
	else:
		sub = (settings.get("llm_auth_mode") or "api_key") in ("oauth", "subscription")
		key = normalize_provider(settings.get("llm_provider"))
		own = (settings.get("llm_model") or "").strip()
	if not key or not own or "image" in own.lower():
		return []
	provider = key if _PROVIDER_RE.fullmatch(key) else ""
	lane = "subscription" if sub else "api_key"
	return [{"provider": provider, "model": own, "lane": lane, "key": key, "exact": True}]


def _is_entry(entry: dict, key: str) -> bool:
	"""``key`` names this catalog entry itself (admin's direct allowlist: no aliases)."""
	return key in {(entry.get("provider_id") or "").lower(), (entry.get("catalog_id") or "").lower()}


def _price(value) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


def _enrich(c: dict, index) -> dict:
	"""Rank / label / cost of one candidate from the catalog (max rank across its rows)."""
	entries = [
		entry for keys, entry in index if (_is_entry(entry, c["key"]) if c.get("exact") else c["key"] in keys)
	]
	hits = [
		(entry, row)
		for entry in entries
		for row in entry.get("models") or []
		if row.get("model_id") == c["model"]
	]
	lane_tier = "api_key" if c["lane"] == "api_key" else "subscription"
	best = max(hits, key=lambda h: frappe.utils.cint(h[1].get("capability_rank")), default=None)
	own = next((h for h in hits if h[1].get("tier") == lane_tier), best)
	entry = own[0] if own else (entries[0] if entries else {})
	if c["lane"] == "api_key":
		p_in, p_out = (
			(_price(own[1].get(k)) for k in ("input_price_per_1m_usd", "output_price_per_1m_usd"))
			if own
			else (0, 0)
		)
		cost = f"${p_in:.2f} in / ${p_out:.2f} out per 1M tokens" if (p_in or p_out) else ""
	else:
		name = entry.get("subscription_label") or entry.get("label") or c["key"]
		cost = f"Uses your {name} subscription allowance"
	return {
		"provider": c["provider"],
		"model": c["model"],
		"label": (own[1].get("label") if own else "") or c["model"],
		"lane_hint": c["lane"],
		"capability_tier": (best[1].get("capability_tier") if best else "") or "",
		"capability_rank": frappe.utils.cint(best[1].get("capability_rank")) if best else 0,
		"cost_note": cost,
	}


def _pool_settings(fresh: bool = False):
	"""The settings doc eligibility reads (``fresh``: DB-loaded, for the fingerprint)."""
	return frappe.get_single(_SETTINGS) if fresh else frappe.get_cached_doc(_SETTINGS)


def model_pool(settings=None, catalog=None):
	"""Every model this tenant can run an agent on, best-first (rank desc, then pool
	order), or ``UNKNOWN`` when the catalog is empty/unreadable. Listing-independent,
	so a batch caller computes it once and filters per agent."""
	if catalog is None:
		from jarvis import admin_client

		catalog = admin_client.get_model_catalog()
	if not isinstance(catalog, list) or not catalog:
		return UNKNOWN
	from jarvis.jarvis.pool_serialize import compute_pool_mode

	settings = settings if settings is not None else _pool_settings()
	index = _catalog_index(catalog)
	cands = _pooled_candidates(settings) if compute_pool_mode(settings) else _direct_candidates(settings)
	ranked = [_enrich(c, index) for c in cands]
	# sort() is stable, so equal ranks keep pool order.
	ranked.sort(key=lambda r: -r["capability_rank"])
	return ranked


def eligible_models(listing, settings=None, catalog=None, *, pool=None):
	"""The models ``listing`` may run on, best-first, or ``UNKNOWN``. Uncurated
	(rank 0) is never eligible."""
	pool = pool if pool is not None else model_pool(settings, catalog)
	if pool == UNKNOWN:
		return UNKNOWN
	need = required_rank(listing)
	return [dict(c) for c in pool if c["capability_rank"] >= max(need, 1)]


def _ref(c) -> dict:
	return {"provider": (c.get("provider") or ""), "model": (c.get("model") or "")}


def _contains(eligible, ref) -> bool:
	return any(_ref(c) == _ref(ref) for c in eligible)


def _fallbacks_for(primary, eligible) -> list[dict]:
	out = []
	for c in eligible:
		ref = _ref(c)
		if ref != _ref(primary) and ref not in out:
			out.append(ref)
		if len(out) >= MAX_FALLBACKS:
			break
	return out


def _sync_marker(settings) -> str:
	"""Moves on every CONFIRMED LLM apply (pool or direct): admin may have refused a
	model only because it did not have the tenant's newest pool yet."""
	return f"{settings.get('llm_pool_synced_at') or ''}|{settings.get('llm_direct_synced_at') or ''}"


def refusal_scope(settings=None, catalog=None) -> dict:
	"""What an admin refusal is valid for: this pool + catalog, until the next sync."""
	settings = settings if settings is not None else _pool_settings(fresh=True)
	return {"fp": validation_fingerprint(settings, catalog), "sync": _sync_marker(settings)}


def _refusal(row) -> dict:
	raw = (row or {}).get("admin_rejected")
	try:
		rej = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
	except ValueError:
		rej = {}
	return rej if isinstance(rej, dict) and _refs(rej.get("refs")) else {}


def _active_refusal(row, scope: dict | None = None) -> list[dict]:
	"""The refs admin refused for this row, while the pool, the catalog and the last
	successful sync are all what they were when it refused them; else nothing."""
	rej = _refusal(row)
	if not rej:
		return []
	scope = scope or refusal_scope()
	if rej.get("fp") != scope["fp"] or rej.get("sync") != scope["sync"]:
		return []
	return _refs(rej["refs"])


def _usable(eligible, row, scope: dict | None = None):
	"""``eligible`` minus what admin refused for this agent's row (while in scope)."""
	if eligible == UNKNOWN or not row:
		return eligible
	refused = _active_refusal(row, scope)
	return [c for c in eligible if _ref(c) not in refused] if refused else eligible


def _refs(raw) -> list[dict]:
	if isinstance(raw, str):
		try:
			raw = json.loads(raw) if raw.strip() else []
		except ValueError:
			raw = []
	return [_ref(r) for r in raw or [] if isinstance(r, dict) and r.get("model")]


# --------------------------------------------------------------------------- #
# the choice row
# --------------------------------------------------------------------------- #
def _get_row(agent: str, for_update: bool = False):
	if not frappe.db.get_value(CHOICE, agent, "name", for_update=for_update):
		return None
	return frappe.get_doc(CHOICE, agent, for_update=for_update)


def _insert_if_absent(agent: str, values: dict) -> bool:
	"""Create the agent's row; a concurrent winner's row is left as it is. True iff created."""
	doc = frappe.get_doc({"doctype": CHOICE, "agent": agent, **values})
	try:
		doc.insert(ignore_permissions=True)
		return True
	except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
		frappe.clear_last_message()
		return False


def _upsert(agent: str, values: dict):
	"""Set ``values`` on the agent's row under its lock, creating it if absent. A missing
	key is inserted, never locked: FOR UPDATE on it gap-locks, and two concurrent first
	picks deadlock. An existing row is locked directly (insert-first would take a shared
	duplicate-key lock that two concurrent updates then both try to upgrade)."""
	doc = _get_row(agent, for_update=True) if frappe.db.exists(CHOICE, agent) else None
	if doc is None:
		if _insert_if_absent(agent, values):
			return frappe.get_doc(CHOICE, agent)
		doc = _get_row(agent, for_update=True)  # lost the insert race: update the winner
	doc.update(values)
	doc.save(ignore_permissions=True)
	return doc


def _mark_dirty() -> None:
	from jarvis.chat.agents_api import _mark_catalog_dirty

	_mark_catalog_dirty()


def _log_for_owners(agent: str, detail: str, actor: str | None = None) -> None:
	"""One ``model_changed`` activity row per installation, owned by its owner (the
	feed is owner-scoped, and the change applies to all of them). With no installation,
	a human ``actor``'s change is still logged, to the actor."""
	title = frappe.db.get_value(LISTING, agent, "title")
	installs = frappe.get_all(INSTALLATION, filters={"agent": agent}, fields=["name", "owner"])
	if not installs and actor:
		installs = [frappe._dict(name=None, owner=actor)]
	for inst in installs:
		log_activity(
			agent=agent,
			agent_title=title,
			installation=inst.name,
			action="model_changed",
			detail=detail,
			owner=inst.owner,
		)


def _auto_values(eligible, listing) -> dict:
	"""Best eligible as an ``auto`` pick, or ``needs_model`` when there is none."""
	if eligible:
		best = eligible[0]
		return {
			"state": "auto",
			**_ref(best),
			"fallbacks": json.dumps(_fallbacks_for(best, eligible)),
			"note": "",
		}
	return {"state": "needs_model", "fallbacks": json.dumps([]), "note": _no_model_message(listing)}


def _no_model_message(listing) -> str:
	return _(
		"This agent needs {0} model. None of your connected AI providers offer one yet — "
		"connect a provider or ask an admin."
	).format(_tier_phrase(listing))


def _desired_push(row, enforced: bool) -> tuple[str, str]:
	"""The (provider, model) the next Apply would pin; ("", "") when it pins nothing."""
	if not enforced or row.get("state") not in _PINNED:
		return ("", "")
	return (row.get("provider") or "", row.get("model") or "")


# --------------------------------------------------------------------------- #
# gates
# --------------------------------------------------------------------------- #
def _listing_or_404(agent: str):
	if not agent or not frappe.db.exists(LISTING, agent):
		frappe.throw(_("Agent not found."), frappe.DoesNotExistError)
	return frappe.get_doc(LISTING, agent)


def _read_gate(agent: str):
	"""Same visibility as ``agents_api.get_agent``, except a masked teaser reveals nothing."""
	from jarvis.chat.agents_api import _DISCOVERABLE_STATUSES, _user_allowed_for_agent

	listing = _listing_or_404(agent)
	me = frappe.session.user
	if frappe.db.exists(INSTALLATION, {"owner": me, "agent": listing.name}):
		return listing
	if (listing.operator_visibility or "available") != "available":
		frappe.throw(_("Agent not found."), frappe.DoesNotExistError)
	if not has_jarvis_admin_access(me) and (
		not _user_allowed_for_agent(listing, me) or listing.status not in _DISCOVERABLE_STATUSES
	):
		frappe.throw(_("You do not have access to this agent."), frappe.PermissionError)
	return listing


def _write_gate(agent: str):
	"""Whoever may install the agent may set its tenant-wide model."""
	from jarvis import catalogue_visibility
	from jarvis.chat.agents_api import _user_allowed_for_agent

	listing = _listing_or_404(agent)
	me = frappe.session.user
	if has_jarvis_admin_access(me):
		return listing
	if (
		not _user_allowed_for_agent(listing, me)
		or listing.status != "Published"
		or (listing.operator_visibility or "available") != "available"
		or catalogue_visibility.is_operator_governed(listing.name)
	):
		frappe.throw(_("You do not have access to this agent."), frappe.PermissionError)
	return listing


def _row_rejections(agent: str):
	return frappe.db.get_value(CHOICE, agent, ["admin_rejected"], as_dict=True)


def _require_enforced() -> None:
	if not is_enforced():
		frappe.throw(_("Per-agent models are not turned on for this workspace."))


def _catalog_unavailable() -> None:
	from jarvis.catalog_store import CATALOG_UNAVAILABLE_MESSAGE

	frappe.throw(_(CATALOG_UNAVAILABLE_MESSAGE))


# --------------------------------------------------------------------------- #
# SPA endpoints
# --------------------------------------------------------------------------- #
def _model_view(listing) -> dict:
	enforced = is_enforced()
	mm = min_model(listing)
	row = frappe.db.get_value(
		CHOICE,
		listing.name,
		[
			"state",
			"provider",
			"model",
			"fallbacks",
			"note",
			"changed_by",
			"changed_at",
			"pushed_provider",
			"pushed_model",
			"fleet_unresolved",
		],
		as_dict=True,
	)
	out = {
		"agent": listing.name,
		"enforced": 1 if enforced else 0,
		"min_model": mm,
		"required_tier": (mm or {}).get("tier"),
		"state": None,
		"choice": None,
		"fallbacks": [],
		"note": None,
		"changed_by": None,
		"changed_at": None,
		"pending_apply": 0,
		"fleet_unresolved": 0,
	}
	if not row:
		return out
	pushed = (row.pushed_provider or "", row.pushed_model or "")
	out.update(
		{
			"state": row.state,
			"fallbacks": _refs(row.fallbacks),
			"note": row.note or None,
			"changed_by": row.changed_by,
			"changed_at": str(row.changed_at) if row.changed_at else None,
			"pending_apply": 1 if _desired_push(row, enforced) != pushed else 0,
			"fleet_unresolved": frappe.utils.cint(row.fleet_unresolved),
		}
	)
	if row.model:
		choice = _ref(row)
		pool = model_pool() if enforced else UNKNOWN
		meta = next((c for c in pool if _ref(c) == choice), None) if pool != UNKNOWN else None
		out["choice"] = {**(meta or {}), **choice}
	return out


@frappe.whitelist()
@require_jarvis_user
def get_agent_model(agent: str) -> dict:
	"""The agent's tenant-wide model, its requirement and whether an Apply is pending."""
	return _model_view(_read_gate(agent))


@frappe.whitelist()
@require_jarvis_user
def get_eligible_models(agent: str) -> dict:
	"""The models the agent may run on here, best-first. ``catalog: unknown`` means
	the model catalog could not be read (never "no models exist")."""
	listing = _read_gate(agent)
	eligible = _usable(eligible_models(listing), _row_rejections(listing.name))
	return {
		"agent": listing.name,
		"required_tier": (min_model(listing) or {}).get("tier"),
		"catalog": UNKNOWN if eligible == UNKNOWN else "ok",
		"models": [] if eligible == UNKNOWN else eligible,
	}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def set_agent_model(agent: str, provider: str, model: str) -> dict:
	"""Pick the agent's tenant-wide model (applies to everyone using it). Validated
	against the live eligible set; marks the catalog dirty, never pushes."""
	_require_enforced()
	listing = _write_gate(agent)
	ref = {"provider": (provider or "").strip(), "model": (model or "").strip()}
	eligible = _usable(eligible_models(listing), _row_rejections(listing.name))
	if eligible == UNKNOWN:
		_catalog_unavailable()
	if not _contains(eligible, ref):
		frappe.throw(_("That model doesn't meet this agent's requirement here. Pick one from the list."))
	me = frappe.session.user
	_upsert(
		listing.name,
		{
			"state": "chosen",
			**ref,
			"fallbacks": json.dumps(_fallbacks_for(ref, eligible)),
			"note": "",
			"changed_by": me,
			"changed_at": frappe.utils.now(),
			"fleet_unresolved": 0,
		},
	)
	_log_for_owners(listing.name, f"model set to {ref['model']} by {me}", actor=me)
	_mark_dirty()
	frappe.db.commit()
	return _model_view(listing)


@frappe.whitelist(methods=["POST"])
def reset_agent_model(agent: str) -> dict:
	"""Jarvis Admin: hand the choice back to Jarvis (best eligible, or needs_model)."""
	require_jarvis_admin()
	_require_enforced()
	listing = _listing_or_404(agent)
	eligible = eligible_models(listing)  # an admin reset also forgets a platform refusal
	if eligible == UNKNOWN:
		_catalog_unavailable()
	me = frappe.session.user
	values = _auto_values(eligible, listing)
	_upsert(
		listing.name,
		{
			**values,
			"changed_by": me,
			"changed_at": frappe.utils.now(),
			"fleet_unresolved": 0,
			"admin_rejected": None,
		},
	)
	_log_for_owners(
		listing.name, f"model reset to {values.get('model') or 'none available'} by {me}", actor=me
	)
	_mark_dirty()
	frappe.db.commit()
	return _model_view(listing)


# --------------------------------------------------------------------------- #
# install / uninstall / flag
# --------------------------------------------------------------------------- #
def plan_install(listing) -> dict | None:
	"""Before an install inserts: the row to create afterwards, or None. Throws when
	a first enforced install has no eligible model."""
	if not is_enforced() or not min_model(listing):
		return None
	# REPEATABLE-READ discipline: install_agent has written nothing yet, so end the
	# snapshot and let the existence reads below see a concurrent last-uninstall.
	frappe.db.commit()
	# A plain read: FOR UPDATE on a missing key would gap-lock and deadlock two
	# concurrent first installs; _insert_if_absent's unique name decides instead.
	if frappe.db.exists(CHOICE, listing.name):
		return None
	if frappe.db.exists(INSTALLATION, {"agent": listing.name}):
		return {"state": "legacy"}
	eligible = eligible_models(listing)
	if eligible == UNKNOWN:
		_catalog_unavailable()
	if not eligible:
		frappe.throw(_no_model_message(listing), title=_("No eligible model"))
	return {
		**_auto_values(eligible, listing),
		"changed_by": frappe.session.user,
		"changed_at": frappe.utils.now(),
	}


def apply_install_plan(agent: str, plan: dict | None) -> None:
	if not plan:
		return
	try:
		created = _insert_if_absent(agent, plan)
	except frappe.QueryDeadlockError:
		# InnoDB rolled the whole transaction back (the install row too): retryable.
		frappe.throw(_("Another change to this agent landed at the same moment. Please try again."))
	if created and plan.get("state") != "legacy":
		_mark_dirty()  # the allowed-listings leg now ships this agent pinned


def on_uninstalled(agent: str) -> None:
	"""After the LAST installation of ``agent`` is gone, drop its row. Runs after the
	uninstall committed; the row lock serializes against a concurrent first install."""
	try:
		row = frappe.db.get_value(CHOICE, agent, ["name", "state"], as_dict=True, for_update=True)
		if not row or frappe.db.exists(INSTALLATION, {"agent": agent}):
			frappe.db.commit()
			return
		frappe.delete_doc(CHOICE, agent, ignore_permissions=True, force=True)
		if row.state != "legacy" and is_enforced():
			_mark_dirty()
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			title=f"Jarvis: agent model row cleanup failed: {agent}", message=frappe.get_traceback()
		)


def _has_pinning_rows() -> bool:
	return bool(frappe.db.exists(CHOICE, {"state": ["!=", "legacy"]}))


def on_enforcement_disabled() -> None:
	"""Flag ON -> OFF: the next Apply drops every model key (admin clears its pins),
	so a pinned or blocked row makes that Apply pending."""
	if _has_pinning_rows():
		_mark_dirty()


def on_enforcement_enabled() -> None:
	"""Flag OFF -> ON: every agent already installed keeps today's unpinned model
	(``legacy``) until someone picks one; the catalog is refreshed because the
	last-good snapshot may predate capability ranks. Rows kept from an earlier ON
	period pin again on the next Apply, so that Apply is pending."""
	for agent in frappe.get_all(INSTALLATION, fields=["agent"], distinct=True, pluck="agent"):
		try:
			_insert_if_absent(agent, {"state": "legacy"})
		except Exception:
			frappe.log_error(
				title=f"Jarvis: legacy model row failed: {agent}", message=frappe.get_traceback()
			)
	if _has_pinning_rows():
		_mark_dirty()
	frappe.enqueue(
		"jarvis.chat.agent_models.refresh_catalog_then_revalidate",
		queue="short",
		job_id=_FLAG_ON_JOB,
		deduplicate=True,
		enqueue_after_commit=True,
	)


def refresh_catalog_then_revalidate() -> None:
	from jarvis.catalog_store import MODELS

	MODELS.refresh(force=True)
	revalidate_all()


# --------------------------------------------------------------------------- #
# push
# --------------------------------------------------------------------------- #
def push_choices() -> dict:
	"""``{agent: row}`` the push pins or blocks; ``{}`` when the flag is off (the
	payload then carries no model keys at all)."""
	if not is_enforced():
		return {}
	rows = frappe.get_all(
		CHOICE,
		filters={"state": ["!=", "legacy"]},
		fields=["agent", "state", "provider", "model", "fallbacks"],
	)
	return {r.agent: r for r in rows}


def model_keys(row) -> dict:
	"""The three push keys for one row (pure read of what validation stored)."""
	if row.get("state") not in _PINNED or not row.get("model"):
		return {"model_fallbacks": [], "model_state": "blocked"}
	primary = _ref(row)
	fallbacks = []
	for ref in _refs(row.get("fallbacks")):
		if ref != primary and ref not in fallbacks:
			fallbacks.append(ref)
	return {"model_choice": primary, "model_fallbacks": fallbacks[:MAX_FALLBACKS], "model_state": "ok"}


def _admin_report(response) -> dict | None:
	"""Admin's ``model_choices`` report on an Apply; None from an admin without the
	model-choice contract (it ignored the model keys)."""
	data = response if isinstance(response, dict) else {}
	report = data.get("model_choices")
	if report is None and isinstance(data.get("data"), dict):
		report = data["data"].get("model_choices")
	return report if isinstance(report, dict) else None


def _admin_verdict(report) -> tuple[dict, set]:
	"""({slug: refs admin refused}, {slugs it blocked}) from its report. Refs come back
	as the ``provider/model`` labels we sent."""
	if not isinstance(report, dict):
		return {}, set()
	refused = {}
	for item in report.get("rejected") or []:
		if isinstance(item, dict) and item.get("slug"):
			refs = []
			for label in item.get("models") or []:
				provider, _sep, model = str(label).partition("/")
				if model:
					refs.append({"provider": provider, "model": model})
			refused[item["slug"]] = refs
	return refused, {s for s in report.get("blocked") or [] if isinstance(s, str)}


def _delivered(entry: dict, refused: list, blocked: bool) -> tuple[str, str]:
	"""The pair the fleet holds: admin swaps a refused primary for the first usable
	fallback, and blocks the delegate when none is left."""
	if blocked or not entry.get("model_choice"):
		return ("", "")
	sent = [_ref(entry["model_choice"]), *_refs(entry.get("model_fallbacks"))]
	usable = [r for r in sent if r not in refused]
	return (usable[0]["provider"], usable[0]["model"]) if usable else ("", "")


def _take_admin_refusal(doc, entry: dict, refused: list, delivered: tuple, scope: dict) -> None:
	"""Align the row with what admin let through, and remember what it refused so no
	re-validation picks it again while the pool, catalog and last sync hold."""
	known = _active_refusal(doc.as_dict(), scope)
	refs = known + [r for r in refused if r not in known]
	doc.admin_rejected = json.dumps({**scope, "refs": refs})
	sent = _ref(entry.get("model_choice") or {})
	if doc.state not in _PINNED or _ref(doc.as_dict()) != sent:
		return  # the row moved after the payload was built: only record the refusal
	names = ", ".join(r["model"] for r in refused)
	kept = [
		r
		for r in _refs(entry.get("model_fallbacks"))
		if r not in refused and (r["provider"], r["model"]) != delivered
	]
	if delivered == ("", ""):
		doc.update(
			{
				"state": "needs_model",
				"fallbacks": json.dumps([]),
				"note": f"The platform refused {names} for this workspace. Choose another model.",
			}
		)
	elif delivered != (doc.provider or "", doc.model or ""):
		doc.update(
			{
				"state": "changed",
				"provider": delivered[0],
				"model": delivered[1],
				"fallbacks": json.dumps(kept),
				"note": f"The platform refused {names} for this workspace; using {delivered[1]}.",
			}
		)
	else:
		doc.fallbacks = json.dumps(kept)
		return
	doc.changed_by, doc.changed_at = None, frappe.utils.now()
	doc.flags.model_moved = True


def push_scope(payload: list[dict]) -> dict | None:
	"""The refusal scope as it stands BEFORE a push that carries model keys (None
	otherwise). Taken first so a pool apply confirming while admin checks the
	choices cannot stamp a refusal with the sync that should expire it."""
	if not any("model_state" in e for e in payload or []):
		return None
	try:
		return refusal_scope()
	except Exception:
		frappe.log_error(title="Jarvis: agent model push scope failed", message=frappe.get_traceback())
		return None


def stamp_pushed(payload: list[dict], response=None, scope: dict | None = None) -> bool:
	"""After a successful push: record per row what the fleet now holds. A row whose
	agent was not in the payload keeps its last stamp. ``scope`` is ``push_scope``
	taken before the push. Each row commits on its own; False when any row was left
	unstamped (the catalog then stays dirty)."""
	from jarvis.chat.agent_catalog import AGENT_PREFIX

	report = _admin_report(response)
	refused_by_slug, blocked = _admin_verdict(report)
	if (refused_by_slug or blocked) and scope is None:
		scope = refusal_scope()
	sent = {(e.get("slug") or "")[len(AGENT_PREFIX) :]: e for e in payload or []}
	complete = True
	for name in frappe.get_all(CHOICE, pluck="name"):
		entry = sent.get(name)
		if entry is None:
			continue
		try:
			if "model_state" in entry and report is None:
				_note_unconfirmed(name, entry)
				complete = False
			else:
				_stamp_row(
					name, entry, refused_by_slug.get(entry["slug"], []), entry["slug"] in blocked, scope
				)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			complete = False
			frappe.log_error(
				title=f"Jarvis: agent model stamp failed: {name}", message=frappe.get_traceback()
			)
	return complete


def _stamp_row(name: str, entry: dict, refused: list, is_blocked: bool, scope: dict | None) -> None:
	want = _delivered(entry, refused, is_blocked)
	doc = _get_row(name, for_update=True)
	if doc is None:
		return  # the last uninstall removed it mid-push
	if not (refused or is_blocked) and (doc.pushed_provider or "", doc.pushed_model or "") == want:
		if doc.note == _PLATFORM_NOTE:  # a successful Apply outran the note it left behind
			doc.note = ""
			doc.save(ignore_permissions=True)
		return
	if refused or is_blocked:
		_take_admin_refusal(doc, entry, refused, want, scope)
	elif doc.note == _PLATFORM_NOTE:
		doc.note = ""  # an admin with the model-choice contract took it
	doc.pushed_provider, doc.pushed_model = want
	doc.save(ignore_permissions=True)
	if doc.flags.get("model_moved"):
		_log_for_owners(name, doc.note)


def _note_unconfirmed(name: str, entry: dict) -> None:
	"""An admin without the model-choice contract ignored the keys: nothing was pinned,
	so leave pushed_* as they were and say why the Apply stays pending."""
	if entry.get("model_state") != "ok":
		return
	doc = _get_row(name, for_update=True)
	if doc and doc.state in _PINNED and doc.note != _PLATFORM_NOTE:
		doc.note = _PLATFORM_NOTE
		doc.save(ignore_permissions=True)


def is_platform_upgrade_pending(exc) -> bool:
	return getattr(exc, "code", "") == PLATFORM_UPGRADE_PENDING or PLATFORM_UPGRADE_PENDING in str(exc)


# --------------------------------------------------------------------------- #
# run gate
# --------------------------------------------------------------------------- #
def _blocked_message(listing) -> str:
	return _("This agent needs {0} model and none is available. Choose a model on the agent page.").format(
		_tier_phrase(listing)
	)


def _source(row) -> str:
	if not row.get("pushed_model"):
		return "pool_default"  # picked but never pushed: fleet holds no pin, renders pool default
	if (row.get("provider") or "", row.get("model") or "") != (
		row.get("pushed_provider") or "",
		row.get("pushed_model") or "",
	):
		return "pending_apply"
	return "fallback" if row.get("state") == "changed" else "choice"


def _degrade(agent: str, eligible) -> dict:
	"""The row's model stopped being eligible: move it to a stored fallback that still
	is (``changed``), else block it (``needs_model``). Committed, so a refused run
	cannot roll the verdict back."""
	doc = _get_row(agent, for_update=True)
	if doc is None or doc.state not in _PINNED or _contains(eligible, doc.as_dict()):
		frappe.db.commit()
		return doc.as_dict() if doc else {}
	old = doc.model
	fb = next((r for r in _refs(doc.fallbacks) if _contains(eligible, r)), None)
	if fb:
		doc.update(
			{
				"state": "changed",
				**fb,
				"fallbacks": json.dumps(_fallbacks_for(fb, eligible)),
				"note": f"{old} no longer meets this agent's requirement here; switched to {fb['model']}.",
			}
		)
	else:
		doc.update({"state": "needs_model", "fallbacks": json.dumps([]), "note": _no_model_message(agent)})
	doc.changed_by = None
	doc.changed_at = frappe.utils.now()
	doc.save(ignore_permissions=True)
	_log_for_owners(agent, doc.note)
	_mark_dirty()
	frappe.db.commit()
	return doc.as_dict()


def gate_run(agent: str) -> dict:
	"""May a run of ``agent`` start? ``{"ok", "model_source", "message"}``. The chosen
	model is re-checked LIVE; an unreadable catalog changes nothing."""
	if not is_enforced():
		return {"ok": True, "model_source": None}
	row = frappe.db.get_value(
		CHOICE,
		agent,
		["state", "provider", "model", "fallbacks", "pushed_provider", "pushed_model", "admin_rejected"],
		as_dict=True,
	)
	if not row or row.state == "legacy":
		return {"ok": True, "model_source": "pool_default"}
	if row.state in _PINNED:
		eligible = _usable(eligible_models(agent), row)
		if eligible != UNKNOWN and not _contains(eligible, row):
			row = frappe._dict(_degrade(agent, eligible))
			if not row:  # the last uninstall removed it meanwhile
				return {"ok": True, "model_source": "pool_default"}
	if row.get("state") not in _PINNED:
		return {"ok": False, "model_source": None, "message": _blocked_message(agent)}
	return {"ok": True, "model_source": _source(row)}


class ModelRunRefused(frappe.ValidationError):
	"""A typed fleet run-start refusal; ``token`` is one of ``RUN_ERROR_TOKENS``."""

	def __init__(self, token: str, *args):
		super().__init__(*args)
		self.token = token


def run_error_token(exc) -> str | None:
	"""The typed fleet refusal in ``exc``. ``apply_in_progress`` is recognised with the
	flag off too (any container can be mid-apply); the rest only when enforced."""
	text = str(exc)
	token = next((t for t in RUN_ERROR_TOKENS if f"{t}:" in text), None)
	if token == "apply_in_progress" or (token and is_enforced()):
		return token
	return None


def handle_run_error(agent: str, token: str) -> str:
	"""Record what a typed fleet refusal says about the agent's row; returns the run's
	user-facing error (<= 140 chars)."""
	if token == "apply_in_progress":
		return _("Your workspace is applying changes right now. Try again in a minute.")
	if token == "render_context_missing":
		_note_row(agent, _PLATFORM_NOTE)
		return _(
			"Platform upgrade pending: this agent can't use its chosen model until your workspace is upgraded."
		)
	if token == "delegate_blocked":
		doc = _get_row(agent, for_update=True) if is_enforced() else None
		if (
			doc
			and doc.state in _PINNED
			and _desired_push(doc, True) == (doc.pushed_provider or "", doc.pushed_model or "")
		):
			doc.update(
				{"state": "needs_model", "fallbacks": json.dumps([]), "note": _no_model_message(agent)}
			)
			doc.changed_by, doc.changed_at = None, frappe.utils.now()
			doc.save(ignore_permissions=True)
			_log_for_owners(agent, doc.note)
			_mark_dirty()
		frappe.db.commit()
		return _("This agent has no usable model. Choose one on the agent page, then apply catalog changes.")
	# delegate_model_unresolved: the fleet could not place the pushed model in its render.
	doc = _get_row(agent, for_update=True) if is_enforced() else None
	state = None
	if doc and doc.state in _PINNED:
		eligible = _usable(eligible_models(agent), doc.as_dict())
		if eligible == UNKNOWN or _contains(eligible, doc.as_dict()):
			doc.fleet_unresolved = 1
			doc.note = _(
				"The platform could not load {0} in the current setup. Apply catalog changes."
			).format(doc.model)
			doc.save(ignore_permissions=True)
		else:
			state = _degrade(agent, eligible).get("state")
	frappe.db.commit()
	if state == "needs_model":
		return _blocked_message(agent)
	return _(
		"This agent's model could not be loaded on your workspace. Apply catalog changes, then run it again."
	)


def _note_row(agent: str, note: str) -> None:
	doc = _get_row(agent, for_update=True) if is_enforced() else None
	if doc and doc.note != note:
		doc.note = note
		doc.save(ignore_permissions=True)
	frappe.db.commit()


def on_run_started(run: str, agent: str, response) -> None:
	"""A 202 from the fleet: keep the rendered ref, clear a stale fleet_unresolved."""
	if not is_enforced():
		return
	from jarvis.chat.agent_scheduler import _polled_state

	rendered = _polled_state(response).get("model_rendered")
	if isinstance(rendered, str) and rendered.strip():
		frappe.db.set_value(RUN, run, "model_rendered", rendered.strip()[:140], update_modified=False)
	doc = _get_row(agent, for_update=True)
	if doc and (doc.fleet_unresolved or doc.note == _PLATFORM_NOTE):
		doc.fleet_unresolved = 0
		doc.note = ""
		doc.save(ignore_permissions=True)


def _model_part(ref: str) -> str:
	return ref.split("/", 1)[1] if "/" in ref else ref


def _same_model(used: str, rendered: str) -> bool:
	rendered = _model_part(rendered)
	return used == rendered or used.rsplit("/", 1)[-1] == rendered.rsplit("/", 1)[-1]


def record_model_used(run: str, state: dict, *, pool=None) -> None:
	"""Store the model the runtime reported; flag a pinned run when it is not what was
	rendered or not eligible for the agent (an unpinned run renders the pool, never a
	floor-checked model). ``pool``: a batch caller's ``model_pool()``."""
	used = (state or {}).get("model_used")
	if not is_enforced() or not isinstance(used, dict) or not (used.get("model") or "").strip():
		return
	model = used["model"].strip()
	provider = (used.get("provider") or "").strip()
	cur = frappe.db.get_value(RUN, run, ["agent", "model_rendered", "model_source"], as_dict=True) or {}
	rendered = (cur.get("model_rendered") or "").strip()
	pinned = cur.get("model_source") in PINNED_SOURCES
	below = pinned and bool(rendered) and not _same_model(model, rendered)
	if pinned and not below and cur.get("agent") and min_model(cur["agent"]):
		eligible = eligible_models(cur["agent"], pool=pool)
		below = eligible != UNKNOWN and not any(_same_model(model, c["model"]) for c in eligible)
	frappe.db.set_value(
		RUN,
		run,
		{
			"model_used": (f"{provider}/{model}" if provider else model)[:140],
			"model_below_min": 1 if below else 0,
		},
		update_modified=False,
	)


# --------------------------------------------------------------------------- #
# re-validation (never pushes, never restarts)
# --------------------------------------------------------------------------- #
def validation_fingerprint(settings=None, catalog=None) -> str:
	"""What eligibility depends on: the pool snapshot, the legacy direct fields (a
	direct tenant has no rows) and the model catalog. Stored as
	``last_validated_pool_fp``; it also scopes an admin refusal."""
	from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import JarvisSettings

	if catalog is None:
		from jarvis import admin_client

		catalog = admin_client.get_model_catalog()
	settings = settings if settings is not None else _pool_settings(fresh=True)
	snap = (
		JarvisSettings._pool_state_snapshot(settings),
		settings.get("llm_provider") or "",
		settings.get("llm_model") or "",
		settings.get("llm_auth_mode") or "",
		hashlib.sha256(json.dumps(catalog or [], sort_keys=True, default=str).encode()).hexdigest(),
	)
	return JarvisSettings._llm_apply_fingerprint(snap)


def check_pool_fingerprint() -> None:
	"""*/5 (reconcile_pending_llm_sync): re-validate when the pool or catalog moved
	(also catches a catalog change a running re-validation's dedupe swallowed)."""
	if not is_enforced():
		return
	settings = _pool_settings(fresh=True)
	fp = validation_fingerprint(settings)
	if fp != (settings.get("last_validated_pool_fp") or "") or _stale_refusals(
		{"fp": fp, "sync": _sync_marker(settings)}
	):
		enqueue_revalidation()


def _stale_refusals(scope: dict) -> bool:
	"""A recorded refusal a later sync (or pool/catalog change) has expired."""
	rows = frappe.get_all(CHOICE, filters={"admin_rejected": ["is", "set"]}, fields=["admin_rejected"])
	return any(_refusal(r) and not _active_refusal(r, scope) for r in rows)


def on_model_catalog_changed() -> None:
	enqueue_revalidation()


def enqueue_revalidation() -> None:
	if not is_enforced():
		return
	inline = bool(frappe.flags.in_test)
	frappe.enqueue(
		"jarvis.chat.agent_models.revalidate_all",
		queue="short",
		job_id=_REVALIDATE_JOB,
		deduplicate=True,
		enqueue_after_commit=not inline,
		now=inline,
	)


def _next_eligible(doc, eligible):
	"""A stored fallback that is still eligible, else the best eligible model."""
	return next((r for r in _refs(doc.fallbacks) if _contains(eligible, r)), None) or (
		_ref(eligible[0]) if eligible else None
	)


def _revalidated(doc, eligible) -> dict:
	"""The row's next (state, provider, model, fallbacks, note) against ``eligible``."""
	state, cur = doc.state, _ref(doc.as_dict())
	ok = bool(doc.model) and _contains(eligible, cur)
	rank = {(c["provider"], c["model"]): c["capability_rank"] for c in eligible}
	if state in ("chosen", "changed") and ok:
		target = cur
	elif state == "auto" and ok:
		best = _ref(eligible[0])
		better = rank[(best["provider"], best["model"])] > rank[(cur["provider"], cur["model"])]
		target = best if better else cur
	elif state in ("needs_model", "auto"):
		target, state = (_ref(eligible[0]), "auto") if eligible else (None, "needs_model")
	else:  # chosen / changed and no longer eligible
		target = _next_eligible(doc, eligible)
		state = "changed" if target else "needs_model"
	if target is None:
		return {"state": "needs_model", "fallbacks": json.dumps([]), "note": _no_model_message(doc.agent)}
	values = {"state": state, **target, "fallbacks": json.dumps(_fallbacks_for(target, eligible))}
	if target != cur or state != doc.state:
		values["note"] = (
			f"{doc.model} no longer meets this agent's requirement here; switched to {target['model']}."
			if doc.model and state == "changed"
			else ""
		)
	return values


def _revalidate_one(agent: str, pool, scope: dict) -> bool:
	doc = _get_row(agent, for_update=True)
	if doc is None:
		return False
	expired = bool(_refusal(doc.as_dict())) and not _active_refusal(doc.as_dict(), scope)
	if expired:
		doc.admin_rejected = None  # the platform may accept it now: offer it again
	listing = frappe.db.get_value(LISTING, agent, ["name", "min_model"], as_dict=True)
	if doc.state == "legacy" or not listing:
		if expired:
			doc.save(ignore_permissions=True)
		return False
	values = _revalidated(doc, _usable(eligible_models(listing, pool=pool), doc.as_dict(), scope))
	moved = (values["state"], values.get("model", doc.model)) != (doc.state, doc.model)
	if all((doc.get(k) or "") == (v or "") for k, v in values.items()):
		if expired:
			doc.save(ignore_permissions=True)
		return False
	doc.update(values)
	if moved:
		doc.changed_by, doc.changed_at = None, frappe.utils.now()
	doc.save(ignore_permissions=True)
	if moved:
		_log_for_owners(agent, doc.note or f"model set to {doc.model} (best available)")
	return True


def revalidate_all() -> dict:
	"""Re-check every enforced row against the current pool + catalog. Per-row
	isolated; a still-eligible ``chosen`` is never overwritten; ``legacy`` untouched;
	an empty/unreadable catalog is a no-op. A change only marks the catalog dirty."""
	if not is_enforced():
		return {"skipped": "off"}
	from jarvis import admin_client

	settings = _pool_settings(fresh=True)
	catalog = admin_client.get_model_catalog()
	scope = refusal_scope(settings, catalog)
	pool = model_pool(settings=settings, catalog=catalog)
	if pool == UNKNOWN:
		return {"skipped": UNKNOWN}
	changed = failed = 0
	rows = frappe.get_all(
		CHOICE, or_filters=[["state", "!=", "legacy"], ["admin_rejected", "is", "set"]], pluck="name"
	)
	for agent in rows:
		try:
			changed += 1 if _revalidate_one(agent, pool, scope) else 0
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			failed += 1
			frappe.log_error(
				title=f"Jarvis: agent model re-validation failed: {agent}", message=frappe.get_traceback()
			)
	if changed:
		_mark_dirty()
	if not failed:
		frappe.db.set_single_value(_SETTINGS, "last_validated_pool_fp", scope["fp"])
	frappe.db.commit()
	return {"changed": changed, "failed": failed}
