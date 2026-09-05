"""Mirror the operator's per-host release notice locally and expose it to the SPA.

The control plane decides which notice applies; the bench stores it and renders it.
The bench also clears the gate itself once its own version reaches the target, so a
tenant that has updated is never stranded by an unreachable control plane.
"""

import frappe
from frappe.utils import cint

from jarvis import __version__

SETTINGS = "Jarvis Settings"
_FIELDS = (
	"release_notice_active",
	"latest_jarvis_version",
	"release_notice_message",
	"release_notice_tier",
	"release_notice_behind",
	"release_banner_interval_days",
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
			# Back-compat: an old CP omits `tier`, so derive it from `active` -- a hard
			# gate still reads "hard", everything else "none".
			"release_notice_tier": n.get("tier") or ("hard" if n.get("active") else "none"),
			# `behind` powers the pill/banner copy; an old CP omits it -> 0. The banner
			# reappear interval is operator-tunable, defaulting to 7 when absent/0.
			"release_notice_behind": cint(n.get("behind")),
			"release_banner_interval_days": cint(n.get("banner_interval_days")) or 7,
		}
		current = frappe.db.get_value(SETTINGS, SETTINGS, list(_FIELDS), as_dict=True) or {}
		if all(current.get(k) == v for k, v in fresh.items()):
			return
		frappe.db.set_value(SETTINGS, SETTINGS, fresh, update_modified=False)
	except Exception:
		pass


def boot_payload() -> dict:
	"""``release_notice`` for context.boot. The SPA gates the hard lockout on
	`active`; the always-on pill and the soft banner derive their display state
	from `version`/`tier`/`behind` (no `state` travels on the wire)."""
	row = frappe.get_cached_value(SETTINGS, SETTINGS, list(_FIELDS), as_dict=True) or {}
	target = (row.get("latest_jarvis_version") or "").strip()
	# Self-clear: this bench is already at the target, so don't wait on the control
	# plane to say so - otherwise an unreachable or mis-credentialed admin would
	# keep an updated tenant blocked with no way out. Clears BOTH tiers and behind.
	current = _already_current(target)
	active = bool(row.get("release_notice_active")) and not current
	tier = "none" if current else (row.get("release_notice_tier") or ("hard" if active else "none"))
	return {
		"active": active,
		"version": target,
		"message": row.get("release_notice_message") or "",
		"tier": tier,
		"behind": 0 if current else cint(row.get("release_notice_behind")),
		"banner_interval_days": cint(row.get("release_banner_interval_days")) or 7,
	}


@frappe.whitelist(methods=["POST"])
def check() -> dict:
	"""Re-pull the notice from admin and return the refreshed payload.

	The gate polls this so an updated tenant unblocks promptly - the mobile PWA has
	no other refresh path and an open tab has none at all. The admin round-trip is
	cached briefly so many gated tabs cost one call."""
	from jarvis import admin_client

	cache = frappe.cache()
	if not cache.get_value(_CHECK_CACHE_KEY, expires=True):
		cache.set_value(_CHECK_CACHE_KEY, "1", expires_in_sec=_CHECK_CACHE_TTL_S)
		try:
			conn = admin_client.get_connection(timeout_s=8) or {}
			persist(conn.get("release_notice") or {})
		except Exception:
			pass
	return boot_payload()


@frappe.whitelist(methods=["POST"])
def notes() -> dict:
	"""Cumulative "what's new" notes for this bench's major line, fetched on demand
	when the customer opens the panel. The bench owns the version, so the client
	passes nothing. Any admin failure (unreachable, or an ``AdminAuthError`` from a
	lapsed customer behind a stale pill) degrades to an empty list - the panel shows
	a friendly error, never raw control-plane prose - and is logged."""
	from jarvis import admin_client

	major = _version(__version__)[0]
	if major < 15:
		return {"notes": []}
	try:
		res = admin_client.get_release_notes(str(major), __version__, timeout_s=8) or {}
	except Exception:
		try:
			frappe.log_error(
				title="release_notice.notes: admin unreachable",
				message=frappe.get_traceback(),
			)
		except Exception:
			pass
		return {"notes": []}
	return {"notes": res.get("notes") or []}
