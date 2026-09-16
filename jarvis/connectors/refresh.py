"""Tool-list freshness for a connector row (MCP spec 2026-07-28, caching
utility): decide when the cached ``tools/list`` is stale and re-run the Test
probe in the BACKGROUND, never inside a chat turn.

The cache is considered stale once ``tools_cached_at + ttl`` has passed, where
``ttl`` is the server's ``ttlMs`` hint stored at Test time or, when the server
gave none (every legacy server), a house default. A call that came back with an
unknown action, a JSON-RPC error or a protocol mismatch is the spec's "reason to
believe the data has changed" and triggers a refresh regardless of the TTL. A
busy connector is debounced through a Redis flag so one stale cache queues one
job, not one per chat turn, and a stable ``job_id`` keeps a second job off the
queue while the first is still queued or running.

The job re-runs the probe (re-detecting the era) and persists it through the
same helper the Settings Test button uses, which can only add default-off rows
or preserve an admin's earlier choice (never grant a write) and, in this
background mode, writes only on success. The after-call refresh runs as the user
who triggered it (``frappe.enqueue`` captures the session user), so a Personal
row and a per-user OAuth token resolve exactly as they did for the call; the
daily :func:`reprobe_all` sweep sets the credential context itself per row.
"""

from __future__ import annotations

from datetime import timedelta

import frappe
from frappe.utils import get_datetime, now_datetime

from jarvis.connectors import oauth

CONNECTOR_DOCTYPE = "Jarvis Connector"
OAUTH_TOKEN_DOCTYPE = "MCP OAuth Token"

#: Freshness assumed when the server sends no ``ttlMs`` (spec default is 0, i.e.
#: immediately stale; a daily re-list is our own heuristic for legacy servers).
DEFAULT_TTL_S = 24 * 60 * 60
#: Debounce window: at most one refresh job per row in this many seconds.
DEBOUNCE_S = 300
#: Error codes from ``broker.call`` that say the cached list no longer matches the
#: server. Deliberately NOT http_error (a 401/403 would loop on every call) and
#: NOT invalid_arguments (that is our OWN local check against a possibly-stale
#: cache, not the server telling us the tool changed).
_REFRESH_ON_ERROR = frozenset({"action_unknown", "rpc_error", "protocol_error"})

JOB_METHOD = "jarvis.connectors.refresh.refresh_tools_cache"


def after_call(row, error_code: str) -> None:
	"""Queue a background refresh for ``row`` when its tool cache is stale or the
	call's outcome suggests it drifted. Never raises into the chat turn: even the
	fallback warning is wrapped, because ``frappe.logger`` itself can fail (e.g. in a
	bare unit-test process with no writable log directory)."""
	try:
		if error_code in _REFRESH_ON_ERROR or is_stale(row):
			request(row.name)
	except Exception:
		try:
			frappe.logger("jarvis.connectors").warning("connector refresh scheduling failed", exc_info=True)
		except Exception:
			pass


def is_stale(row, now=None) -> bool:
	"""True when the row's cached tool list is past its freshness window."""
	cached_at = row.get("tools_cached_at")
	if not cached_at or not row.get("tools_cache"):
		return False  # nothing cached yet: Test connection owns the first fill
	ttl_s = ttl_seconds(row.get("tools_ttl_ms"))
	now = now or now_datetime()
	return get_datetime(cached_at) + timedelta(seconds=ttl_s) < get_datetime(now)


def ttl_seconds(ttl_ms) -> float:
	"""The server's ``ttlMs`` hint as seconds; absent, zero or negative falls to the
	house default (spec: 0 means immediately stale, which for us would mean a probe on
	every call, so the daily default is the deliberate compromise)."""
	try:
		ttl = int(ttl_ms or 0)
	except (TypeError, ValueError):
		ttl = 0
	return ttl / 1000.0 if ttl > 0 else DEFAULT_TTL_S


def request(row_name: str) -> bool:
	"""Enqueue one refresh for ``row_name`` unless one was queued in the last
	``DEBOUNCE_S`` seconds (Redis SET NX window) or one is already queued/running (a
	stable ``job_id`` with ``deduplicate``). Returns True when a job was queued."""
	if not _claim(row_name):
		return False
	# Queued NOW, not after commit: the debounce claim above is already taken, so a
	# job deferred to commit time and then lost to a rollback of the calling
	# request would leave the claim blocking every retry for DEBOUNCE_S. The job
	# reads the row afresh on its own connection, so it needs nothing from the
	# calling transaction.
	frappe.enqueue(
		JOB_METHOD,
		queue="short",
		job_id=_job_id(row_name),
		deduplicate=True,
		name=row_name,
	)
	return True


def _job_id(row_name: str) -> str:
	# One in-flight refresh per connector row. Kept colon-free: frappe's
	# create_job_id rewrites ':' to '|', and a stable, readable id is easier to trace.
	return f"jarvis-connector-refresh-{row_name}"


def _claim(row_name: str) -> bool:
	"""Set-if-absent on the debounce flag: the first caller wins the window."""
	return bool(frappe.cache().set(_debounce_key(row_name), 1, ex=DEBOUNCE_S, nx=True))


def _debounce_key(row_name: str) -> str:
	return frappe.cache().make_key(f"jarvis:connectors:refresh:{row_name}")


def refresh_tools_cache(name: str) -> None:
	"""Background job: re-probe the connector and persist the fresh tool list.
	``get_doc`` (not a plain read) because ``broker.test_connector`` needs the
	credential off the Document and the persist path writes the row and its child
	table. The worker commits; no explicit commit here. Background persist writes only
	on success and never flips the row to Failed (see ``persist_probe_result``)."""
	from jarvis.chat import connectors_api  # circular at import time (api imports broker)
	from jarvis.connectors import broker

	try:
		doc = frappe.get_doc(CONNECTOR_DOCTYPE, name)
	except frappe.DoesNotExistError:
		return
	if not doc.get("enabled"):
		return
	result = broker.test_connector(doc)
	# can_write=True on purpose: this is the server's own refresh of a server-derived
	# cache, not a user's edit, so the button's has_permission("write") gate does not
	# apply (the job may run as a Personal row's owner who only reads the row).
	connectors_api.persist_probe_result(doc, result, now_datetime(), can_write=True, background=True)


def reprobe_all() -> None:
	"""Daily scheduler sweep: queue a background re-list for every enabled,
	previously-Passed connector so a cache goes stale in the background rather
	than mid-chat. The sweep only ENQUEUES (through :func:`request`, so the
	per-row ``job_id`` + ``deduplicate`` serialises it against a chat-turn refresh
	of the same row: ``_replace_allowed_actions`` is a delete-then-insert with no
	row lock, and two concurrent rewriters could leave the table empty or
	duplicated); the worker does the probe, one row per job, so a large fleet never
	holds one worker for N x 20 s. A row refreshed by a chat turn in the last
	``DEBOUNCE_S`` seconds is skipped by the same claim.

	``frappe.enqueue`` captures the session user, so each row is queued under the
	credential context a real call would use:
	  * a token/open row carries its credential on the row itself, so it runs as
	    ``Administrator`` (system context);
	  * a Personal OAuth row runs as its owner (who holds the per-user token);
	  * a Shared OAuth row runs as the user holding the most recently modified
	    ``MCP OAuth Token`` for that connector, and is SKIPPED when nobody has signed
	    in (there is no bearer to probe with).
	``frappe.set_user`` is restored in a ``finally``. The scheduler is paused locally,
	so run this by hand: ``bench --site e2e2.localhost execute
	jarvis.connectors.refresh.reprobe_all`` (then the queued jobs run on the workers)."""
	# System (scheduler) context: get_all is intentional here. There is no session
	# user to scope get_list to, and every row is queued under a credential context
	# this function sets explicitly below.
	rows = frappe.get_all(
		CONNECTOR_DOCTYPE,
		filters={"enabled": 1, "last_test_status": "Passed"},
		fields=["name", "scope", "auth_method", "owner"],
	)
	if not rows:
		return
	shared_owners = _shared_oauth_owners([r["name"] for r in rows if _is_shared_oauth(r)])
	previous_user = frappe.session.user
	for row in rows:
		as_user = _reprobe_user(row, shared_owners)
		if not as_user:
			continue  # Shared OAuth with no stored sign-in: nobody to run as.
		try:
			frappe.set_user(as_user)
			request(row["name"])
		except Exception:
			try:
				frappe.logger("jarvis.connectors").warning(
					f"connector reprobe could not be queued for {row['name']}", exc_info=True
				)
			except Exception:
				pass
		finally:
			frappe.set_user(previous_user)


def _is_shared_oauth(row) -> bool:
	return row.get("scope") == "Shared" and (row.get("auth_method") or "") == oauth.OAUTH_AUTH_METHOD


def _reprobe_user(row, shared_owners: dict[str, str]) -> str | None:
	"""The user to run ``row``'s re-probe as, or ``None`` to skip it."""
	if (row.get("auth_method") or "") != oauth.OAUTH_AUTH_METHOD:
		return "Administrator"  # credential lives on the row; system context is right.
	if row.get("scope") == "Personal":
		return row.get("owner")
	return shared_owners.get(row["name"])


def _shared_oauth_owners(connector_names: list[str]) -> dict[str, str]:
	"""For each Shared OAuth connector, the user holding its most recently modified
	``MCP OAuth Token``. ONE query (system/scheduler context, so get_all is fine), then
	first-seen wins over a modified-desc ordering, so there is no get_value in a loop
	and no raw SQL."""
	if not connector_names:
		return {}
	tokens = frappe.get_all(
		OAUTH_TOKEN_DOCTYPE,
		filters={"connector": ["in", connector_names]},
		fields=["connector", "user"],
		order_by="modified desc",
	)
	owners: dict[str, str] = {}
	for token in tokens:
		owners.setdefault(token["connector"], token["user"])
	return owners
