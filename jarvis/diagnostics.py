"""Operator-facing diagnostics for the Jarvis Settings page.

Three whitelisted endpoints exposed as buttons:

- ping_admin: hits an authenticated admin endpoint to verify the
  customer's jarvis_admin_api_key works against jarvis_admin_url.
- ping_openclaw: opens a WS to agent_url with agent_token and completes
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
def ping_openclaw() -> dict:
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
	"""Bypass on_update change-detection. Run the same sync path on
	current Settings values. action in {'reload', 'restart'}.

	Gated on ``require_jarvis_admin`` (PART 4 REVISED, TASK 45): a restart
	reconciles + recreates the tenant container (DoS-class)."""
	require_jarvis_admin()
	if action not in ("reload", "restart"):
		raise frappe.ValidationError(f"invalid action {action!r}; expected reload or restart")
	settings = frappe.get_single("Jarvis Settings")
	# Always the admin path: the legacy local-openclaw sync was retired with
	# the managed fleet (its method no longer exists), and _sync_via_admin
	# surfaces its own clear error on an unconfigured bench.
	settings._sync_via_admin(action)
	settings.reload()
	return {
		"action": action,
		"last_sync_at": str(settings.get("last_sync_at") or ""),
		"last_sync_status": settings.get("last_sync_status") or "",
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

	Use when openclaw rejects the existing pairing (e.g. 'device token
	mismatch') and the automatic repair did not fire because openclaw
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
