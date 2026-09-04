"""Operator-facing diagnostics for the Jarvis Settings page.

Three whitelisted endpoints exposed as buttons:

- ping_admin: hits an authenticated admin endpoint to verify the
  customer's jarvis_admin_api_key works against jarvis_admin_url.
- ping_agent: opens a WS to agent_url with agent_token and completes
  the connect handshake only. No restart, no reload.
- force_resync: re-runs the same sync path as Jarvis Settings.on_update
  without depending on its change-detection. Useful when an LLM key
  change didn't register as a change (Password field UX bites).

All three return {ok: bool, ...} envelopes; the JS button shows a
green / red toast based on `ok`.
"""

import frappe

from jarvis.permissions import require_jarvis_admin


@frappe.whitelist()
def ping_admin() -> dict:
	"""Hit the admin's get_connection endpoint with the customer's stored
	jarvis_admin_api_key. Distinguishes auth failure from unreachable.

	SECURITY (security review PART 4 REVISED, TASK 34-R): the admin
	``get_connection`` payload carries the live container ``agent_token`` (an
	``operator.admin``-scoped bearer) + the admin/agent URLs. This diagnostic
	returns ONLY the connectivity verdict — NEVER the token or any operator URL,
	for ANY role (even a System Manager reads the token via the permlevel-fenced
	Settings form, not a ping). Gated on ``require_jarvis_admin`` (Jarvis Admin /
	System Manager / Administrator)."""
	require_jarvis_admin()
	from jarvis import admin_client

	settings = frappe.get_single("Jarvis Settings")
	if not (settings.get_password("jarvis_admin_api_key", raise_exception=False) or "").strip():
		return {
			"ok": False,
			"kind": "config",
			"error": "jarvis_admin_api_key is not set; complete onboarding first.",
		}
	try:
		# Consume the connection to prove reachability + auth, but DISCARD it —
		# do NOT return agent_token / agent_url / admin_url to the browser.
		admin_client.get_connection()
		return {"ok": True, "kind": "ok", "connected": True}
	except admin_client.AdminAuthError as e:
		return {"ok": False, "kind": "auth", "error": str(e)}
	except admin_client.AdminUnreachableError as e:
		return {"ok": False, "kind": "unreachable", "error": str(e)}


@frappe.whitelist()
def ping_agent() -> dict:
	"""Open WS to agent_url with agent_token; connect handshake only.

	SECURITY (PART 4 REVISED, TASK 34-R / 45): gated on ``require_jarvis_admin``
	and the ``agent_url`` is dropped from the response (endpoint disclosure +
	operator-scope probe surface). Returns only the connectivity verdict."""
	require_jarvis_admin()
	from jarvis import agent_ws
	from jarvis.exceptions import AgentUnreachableError

	settings = frappe.get_single("Jarvis Settings")
	url = (settings.agent_url or "").strip()
	token = settings.get_password("agent_token", raise_exception=False) or ""
	if not url:
		return {"ok": False, "kind": "config", "error": "agent_url is not set."}
	if not token:
		return {"ok": False, "kind": "config", "error": "agent_token is not set."}
	try:
		agent_ws.ping(url, token)
		return {"ok": True, "kind": "ok", "connected": True}
	except AgentUnreachableError as e:
		return {"ok": False, "kind": "unreachable", "error": str(e)}
	except Exception as e:
		return {"ok": False, "kind": "error", "error": f"{type(e).__name__}: {e}"}


@frappe.whitelist()
def force_resync(action: str = "reload") -> dict:
	"""Bypass on_update change-detection. Re-drive the CURRENT Settings through
	admin, branching pool vs direct like every other dispatch path.

	A pool/subscription tenant goes through the /llm-pool leg so the Bifrost +
	CLIProxyAPI sidecars are reconciled; a direct/api-key tenant keeps the
	/llm-creds leg. Before this branch existed the button used /llm-creds
	unconditionally and never touched a pool tenant's sidecars.

	action in {'reload', 'restart'}:
	- direct leg: 'reload' hot-rotates the key, 'restart' re-renders + bounces
	  the container (and re-pushes skills), as before.
	- pool leg: the fleet's /llm-pool apply is health-aware - it bounces the
	  container when the config drifted or the container is unhealthy, and
	  no-ops an already-correct healthy one. 'restart' additionally re-pushes
	  custom + learned skills (no-op when the tenant has none). A wedged but
	  healthy pool container is a reprovision, not a resync - out of scope here.

	Returns ``{action, state, last_sync_at, last_sync_status}`` where ``state``
	is one of applied / applying / failed / skipped (the pool leg hands its
	converge tail to an async worker, so ``applying`` is a normal outcome).

	Gated on ``require_jarvis_admin`` (PART 4 REVISED, TASK 45): a restart
	reconciles + bounces the tenant container (DoS-class)."""
	require_jarvis_admin()
	if action not in ("reload", "restart"):
		raise frappe.ValidationError(f"invalid action {action!r}; expected reload or restart")
	from jarvis import admin_client
	from jarvis.jarvis.pool_serialize import compute_pool_mode

	settings = frappe.get_single("Jarvis Settings")
	# Branch on pool vs direct the way every other dispatch path does (on_update,
	# request_resync). A pool/subscription tenant's proxy sidecars (Bifrost +
	# CLIProxyAPI) live ONLY on the /llm-pool leg: _sync_via_admin's /llm-creds path
	# rotates the key or re-pushes a single model and never rebuilds the sidecars
	# (see jarvis_settings.py's _sync_via_admin docstring). Without this branch a
	# "Force Resync" on a pool tenant silently used the wrong wire contract and left
	# the sidecars untouched.
	skipped = False
	if compute_pool_mode(settings):
		# sync_pool_now does the bounded /llm-pool push - the fleet re-materializes
		# Bifrost + CLIProxyAPI and bounces the container when the config drifted or
		# it is unhealthy, and force_probe=True fires a live chat-completion probe so a
		# byte-identical healthy no-op still returns an honest verdict instead of doing
		# nothing. It handles its own admin errors (writing a terminal status) and hands
		# the converge tail to the async worker - this mirrors save_llm_pool's
		# settings-save flow (bounded push + async converge), NOT the SPA "Resync"
		# button, which enqueues fully async via request_resync/_enqueue_pool_sync.
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import sync_pool_now

		outcome = sync_pool_now(force_probe=True) or {}
		skipped = bool(outcome.get("skipped"))
		if action == "restart":
			# Match the direct leg's restart: restore custom + learned skills a
			# container (re)provision would have wiped. Both no-op when the tenant has
			# no such skills, so a plain subscription tenant pays no extra restart.
			settings._resync_custom_skills_after_restart()
			settings._resync_learned_skills_after_restart()
	else:
		# Direct (api-key / lone-subscription) tenant: no sidecars to reconcile, so the
		# legacy /llm-creds leg is correct and ``action`` still selects reload vs
		# restart. The local-agent sync was retired with the managed fleet.
		try:
			settings._sync_via_admin(action)
		except admin_client.AdminAuthError:
			# #388: _sync_via_admin now re-raises a genuine (non-"not paid") auth
			# failure instead of swallowing it, so the RQ-enqueued caller sees it.
			# This is the one SYNCHRONOUS, whitelisted caller with a JSON {ok, ...}
			# contract of its own to preserve - _sync_via_admin already wrote the
			# terminal "failed: auth: ..." status onto Settings before raising, so
			# swallow here and let the reload below report it the same way every
			# other outcome of this endpoint is reported.
			pass
	settings.reload()
	# Report an explicit state instead of making the caller parse the status string.
	# The pool leg stamps "pending: ..." synchronously and converges async, so
	# ``applying`` is a normal, non-error outcome (the Desk toast keys off this).
	status = settings.get("last_sync_status") or ""
	if skipped:
		state = "skipped"
	elif status.startswith("ok"):
		state = "applied"
	elif status.startswith("failed"):
		state = "failed"
	else:
		state = "applying"
	return {
		"action": action,
		"state": state,
		"last_sync_at": str(settings.get("last_sync_at") or ""),
		"last_sync_status": status,
	}


@frappe.whitelist()
def chat_recovery_stats() -> dict:
	"""Operator-facing visibility into snapshot recovery (turn_recovery):
	how often the never-error machinery is quietly compensating for a
	gateway/turn that never completed live. currently_recovering is a live,
	un-windowed snapshot (streaming=1 AND recovering=1 right now); the other
	counts are windowed over 24h and 7d.

	Gated on ``require_jarvis_admin`` (PART 4 REVISED, TASK 45): the raw
	``frappe.db.sql`` COUNT/SUM spans ALL users' chat messages (tenant-wide
	operational metadata), bypassing the PART-1 chat query hook."""
	require_jarvis_admin()
	from jarvis.chat.turn_recovery import CEILING_ERROR_MESSAGE

	currently_recovering = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabJarvis Chat Message`
		WHERE role = 'assistant' AND streaming = 1 AND recovering = 1
		"""
	)[0][0]

	def _window(hours: int) -> dict:
		row = frappe.db.sql(
			"""
			SELECT
				COUNT(*) AS total,
				SUM(CASE WHEN was_recovered = 1 THEN 1 ELSE 0 END) AS recovered,
				SUM(CASE WHEN error = %(ceiling_msg)s THEN 1 ELSE 0 END) AS ceiling_errored
			FROM `tabJarvis Chat Message`
			WHERE role = 'assistant' AND creation >= %(since)s
			""",
			{
				"ceiling_msg": CEILING_ERROR_MESSAGE,
				"since": frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-hours),
			},
			as_dict=True,
		)[0]
		return {
			"total": row.total or 0,
			"recovered": row.recovered or 0,
			"currently_recovering": currently_recovering,
			"ceiling_errored": row.ceiling_errored or 0,
		}

	win_24h = _window(24)
	win_7d = _window(24 * 7)
	recovered_rate_24h = (win_24h["recovered"] / win_24h["total"]) if win_24h["total"] else 0
	return {
		"24h": win_24h,
		"7d": win_7d,
		"recovered_rate_24h": recovered_rate_24h,
	}


@frappe.whitelist()
def reset_agent_pairing() -> dict:
	"""Clear the cached chat-device pairing and re-pair from scratch.

	Use when agent rejects the existing pairing (e.g. 'device token
	mismatch') and the automatic repair did not fire because agent
	returned a generic error code. Clears the chat-device creds, drops any
	pooled connection, then opens a fresh device-paired connection (which
	re-pairs via the ops bench + fleet-agent) to verify.

	Gated on ``require_jarvis_admin`` (PART 4 REVISED, TASK 45) — tenant operator
	diagnostic, widened from SM-only.
	"""
	require_jarvis_admin()
	from jarvis.chat import agent_session_pool
	from jarvis.chat.agent_client import AgentSession
	from jarvis.chat.device import clear_credentials
	from jarvis.exceptions import AgentUnreachableError

	settings = frappe.get_single("Jarvis Settings")
	gateway_url = (settings.agent_url or "").strip().replace("http://", "ws://").replace("https://", "wss://")
	if not gateway_url:
		return {"ok": False, "kind": "config", "error": "agent_url is not set."}

	clear_credentials()
	try:
		agent_session_pool.drain_all()
	except Exception:
		pass
	try:
		sess = AgentSession.connect(gateway_url)
		sess.close()
		return {"ok": True, "message": "Cleared the old pairing and reconnected to the agent."}
	except AgentUnreachableError as e:
		return {"ok": False, "kind": "unreachable", "error": str(e)}
	except Exception as e:
		return {"ok": False, "kind": "error", "error": f"{type(e).__name__}: {e}"}


@frappe.whitelist()
def import_announce_stats() -> dict:
	"""Operator visibility into the Slice B import auto-tell (import_announce). Surfaces
	the pending/stuck backlog broken out by reason, and the fast-path ATTRIBUTION so a
	silently-dead after_job hook is visible: if ``hook_rate_24h`` collapses toward 0 while
	imports keep finishing (``total`` > 0), the fast path is down and the ``*/2`` poll is
	carrying everything - act on it.

	Gated on ``require_jarvis_admin``: the raw COUNTs span ALL users' announcements
	(tenant-wide operational metadata), and the doctype has NO user-facing read grant."""
	require_jarvis_admin()
	ann = "Jarvis Import Announcement"
	if not frappe.db.exists("DocType", ann):
		return {"pending": 0, "note": "doctype not migrated"}
	now = frappe.utils.now_datetime()
	grace_cutoff = frappe.utils.add_to_date(now, minutes=-30)
	pending = frappe.db.count(ann, {"announced": 0})
	stuck = frappe.db.count(ann, {"announced": 0, "kicked_off_at": ["<", grace_cutoff]})
	oldest = frappe.db.get_value(ann, {"announced": 0}, "kicked_off_at", order_by="kicked_off_at asc")
	oldest_age_min = (
		round((now - frappe.utils.get_datetime(oldest)).total_seconds() / 60.0, 1) if oldest else 0
	)
	since_24h = frappe.utils.add_to_date(now, hours=-24)
	win = frappe.db.sql(
		"""
		SELECT COUNT(*) AS total,
			   SUM(CASE WHEN announced_via = 'hook' THEN 1 ELSE 0 END) AS via_hook,
			   SUM(CASE WHEN announced_via = 'poll' THEN 1 ELSE 0 END) AS via_poll
		FROM `tabJarvis Import Announcement`
		WHERE announced = 1 AND modified >= %(since)s
		""",
		{"since": since_24h},
		as_dict=True,
	)[0]
	total = win.total or 0
	reasons = frappe.db.sql(
		"""
		SELECT reason, COUNT(*) AS c FROM `tabJarvis Import Announcement`
		WHERE announced = 1 AND modified >= %(since)s GROUP BY reason
		""",
		{"since": since_24h},
		as_dict=True,
	)
	return {
		"pending": pending,
		"stuck_past_grace": stuck,
		"oldest_pending_age_min": oldest_age_min,
		"24h": {"total": total, "via_hook": win.via_hook or 0, "via_poll": win.via_poll or 0},
		"hook_rate_24h": (win.via_hook / total) if total else 0,
		"by_reason_24h": {(r.reason or "unknown"): r.c for r in reasons},
	}
