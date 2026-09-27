"""Mirror the operator's resolved upgrade-maintenance hold locally and expose it to chat.

The control plane resolves the hold (tenant -> host -> cell) and the bench stores +
renders it. Pure toggle owned by the CP -- there is NO local TTL / self-clear.

Key discipline (Stream E, marker-aware -- see ``persist_from_connection``): the CP stamps a
``maintenance_supported`` capability marker on every payload. Marker ABSENT = an old /
rolled-back CP -> CLEAR (never strand the bench behind a hold the old CP can't lift). Marker
present + a PRESENT ``maintenance`` dict = authoritative (sets OR clears). Marker present + an
ABSENT key = the CP could not resolve one (a transient / partial payload -- e.g. the
destroy+reprovision window, or a resolver error) -> KEEP the last-known state, so a brief
unresolved poll can't flip a live hold off and dump the customer into a raw error mid-teardown.

Clearing happens on the CP (operator / roll ``clear_maintenance`` or the fleet-wide
``disable_maintenance_hold`` kill-switch); the bench reflects it on the next refresh.
"""

import frappe

SETTINGS = "Jarvis Settings"
_FIELDS = ("maintenance_active", "maintenance_message")
# One maintenance check refreshes BOTH notices (release + maintenance) from a single
# get_connection, so an active hold doesn't add a second admin round-trip. This throttle
# is INDEPENDENT of release_notice's own (different key) - the two check endpoints don't
# share a window, so polling both within 30s can still make two calls.
_CHECK_CACHE_KEY = "jarvis:maintenance_checked"
_CHECK_CACHE_TTL_S = 30


def persist(notice: dict | None) -> None:
	"""Mirror the admin-sent maintenance notice onto Jarvis Settings.

	``notice`` is a dict (authoritative: ``active`` True sets, False/`{}` clears) or
	``None`` (unknown -> keep the last-known mirror; see the module docstring). Skips a
	no-op write so it doesn't churn ``modified`` under an operator editing the form."""
	try:
		if notice is None:
			return
		fresh = {
			"maintenance_active": 1 if notice.get("active") else 0,
			"maintenance_message": notice.get("message") or "",
		}
		current = frappe.db.get_value(SETTINGS, SETTINGS, list(_FIELDS), as_dict=True) or {}
		if (
			frappe.utils.cint(current.get("maintenance_active")) == fresh["maintenance_active"]
			and (current.get("maintenance_message") or "") == fresh["maintenance_message"]
		):
			return
		frappe.db.set_value(SETTINGS, SETTINGS, fresh, update_modified=False)
	except Exception:
		frappe.log_error(title="maintenance_notice.persist failed", message=frappe.get_traceback())


def _mirror_payload() -> dict:
	"""The operator/roll maintenance mirror alone, with no switch overlay. active iff
	the mirror flag is set (pure toggle -- the CP owns clearing it; this bench reflects
	the last-known state until the next poll refreshes it). Fails to not-held on any
	read error. ``persist_from_connection`` reads only this, never ``boot_payload``'s
	switch overlay below -- a proxy<->direct switch is bench-local and the CP has no
	notion of it."""
	try:
		row = frappe.get_cached_value(SETTINGS, SETTINGS, list(_FIELDS), as_dict=True) or {}
		return {
			"active": bool(frappe.utils.cint(row.get("maintenance_active"))),
			"message": row.get("maintenance_message") or "",
		}
	except Exception:
		frappe.log_error(title="maintenance_notice._mirror_payload failed", message=frappe.get_traceback())
		return {"active": False, "message": ""}


def boot_payload() -> dict:
	"""``maintenance`` for context.boot and the send gate (``policy._maintenance_hold``).

	The operator/roll mirror wins when active (it is the higher-priority, CP-driven
	hold). Otherwise, while a bench-local proxy<->direct switch is held open
	(jarvis#1425 follow-up -- ``jarvis.chat.llm_switch``), reuse this same hold and
	banner so the send gate, the boot flag and the SPA banner all key off one signal
	instead of a second parallel mechanism. Fails to not-held on any error (a redis
	blip must never block chat)."""
	mirror = _mirror_payload()
	if mirror.get("active"):
		return mirror
	try:
		from jarvis.chat import llm_switch

		if llm_switch.is_active():
			return {"active": True, "message": llm_switch.MESSAGE}
	except Exception:
		frappe.log_error(
			title="maintenance_notice.boot_payload llm_switch check failed",
			message=frappe.get_traceback(),
		)
	return {"active": False, "message": ""}


def persist_from_connection(conn: dict) -> None:
	"""Apply the maintenance mirror from a full get_connection payload, honouring the CP
	capability marker (Stream E review App-1/#1). Three cases:

	- marker ABSENT -> the CP does not speak maintenance (old / rolled back): clear the mirror
	  authoritatively so a bench is never stranded behind a hold the old CP can no longer lift.
	- marker present, ``maintenance`` key PRESENT -> authoritative (dict sets or clears).
	- marker present, ``maintenance`` key ABSENT -> transient / partial payload (destroy window,
	  or a CP resolver error that omitted the key) -> keep last-known.

	DEPLOY-ORDER GUARDRAIL: a NEW bench must never poll an OLD (un-upgraded) CP while a hold is
	live, or this clears it every poll -- deploy the CP before the app, and set no hold until both
	are live. The breadcrumb below is the diagnostic if that ordering is ever violated."""
	if not conn.get("maintenance_supported"):
		if _mirror_payload().get("active"):
			frappe.logger("jarvis").info(
				"maintenance: clearing hold -- connection lacks maintenance_supported "
				"(old/rolled-back control plane); confirm CP-before-app deploy order"
			)
		persist({"active": False})  # authoritative clear -- no strand on rollback
		return
	persist(conn.get("maintenance"))  # present dict = authoritative; absent -> None -> keep


@frappe.whitelist(methods=["POST"])
def check() -> dict:
	"""Re-pull the connection from admin and refresh the local maintenance mirror, so an
	open chat tab lifts the hold promptly when the operator/roll clears it. One admin
	round-trip refreshes BOTH notices (release + maintenance) -- an active maintenance
	poll therefore keeps the release mirror fresh too, not a wasted second call. The
	round-trip is cached briefly so many gated tabs cost one call."""
	from jarvis import admin_client, release_notice

	cache = frappe.cache()
	if not cache.get_value(_CHECK_CACHE_KEY, expires=True):
		cache.set_value(_CHECK_CACHE_KEY, "1", expires_in_sec=_CHECK_CACHE_TTL_S)
		try:
			conn = admin_client.get_connection(timeout_s=8) or {}
			release_notice.persist(conn.get("release_notice") or {})
			persist_from_connection(conn)
		except Exception:
			frappe.log_error(title="maintenance_notice.check failed", message=frappe.get_traceback())
	return boot_payload()
