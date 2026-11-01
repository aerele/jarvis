"""JF-017 — the delegate run's CAPABILITY CONTRACT: snapshotted at launch,
enforced at the BENCH.

Before this module the container-side ``tools.allow`` was the ONLY thing bounding
which ``jarvis__*`` tools a marketplace-agent delegate could call. That list is
CONFIGURATION (rendered into agent configuration by fleet-agent), not authorization: a
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
import re

import frappe

RUN = "Jarvis Agent Run"
LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
VERSION = "Version"

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

# An agent that declares this tool uses a launch-time copy of its engagement
# configuration.  Its installation row is mutable, so it must never become a
# second configuration authority while the run is active.
_ENGAGEMENT_CONFIG_TOOL = "get_engagement_config"
_CONFIG_SOURCE_DOCTYPES = frozenset({RUN.casefold(), INSTALLATION.casefold(), VERSION.casefold()})
_SNAPSHOT_CONFIG_READ_MESSAGE = (
	"this run cannot read raw engagement configuration through generic tools. Use "
	"get_engagement_config instead; it returns the immutable configuration "
	"snapshot stamped when this run started. Retrying this read will not help."
)
_CHAT_SNAPSHOT_CONFIG_READ_MESSAGE = "This agent used the configuration snapshot fixed when this run started."
_SNAPSHOT_CONFIG_REPORT_MESSAGE = (
	"this run cannot execute saved reports because their definitions can change "
	"the data source during the run. Use query for the audit data and "
	"get_engagement_config for the immutable configuration snapshot. Retrying this "
	"report will not help."
)

# ``frappe.get_list`` accepts enough SQL-shaped syntax to read a linked or
# explicitly named second DocType. For a snapshot-config run, allow only a
# plain field/filter/order expression on the requested base DocType. This is
# stronger than looking for ``Jarvis Agent Installation`` text: a link such as
# ``installation.config`` names the mutable row without spelling its DocType.
_PLAIN_LIST_FIELD = re.compile(r"\*|[A-Za-z_][A-Za-z0-9_]*(?:\s+as\s+[A-Za-z_][A-Za-z0-9_]*)?", re.IGNORECASE)
_PLAIN_LIST_ORDER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\s+(?:asc|desc))?", re.IGNORECASE)
_PLAIN_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


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


def _is_config_source_doctype(value) -> bool:
	"""True when ``value`` can expose an engagement configuration value.

	Frappe resolves DocType names without case sensitivity. Match that behaviour
	here so a differently cased model argument cannot bypass the boundary before
	the generic read tool canonicalises it. A Run stores the stamped snapshot, but
	only ``get_engagement_config`` may return it to its active delegate.
	``Version`` is included because Installation tracks changes: its Version rows
	can preserve a prior mutable ``config`` value after the Installation changed.
	"""
	return isinstance(value, str) and value.strip().casefold() in _CONFIG_SOURCE_DOCTYPES


def _query_mentions_config_source(spec) -> bool:
	"""Whether a structured query can read a mutable configuration source.

	``query`` supports FROM tables, joins, and EXISTS / NOT EXISTS sub-specs. A
	check of only the outer FROM table would leave both join and nested-query
	routes open, so this walker follows the same source locations as
	``query._collect_doctypes``. It deliberately ignores ordinary literal values:
	a filter value that merely contains the DocType's words is not a read.
	"""

	def walk(node) -> bool:
		if not isinstance(node, dict):
			return False
		if _is_config_source_doctype(node.get("from")):
			return True
		joins = node.get("joins")
		if isinstance(joins, list):
			for join in joins:
				if isinstance(join, dict) and _is_config_source_doctype(join.get("doctype")):
					return True
		for key in ("where", "having"):
			predicates = node.get(key)
			if not isinstance(predicates, list):
				continue
			for predicate in predicates:
				if not isinstance(predicate, dict):
					continue
				op = str(predicate.get("op") or "").strip().lower()
				if op in {"exists", "not exists"} and walk(predicate.get("value")):
					return True
		return False

	return walk(spec)


def _is_snapshot_config_cap(cap) -> bool:
	"""Whether an immutable delegate contract uses the config snapshot boundary."""
	if not isinstance(cap, dict) or cap.get("legacy"):
		return False
	tools_allow = cap.get("tools_allow")
	return isinstance(tools_allow, list) and _ENGAGEMENT_CONFIG_TOOL in bench_tools(tools_allow)


def snapshot_config_report_refusal(report_name, *, cap=None) -> str | None:
	"""Refuse saved reports for a snapshot-config delegate.

	A saved report is mutable data access policy. Query and Script Reports can run
	arbitrary sources. Report Builder reports can name a linked source in their
	saved body, and Frappe's normal report runner does not execute them through a
	stable, lockable route. Refusing the whole route keeps the launch snapshot as
	the sole configuration authority. The paired bundle contract removes this
	tool for new runs; this execution-time guard also protects already-stamped
	runs that still list it.
	"""
	cap = resolve() if cap is None else cap
	if not _is_snapshot_config_cap(cap):
		return None
	if not isinstance(report_name, str) or not report_name.strip():
		return None  # Preserve run_report's normal missing-argument error.
	return _SNAPSHOT_CONFIG_REPORT_MESSAGE


def _same_doctype(left, right) -> bool:
	"""Case-insensitive DocType equality, matching Frappe's lookup behaviour."""
	return (
		isinstance(left, str)
		and isinstance(right, str)
		and left.strip().casefold() == right.strip().casefold()
	)


def _get_list_fields_are_base_fields(fields) -> bool:
	"""Whether every selected get_list field is a plain base-DocType field."""
	if fields is None:
		return True
	return isinstance(fields, list) and all(
		isinstance(field, str) and bool(_PLAIN_LIST_FIELD.fullmatch(field.strip())) for field in fields
	)


def _get_list_order_is_base_fields(order_by) -> bool:
	"""Whether get_list ordering cannot reference a linked or extra table."""
	if order_by in (None, ""):
		return True
	if not isinstance(order_by, str):
		return False
	parts = [part.strip() for part in order_by.split(",")]
	return bool(parts) and all(part and _PLAIN_LIST_ORDER.fullmatch(part) for part in parts)


def _get_list_filter_is_base(condition, doctype) -> bool:
	"""Whether one Frappe filter stays on ``doctype`` and a plain field."""
	if isinstance(condition, dict):
		return all(
			isinstance(field, str) and _PLAIN_FIELD_NAME.fullmatch(field.strip()) for field in condition
		)
	if not isinstance(condition, list | tuple):
		return False
	if len(condition) in (2, 3):
		return isinstance(condition[0], str) and bool(_PLAIN_FIELD_NAME.fullmatch(condition[0].strip()))
	if len(condition) in (4, 5):
		return (
			_same_doctype(condition[0], doctype)
			and isinstance(condition[1], str)
			and bool(_PLAIN_FIELD_NAME.fullmatch(condition[1].strip()))
		)
	return False


def _get_list_filters_are_base_fields(filters, doctype) -> bool:
	"""Whether Frappe filter syntax cannot add a second data source.

	``get_list`` accepts a mapping, one two/three/four-part condition, or a list
	of conditions. JSON text is decoded before validation because the tool accepts
	it too. Invalid JSON never reaches a read, so leave its normal validation
	error to the tool instead of changing that error into a security refusal.
	"""
	if filters is None:
		return True
	if isinstance(filters, str):
		try:
			filters = json.loads(filters)
		except (TypeError, ValueError):
			return True
	if isinstance(filters, dict):
		return _get_list_filter_is_base(filters, doctype)
	if not isinstance(filters, list | tuple):
		return False
	if not filters:
		return True
	conditions = [filters] if isinstance(filters[0], str) else filters
	return all(_get_list_filter_is_base(condition, doctype) for condition in conditions)


def _get_list_args_are_canonical(args: dict) -> bool:
	"""Reject Frappe's legacy field/filter argument swap for this guarded call."""
	fields = args.get("fields")
	filters = args.get("filters")
	if isinstance(fields, dict):
		return False
	if isinstance(fields, list) and fields and isinstance(fields[0], list):
		return False
	return not (fields and isinstance(filters, list) and len(filters) > 1 and isinstance(filters[0], str))


def _get_list_can_only_read_its_base_doctype(args: dict) -> bool:
	"""Whether a snapshot delegate's generic list call has one data source."""
	doctype = args.get("doctype")
	if not isinstance(doctype, str) or not doctype.strip():
		return True  # Let get_list return its normal missing-argument error.
	if _is_config_source_doctype(doctype) or _is_config_source_doctype(args.get("parent_doctype")):
		return False
	return (
		_get_list_args_are_canonical(args)
		and _get_list_fields_are_base_fields(args.get("fields"))
		and _get_list_filters_are_base_fields(args.get("filters"), doctype)
		and _get_list_order_is_base_fields(args.get("order_by"))
	)


def snapshot_config_read_denial(cap, tool: str, args: dict | None) -> dict | None:
	"""Refuse generic configuration reads for a run with an immutable snapshot.

	The immutable capability row, not the mutable Listing or Installation row,
	decides whether this boundary applies. A snapshot contract that declared
	``get_engagement_config`` must get its configuration only through that tool.
	Legacy and ordinary sessions retain their existing read behaviour.

	The caller invokes this after argument parsing and before dispatch. Therefore
	it protects the real delegate route. ``get_list`` is restricted to a single,
	plain base-DocType read because Frappe otherwise lets fields and filters add
	linked or explicitly named sources.
	"""
	if not _is_snapshot_config_cap(cap):
		return None
	name = normalize_tool(tool)
	args = args if isinstance(args, dict) else {}
	must_refuse = (
		(name == "get_doc" and _is_config_source_doctype(args.get("doctype")))
		or (name == "get_list" and not _get_list_can_only_read_its_base_doctype(args))
		or (name == "query" and _query_mentions_config_source(args.get("spec")))
	)
	if not must_refuse:
		return None
	return {"message": _SNAPSHOT_CONFIG_READ_MESSAGE, "chat_message": _CHAT_SNAPSHOT_CONFIG_READ_MESSAGE}


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

	Returns ``{run, owner, status, agent, legacy, tools_allow, nature, writes,
	run_as}``. ``owner`` and ``status`` are along for the ride so a caller that
	needs the run ROW (the #1062 step timeline pins each step to the run owner and
	only narrates a ``running`` run) does not have to fetch the same row a second
	time on the hot path of every plugin tool call. ``legacy``
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
			"owner",
			"status",
			"agent",
			"creation",
			"capability_contract",
			"tools_allow_json",
			"capability_nature",
			"capability_writes_json",
		],
		as_dict=True,
	)
	# Shape-checked, not just truthiness-checked. ``as_dict=True`` yields a
	# frappe._dict or None in production, so the isinstance arm is unreachable
	# there - but ``frappe.db.get_value`` is a seam tests stub WHOLESALE (test_api's
	# budget cases patch it to return one fixed string for every lookup on the
	# dispatch path), and this function now runs on that path directly rather than
	# only behind ``tool_denial``. A stub must make the caller a non-delegate, never
	# an AttributeError out of the dispatcher. ``.get`` below for the same reason: a
	# plain dict stub has no attribute access.
	if not isinstance(run, dict) or not run.get("agent"):
		return None  # no delegate run bound -> not a delegate; leave the caller untouched

	base = {
		"run": run.get("name"),
		"owner": run.get("owner"),
		"status": run.get("status"),
		"agent": run.get("agent"),
		"run_as": frappe.session.user,
	}
	contract = (run.get("capability_contract") or "").strip().lower()

	if contract == CONTRACT_LEGACY or (not contract and _is_pre_patch(run.get("creation"))):
		# Pre-JF-017 run: nature/writes come from the live listing, as they did
		# before the snapshot existed, and no tools_allow gate applies.
		listing = frappe.db.get_value(LISTING, run.get("agent"), ["nature", "writes"], as_dict=True) or {}
		listing = listing if isinstance(listing, dict) else {}
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
		"tools_allow": _parse_list(run.get("tools_allow_json")),
		"nature": (run.get("capability_nature") or "").strip().lower(),
		"writes": parse_writes(run.get("capability_writes_json")),
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

# Tools that must NEVER slip through the ``legacy`` short-circuit below, MCP
# connectors: call_connector reaches a connector credential under the
# delegate's identity, and a Personal-scope row can be the run-as user's OWN
# PAT, so handing it to a marketplace agent is a privilege escalation, not an
# ordinary tool call. Every OTHER tool is already default-denied for a
# non-legacy run by the ordinary snapshot check below; the one gap is a
# ``legacy`` run, whose whole definition is "no snapshot was ever captured" -
# there is no contract that could have named this tool, so it can never be
# authorised, not "authorised because nothing said otherwise". Checked BEFORE
# the ``cap.get("legacy")`` return, which otherwise skips enforcement (and
# every other tool's own gate) entirely for such a run.
_ALWAYS_GATED = frozenset({"call_connector"})
_CHAT_CONNECTOR_NOT_DECLARED = (
	"This agent tried to use a connector, which isn't allowed for this kind of run, so the step was skipped."
)


#: Distinguishes "caller passed no contract" from "caller resolved and got None"
#: (a non-delegate). Without it, a caller that correctly resolved None would make
#: this function resolve all over again - the exact double read it exists to avoid.
_UNRESOLVED = object()


def tool_denial(session_key: str | None, tool: str, cap=_UNRESOLVED) -> dict | None:
	"""The refusal for this delegate's ``tool`` call, or None when it may proceed.

	None is also returned for a NON-delegate caller (nothing to enforce) and for a
	``legacy`` run (no snapshot to enforce against) - EXCEPT for the tools in
	:data:`_ALWAYS_GATED` (currently just ``call_connector``), which a legacy run
	can never reach either, snapshot or not. Everything else is decided by the
	snapshot alone: a tool absent from it — even one the run-as user's Frappe
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

	``cap`` lets a caller that has ALREADY resolved this session's contract hand it
	in instead of paying for a second identical run-row read. Omit it and the
	contract is resolved here, exactly as before.
	"""
	cap = resolve(session_key) if cap is _UNRESOLVED else cap
	# Shape-checked like resolve's own row guard: a non-delegate (None) and a
	# stubbed/mocked cap both mean "nothing to enforce here", which is what a
	# patched resolve already produced before the contract was passed in.
	if not isinstance(cap, dict):
		return None
	name = normalize_tool(tool)

	# _ALWAYS_GATED tools are refused for a legacy run OUTRIGHT, before the
	# ordinary ``legacy`` bypass below: ``resolve`` hardcodes ``tools_allow: []``
	# for a legacy cap (see above), so there is no declared surface a legacy run
	# could ever have named this tool in. Not the ``fatal``/no-surface branch:
	# a legacy run's OTHER tools still run exactly as before, only this one is
	# refused.
	if name in _ALWAYS_GATED and cap.get("legacy"):
		return {
			"run": cap.get("run"),
			"agent": cap.get("agent"),
			"tool": name,
			"fatal": False,
			"message": (
				f"agent '{cap.get('agent')}' is not permitted to call '{name}': legacy "
				"delegate runs (no capability snapshot) can never authorise a connector "
				"call, which requires an explicit, snapshotted contract naming it. "
				"Retrying will not help, continue with the tools you do have."
			),
			"chat_message": _CHAT_CONNECTOR_NOT_DECLARED,
			"run_error": "",
		}

	# ``.get`` so a cap missing the key is treated as NOT legacy, i.e. enforced -
	# the fail-closed direction.
	if cap.get("legacy"):
		return None
	allowed = bench_tools(cap["tools_allow"])
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
