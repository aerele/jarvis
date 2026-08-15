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
    ``Patch Log`` timestamp plus ``LEGACY_GRACE_SECONDS``: a blank-contract row
    created inside the deploy window is treated as legacy (it raced the migrate /
    restart), one created after it is not.

**A refused run is a DEAD run, and says so.** A contract that authorises no bench
tool refuses everything, ``record_agent_run`` included, so the run could never
finalize itself — it would sit ``running`` until the 3h stale-run sweep stamped
"exceeded max duration" on it, which is false. So: ``_launch_audit`` refuses the
LAUNCH outright when the bundled registry declares no tool surface (no
conversation, no run row, an error the human can act on), and ``tool_denial``
marks such a refusal ``fatal`` so the dispatcher terminalizes the run at the FIRST
refused call with the honest reason.

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

# DEPLOY-WINDOW GRACE. ``bench migrate`` finishing and the LAST worker restarting
# are not the same instant: in between, workers still running the old code launch
# runs that physically cannot carry a snapshot (the stamping code isn't loaded),
# and the reverse order is no better (a pre-migrate insert silently drops the new
# columns because ``get_valid_columns`` reads the live schema). Either way the
# window produces blank-contract rows that are NOT forgeries. Grandfather them for
# a bounded 6h past the patch — far longer than any real rolling restart, far
# shorter than the horizon on which a planted row would matter, and self-limiting
# because the runs themselves are terminalized by the 3h stale-run reaper.
LEGACY_GRACE_SECONDS = 6 * 3600

# Registry/manifest tool ids are agent-facing (``jarvis__get_doc``); the bench
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
	``tools_allow`` (``exec``, ``canvas``, ``message``) names agent's own tools,
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
def contract_for_launch(listing, tools_allow: list | None = None) -> dict:
	"""The capability contract to stamp on a run being launched for ``listing``.

	``tools_allow`` comes from the BUNDLED registry (it is delegate metadata that
	never enters the customer DB — the same source ``build_agent_push_payload``
	echoes into the container's enablement signal), stored VERBATIM so the run
	carries the exact declared list as provenance. Pass it in when the caller has
	already resolved it (``_launch_audit`` does, to refuse an empty surface before
	any row exists); otherwise it is read here. ``nature``/``writes`` are the
	listing's values AT THIS INSTANT — the whole point of the snapshot is that a
	later edit cannot move them.

	Returns the three ``Jarvis Agent Run`` field values plus the contract marker,
	ready to splat into the insert."""
	from jarvis.chat.agent_catalog import registry_tools_allow

	declared = tools_allow if tools_allow is not None else registry_tools_allow(listing.agent_slug)
	return {
		"capability_contract": CONTRACT_SNAPSHOT,
		"tools_allow_json": frappe.as_json(declared),
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
	"""True iff a blank-contract run predates the guard (plus the bounded
	``LEGACY_GRACE_SECONDS`` deploy window) and may use the legacy fallback.

	Anchored on the patch's own timestamp so a run created long AFTER the cutover
	can never buy itself legacy authority by simply having no snapshot."""
	cutoff = _legacy_cutoff()
	if not cutoff or not run_creation:
		return False
	deadline = frappe.utils.add_to_date(frappe.utils.get_datetime(cutoff), seconds=LEGACY_GRACE_SECONDS)
	return frappe.utils.get_datetime(run_creation) < deadline


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


# The customer reads the chat transcript; the delegate and the audit trail read
# ``message``. Neither audience is served by showing the other's copy, so the two
# are written separately (UX P1-2 — "capability contract snapshotted at launch" is
# implementation vocabulary and has no place in a customer's chat).
# Message = what happened; the "what you can do" line is the envelope's ``hint``
# (``api._ERROR_HINTS``), per the house convention.
_CHAT_NOT_DECLARED = "This agent tried to use a tool it isn't allowed to use, so the step was skipped."
_CHAT_NO_SURFACE = (
	"This agent was started without a usable set of tools, so it couldn't do "
	"anything and the run has been stopped."
)
# Stamped on the failed run and shown in the run's error banner. Short (the field
# is displayed inline) and honest about the REAL cause — the whole point of
# failing here rather than letting the 3h stale-run sweep call it a timeout.
_RUN_ERROR_NO_SURFACE = "the agent's capability contract authorises no tools; refused at the first call"


def tool_denial(session_key: str | None, tool: str) -> dict | None:
	"""The refusal for this delegate's ``tool`` call, or None when it may proceed.

	None is also returned for a NON-delegate caller (nothing to enforce) and for a
	``legacy`` run (no snapshot to enforce against). Everything else is decided by
	the snapshot alone: a tool absent from it — even one the run-as user's Frappe
	roles would happily permit — is refused BEFORE dispatch.

	The refusal is a dict, not a string, because two of its facts are decisions the
	caller has to make:

	  * ``fatal`` — the contract authorises NO bench tool at all (a blank contract,
	    an empty/unparseable snapshot, or a bundle whose declared surface is
	    container-side only). Every call this run will ever make is refused,
	    INCLUDING the ``record_agent_run`` writeback that finalizes it, so the run
	    is not merely mistaken, it is dead. The caller must terminalize it rather
	    than leave it ``running`` for the 3h reaper to mislabel as a timeout.
	  * ``chat_message`` vs ``message`` — plain language for the customer-visible
	    transcript, contract vocabulary for the delegate and the audit trail.
	"""
	cap = resolve(session_key)
	if cap is None or cap["legacy"]:
		return None
	allowed = bench_tools(cap["tools_allow"])
	name = normalize_tool(tool)
	if name and name in allowed:
		return None
	base = {"run": cap["run"], "agent": cap["agent"], "tool": name or str(tool or "")}
	if not allowed:
		return {
			**base,
			"fatal": True,
			"message": (
				f"agent '{cap['agent']}' has no usable tool surface for this run: its "
				f"capability contract authorises no bench tool, so '{name or tool}' — and "
				"every other call — is refused. The run has been marked failed; no further "
				"call can succeed."
			),
			"chat_message": _CHAT_NO_SURFACE,
			"run_error": _RUN_ERROR_NO_SURFACE,
		}
	return {
		**base,
		"fatal": False,
		# The no-retry instruction rides in the MESSAGE, not the envelope's ``hint``:
		# the agent plugin relays a failed tool call to the model as
		# ``"<code>: <message>"`` and drops every other field, so a hint the delegate
		# must act on has to be in the message or it never arrives. ``hint`` keeps its
		# usual role — the human-facing "what you can do" line.
		"message": (
			f"agent '{cap['agent']}' is not permitted to call '{name or tool}': the tool is "
			"not in the capability contract snapshotted for this run at launch. That "
			"contract is fixed for the whole run, so retrying, renaming the tool or "
			"changing the arguments will not help — continue with the tools you do have."
		),
		"chat_message": _CHAT_NOT_DECLARED,
		"run_error": "",
	}
