"""Health of the chat-subscription sign-ins (OpenAI, Anthropic, xAI Grok, Kimi).

One map on ``Jarvis Settings.subscription_health``, keyed by ``account_ref``, with three writers:

* admin's heartbeat reply (``apply_admin_health``): ``poll`` entries, governed by admin;
* this site's own failed chat turns (``record_chat_failure``): ``chat`` entries, cleared by a
  later successful turn (``record_chat_success``) and never by admin's reply;
* a completed sign-in (``record_signin_complete``): clears that upstream's entries.

Pending transitions are queued in Redis and ride the next heartbeat to admin
(``drain_signals``). Nothing here sends a completion or raises into a caller that is serving a
chat turn or the heartbeat.

A lone OpenAI subscription served by openclaw directly (empty ``models[]``, flat ``llm_*``
fields) has no ``account_ref``; both planes key it ``direct:openai``.

No ``frappe.db.commit()``: the callers' own transaction (scheduler job, turn worker) commits.
"""

from __future__ import annotations

import json
import time
from typing import NamedTuple

import frappe

from jarvis.chat.error_taxonomy import SUBSCRIPTION_LABELS as LABELS

SETTINGS = "Jarvis Settings"
FIELD = "subscription_health"
EVENT = "jarvis:subscription_health"
# Under the ``jarvis:subscription_`` prefix in hooks.py persistent_cache_keys: frappe.clear_cache()
# (pump's watchdog triggers one) would otherwise wipe pending signals before a heartbeat sends them.
SIGNAL_KEY = "jarvis:subscription_signals"
# Per-upstream "cleared at" epoch (same persistent prefix). Admin builds its reply before it sees our
# ok signal, so the next reply can still list a chat entry we just cleared; entries no later than this
# epoch are stale.
CLEARED_KEY_PREFIX = "jarvis:subscription_cleared:"
_LOG_KEY = "jarvis:subscription_log_hour"
CODE_EXPIRED = "subscription-expired"
CODE_OK = "ok"
MAX_SIGNALS = 8
_QUEUE_CAP = 50
DIRECT_REF = "direct:openai"
_DIRECT_PROVIDER = "OpenAI"
_SOURCES = ("poll", "chat")


# ---- storage seams (tests patch these) -------------------------------------------------


def _has_field() -> bool:
	"""False on a site where this code is deployed but ``bench migrate`` has not added the field yet."""
	try:
		return bool(frappe.get_meta(SETTINGS).has_field(FIELD))
	except Exception:
		return False


def _read_stored() -> str:
	if not _has_field():
		return ""
	return frappe.db.get_single_value(SETTINGS, FIELD, cache=False) or ""


def _write_stored(text: str) -> None:
	if not _has_field():
		return
	frappe.db.set_single_value(SETTINGS, FIELD, text, update_modified=False)


def _publish_changed() -> None:
	"""No ``user=``: every connected socket gets it and the SPA filters by role."""
	try:
		frappe.publish_realtime(EVENT, {"changed": True}, after_commit=True)
	except Exception:
		_log_throttled()


def _settings():
	return frappe.get_cached_doc(SETTINGS)


def _log_throttled() -> None:
	"""Log a fault in this module at most once an hour (the heartbeat's throttled logger, own key)."""
	from jarvis.chat.heartbeat import _log_failure_throttled

	_log_failure_throttled(_LOG_KEY, "jarvis.subscription_health failed")


# ---- which accounts exist right now ------------------------------------------------------


def _live_accounts() -> dict[str, dict]:
	"""``{account_ref: {upstream, label, email}}`` for every connected chat-subscription account:
	the enabled pool subscription rows' accounts, or, in direct mode, the single ``direct:openai``
	account. Direct mode is the predicate ``get_direct_subscription_status`` uses, and the flat
	provider must also be OpenAI (the only direct upstream admin tracks)."""
	from jarvis.jarvis.pool_serialize import _credential_type, _enabled_models, _field, _model_accounts
	from jarvis.oauth.api import _is_direct_subscription
	from jarvis.oauth.providers import is_oauth_provider

	settings = _settings()
	live: dict[str, dict] = {}
	for model in _enabled_models(settings):
		if _credential_type(model) != "subscription":
			continue
		for account in _model_accounts(model):
			ref = (_field(account, "account_ref") or "").strip()
			upstream = (_field(account, "upstream") or "").strip().lower()
			if not ref or upstream not in LABELS:
				continue
			label = (_field(account, "label") or "").strip()
			email = (_field(account, "account_email") or "").strip() or (label if "@" in label else "")
			live[ref] = {"upstream": upstream, "label": LABELS[upstream], "email": email}
	if live:
		return live
	provider = settings.get("llm_provider") or ""
	if provider == _DIRECT_PROVIDER and _is_direct_subscription(
		settings.get("llm_auth_mode"),
		bool(settings.get("models")),
		bool(settings.get("proxy_active")),
		is_oauth_provider(provider),
	):
		return {
			DIRECT_REF: {
				"upstream": "openai",
				"label": LABELS["openai"],
				"email": settings.get("llm_oauth_account_email") or "",
			}
		}
	return {}


class _PoolRow(NamedTuple):
	"""One enabled pool row with its account refs read ONCE (each read decrypts a Password field)."""

	model: object
	model_id: str
	is_subscription: bool
	upstream: str  # a subscription row's upstream, else ""
	refs: tuple  # account refs in account order, blank and repeated ones dropped


def _enabled_rows() -> list[_PoolRow]:
	"""Every enabled row in ``_enabled_models`` order, one account read per subscription row."""
	from jarvis.jarvis.pool_serialize import (
		_credential_type,
		_enabled_models,
		_field,
		_model_accounts,
		_upstream_of,
	)

	rows = []
	for model in _enabled_models(_settings()):
		model_id = (_field(model, "model") or "").strip()
		if _credential_type(model) != "subscription":
			rows.append(_PoolRow(model, model_id, False, "", ()))
			continue
		accounts = _model_accounts(model)
		refs = dict.fromkeys(r for r in ((_field(a, "account_ref") or "").strip() for a in accounts) if r)
		rows.append(_PoolRow(model, model_id, True, _upstream_of(model, accounts), tuple(refs)))
	return rows


def _account_models(expired_refs: set[str], rows: list[_PoolRow] | None = None) -> dict[str, list[str]]:
	"""``{account_ref: [model id, ...]}`` for the expired accounts in ``expired_refs``.

	A model id is listed only when EVERY enabled row serving that bare id (any provider, credential
	type or account) belongs to an expired account: the error card recognises a failed turn by its
	model alone, so an id also served by a healthy account or an API-key row must not be blamed on
	the dead sign-in. The direct ``direct:openai`` account serves the flat ``llm_model``.
	Model ids are not sensitive, so members get them too. ``rows`` is ``_enabled_rows()``."""
	rows = _enabled_rows() if rows is None else rows
	servers: dict[str, set[str]] = {}
	for row in rows:
		if not row.model_id:
			continue
		# A row with no subscription account (api key, or no live sign-in) is a healthy server.
		servers.setdefault(row.model_id.split("/")[-1], set()).update(set(row.refs) or {""})
	served: dict[str, list[str]] = {}
	for row in rows:
		if not row.model_id or not servers[row.model_id.split("/")[-1]] <= expired_refs:
			continue
		for ref in row.refs:
			if row.model_id not in served.setdefault(ref, []):
				served[ref].append(row.model_id)
	if served:
		return served
	# Pool rows win: the flat llm_* fields only describe the served model when there is no pool.
	direct_model = (_settings().get("llm_model") or "").strip()
	if direct_model and DIRECT_REF in _live_accounts():
		return {DIRECT_REF: [direct_model]}
	return {}


# ---- the stored map ------------------------------------------------------------------------


def _parse(raw) -> dict:
	try:
		data = json.loads(raw) if raw else {}
	except (TypeError, ValueError):
		return {}
	if not isinstance(data, dict):
		return {}
	return {ref: entry for ref, entry in data.items() if isinstance(entry, dict)}


def current_health() -> dict:
	return _parse(_read_stored())


def _store(health: dict) -> bool:
	"""Write ``health`` unless the stored map is already identical (the Single doc is also written by
	the apply path, so a needless write is a needless conflict). Never ``save()``."""
	if _parse(_read_stored()) == health:
		return False
	_write_stored(json.dumps(health, sort_keys=True, separators=(",", ":")))
	_publish_changed()
	return True


def _int(value) -> int:
	try:
		return int(value)
	except (TypeError, ValueError):
		return 0


def expired_entries() -> list[dict]:
	"""Stored expired entries for accounts that still exist, oldest first, each with its ``account_ref``."""
	stored = {ref: e for ref, e in current_health().items() if e.get("state") == "expired"}
	if not stored:
		return []  # nothing flagged: skip the live-account read (it decrypts every row)
	live = _live_accounts()
	entries = [{"account_ref": ref, **entry} for ref, entry in stored.items() if ref in live]
	return sorted(entries, key=lambda e: (_int(e.get("since")), e["account_ref"]))


def _stamp_cleared(upstream: str) -> None:
	frappe.cache().set_value(CLEARED_KEY_PREFIX + upstream, int(time.time()))


def _cleared_at(upstream: str) -> int:
	return _int(frappe.cache().get_value(CLEARED_KEY_PREFIX + upstream))


def apply_admin_health(health) -> bool:
	"""Merge admin's heartbeat reply (I3) into the stored map. True when the stored map changed.

	An admin entry keeps the ``source`` admin gave it. A local ``chat`` entry is never cleared by
	admin (neither by ``ok`` nor by omission); only a successful turn or a completed sign-in does.
	Two guards against admin's reply lagging our ok signal: a ``chat`` entry whose ``since`` is not later
	than the upstream's clear epoch is stale and ignored, and a local ``chat`` entry that predates that
	epoch is dropped once admin no longer lists it."""
	if not isinstance(health, dict):
		return False  # key absent (older admin) or junk: never wipe chat-detected state
	live = _live_accounts()
	stored = current_health()
	merged: dict[str, dict] = {}
	for ref, account in live.items():
		local = stored.get(ref) or {}
		remote = health.get(ref)
		local_chat = local.get("state") == "expired" and local.get("source") == "chat"
		cleared_at = _cleared_at(account["upstream"])
		if local_chat and _int(local.get("since")) <= cleared_at:
			local_chat = False  # a leftover from before the clear: nothing keeps it alive
		if (
			isinstance(remote, dict)
			and remote.get("state") == "expired"
			and not (remote.get("source") == "chat" and _int(remote.get("since")) <= cleared_at)
		):
			since = _int(remote.get("since")) or _int(local.get("since")) or int(time.time())
			if local_chat and _int(local.get("since")):
				since = min(since, _int(local.get("since")))
			source = remote.get("source") if remote.get("source") in _SOURCES else "poll"
			merged[ref] = {
				"upstream": account["upstream"],
				"label": remote.get("label") or account["label"],
				"email": remote.get("email") or account["email"],
				"state": "expired",
				"source": source,
				"since": since,
			}
		elif local_chat:
			merged[ref] = local  # admin never clears what this site saw fail
	return _store(merged)


# ---- chat-detected state -----------------------------------------------------------------------


def record_chat_failure(upstream: str) -> None:
	"""A turn failed with a dead sign-in on ``upstream``: mark its account expired (``source: chat``)
	at once and queue a signal for admin. Acts only when the upstream has exactly ONE account (or is
	the direct account): the failure cannot say which of several died, so the poll decides. Never raises."""
	try:
		upstream = (upstream or "").strip().lower()
		live = _live_accounts()
		mine = {ref: acct for ref, acct in live.items() if acct["upstream"] == upstream}
		if len(mine) != 1:
			return
		(ref, acct) = next(iter(mine.items()))
		health = {r: e for r, e in current_health().items() if r in live}
		if (health.get(ref) or {}).get("state") != "expired":
			health[ref] = {**acct, "state": "expired", "source": "chat", "since": int(time.time())}
			_store(health)
		_queue_signal(upstream, CODE_EXPIRED)
	except Exception:
		_log_throttled()


def _clear_upstream(upstream: str, *, account_ref: str | None = None, all_sources: bool = False) -> bool:
	"""Drop the stored entries to clear and queue an ``ok`` signal. Cleared: the upstream's ``chat``
	entries, plus ``account_ref``'s entry, plus (``all_sources``) every entry of the upstream. The
	signal is queued only when no other account of the upstream is still expired. True when an entry
	was dropped."""
	live = _live_accounts()
	health = {ref: e for ref, e in current_health().items() if ref in live}
	cleared = [
		ref
		for ref, e in health.items()
		if e.get("upstream") == upstream and (all_sources or e.get("source") == "chat" or ref == account_ref)
	]
	_stamp_cleared(upstream)
	if not cleared:
		return False
	for ref in cleared:
		del health[ref]
	_store(health)
	if not any(e.get("upstream") == upstream and e.get("state") == "expired" for e in health.values()):
		_queue_signal(upstream, CODE_OK)
	return True


def record_chat_success(upstream: str) -> None:
	"""A turn on ``upstream`` succeeded: clear its ``chat`` entries (a ``poll`` entry is admin's to
	clear) and queue an ``ok`` signal when one existed. Also stamps the per-upstream clear epoch on
	every successful turn, entry or not. Never raises."""
	try:
		_clear_upstream((upstream or "").strip().lower())
	except Exception:
		_log_throttled()


def record_signin_complete(upstream: str, account_ref: str | None = None, all_sources: bool = False) -> None:
	"""A sign-in for ``upstream`` completed. Clears its ``chat`` entries and the replaced account's entry
	(``account_ref``; None for a brand-new account) and queues an ``ok`` signal unless another account
	of the upstream is still expired. ``all_sources`` (single-account paths: direct, Claude CLI) clears
	every entry of the upstream. A pool sign-in must NOT clear another account's ``poll`` entry. With
	nothing to clear it still queues ``ok`` when none is expired (it is what clears ``direct:openai``
	on admin, whose key never changes across reconnects). Never raises."""
	try:
		upstream = (upstream or "").strip().lower()
		if upstream not in LABELS:
			return
		if _clear_upstream(upstream, account_ref=account_ref, all_sources=all_sources):
			return
		still = [
			e
			for ref, e in current_health().items()
			if ref in _live_accounts() and e.get("upstream") == upstream
		]
		if not any(e.get("state") == "expired" for e in still):
			_queue_signal(upstream, CODE_OK)
	except Exception:
		_log_throttled()


# ---- the signal queue (site -> admin, I2) -----------------------------------------------------------


def _parse_signal(raw) -> dict | None:
	try:
		item = json.loads(raw)
	except (TypeError, ValueError):
		return None
	if not isinstance(item, dict) or item.get("upstream") not in LABELS:
		return None
	if item.get("code") not in (CODE_EXPIRED, CODE_OK) or not isinstance(item.get("at"), int):
		return None
	return {"upstream": item["upstream"], "code": item["code"], "at": item["at"]}


def _queue_signal(upstream: str, code: str) -> None:
	cache = frappe.cache()
	# Collapse repeats: skip when the newest queued signal for this upstream says the same thing.
	for raw in reversed(cache.lrange(SIGNAL_KEY, 0, -1) or []):
		item = _parse_signal(raw)
		if item and item["upstream"] == upstream:
			if item["code"] == code:
				return
			break
	signal = {"upstream": upstream, "code": code, "at": int(time.time())}
	cache.rpush(SIGNAL_KEY, json.dumps(signal, sort_keys=True))
	cache.ltrim(SIGNAL_KEY, -_QUEUE_CAP, -1)


def drain_signals() -> list[dict]:
	"""Pop at most ``MAX_SIGNALS`` pending signals, oldest first."""
	cache = frappe.cache()
	out: list[dict] = []
	while len(out) < MAX_SIGNALS:
		raw = cache.lpop(SIGNAL_KEY)
		if raw is None:
			break
		item = _parse_signal(raw)
		if item:
			out.append(item)
	return out


def requeue_signals(signals: list[dict]) -> None:
	"""Put drained signals back at the front, in their original order (the push failed)."""
	cache = frappe.cache()
	for item in reversed(signals or []):
		cache.lpush(SIGNAL_KEY, json.dumps(item, sort_keys=True))
	cache.ltrim(SIGNAL_KEY, 0, _QUEUE_CAP - 1)


# ---- turn hooks ---------------------------------------------------------------------------------

# The served provider id the gateway reports, when it names the upstream itself. The pool proxy reports
# the synthetic ``openai_compat`` endpoint, which names nothing: the served MODEL id decides then.
_PROVIDER_UPSTREAM = {
	"openai": "openai",
	"openai-codex": "openai",
	"anthropic": "anthropic",
	"anthropic_cli": "anthropic",
	"claude-cli": "anthropic",
	"xai": "xai",
	"kimi": "kimi",
	"moonshot": "kimi",
}


def note_turn_error(err_text, code) -> None:
	"""Called at the run-error publish sites (never from the classifier). Records only a dead
	sign-in whose text names an upstream. Never raises."""
	if code != CODE_EXPIRED:
		return
	try:
		from jarvis.chat.error_taxonomy import subscription_upstream_from_error

		upstream = subscription_upstream_from_error(err_text)
		if upstream:
			record_chat_failure(upstream)
	except Exception:
		_log_throttled()


def _subscription_models(settings) -> list[tuple[str, str]]:
	"""``(model_id, upstream)`` of every enabled subscription row."""
	from jarvis.jarvis.pool_serialize import _credential_type, _enabled_models, _field, _subscription_upstream

	return [
		((_field(m, "model") or "").strip(), _subscription_upstream(m))
		for m in _enabled_models(settings)
		if _credential_type(m) == "subscription"
	]


def upstream_for_serving(model: str, provider: str) -> str:
	"""The subscription upstream that served a reply, or ``""`` when it cannot be told or the reply was
	served by an api key (which proves nothing about a sign-in). Never guesses."""
	settings = _settings()
	full = (model or "").strip()
	served = full.rsplit("/", 1)[-1]
	provider_upstream = _PROVIDER_UPSTREAM.get((provider or "").strip().lower(), "")
	candidates = {
		upstream
		for row_model, upstream in _subscription_models(settings)
		if row_model and row_model in (full, served)
	}
	if len(candidates) == 1:
		return candidates.pop()
	if len(candidates) > 1:
		return provider_upstream if provider_upstream in candidates else ""
	# No pool subscription row: direct mode names its upstream through the provider id.
	if not settings.get("models") and provider_upstream == "openai":
		return "openai" if DIRECT_REF in _live_accounts() else ""
	return ""


def note_turn_success(model: str, provider: str) -> None:
	"""A completed turn is first-hand proof the sign-in it ran on works: clear a chat-detected expiry
	for the serving upstream. One read when nothing is flagged. Never raises."""
	try:
		chat_upstreams = {e.get("upstream") for e in current_health().values() if e.get("source") == "chat"}
		if not chat_upstreams:
			return
		upstream = upstream_for_serving(model, provider)
		if upstream in chat_upstreams:
			record_chat_success(upstream)
	except Exception:
		_log_throttled()


def note_session_row(row) -> None:
	"""``note_turn_success`` for the ``sessions.list`` row both turn paths already hold."""
	try:
		from jarvis.chat.usage import resolved_model_identity

		model, provider = resolved_model_identity(row)
		if model:
			note_turn_success(model, provider)
	except Exception:
		_log_throttled()


def note_recorded_turn(outcome: str, row) -> None:
	"""``note_session_row`` gated on ``record_turn_usage``'s outcome: only a recorded or valid-zero turn
	read a fresh row. A ``retry`` row may still describe a previous turn on another model, and clearing
	an upstream on that guess is forbidden. Never raises."""
	from jarvis.chat.usage import USAGE_RECORDED, USAGE_VALID_ZERO

	if outcome in (USAGE_RECORDED, USAGE_VALID_ZERO):
		note_session_row(row)


# ---- what the SPA reads ---------------------------------------------------------------------------


def _pool_rows(rows: list[_PoolRow] | None = None) -> list[_PoolRow]:
	"""The enabled rows in pool order, the order Auto tries them."""
	from jarvis.jarvis.pool_serialize import _fleet_sort_key

	rows = _enabled_rows() if rows is None else rows
	return sorted(rows, key=lambda r: _fleet_sort_key(r.model))


def _answering(row: _PoolRow, expired_refs: set) -> str:
	"""The label ``row`` answers under while ``expired_refs`` are dead, or ``""`` if it cannot. A
	subscription row answers while it has an account that is not expired (its upstream's label); an
	api-key row answers under its model id."""
	if row.is_subscription:
		return LABELS.get(row.upstream, "") if set(row.refs) - set(expired_refs) else ""
	return row.model_id


def fallback_label(expired_refs: set, rows: list[_PoolRow] | None = None) -> str:
	"""Label of the first enabled row, in pool order, that can still answer while ``expired_refs`` are
	dead, or ``""`` when none can (chats fail). A subscription row answers while it has at least one
	account that is not expired; its label is its upstream's. An api-key row answers; its label is its
	model id, the row's own label in the UI. Computed once here so the banner, the AI models row and the
	member notice never disagree. ``rows`` is ``_enabled_rows()``."""
	for row in _pool_rows(rows):
		label = _answering(row, expired_refs)
		if label:
			return label
	return ""


def _skipped_refs(expired_refs: set, rows: list[_PoolRow] | None = None) -> set[str]:
	"""The expired accounts whose own row sits AFTER the row that answers. Auto tries rows in pool
	order, so it was already past those rows: nothing switched when they expired, and the card must
	not say "Auto uses X". An account's own row is the first enabled subscription row listing it."""
	ordered = _pool_rows(rows)
	answering_at = next((i for i, r in enumerate(ordered) if _answering(r, expired_refs)), None)
	if answering_at is None:
		return set()
	skipped = set()
	for ref in expired_refs:
		own_at = next((i for i, r in enumerate(ordered) if ref in r.refs), None)
		if own_at is not None and own_at > answering_at:
			skipped.add(ref)
	return skipped


def _fallback_for(entry: dict, fallback: str) -> str:
	"""When the account that answers is another one of the dead sign-in's own upstream, say so; the
	SPA reads ``Auto uses {fallback} until you reconnect``."""
	if fallback and fallback == LABELS.get(entry.get("upstream")):
		return f"Another {fallback} account"
	return fallback


def _with_fallbacks(entries: list[dict], rows: list[_PoolRow]) -> list[dict]:
	refs = {e["account_ref"] for e in entries}
	fallback = fallback_label(refs, rows)
	skipped = _skipped_refs(refs, rows) if fallback else set()
	return [
		{
			**entry,
			"fallback": _fallback_for(entry, fallback),
			"skipped": entry["account_ref"] in skipped and entry["account_ref"] != DIRECT_REF,
		}
		for entry in entries
	]


def ui_entries() -> list[dict]:
	"""``expired_entries()`` with the ``fallback`` label the SPA shows and whether the entry was
	``skipped`` (its row is after the answering one), oldest first. Nothing expired reads no row."""
	entries = expired_entries()
	return _with_fallbacks(entries, _enabled_rows()) if entries else []


def _member_entry(entry: dict) -> dict:
	return {"upstream": entry["upstream"], "label": entry["label"], "models": entry["models"]}


def _upstreams() -> list[str]:
	return sorted({account["upstream"] for account in _live_accounts().values()})


@frappe.whitelist()
def get_subscription_notice() -> dict:
	"""The expired chat sign-ins, for the chat banner, the error card and the Settings rail.

	Any workspace member may call it, so a member only learns what they need: the first expired
	sign-in that has no fallback (their chats are failing) as ``{upstream, label}``, and nothing about
	the rest of the pool. Each entry carries ``models``: the model ids only that expired sign-in
	serves, so the error card can recognise a failed turn from its model. An admin gets every
	entry (with ``account_ref`` and ``fallback`` for the Reconnect link) and the workspace's
	subscription upstreams."""
	from jarvis.permissions import has_jarvis_admin_access, require_jarvis_access

	require_jarvis_access()
	is_admin = has_jarvis_admin_access()
	# Nothing expired is the common case (every chat load, every realtime event): answer before any
	# pool row is read. Only the admin's upstreams list reads the accounts, once, in _live_accounts.
	entries = expired_entries()
	if not entries:
		if is_admin:
			return {"expired": [], "upstreams": _upstreams()}
		return {"expired": [], "expired_models": [], "upstreams": None}
	rows = _enabled_rows()
	entries = _with_fallbacks(entries, rows)
	served = _account_models({e["account_ref"] for e in entries}, rows)
	entries = [{**e, "models": served.get(e["account_ref"], [])} for e in entries]
	if is_admin:
		return {"expired": entries, "upstreams": _upstreams()}
	failing = [_member_entry(e) for e in entries if not e["fallback"]]
	# The banner shows only a failing entry with no fallback, but an explicit pick of ANY expired
	# model does not fail over, so the error card needs every expired entry's models.
	return {
		"expired": failing[:1],
		"expired_models": [_member_entry(e) for e in entries],
		"upstreams": None,
	}
