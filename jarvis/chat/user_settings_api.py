"""Whitelisted API for per-user chat settings, usage, and tenant-admin
management (design section 4).

House response shapes: ``{"ok": True, "data": ...}`` on success,
``{"ok": False, "reason": ...}`` on a soft failure. Every method is fully
type-annotated because ``hooks.py`` sets ``require_type_annotated_api_methods``.

Owner methods (``get_my_settings`` / ``update_my_settings``) are gated by
``require_jarvis_access`` and only ever touch the caller's own row. Admin
methods are gated by ``require_jarvis_admin`` (System Manager, Jarvis Admin,
or Administrator) and re-check server-side — the SPA's ``is_jarvis_admin``
boot flag is UX only.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, sbool

from jarvis.chat import openclaw_session_pool, usage
from jarvis.exceptions import OpenclawUnreachableError
from jarvis.permissions import require_jarvis_access, require_jarvis_admin

# NOTE: `from __future__ import annotations` above turns every annotation in this
# module into a STRING, and frappe's transform_parameter_types skips string
# annotations - so the whitelist arg-type gate does NOT coerce or validate inputs
# here. Each endpoint must defensively coerce its own string args through _s()
# below; never rely on the declared parameter types for runtime safety.
USER_SETTINGS = "Jarvis User Settings"

# Version of the chat-home introduction the SPA currently renders (the static,
# assistant-styled welcome bubble - presentation only, never a Jarvis Chat
# Message row). The server owns this number, not the client: the boot payload
# publishes it (get_chat_ui_settings) and mark_home_intro_seen clamps to it, so
# a client can neither invent a version nor mute a future one. Bump it ONLY
# when the introduction's content materially changes and every user should see
# it once more.
HOME_INTRO_VERSION = 1


def _s(x) -> str:
	"""Coerce a whitelisted string arg to trimmed text. The runtime type gate is
	off in this module (see the NOTE above), so a hostile list/dict must be forced
	to text before it reaches .strip() or a filter-shaped db call."""
	return str(x or "").strip()


def _month_tokens_effective(usage_month: str | None, month_tokens) -> int:
	"""Current-month token usage respecting rollover: a stale ``usage_month``
	(a prior month) reads as 0 used this month."""
	if usage_month != usage.current_month_key():
		return 0
	return int(month_tokens or 0)


def _per_model_row_dict(r) -> dict:
	"""Shared row-shaping for a single ``Jarvis User Model Usage`` record (used
	by both the single-user and batched-admin paths, so the two never drift on
	the payload shape)."""
	mi = int(r.month_input_tokens or 0)
	mo = int(r.month_output_tokens or 0)
	return {
		"model": r.model,
		"month_input_tokens": mi,
		"month_output_tokens": mo,
		"month_tokens": mi + mo,
		"monthly_token_limit": int(r.monthly_token_limit or 0),
	}


def _per_model_rows_by_user(users: list[str]) -> dict[str, list[dict]]:
	"""Current-month per-model usage + caps for every user in ``users``, in ONE
	query (``parent IN (...)``), bucketed in Python — the admin listing's
	batched counterpart to ``_per_model_rows`` (which stays single-query-per-
	user for the single-user settings/measured-usage paths). Empty dict when
	``users`` is empty; a user with no rows simply gets an empty list, same as
	``_per_model_rows`` would return for them."""
	if not users:
		return {}
	rows = frappe.get_all(
		usage.MODEL_USAGE,
		filters={
			"parent": ["in", users],
			"parenttype": USER_SETTINGS,
			"parentfield": usage.MODEL_USAGE_FIELD,
			"month_key": usage.current_month_key(),
		},
		fields=["parent", "model", "month_input_tokens", "month_output_tokens", "monthly_token_limit"],
		order_by="month_input_tokens desc",
	)
	out: dict[str, list[dict]] = {u: [] for u in users}
	for r in rows:
		out.setdefault(r.parent, []).append(_per_model_row_dict(r))
	return out


def _per_model_rows(user: str) -> list[dict]:
	"""Current-month per-model usage + caps for ``user`` (fleet spec §7). Empty
	list when no rows. Ordered by usage descending for a stable UI."""
	rows = frappe.get_all(
		usage.MODEL_USAGE,
		filters={
			"parent": user,
			"parenttype": USER_SETTINGS,
			"parentfield": usage.MODEL_USAGE_FIELD,
			"month_key": usage.current_month_key(),
		},
		fields=["model", "month_input_tokens", "month_output_tokens", "monthly_token_limit"],
		order_by="month_input_tokens desc",
	)
	return [_per_model_row_dict(r) for r in rows]


def _settings_payload(doc) -> dict:
	"""Owner-facing view of a settings doc: prefs + own usage + limit."""
	return {
		"user": doc.user,
		"notify_enabled": cint(doc.notify_enabled),
		"activity_detail": cint(doc.activity_detail),
		# getattr, not a bare read: in the migrate window before preferred_persona
		# syncs onto the doc's meta, doc.preferred_persona would AttributeError and
		# 500 get_my_settings / update_my_settings. Default to Jarvis like the rest.
		"preferred_persona": getattr(doc, "preferred_persona", None) or "Jarvis",
		"monthly_token_limit": cint(doc.monthly_token_limit),
		"usage_month": doc.usage_month,
		"month_tokens": _month_tokens_effective(doc.usage_month, doc.month_tokens),
		"month_input_tokens": _month_tokens_effective(doc.usage_month, doc.month_input_tokens),
		"month_output_tokens": _month_tokens_effective(doc.usage_month, doc.month_output_tokens),
		"total_tokens": cint(doc.total_tokens),
		"last_usage_at": doc.last_usage_at,
		"last_synced_at": doc.last_synced_at,
		"per_model": _per_model_rows(doc.user),
	}


@frappe.whitelist()
def get_my_settings() -> dict:
	"""Lazy-create the caller's settings row and return prefs + own usage +
	limit."""
	require_jarvis_access()
	doc = usage.get_or_create_user_settings(frappe.session.user)
	return {"ok": True, "data": _settings_payload(doc)}


@frappe.whitelist()
def update_my_settings(
	notify_enabled: int | None = None,
	activity_detail: int | None = None,
	preferred_persona: str | None = None,
) -> dict:
	"""Update the caller's own chat preferences only. The usage limit and
	counters (permlevel 1 / read-only) are never writable here."""
	require_jarvis_access()
	doc = usage.get_or_create_user_settings(frappe.session.user)
	if notify_enabled is not None:
		doc.notify_enabled = 1 if sbool(notify_enabled) else 0
	if activity_detail is not None:
		doc.activity_detail = 1 if sbool(activity_detail) else 0
	if preferred_persona is not None:
		# Blank clears back to the default; otherwise it must be a known persona
		# (this value rides the trusted [Context:] line in turn_handler).
		# _s() coerces a hostile non-string before .strip() (see the module NOTE);
		# without it a list/dict would 500 the endpoint on AttributeError.
		persona = _s(preferred_persona) or "Jarvis"
		if persona not in ("Jarvis", "Jara"):
			# Fixed-enum message that reflects NOTHING back (house style for small
			# enums): naming the valid set is clearer than echoing the bad value,
			# and reflecting nothing removes the self-XSS vector entirely.
			frappe.throw(frappe._("Persona must be Jarvis or Jara."))
		doc.preferred_persona = persona
	# ignore_permissions: the row is owner-scoped by construction (we loaded the
	# caller's own row), and only permlevel-0 pref fields are touched here.
	doc.save(ignore_permissions=True)
	return {"ok": True, "data": _settings_payload(doc)}


@frappe.whitelist()
def mark_home_intro_seen(version: int = 0) -> dict:
	"""Record that the caller has been shown the chat-home introduction.

	Fired best-effort by the SPA when the welcome bubble actually renders. It
	must never block chat, so the client ignores failures - the only cost of a
	lost ack is the introduction appearing again on the next empty chat home.

	Idempotent and monotonic, and enforced by the DB rather than by a read
	followed by a write: the whole update is ONE conditional UPDATE whose
	``home_intro_seen_version < %(v)s`` predicate is the monotonicity guard, so
	two tabs racing the ack (or a stale tab replaying an older version) cannot
	interleave into a lowered value. ``version`` is clamped to
	``HOME_INTRO_VERSION`` first, so a client can neither invent a version nor
	mute a future introduction by acking one that does not exist yet.

	``get_or_create_user_settings`` is called ONLY to guarantee the row exists -
	Jarvis User's permlevel-0 grant is ``if_owner`` with NO create, so a user
	meeting the introduction before they have a row must not 403.

	Deliberately NOT a ``doc.save()``, and deliberately NOT bumping ``modified``.
	This row is written concurrently by several server paths that use raw,
	``update_modified=False`` writes on single fields (``admin_set_user_limit``
	on ``monthly_token_limit``, ``usage``'s atomic counter SQL,
	``greeting``'s cadence counter). A full-doc save writes back EVERY field
	from a snapshot taken before those writes, and because they leave
	``modified`` untouched there is no timestamp mismatch to catch it: an admin
	setting a user's monthly spend cap in the same moment this user's browser
	acks the introduction would have the cap silently reverted to the stale
	value. Touching exactly the two columns this endpoint owns removes that
	whole class of interaction.
	"""
	require_jarvis_access()
	wanted = min(max(0, cint(version)), HOME_INTRO_VERSION)
	user = frappe.session.user
	usage.get_or_create_user_settings(user)  # row existence only; never saved here
	if wanted:
		frappe.db.sql(
			"""
			UPDATE `tabJarvis User Settings`
			SET home_intro_seen_version = %(version)s, home_intro_seen_at = %(now)s
			WHERE user = %(user)s AND home_intro_seen_version < %(version)s
			""",
			{"version": wanted, "now": frappe.utils.now_datetime(), "user": user},
		)
	seen = cint(frappe.db.get_value(USER_SETTINGS, {"user": user}, "home_intro_seen_version"))
	return {"ok": True, "data": {"home_intro_seen_version": seen}}


# Chat-home introduction telemetry: a bounded, privacy-free product-analytics
# sink (design section: Plan 06 telemetry). Everything is allow-listed - the
# event name, the suggestion category, and the time-to-first-prompt bucket - so
# no message content, no prompt text, and no user name can ever reach the log.
# The caller is recorded only as a salted-free SHA1 hash, exactly like
# jarvis/telemetry.py. Emits one JSON line to a dedicated logger and NEVER raises
# (telemetry must not break the empty-chat home).
_HOME_INTRO_EVENTS = frozenset({"displayed", "acknowledged", "suggestion_selected", "first_prompt"})
_HOME_INTRO_CATEGORIES = frozenset({"analyse", "action", "search", "draft"})
_HOME_INTRO_BUCKETS = frozenset({"0-5s", "5-15s", "15-60s", "1-5m", "5m+", "unknown"})
_HOME_INTRO_LOGGER = "jarvis.home_intro_telemetry"


@frappe.whitelist()
def record_home_intro_event(
	event: str,
	version: int = 0,
	category: str = "",
	bucket: str = "",
) -> dict:
	"""Record one bounded chat-home introduction telemetry event.

	Fired best-effort by the SPA (welcome displayed, a suggestion category
	chosen, time-to-first-accepted-prompt bucket). PRIVACY-BOUNDED by
	construction: the event name, category and bucket are all allow-listed, the
	version is an int, and the caller is stored only as a hash - no message
	content, prompt text, or user name is ever emitted. Unknown values are
	dropped rather than logged. Always returns ``{"ok": True}`` and never raises,
	so a telemetry failure can never affect chat.
	"""
	try:
		require_jarvis_access()
		ev = _s(event)
		if ev not in _HOME_INTRO_EVENTS:
			return {"ok": True}
		import hashlib

		# Salt the caller hash with a stable per-site secret before truncating: a
		# bare SHA1 of the email is trivially reversible by dictionary/rainbow attack
		# over the small, enumerable address space. encryption_key is Frappe's own
		# per-site secret (site_config.json), present on every provisioned site and
		# constant across restarts, so the same user still hashes to the same value
		# within a site but the digest is no longer a bare email hash. Read-only via
		# frappe.conf (never get_encryption_key, which would WRITE a key on the rare
		# site missing one); on that theoretical site we fall back to the site name -
		# a weak salt, but still not a bare email digest and it can never break this
		# best-effort telemetry.
		salt = frappe.conf.get("encryption_key") or getattr(frappe.local, "site", "") or ""
		entry = {
			"kind": "home_intro",
			"event": ev,
			"ts": frappe.utils.now(),
			"site": getattr(frappe.local, "site", None),
			"user_hash": hashlib.sha1(f"{salt}:{frappe.session.user or ''}".encode()).hexdigest()[:12],
			"version": cint(version),
		}
		cat = _s(category)
		if ev == "suggestion_selected" and cat in _HOME_INTRO_CATEGORIES:
			entry["category"] = cat
		bkt = _s(bucket)
		if ev == "first_prompt" and bkt in _HOME_INTRO_BUCKETS:
			entry["bucket"] = bkt
		frappe.logger(_HOME_INTRO_LOGGER).info(frappe.as_json(entry))
	except Exception:
		# Telemetry is never load-bearing; swallow everything (incl. an access
		# failure) so it cannot surface an error onto the empty-chat home.
		pass
	return {"ok": True}


@frappe.whitelist()
def admin_list_user_usage() -> dict:
	"""Every user with a settings row, joined to enabled site users: usage
	counters (rollover-aware), limits, and last activity. Admins only."""
	require_jarvis_admin()
	rows = frappe.get_all(
		USER_SETTINGS,
		fields=[
			"user",
			"monthly_token_limit",
			"usage_month",
			"month_tokens",
			"month_input_tokens",
			"month_output_tokens",
			"total_tokens",
			"last_usage_at",
			"last_synced_at",
		],
	)
	# One batched query for both "is this user enabled" and full_name, instead
	# of an enabled-set query plus a frappe.db.get_value per row (N+1). Only
	# enabled users are returned, same as before: a user missing from
	# user_map (disabled, or deleted) is skipped below.
	user_map = {
		u.name: u.full_name
		for u in frappe.get_all(
			"User",
			filters={"name": ["in", [r.user for r in rows]], "enabled": 1},
			fields=["name", "full_name"],
		)
	}
	# One batched query for every listed user's current-month per-model rows,
	# bucketed in Python, instead of one _per_model_rows(user) call per row
	# (N+1). The single-user path (get_my_settings / _measured_usage) keeps
	# using _per_model_rows directly - it's already a single query there.
	per_model_by_user = _per_model_rows_by_user([r.user for r in rows])
	out = []
	for r in rows:
		if r.user not in user_map:
			continue
		out.append(
			{
				"user": r.user,
				"full_name": user_map[r.user] or r.user,
				"monthly_token_limit": cint(r.monthly_token_limit),
				"usage_month": r.usage_month,
				"month_tokens": _month_tokens_effective(r.usage_month, r.month_tokens),
				"month_input_tokens": _month_tokens_effective(r.usage_month, r.month_input_tokens),
				"month_output_tokens": _month_tokens_effective(r.usage_month, r.month_output_tokens),
				"total_tokens": cint(r.total_tokens),
				"last_usage_at": r.last_usage_at,
				"last_synced_at": r.last_synced_at,
				"per_model": per_model_by_user.get(r.user, []),
			}
		)
	return {"ok": True, "data": out}


@frappe.whitelist()
def admin_set_user_limit(user: str, monthly_token_limit: int = 0) -> dict:
	"""Set a user's monthly token cap (0 = unlimited), creating the settings
	row if absent. Admins only."""
	require_jarvis_admin()
	# Coerce before the db calls: a dict `user` would turn the identity check into
	# a filter match (see the module NOTE / N10).
	user = _s(user)
	if not user or not frappe.db.exists("User", user):
		return {"ok": False, "reason": "unknown_user"}
	limit = max(0, cint(monthly_token_limit))
	doc = usage.get_or_create_user_settings(user)
	# monthly_token_limit is permlevel 1; write it directly (admin-gated above).
	frappe.db.set_value(
		USER_SETTINGS,
		doc.name,
		"monthly_token_limit",
		limit,
		update_modified=False,
	)
	frappe.db.commit()
	return {"ok": True, "data": {"user": user, "monthly_token_limit": limit}}


@frappe.whitelist()
def admin_set_user_model_limit(user: str, model: str, monthly_token_limit: int = 0) -> dict:
	"""Set a user's PER-MODEL monthly token cap (0 = unlimited), creating the
	settings row + current-month child row if absent. Mirrors admin_set_user_limit.
	Admins only (server re-checks; the SPA gate is UX)."""
	require_jarvis_admin()
	# Coerce before the db calls: a dict `user` would otherwise turn the identity
	# check into a filter match, and a non-string `model` would crash the per-model
	# lookup - a list reaches frappe.db.get_value as filters and unpacks wrong (N10).
	user = _s(user)
	if not user or not frappe.db.exists("User", user):
		return {"ok": False, "reason": "unknown_user"}
	model = _s(model)
	if not model:
		return {"ok": False, "reason": "invalid_model"}
	limit = max(0, cint(monthly_token_limit))
	usage.get_or_create_user_settings(user)
	usage.set_model_limit(user, model, limit)
	return {"ok": True, "data": {"user": user, "model": model, "monthly_token_limit": limit}}


@frappe.whitelist()
def admin_sync_usage() -> dict:
	"""Sweep ``sessions.list`` over the pooled gateway connection and refresh
	per-session snapshot fields + ``last_synced_at`` (no counter accumulation).
	Returns a per-user summary. Degrades to ``gateway_unreachable`` when the
	gateway can't be reached. Admins only."""
	require_jarvis_admin()

	settings = frappe.get_single("Jarvis Settings")
	gateway_url = (settings.agent_url or "").replace("http://", "ws://").replace("https://", "wss://")
	if not gateway_url:
		return {"ok": False, "reason": "gateway_unreachable"}

	try:
		with openclaw_session_pool.checkout(gateway_url) as sess:
			rows = sess.list_sessions()
	except OpenclawUnreachableError:
		return {"ok": False, "reason": "gateway_unreachable"}
	except Exception:
		frappe.log_error(
			title="jarvis usage: admin_sync_usage failed",
			message=frappe.get_traceback(),
		)
		return {"ok": False, "reason": "gateway_unreachable"}

	summary = usage.refresh_session_snapshots(rows)
	# synced_sessions counts rows that actually mapped to a known chat session
	# (prewarm/throwaway gateway sessions don't); the admin pane renders these
	# two counters verbatim.
	return {
		"ok": True,
		"data": {
			"synced_sessions": sum(b.get("sessions", 0) for b in summary.values()),
			"users_updated": len(summary),
			"users": summary,
		},
	}


# SPA theme choice maps to Frappe's native User.desk_theme so it roams per-user.
_THEME_TO_DESK = {"light": "Light", "dark": "Dark", "system": "Automatic"}


@frappe.whitelist()
def set_user_theme(theme: str) -> dict:
	"""Persist the caller's theme choice on their own User.desk_theme
	(Light/Dark/Automatic). ``theme`` is the SPA vocabulary light|dark|system."""
	require_jarvis_access()
	desk = _THEME_TO_DESK.get(str(theme or "").strip().lower())
	if not desk:
		return {"ok": False, "reason": "invalid_theme"}
	frappe.db.set_value("User", frappe.session.user, "desk_theme", desk, update_modified=False)
	return {"ok": True, "data": {"theme": theme, "desk_theme": desk}}
