"""What admin tells this site about the tenant's runtime: whether the subscription pool
runs natively (no Bifrost or CLIProxy sidecars) and which agent runtime version the gateway is.

Admin sends ``native_subscription_pool`` (bool) and ``agent_runtime_version`` (``"2026.11.5"`` or
null) on every heartbeat reply and on the connection payload. They are stored on Jarvis
Settings and read from there, so the hot chat path makes no admin call. A key that is absent
(an older admin) keeps what is stored; the default is the sidecar behaviour.

The stored ``proxy_active`` is derived from the pool and from this fact, and the fact can
change with no Settings save (ops switch the flag in admin), so a change recomputes it."""

import re

import frappe
from frappe.utils import cint

SETTINGS = "Jarvis Settings"
NATIVE_FIELD = "native_subscription_pool"
VERSION_FIELD = "agent_runtime_version"

# First agent runtime release that renames a few provider ids (see jarvis.chat.gateway_ids).
GATEWAY_V11 = (2026, 11, 0)

_VERSION = re.compile(r"^\d{1,5}(\.\d{1,5}){2}$")


def _stored(settings, field):
	"""``field`` off a settings doc or dict; a stand-in without it reads as unset."""
	if settings is None:
		return frappe.get_cached_value(SETTINGS, SETTINGS, field)
	getter = getattr(settings, "get", None)
	return getter(field) if callable(getter) else getattr(settings, field, None)


def is_native_pool(settings=None) -> bool:
	"""True when admin runs this tenant's subscription pool natively."""
	value = _stored(settings, NATIVE_FIELD)
	# Only a stored 0/1 counts: a mock or other stand-in must not read as native.
	return isinstance(value, int | str) and bool(cint(value))


def agent_runtime_version(settings=None) -> tuple[int, ...] | None:
	"""The gateway's version as ``(2026, 11, 5)``, or None when admin did not report one."""
	raw = _stored(settings, VERSION_FIELD)
	raw = raw.strip() if isinstance(raw, str) else ""
	return tuple(int(p) for p in raw.split(".")) if _VERSION.match(raw) else None


def gateway_is_v11(settings=None) -> bool:
	"""True when the gateway is known to run agent runtime 2026.11 or later."""
	version = agent_runtime_version(settings)
	return version is not None and version >= GATEWAY_V11


def persist(reply) -> bool:
	"""Store admin's two facts from a heartbeat reply or a connection payload.

	A missing key keeps what is stored, and so does a value of the wrong type. Writes only on
	change and returns True when it wrote. When the native flag changed, ``proxy_active`` is recomputed, since a native tenant
	has no proxy. No commit: the caller's job commits."""
	if not isinstance(reply, dict):
		return False
	changes: dict = {}
	if NATIVE_FIELD in reply and isinstance(reply[NATIVE_FIELD], bool):
		changes[NATIVE_FIELD] = 1 if reply[NATIVE_FIELD] else 0
	if VERSION_FIELD in reply:
		raw = reply[VERSION_FIELD]
		if raw is None:
			changes[VERSION_FIELD] = ""
		elif isinstance(raw, str) and _VERSION.match(raw.strip()):
			changes[VERSION_FIELD] = raw.strip()
	if not changes:
		return False
	# Two cached field reads, not the whole Single; set_single_value below clears the cache.
	stored = frappe.get_cached_value(SETTINGS, SETTINGS, [NATIVE_FIELD, VERSION_FIELD], as_dict=True) or {}
	changes = {
		k: v
		for k, v in changes.items()
		if (cint(stored.get(k)) if k == NATIVE_FIELD else (stored.get(k) or "")) != v
	}
	if not changes:
		return False
	frappe.db.set_single_value(SETTINGS, changes, update_modified=False)
	if NATIVE_FIELD in changes:
		_refresh_proxy_active()
	return True


def _refresh_proxy_active() -> None:
	from jarvis.jarvis.pool_serialize import compute_proxy_active

	settings = frappe.get_single(SETTINGS)
	wanted = 1 if compute_proxy_active(settings) else 0
	if cint(settings.get("proxy_active")) != wanted:
		frappe.db.set_single_value(SETTINGS, "proxy_active", wanted, update_modified=False)


REFRESH_MIN_INTERVAL_S = 60
_REFRESH_CACHE_KEY = "jarvis:runtime_facts_refreshed"


def refresh_from_admin() -> bool:
	"""Read the two facts from admin now, through the same connection read the daily sync
	uses, and store them. For the moment a pin is refused and the stored facts may lag a
	flip by up to one heartbeat. A refresh that found NOTHING new is not repeated for a minute
	(a pin that stays refused must not cost an admin round trip every turn); one that found a
	change is never throttled, so a refusal soon after a real change still refreshes. True when
	the read and the store worked. Never raises."""
	try:
		if frappe.cache().get_value(_REFRESH_CACHE_KEY):
			return False
	except Exception:
		pass  # no cache, no throttle
	try:
		from jarvis import admin_client

		changed = persist(admin_client.get_connection(timeout_s=8) or {})
	except Exception:
		return False
	if not changed:
		try:
			frappe.cache().set_value(_REFRESH_CACHE_KEY, 1, expires_in_sec=REFRESH_MIN_INTERVAL_S)
		except Exception:
			pass
	return True
