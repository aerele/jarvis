"""SPA-facing endpoints for the private-network onboarding (design spec 5.2, 6.1, 6.6, 8).

Every endpoint is gated on ``require_jarvis_admin`` like ``onboarding.start_signup``: the
person configuring Jarvis for the whole site. They are thin on purpose - the browser never
holds admin credentials and never chooses a network target.

Expected refusals are DATA, not exceptions: ``{"ok": False, "code": ..., "message": ...}``
with HTTP 200. frappe-ui's ``call`` discards the body of a 4xx, and the SPA branches on the
code (``PRIVATE_HOLD_UNAVAILABLE`` re-reserves, ``EMAIL_NOT_CONFIRMED`` waits), so a thrown
error would lose exactly the part the screen needs. Unexpected failures (a permission error,
a bug) still raise.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

import frappe

from jarvis import admin_client, onboarding_contract
from jarvis.exceptions import AdminRateLimitedError, AdminUnreachableError
from jarvis.network_address import check_public_address, private_test_mode
from jarvis.onboarding import _ADMIN_ERRORS
from jarvis.permissions import require_jarvis_admin

# Short budget for the guest probes: a blocked network DROPs packets, so a long timeout
# would freeze the first screen the customer sees.
_PROBE_TIMEOUT_S = 5
_MAX_ADMIN_IPS = 8
_UNEXPECTED = "Jarvis returned an unexpected answer. Please try again."
_BUSY = "Too many attempts. Try again in a minute."
# Codes the SPA has copy for. Anything else (an admin newer than this bench, INVALID_REQUEST
# from an endpoint that has no field to blame) reads as the generic failure.
_KNOWN_CODES = frozenset(
	{
		"EMAIL_NOT_CONFIRMED",
		"NOT_PRIVATE_CUSTOMER",
		"PRIVATE_NETWORK_DISABLED",
		"PRIVATE_HOLD_UNAVAILABLE",
		"PRIVATE_ADDRESS_TAKEN",
		"PRIVATE_ADDRESS_INVALID",
		"CONNECTION_CHECK_BUSY",
		"RATE_LIMITED_PRIVATE_NETWORK",
	}
)


def _refusal(err) -> dict:
	"""One admin_client exception -> the soft refusal the SPA reads. Uses the bench's one
	contract reader (``onboarding_contract.error_object``), which keeps admin's code on a
	contract error, a coded auth refusal and a coded rate limit. A code this bench has no copy
	for gets the generic message (admin's sentence may describe a state the screen cannot act
	on), but keeps its code for the log."""
	error, _status = onboarding_contract.error_object(err)
	code = error.get("code") or ""
	# BENCH_ADMIN_REJECTED is the catch-all for prose with no code; never show that raw.
	known = code in _KNOWN_CODES or (code.startswith("BENCH_") and code != "BENCH_ADMIN_REJECTED")
	message = (error.get("message") or "") if known else ""
	out = {"ok": False, "code": code, "message": message or _UNEXPECTED}
	if error.get("retry_after_seconds"):
		out["retry_after_seconds"] = error["retry_after_seconds"]
	return out


def _clean_ipv4(value) -> str:
	try:
		ip = ipaddress.ip_address(str(value or "").strip())
	except ValueError:
		return ""
	return str(ip) if ip.version == 4 else ""


def _probe_admin() -> dict:
	"""``{reachable, enabled, caller_ip}`` from one guest ``private_setup_info`` call."""
	try:
		info = admin_client.private_setup_info(timeout_s=_PROBE_TIMEOUT_S) or {}
	except AdminUnreachableError:
		# Includes AdminAmbiguousError (a read timeout): either way this site cannot hold a
		# conversation with admin.
		return {"reachable": False, "enabled": False, "caller_ip": ""}
	except AdminRateLimitedError:
		# Frappe's stock limiter answered: admin is up and merely busy with this caller.
		return {"reachable": True, "enabled": False, "caller_ip": "", "message": _BUSY}
	except _ADMIN_ERRORS:
		# Admin ANSWERED with a refusal: an older admin without the method, a rate limit. It
		# is reachable, and private setup is not on offer.
		return {"reachable": True, "enabled": False, "caller_ip": ""}
	return {
		"reachable": True,
		"enabled": bool(info.get("enabled")),
		"caller_ip": _clean_ipv4(info.get("caller_ip")),
	}


@frappe.whitelist()
def network_status() -> dict:
	"""What the first onboarding screen needs: is the host public, is Jarvis reachable from
	here, is the private setup already done. Returns no address of the Jarvis setup server
	(that waits for the customer to pick the private option, ``private_setup_targets``).

	``admin_reachable`` is probed only on a bench that is neither public nor already set up;
	``None`` means "not asked", never a guess."""
	require_jarvis_admin()
	host_public = bool(admin_client._public_host_configured())
	address = admin_client.private_public_address()
	reachable = None
	if not host_public and not address:
		reachable = _probe_admin()["reachable"]
	return {
		"host_public": host_public,
		"admin_reachable": reachable,
		"network_mode": "private" if address else "",
		"public_address": address,
		"test_mode": private_test_mode(),
	}


@frappe.whitelist()
def check_admin_connection() -> dict:
	"""The "Check connection" button: can this site reach admin, is private setup switched
	on, and what public address does admin see us calling from (pre-fills the next screen)."""
	require_jarvis_admin()
	return _probe_admin()


def _configured_admin_ips() -> list[str]:
	raw = frappe.conf.get("jarvis_admin_ips") or []
	if isinstance(raw, str):
		raw = raw.split(",")
	return [ip for ip in dict.fromkeys(_clean_ipv4(v) for v in raw) if ip][:_MAX_ADMIN_IPS]


def _resolve_ipv4(host: str) -> list[str]:
	"""IPv4 addresses of ``host``: a literal as-is, else a server-side lookup (containers
	leave over IPv4 only), else the site config ``jarvis_admin_ips`` fallback for a network
	whose own DNS cannot resolve it."""
	if not host:
		return []
	literal = _clean_ipv4(host)
	if literal:
		return [literal]
	try:
		infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
	except OSError:
		return _configured_admin_ips()
	found = sorted({_clean_ipv4(info[4][0]) for info in infos} - {""})
	return found[:_MAX_ADMIN_IPS] or _configured_admin_ips()


@frappe.whitelist()
def private_setup_targets() -> dict:
	"""The Jarvis setup server the customer's network team must allow: the host name and its
	IPv4 addresses. Called only after the customer picks the private option, so the address
	is not on the first screen (design 11). Resolved here, not in the browser, from the same
	admin URL ``admin_client`` uses."""
	require_jarvis_admin()
	configured = frappe.db.get_single_value("Jarvis Settings", "jarvis_admin_url")
	url = admin_client._admin_url(frappe._dict(jarvis_admin_url=configured))
	host = (urlsplit(url).hostname or "").lower()
	return {"admin_host": host, "admin_ips": _resolve_ipv4(host)}


@frappe.whitelist()
def save_public_address(address: str = "") -> dict:
	"""Validate the customer's public address and make this bench a private one: sets
	``network_mode`` to Private and stores the canonical address. ``set_single_value`` and not
	a document save: Jarvis Settings' ``on_update`` pushes credentials to the container, and
	onboarding must not retrigger it."""
	require_jarvis_admin()
	if admin_client._public_host_configured():
		return {
			"ok": False,
			"code": "PUBLIC_HOST_CONFIGURED",
			"message": "This site already has a public host name, so it doesn't need a private network setup.",
		}
	normalized, problem = check_public_address(address, test_mode=private_test_mode())
	if problem:
		return {"ok": False, "code": "PRIVATE_ADDRESS_INVALID", "message": problem}
	frappe.db.set_single_value(
		"Jarvis Settings",
		{"network_mode": "Private", "public_address": normalized},
		update_modified=False,
	)
	return {"ok": True, "public_address": normalized}
