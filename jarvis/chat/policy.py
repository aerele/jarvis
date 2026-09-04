"""Subscription / credits / rate-limit validation seam.

Called by jarvis.chat.api.send_message before enqueuing the agent worker.
It rejects empty users and Guest, and (design section 5) enforces the
per-user all-time token cap set via ``Jarvis User Settings``. Phase 3's
jarvis_admin app may layer real subscription gating on top by overriding
this module's contract.

Returns (True, None) on success or (False, reason: str) on rejection. The
reason is a machine code the SPA maps to a human toast: ``"usage_limit"`` for
enforcement, ``"subscription_suspended"`` for billing,
and ``"release_update_required"`` while a release rollout is blocking this
bench. Worker health is never a gate here: RQ's worker registry can read zero
while every worker is alive (see ``jarvis.chat.pump._registry_is_stale``), and a
turn nobody picks up is the orphan sweep's job (``stale_scan``).
"""

from __future__ import annotations

import frappe


def validate_can_send(user: str, model: str | None = None) -> tuple[bool, str | None]:
	if not user:
		return False, "no authenticated user"
	if user == "Guest":
		return False, "Guest users cannot use Jarvis chat"
	# Aggregate all-time cap is the OUTER gate — checked first (fleet spec §7).
	if _over_total_limit(user):
		return False, "usage_limit"
	if _subscription_suspended():
		return False, "subscription_suspended"
	if _release_update_required():
		return False, "release_update_required"
	if _workspace_resetting():
		return False, "workspace_resetting"
	if _llm_not_configured():
		return False, "llm_not_configured"
	# Per-model cap: only when a concrete model is known. ``model`` is resolved
	# in chat.api (which knows the conversation) and passed in as a plain string,
	# so policy never imports turn_handler (no import cycle). Pool "Auto" resolves
	# to "" -> per-model gate skipped (accepted enforcement gap, spec §2).
	if model and _over_model_limit(user, model):
		return False, "usage_limit"
	return True, None


def _release_update_required() -> bool:
	"""True while a published release notice applies to this bench.

	The full-page gate only latches at boot, so without this an already-open tab
	chats straight through a rollout. Reads the locally mirrored notice (no admin
	round-trip) and reuses boot_payload's self-clear, so the send gate and the UI
	gate can never disagree. Fails OPEN."""
	try:
		from jarvis import release_notice

		return bool(release_notice.boot_payload().get("active"))
	except Exception:
		frappe.log_error(
			title="jarvis policy: release-notice check failed (allowing send)",
			message=frappe.get_traceback(),
		)
		return False


def _workspace_resetting() -> bool:
	"""True while a self-serve workspace reset is rebuilding the container
	(transport cleared + resetting marker set). Without this a send throws a raw
	"agent is not configured" instead of a gated state. Fails OPEN."""
	try:
		from jarvis.onboarding import _RESETTING_STATUS

		s = frappe.get_single("Jarvis Settings")
		return not s.get("agent_url") and (s.get("last_sync_status") or "").startswith(_RESETTING_STATUS)
	except Exception:
		# See _over_model_limit: don't let a logging failure defeat fail-open.
		try:
			frappe.log_error(
				title="jarvis policy: workspace-reset check failed (allowing send)",
				message=frappe.get_traceback(),
			)
		except Exception:
			pass
		return False


def _llm_not_configured() -> bool:
	"""True when NO LLM connection is configured at all (never set up, or revoked
	via the workspace-reset "disconnect AI model connections" option). Mirrors
	account.is_ready_for_chat's local llm_credentials branch — without this gate a
	send queues against the container's stub key and hangs instead of telling the
	customer to connect a model. Fails OPEN.

	Skipped under test unless a test opts in via
	``frappe.flags.test_llm_configured_gate``: the CI site has no LLM configured,
	so this gate would otherwise reject every chat test's send."""
	try:
		if frappe.flags.in_test and not frappe.flags.test_llm_configured_gate:
			return False
		s = frappe.get_single("Jarvis Settings")
		if s.get("proxy_active"):
			return False  # a pool is configured; pool health is admin's concern
		mode = (s.get("llm_auth_mode") or "api_key").strip() or "api_key"
		if mode == "subscription":
			# Unified models[]-table subscription on the DIRECT leg (jarvis#715):
			# the credential lives in models[], not the flat oauth fields.
			# llm_oauth_connected_at belongs to the legacy flat-field flow and is
			# unconditionally cleared by save_llm_pool on every models[]-table
			# save, so gating on it here would block every send on a
			# fully-working direct-leg subscription tenant.
			#
			# Checks the row is ENABLED, subscription-typed, AND has a connected
			# account - not merely that the table is non-empty (jarvis#755
			# review): a bare-presence check let a send queue against a container
			# with no credential yet (mid-connect, before the OAuth handshake
			# lands) and hang. Shared with account.is_ready_for_chat's parallel
			# check and reconcile_pending_llm_sync's direct_leg_configured.
			from jarvis.jarvis.pool_serialize import has_configured_subscription_model

			return not has_configured_subscription_model(s)
		if mode == "oauth":
			return not s.get("llm_oauth_connected_at")
		key = s.get_password("llm_api_key", raise_exception=False) or ""
		provider = (s.get("llm_provider") or "").strip()
		model = (s.get("llm_model") or "").strip()
		return not (key and provider and model)
	except Exception:
		# See _over_model_limit: don't let a logging failure defeat fail-open.
		try:
			frappe.log_error(
				title="jarvis policy: llm-configured check failed (allowing send)",
				message=frappe.get_traceback(),
			)
		except Exception:
			pass
		return False


def _subscription_suspended() -> bool:
	"""True iff admin reports the subscription no longer entitles chat. Reuses
	_admin_chat_gate's cache; fails OPEN (a control-plane hiccup must never block
	a paying customer)."""
	try:
		from jarvis.account import _admin_chat_gate

		return _admin_chat_gate().get("reason") == "subscription_suspended"
	except Exception:
		frappe.log_error(
			title="jarvis policy: entitlement check failed (allowing send)",
			message=frappe.get_traceback(),
		)
		return False


def _over_model_limit(user: str, model: str) -> bool:
	"""True iff ``user`` has a positive per-model cap for ``model`` this month and
	this month's per-model usage has reached it. Rollover-aware: the lookup is
	scoped to the current month_key, so a stale row reads as no cap. Fails open on
	any error — an accounting lookup bug must never block a legitimate send (G2).

	SUMs across all matching rows rather than reading one via
	``frappe.db.get_value`` — a write-side race in
	``jarvis.chat.usage._upsert_model_usage`` (closed by an atomic upsert +
	unique index, but pre-existing duplicates or any future anomaly must
	still be tolerated here) could otherwise leave two child rows for the
	same (user, model, month); reading one arbitrary row could silently
	under-count usage and let a per-model cap be exceeded."""
	try:
		if not model:
			return False
		from jarvis.chat.usage import (
			MODEL_USAGE,
			MODEL_USAGE_FIELD,
			current_month_key,
		)

		row = frappe.db.sql(
			"""
			SELECT MAX(monthly_token_limit) AS limit_,
				   SUM(month_input_tokens) AS in_,
				   SUM(month_output_tokens) AS out_
			FROM `tabJarvis User Model Usage`
			WHERE parent = %(user)s AND parenttype = %(ptype)s
			  AND parentfield = %(pfield)s AND model = %(model)s
			  AND month_key = %(month)s
			""",
			{
				"user": user,
				"ptype": "Jarvis User Settings",
				"pfield": MODEL_USAGE_FIELD,
				"model": model,
				"month": current_month_key(),
			},
			as_dict=True,
		)
		# MAX()/SUM() over zero matching rows returns one row of NULLs, not an
		# empty result set — a NULL limit means "no row" (no cap).
		if not row or row[0].limit_ is None:
			return False
		limit = int(row[0].limit_ or 0)
		if limit <= 0:
			return False
		used = int(row[0].in_ or 0) + int(row[0].out_ or 0)
		return used >= limit
	except Exception:
		# The logging call itself touches the DB (Error Log controller
		# resolution) - if the triggering failure is DB-wide, log_error can
		# raise too. Never let a logging failure defeat fail-open (G2).
		try:
			frappe.log_error(
				title="jarvis usage: per-model limit check failed (allowing send)",
				message=frappe.get_traceback(),
			)
		except Exception:
			pass
		return False


def _over_total_limit(user: str) -> bool:
	"""True iff ``user`` has a positive all-time token cap and their all-time
	recorded usage has reached it. Dependency-light: one ``db.get_value`` on the
	settings row, no lazy create (a missing row = no limit). No rollover: unlike
	the per-model gate, this cap never resets, so it compares against
	``total_tokens`` (the cumulative, never-reset counter) rather than a
	month-scoped bucket. Fails open on any error — an accounting lookup bug
	must never block a legitimate send.

	NOTE: the field is still named ``monthly_token_limit`` (kept to avoid a
	migration for ~15 existing references / the wire contract) but the cap it
	now enforces is all-time, not monthly."""
	try:
		row = frappe.db.get_value(
			"Jarvis User Settings",
			{"user": user},
			["monthly_token_limit", "total_tokens"],
			as_dict=True,
		)
		if not row:
			return False
		limit = int(row.monthly_token_limit or 0)
		if limit <= 0:
			return False
		used = int(row.total_tokens or 0)
		return used >= limit
	except Exception:
		# See _over_model_limit: don't let a logging failure defeat fail-open.
		try:
			frappe.log_error(
				title="jarvis usage: limit check failed (allowing send)",
				message=frappe.get_traceback(),
			)
		except Exception:
			pass
		return False
