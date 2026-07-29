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

USER_SETTINGS = "Jarvis User Settings"


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
def update_my_settings(notify_enabled: int | None = None, activity_detail: int | None = None) -> dict:
	"""Update the caller's own chat preferences only. The usage limit and
	counters (permlevel 1 / read-only) are never writable here."""
	require_jarvis_access()
	doc = usage.get_or_create_user_settings(frappe.session.user)
	if notify_enabled is not None:
		doc.notify_enabled = 1 if sbool(notify_enabled) else 0
	if activity_detail is not None:
		doc.activity_detail = 1 if sbool(activity_detail) else 0
	# ignore_permissions: the row is owner-scoped by construction (we loaded the
	# caller's own row), and only permlevel-0 pref fields are touched here.
	doc.save(ignore_permissions=True)
	return {"ok": True, "data": _settings_payload(doc)}


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
	if not user or not frappe.db.exists("User", user):
		return {"ok": False, "reason": "unknown_user"}
	model = (model or "").strip()
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
