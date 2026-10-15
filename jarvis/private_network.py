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
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
import socket
from urllib.parse import urlsplit

import frappe
import requests
from frappe.utils import get_datetime, now_datetime

from jarvis import admin_client, onboarding_contract
from jarvis.exceptions import (
	AdminContractError,
	AdminRateLimitedError,
	AdminUnreachableError,
	AdminValidationError,
)
from jarvis.network_address import check_public_address, private_test_mode
from jarvis.onboarding import _ADMIN_ERRORS
from jarvis.permissions import require_jarvis_admin

# Short budget for the guest probes: a blocked network DROPs packets, so a long timeout
# would freeze the first screen the customer sees.
_PROBE_TIMEOUT_S = 5
_MAX_ADMIN_IPS = 8
# A resolver that hangs must not hold a web worker.
_DNS_TIMEOUT_S = 4
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
		info = admin_client.private_setup_info(timeout_s=_PROBE_TIMEOUT_S)
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
	if not isinstance(info, dict):
		return {"reachable": False, "enabled": False, "caller_ip": ""}
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
	pool = ThreadPoolExecutor(max_workers=1)
	try:
		infos = pool.submit(socket.getaddrinfo, host, None, socket.AF_INET, socket.SOCK_STREAM).result(
			timeout=_DNS_TIMEOUT_S
		)
	except (OSError, FutureTimeout):
		return _configured_admin_ips()
	finally:
		pool.shutdown(wait=False)
	found = sorted({_clean_ipv4(info[4][0]) for info in infos} - {""})
	return found[:_MAX_ADMIN_IPS] or _configured_admin_ips()


@frappe.whitelist()
def private_setup_targets() -> dict:
	"""The Jarvis setup server the customer's network team must allow: the host name and its
	IPv4 addresses. Called only after the customer picks the private option, so the address
	is not on the first screen (design 11). Resolved here, not in the browser, from the same
	admin URL ``admin_client`` uses."""
	require_jarvis_admin()
	if admin_client._public_host_configured():
		# A public bench never shows the setup server's address, even to an admin.
		return {"admin_host": "", "admin_ips": []}
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


_NO_ADDRESS = "Jarvis could not get the address of your server. Please contact support."


def _hold_is_live(hold: dict) -> bool:
	"""A stored hold that can still be shown: not yet expired, or a tenant's own host
	(``hold_until`` null), which admin never expires."""
	until = hold.get("hold_until")
	if not until:
		return bool(hold)
	try:
		return get_datetime(until) > now_datetime()
	except (ValueError, TypeError):
		return False


def _reserve_refusal(err) -> dict:
	"""``_refusal``, except that a rate limit with a live stored hold is answered from the
	store: admin counts every reserve call (10 per hour), even a read of a live hold, and
	the SPA calls this on every screen entry and retry."""
	out = _refusal(err)
	if out["code"] == "RATE_LIMITED_PRIVATE_NETWORK":
		stored = onboarding_contract.private_hold()
		if stored and _hold_is_live(stored):
			return {"ok": True, **stored, "cached": True}
	return out


@frappe.whitelist()
def reserve_host() -> dict:
	"""The host admin holds for this customer: ``{ok, host_ip, reach_url, hold_until}``.

	Admin makes the hold when the email link is clicked, and here if that job has not run
	yet. A repeat call during a live hold returns the same hold; for a customer who already
	has a tenant it returns that tenant's host with ``hold_until`` null. The answer is stored
	in the signup context so ``start_connection_test`` reads its outbound target from admin's
	own words, never from the browser. The SPA calls this on entering the AllowServer screen
	and the Connect panel, which is also what refreshes a hold that expired meanwhile."""
	require_jarvis_admin()
	try:
		raw = admin_client.reserve_private_host()
	except _ADMIN_ERRORS as e:
		return _reserve_refusal(e)
	hold = onboarding_contract.store_private_hold(raw)
	if hold:
		return {"ok": True, **hold}
	if isinstance(raw, dict) and not raw.get("host_ip") and not raw.get("reach_url"):
		# A tenant host admin has no public address for: not a malformed answer.
		message = _NO_ADDRESS
	else:
		message = _UNEXPECTED
	return {"ok": False, "code": onboarding_contract.BENCH_ADMIN_REJECTED, "message": message}


# --------------------------------------------------------------------------- #
# the connection test (design spec 6.1 and 6.6)
# --------------------------------------------------------------------------- #
OUTBOUND_TIMEOUT_S = 5
_DNS_MARKERS = (
	"name or service not known",
	"temporary failure in name resolution",
	"nodename nor servname",
	"getaddrinfo failed",
	"name resolution",
)
_CHECK_RE = re.compile(r"JCC-\d{1,12}")


def _connection_reason(err) -> str:
	"""A requests ConnectionError -> the reason the SPA words. Anything that is neither a name
	failure nor a refusal ("no route", "network unreachable") reads as a timeout: from the
	customer's chair it is the same "couldn't connect", and a firewall DROP is a timeout."""
	text = str(err).lower()
	if any(marker in text for marker in _DNS_MARKERS):
		return "dns"
	if "refused" in text or "reset by peer" in text:
		return "refused"
	return "timeout"


def run_outbound_check(reach_url: str, *, timeout_s: int = OUTBOUND_TIMEOUT_S) -> dict:
	"""Can THIS bench reach the held host over HTTPS? ``GET reach_url`` with a 5 s timeout and
	normal certificate checks; a completed TLS handshake and ANY HTTP status mean pass (Traefik
	answers a host name with no route). ``reach_url`` must come from admin's stored answer,
	never from the browser. ``http`` is accepted only in local test mode. No redirect is
	followed and no body is read."""
	allowed = ("https", "http") if private_test_mode() else ("https",)
	try:
		parts = urlsplit(reach_url or "")
		hostname = parts.hostname
	except (ValueError, AttributeError):
		return {"ok": False, "reason": "bad_target"}
	if parts.scheme not in allowed or not hostname:
		return {"ok": False, "reason": "bad_target"}
	try:
		response = requests.get(
			reach_url,
			timeout=timeout_s,
			allow_redirects=False,
			stream=True,
			headers={"User-Agent": "jarvis-bench-reach-check/1"},
		)
	except requests.exceptions.SSLError:
		return {"ok": False, "reason": "tls"}
	except requests.exceptions.Timeout:
		return {"ok": False, "reason": "timeout"}
	except requests.exceptions.ConnectionError as e:
		return {"ok": False, "reason": _connection_reason(e)}
	except requests.exceptions.RequestException:
		return {"ok": False, "reason": "bad_target"}
	response.close()
	return {"ok": True, "reason": "ok"}


@frappe.whitelist()
def start_connection_test() -> dict:
	"""Start both halves of the connection test at once (design 6.1).

	Inbound: admin's fleet host calls this bench's public address (``start_connection_check``,
	bound to this site's name). Outbound: this bench calls the held host (``run_outbound_check``
	against the stored ``reach_url``). Takes no argument on purpose - the browser chooses
	nothing. With no live stored hold it reserves one first (which also refreshes a hold that
	expired or moved), so a reload cannot strand the test. The SPA then polls
	``connection_test_status`` for the inbound half; the outbound result is final here."""
	require_jarvis_admin()
	hold = onboarding_contract.private_hold()
	if not hold or not _hold_is_live(hold):
		reserved = reserve_host()
		if not reserved.get("ok"):
			return reserved
		hold = onboarding_contract.private_hold()
	try:
		started = admin_client.start_connection_check(site_name=frappe.local.site)
	except _ADMIN_ERRORS as e:
		return _refusal(e)
	check = str((started or {}).get("check") or "").strip() if isinstance(started, dict) else ""
	if not check:
		return {"ok": False, "code": onboarding_contract.BENCH_ADMIN_REJECTED, "message": _UNEXPECTED}
	return {"ok": True, "check": check, "outbound": run_outbound_check(hold.get("reach_url"))}


def _is_unknown_check(err) -> bool:
	"""Admin answers an unknown or foreign check name with a bare 404: a rejection with no
	contract code (every coded refusal arrives as ``AdminContractError``)."""
	return isinstance(err, AdminValidationError) and not isinstance(err, AdminContractError)


@frappe.whitelist()
def connection_test_status(check: str = "") -> dict:
	"""The inbound half's state, proxied from admin: ``{ok, status, reason, checked_at}``.
	An unknown or foreign check name is a bare 404 from admin; it reads as an expired result."""
	require_jarvis_admin()
	if not (isinstance(check, str) and _CHECK_RE.fullmatch(check)):
		return {"ok": False, "code": "INVALID_REQUEST", "message": "That test could not be found."}
	try:
		state = admin_client.get_connection_check(check)
	except _ADMIN_ERRORS as e:
		if _is_unknown_check(e):
			return {"ok": True, "status": "Failed", "reason": "stale", "checked_at": ""}
		return _refusal(e)
	state = state if isinstance(state, dict) else {}
	return {
		"ok": True,
		"status": str(state.get("status") or ""),
		"reason": str(state.get("reason") or ""),
		"checked_at": str(state.get("checked_at") or ""),
	}
