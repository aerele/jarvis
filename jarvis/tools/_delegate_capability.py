"""JF-017 — the delegate run's CAPABILITY CONTRACT: snapshotted at launch,
enforced at the BENCH.

Before this module the container-side ``tools.allow`` was the ONLY thing bounding
which ``jarvis__*`` tools a marketplace-agent delegate could call. That list is
CONFIGURATION (rendered into openclaw.json by fleet-agent), not authorization: a
compromised container / plugin, or anyone holding a leaked per-run session bearer,
could call ANY registered tool the run-as user's Frappe permissions happened to
allow — ``jarvis__delete_doc`` from an auditor whose contract is four read tools.
The bench never compared the call against the manifest.

Worse, the write guard (``_delegate_write_caps``) read the CURRENT
``Jarvis Agent Listing.nature``/``.writes`` at call time, so editing a listing
mid-run silently re-authorised an IN-FLIGHT run.

The fix is one idea applied twice:

  * **Snapshot at launch.** ``agent_scheduler._launch_audit`` stamps the whole
    contract onto the ``Jarvis Agent Run`` row — ``tools_allow`` (the exact
    bundled-registry list), ``nature`` and ``writes`` (from the listing at that
    instant) — under the SAME immutability guard as the other launch-provenance
    fields (PP-5 ``bundle_version`` / ``preparation_mode`` / ``initiating_human``).
  * **Enforce from the snapshot.** ``jarvis.api._dispatch_from_session`` refuses
    any tool outside the snapshotted ``tools_allow`` BEFORE ``_run_tool``, and
    ``_delegate_write_caps`` reads nature/writes from the snapshot, never the live
    listing. A listing edited mid-run therefore cannot change what an in-flight run
    may do, in either direction.

**Fail closed.** ``capability_contract`` records which regime a run is under:

  * ``snapshot`` — the contract is on the row; it is the authority. An empty /
    unparseable ``tools_allow_json`` authorises NOTHING.
  * ``legacy`` — the run pre-dates this change (stamped by
    ``v2_07_agent_run_capability_snapshot``); no tools_allow gate, and the write
    caps fall back to the live listing exactly as before, so runs in flight across
    the deploy are not killed.
  * blank — a run created after the patch with no contract stamped. That can only
    mean a launch path that bypassed ``_launch_audit`` (or a forged row), so it is
    refused outright. The pre-patch grace is bounded by the patch's own
    ``Patch Log`` timestamp: a blank-contract row created BEFORE the patch ran is
    treated as legacy (it raced the deploy), one created after is not.

Non-delegate callers are untouched: a session_key with no bound
``Jarvis Agent Run`` resolves to None and every helper here is a no-op, so
standard chat / macros / direct-Python keep the ordinary Frappe permission engine
as their only gate.
"""

from __future__ import annotations

import json

import frappe

RUN = "Jarvis Agent Run"
LISTING = "Jarvis Agent Listing"

# ``capability_contract`` values (Select on Jarvis Agent Run).
CONTRACT_SNAPSHOT = "snapshot"
CONTRACT_LEGACY = "legacy"

# The patch that stamps every pre-existing run ``legacy``. Its Patch Log row's
# ``creation`` is the cutover instant used to classify a blank-contract row: the
# ONLY runs allowed the legacy fallback are the ones that already existed when the
# guard landed.
LEGACY_CUTOFF_PATCH = "jarvis.patches.v2_07_agent_run_capability_snapshot"

# Registry/manifest tool ids are openclaw-facing (``jarvis__get_doc``); the bench
# dispatches by the bare tool name (``get_doc``). Entries that are not jarvis tools
# at all (``exec``, ``canvas``, ``message`` — container-side) simply never match a
# bench tool name.
_TOOL_PREFIX = "jarvis__"


def normalize_tool(name) -> str:
	"""A registry/manifest tool id reduced to the bench dispatch name."""
	value = str(name or "").strip()
	return value[len(_TOOL_PREFIX) :] if value.startswith(_TOOL_PREFIX) else value


def bench_tools(tools_allow) -> set:
	"""The BENCH tool names a declared surface authorises.

	ONLY ``jarvis__``-prefixed entries count. The rest of a manifest's
	``tools_allow`` (``exec``, ``canvas``, ``message``) names openclaw's own tools,
	which never reach ``call_tool`` — so an unprefixed entry must never be able to
	satisfy a bench tool name, today or the day a bench tool happens to be called
	``message``."""
	return {normalize_tool(t) for t in tools_allow if str(t or "").strip().startswith(_TOOL_PREFIX)}


def _parse_list(raw) -> list:
	"""A stored JSON list -> a Python list. Anything else (None, malformed JSON, a
	dict) -> ``[]``, which authorises nothing under a ``snapshot`` contract."""
	if not raw:
		return []
	if isinstance(raw, list):
		return list(raw)
	try:
		parsed = json.loads(raw)
	except (TypeError, ValueError):
		return []
	return list(parsed) if isinstance(parsed, list) else []


def parse_writes(raw) -> list:
	"""The declarative write contract -> a list of ``{doctype, mode, ...}`` dicts.
	A malformed / non-list value is an EMPTY contract (fail-closed: an operator with
	an unreadable contract writes nothing)."""
	return [w for w in _parse_list(raw) if isinstance(w, dict) and w.get("doctype")]


# --------------------------------------------------------------------------- #
# launch-time snapshot
# --------------------------------------------------------------------------- #
def contract_for_launch(listing) -> dict:
	"""The capability contract to stamp on a run being launched for ``listing``.

	``tools_allow`` comes from the BUNDLED registry (it is delegate metadata that
	never enters the customer DB — the same source ``build_agent_push_payload``
	echoes into the container's enablement signal), stored VERBATIM so the run
	carries the exact declared list as provenance. ``nature``/``writes`` are the
	listing's values AT THIS INSTANT — the whole point of the snapshot is that a
	later edit cannot move them.

	Returns the three ``Jarvis Agent Run`` field values plus the contract marker,
	ready to splat into the insert."""
	from jarvis.chat.agent_catalog import registry_tools_allow

	return {
		"capability_contract": CONTRACT_SNAPSHOT,
		"tools_allow_json": frappe.as_json(registry_tools_allow(listing.agent_slug)),
		"capability_nature": (listing.nature or "").strip().lower(),
		"capability_writes_json": frappe.as_json(parse_writes(listing.writes)),
	}


# --------------------------------------------------------------------------- #
# resolution + enforcement
# --------------------------------------------------------------------------- #
def _legacy_cutoff():
	"""When ``LEGACY_CUTOFF_PATCH`` ran on this site, or None if it has not (yet).

	None means "no grace": on a site where the patch has not run there is nothing
	to grandfather that the patch would not itself have stamped, so a blank
	contract is refused."""
	try:
		return frappe.db.get_value("Patch Log", {"patch": LEGACY_CUTOFF_PATCH}, "creation")
	except Exception:
		return None


def _is_pre_patch(run_creation) -> bool:
	"""True iff a blank-contract run predates the guard and may use the legacy
	fallback. Bounded by the patch's own timestamp so a run created AFTER the
	cutover can never buy itself legacy authority by simply having no snapshot."""
	cutoff = _legacy_cutoff()
	if not cutoff or not run_creation:
		return False
	return frappe.utils.get_datetime(run_creation) < frappe.utils.get_datetime(cutoff)


def resolve(session_key: str | None = None) -> dict | None:
	"""The capability contract governing the CURRENT call, or None when the caller
	is not a delegate.

	Delegate iff a ``Jarvis Agent Run`` row is bound to the caller's session_key.
	The session_key is the delegate's opaque HTTPS bearer (never a model-supplied
	id, so a delegate can only ever act as its own run); ``session_key=None`` reads
	it from the request-scoped ``_agent_run_ctx``.

	Returns ``{run, agent, legacy, tools_allow, nature, writes, run_as}``. ``legacy``
	True means no snapshot governs this run — the caller falls back to pre-JF-017
	behaviour. A run that is neither snapshotted nor legacy yields an EMPTY contract
	with ``legacy`` False, i.e. everything is refused."""
	from jarvis.tools._agent_run_ctx import get_session_key

	key = session_key if session_key is not None else get_session_key()
	if not key:
		return None
	run = frappe.db.get_value(
		RUN,
		{"session_key": key},
		[
			"name",
			"agent",
			"creation",
			"capability_contract",
			"tools_allow_json",
			"capability_nature",
			"capability_writes_json",
		],
		as_dict=True,
	)
	if not run or not run.agent:
		return None  # no delegate run bound -> not a delegate; leave the caller untouched

	base = {"run": run.name, "agent": run.agent, "run_as": frappe.session.user}
	contract = (run.capability_contract or "").strip().lower()

	if contract == CONTRACT_LEGACY or (not contract and _is_pre_patch(run.creation)):
		# Pre-JF-017 run: nature/writes come from the live listing, as they did
		# before the snapshot existed, and no tools_allow gate applies.
		listing = frappe.db.get_value(LISTING, run.agent, ["nature", "writes"], as_dict=True) or {}
		return {
			**base,
			"legacy": True,
			"tools_allow": [],
			"nature": (listing.get("nature") or "").strip().lower(),
			"writes": parse_writes(listing.get("writes")),
		}

	if contract != CONTRACT_SNAPSHOT:
		# A post-cutover run with no (or an unrecognised) contract marker: no launch
		# path stamped it, so it holds NO authority. Deliberately not derived from
		# whatever sits in the snapshot columns — those are only trustworthy when the
		# marker says a launch wrote them.
		return {**base, "legacy": False, "tools_allow": [], "nature": "", "writes": []}

	return {
		**base,
		"legacy": False,
		"tools_allow": _parse_list(run.tools_allow_json),
		"nature": (run.capability_nature or "").strip().lower(),
		"writes": parse_writes(run.capability_writes_json),
	}


def tool_denial(session_key: str | None, tool: str) -> str | None:
	"""The refusal reason when this delegate may not call ``tool``, else None.

	None is also returned for a NON-delegate caller (nothing to enforce) and for a
	``legacy`` run (no snapshot to enforce against). Everything else is decided by
	the snapshot alone: a tool absent from it — even one the run-as user's Frappe
	roles would happily permit — is refused BEFORE dispatch."""
	cap = resolve(session_key)
	if cap is None or cap["legacy"]:
		return None
	name = normalize_tool(tool)
	if name and name in bench_tools(cap["tools_allow"]):
		return None
	return (
		f"agent '{cap['agent']}' is not permitted to call '{name or tool}': the tool is "
		"not in the capability contract snapshotted for this run at launch"
	)
