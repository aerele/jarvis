"""Mirror the operator's per-host release notice locally and expose it to the SPA.

The control plane decides which notice applies; the bench stores it and renders it.
The bench also clears the gate itself once its own version reaches the target, so a
tenant that has updated is never stranded by an unreachable control plane.
"""

import frappe

from jarvis import __version__

SETTINGS = "Jarvis Settings"
_FIELDS = (
	"release_notice_active",
	"latest_jarvis_version",
	"release_notice_message",
)
_CHECK_CACHE_KEY = "jarvis:release_notice_checked"
_CHECK_CACHE_TTL_S = 30


def _version(raw) -> tuple:
	"""Dotted-int triple; unparseable => (0, 0, 0). Mirrors the control plane's
	compare so the two sides can't disagree about who is current."""
	try:
		parts = tuple(int(x) for x in str(raw or "").split(".")[:3])
	except (ValueError, TypeError):
		return (0, 0, 0)
	return (parts + (0, 0, 0))[:3]


def _already_current(target: str) -> bool:
	"""True when this bench provably reached `target`. Unparseable either side =>
	False, so a bad version leaves the notice up rather than silently lifting it."""
	local, want = _version(__version__), _version(target)
	if local == (0, 0, 0) or want == (0, 0, 0):
		return False
	return local >= want


def persist(notice: dict) -> None:
	"""Mirror the admin-sent notice onto Jarvis Settings. Best-effort; an empty
	dict clears it. Skips the write when nothing changed - the gate re-checks on a
	timer, and churning `modified` would collide with an operator editing the
	Settings form."""
	try:
		n = notice or {}
		fresh = {
			"release_notice_active": 1 if n.get("active") else 0,
			"latest_jarvis_version": n.get("version") or "",
			"release_notice_message": n.get("message") or "",
		}
		current = frappe.db.get_value(SETTINGS, SETTINGS, list(_FIELDS), as_dict=True) or {}
		if all(current.get(k) == v for k, v in fresh.items()):
			return
		frappe.db.set_value(SETTINGS, SETTINGS, fresh, update_modified=False)
	except Exception:
		pass


def boot_payload() -> dict:
	"""``release_notice`` for context.boot. The SPA gates on `active`."""
	row = frappe.get_cached_value(SETTINGS, SETTINGS, list(_FIELDS), as_dict=True) or {}
	target = (row.get("latest_jarvis_version") or "").strip()
	# Self-clear: this bench is already at the target, so don't wait on the control
	# plane to say so - otherwise an unreachable or mis-credentialed admin would
	# keep an updated tenant blocked with no way out.
	active = bool(row.get("release_notice_active")) and not _already_current(target)
	return {
		"active": active,
		"version": target,
		"message": row.get("release_notice_message") or "",
	}


@frappe.whitelist(methods=["POST"])
def check() -> dict:
	"""Re-pull the notice from admin and return the refreshed payload.

	The gate polls this so an updated tenant unblocks promptly - the mobile PWA has
	no other refresh path and an open tab has none at all. The admin round-trip is
	cached briefly so many gated tabs cost one call."""
	from jarvis import admin_client

	cache = frappe.cache()
	if not cache.get_value(_CHECK_CACHE_KEY):
		cache.set_value(_CHECK_CACHE_KEY, "1", expires_in_sec=_CHECK_CACHE_TTL_S)
		try:
			conn = admin_client.get_connection(timeout_s=8) or {}
			persist(conn.get("release_notice") or {})
		except Exception:
			pass
	return boot_payload()
