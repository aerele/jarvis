"""The per-run STEP TIMELINE: what an agent run is doing, while it does it.

``Jarvis Agent Activity`` records a run's LIFECYCLE (started / completed /
failed) - four rows at most, and nothing at all between the launch and the
finish. A delegate audit takes minutes, so the customer watching a running run
saw a static "Run in progress" line and had to trust it.

The bench can do better, because it already SEES every step: the delegate calls
back into it for each ``jarvis__*`` tool over the run's own session bearer. This
module turns those observations into one append-only ``Jarvis Agent Run Step``
row apiece, plus the two the bench knows first-hand - the launch ``dispatched``
and the findings ``writeback``.

Three rules hold everywhere here:

* **Best-effort.** :func:`record_step` never raises. A timeline is a narration of
  the run; failing to narrate a step must never fail the step.
* **Cheap.** One insert, no commit of its own. It rides on the caller's
  transaction so a rolled-back tool call leaves no phantom step behind.
* **Shapes, never contents.** A step names DocTypes, reports, references and
  counts. It never carries a row's field values, so the timeline is safe to show
  to anyone permitted to read the run.
"""

from __future__ import annotations

import frappe

STEP = "Jarvis Agent Run Step"
RUN = "Jarvis Agent Run"

#: Hard caps mirroring the DocType (``label`` is a Data(140); ``detail`` is a
#: Small Text we keep short so the timeline row stays one glanceable line).
LABEL_MAX = 140
DETAIL_MAX = 500
#: A failed step carries the tool's own error message so the timeline explains
#: what went wrong. First line only, and short: enough to name the fault, never
#: enough to paste a traceback or a payload into the customer's view.
ERROR_DETAIL_MAX = 300

#: Bench-internal bookkeeping DocTypes an agent reads to orient itself before it
#: touches any ERP data. Their real names are implementation vocabulary the
#: customer has never seen and their row names are opaque hashes, so a step names
#: the ACT instead of the record: "Read engagement configuration", never
#: "Read Jarvis Agent Installation 8f3ac1...". Anything else under the same
#: prefix is a doctype this map has not been taught, so it degrades to the
#: honest generic rather than leaking a name.
_INTERNAL_DOCTYPE_LABELS = {
	"Jarvis Agent Installation": "Read engagement configuration",
	"Jarvis Agent Listing": "Read agent definition",
	"Jarvis Agent Run": "Checked run state",
}
_INTERNAL_DOCTYPE_PREFIX = "Jarvis Agent "
_INTERNAL_DOCTYPE_FALLBACK = "Read agent metadata"

#: Tool names arrive from the agent registry as ``jarvis__get_list``; the bench
#: dispatches the bare name. Both forms normalize to the bare one.
_TOOL_PREFIX = "jarvis__"


def _clip(value, limit: int) -> str:
	text = str(value or "").strip()
	return text[:limit]


def bare_tool(tool) -> str:
	"""``jarvis__get_list`` / ``get_list`` -> ``get_list``."""
	name = str(tool or "").strip()
	return name[len(_TOOL_PREFIX) :] if name.startswith(_TOOL_PREFIX) else name


def _next_seq(run_name: str) -> int:
	"""1-based position of the next step of ``run_name``.

	MAX(seq)+1 rather than a row COUNT, so a deleted row can never renumber the
	rest. It is deliberately UNLOCKED and therefore best-effort: a delegate that
	fires tool calls in parallel reads the same MAX in two requests and both
	write the same seq (observed live on the bench - 2,2,2 then 3,3). Taking a
	lock here would put a write barrier on the hot path of every plugin tool call
	to order a decoration, which is the wrong trade.

	So the stored ``seq`` is a hint, not the contract. ``list_run_steps`` orders
	by ``occurred_at`` and RE-NUMBERS the response 1..n, which is what the UI
	renders - duplicates in the column can never reach a customer."""
	try:
		row = frappe.db.sql(
			"select max(seq) from `tabJarvis Agent Run Step` where run = %s",
			(run_name,),
		)
		current = (row and row[0] and row[0][0]) or 0
		return int(current) + 1
	except Exception:
		return 1


def record_step(
	run_name: str,
	*,
	kind: str,
	tool: str | None = None,
	label: str,
	detail: str | None = None,
	status: str = "ok",
	duration_ms: int | None = None,
	owner: str | None = None,
) -> str | None:
	"""Append ONE step to ``run_name``'s timeline. Returns its name, or None.

	``owner`` pins the row to the human who owns the run, and is applied AFTER the
	insert exactly like ``agent_activity.log_activity``. This is not optional
	bookkeeping: ``Document.insert`` stamps ``owner = frappe.session.user``
	UNCONDITIONALLY (``set_user_and_timestamp`` assigns it, it does not defer to a
	pre-set value), and every caller here runs impersonated as the run-as user or
	as the scheduler's Administrator. Left alone, the owner-scoped (``if_owner``)
	timeline would be invisible to the very customer it is for.

	Omit ``owner`` and it is resolved from the RUN row instead. A step belongs to
	whoever owns the run, never to whoever happened to be executing - so the
	fallback is the rule restated, not a guess, and no future caller can leak a
	step to the session user by forgetting the argument.

	NEVER raises: a failed narration is logged and swallowed."""
	if not run_name:
		return None
	try:
		owner = owner or run_owner(run_name)
		doc = frappe.get_doc(
			{
				"doctype": STEP,
				"run": run_name,
				"seq": _next_seq(run_name),
				"kind": kind,
				"tool": bare_tool(tool),
				"label": _clip(label, LABEL_MAX) or kind,
				"detail": _clip(detail, DETAIL_MAX),
				"status": status if status in ("ok", "error") else "ok",
				"duration_ms": int(duration_ms) if duration_ms is not None else None,
				"occurred_at": frappe.utils.now(),
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		if owner and owner != doc.owner:
			frappe.db.set_value(STEP, doc.name, "owner", owner, update_modified=False)
		return doc.name
	except Exception:
		# The logger, not frappe.log_error: a step is decoration on a run that is
		# working fine without it, and an Error Log row per missed narration would
		# bury the surface where real run faults show up.
		try:
			frappe.logger("jarvis.agents").warning(
				f"run-step not recorded for {run_name}: {frappe.get_traceback()}"
			)
		except Exception:
			pass
		return None


def run_owner(run_name: str) -> str | None:
	"""The human the run's rows belong to (the installation owner), for pinning a
	step's owner from an impersonated context."""
	try:
		return frappe.db.get_value(RUN, run_name, "owner")
	except Exception:
		return None


def running_run_for_session(session_key: str | None) -> dict | None:
	"""``{name, owner}`` of the RUNNING agent run bound to ``session_key``, or None.

	The same resolution ``record_agent_run`` uses: the run is found from the
	CALLER's opaque session bearer, never from anything the model could author,
	so a delegate can only ever narrate its own run. An ordinary chat session
	resolves to None and the caller does nothing."""
	key = (session_key or "").strip()
	if not key:
		return None
	try:
		return frappe.db.get_value(
			RUN,
			{"session_key": key, "status": "running"},
			["name", "owner"],
			as_dict=True,
		)
	except Exception:
		return None


# --------------------------------------------------------------------------- #
# Humanizing a tool call
# --------------------------------------------------------------------------- #
def _n_rows(result) -> int | None:
	"""Row count of a tool's data payload, when it has an obvious one."""
	if isinstance(result, list):
		return len(result)
	if isinstance(result, dict):
		for key in ("rows", "docs", "result"):
			value = result.get(key)
			if isinstance(value, list):
				return len(value)
		count = result.get("count")
		if isinstance(count, int):
			return count
	return None


def _plural(n: int, word: str) -> str:
	return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _title_case(tool: str) -> str:
	"""``get_balance_on`` -> ``Get Balance On`` - the fallback label for a tool
	with no bespoke phrasing. Readable, and it never invents a description of a
	tool this module has not been taught."""
	return " ".join(part.capitalize() for part in bare_tool(tool).split("_") if part) or "Step"


def internal_doctype_label(doctype) -> str | None:
	"""The customer-facing phrasing for a read of a bench-internal DocType, or
	None when it is ordinary ERP data the customer knows by name (and may see)."""
	name = str(doctype or "").strip()
	if not name:
		return None
	if name in _INTERNAL_DOCTYPE_LABELS:
		return _INTERNAL_DOCTYPE_LABELS[name]
	return _INTERNAL_DOCTYPE_FALLBACK if name.startswith(_INTERNAL_DOCTYPE_PREFIX) else None


def _is_single(doctype: str) -> bool:
	"""True for a Single DocType (Stock Settings, Selling Settings, ...).

	A Single's document name IS its doctype, so ``get_doc("Stock Settings")``
	rendered as "Read <doctype> <name>" reads "Read Stock Settings Stock
	Settings". Metadata is cached, and an unknown/bogus doctype (exactly the
	failed-lookup case) simply answers False."""
	try:
		return bool(frappe.get_meta(doctype).issingle)
	except Exception:
		return False


def error_detail(message) -> str:
	"""The FIRST line of a tool's error message, clipped to
	:data:`ERROR_DETAIL_MAX` - what failed, without a traceback or a payload."""
	text = str(message or "").strip()
	if not text:
		return ""
	return text.splitlines()[0].strip()[:ERROR_DETAIL_MAX]


def _query_doctype(args: dict) -> str:
	"""The ``from`` DocType of a structured query spec (``query`` tool)."""
	spec = args.get("spec")
	if isinstance(spec, dict):
		source = spec.get("from")
		if isinstance(source, str):
			return source
	return ""


def humanize_tool_call(tool, args, result) -> tuple[str, str]:
	"""One short sentence describing a delegate tool call, plus an optional
	second line. Returns ``(label, detail)``, both already clipped.

	``args`` is the parsed argument dict the tool ran against; ``result`` is the
	tool's DATA payload (the envelope's ``data``), or None when the call failed.

	The copy is deliberately about SHAPES - the DocType read, the report run, how
	many rows came back. It never quotes a value out of the payload, so a step is
	as safe to render as the run itself."""
	name = bare_tool(tool)
	args = args if isinstance(args, dict) else {}
	label = ""
	detail = ""

	if name == "get_list":
		doctype = args.get("doctype") or "records"
		n = _n_rows(result)
		internal = internal_doctype_label(doctype)
		if internal:
			# The count is a shape, so it survives - but on the second line, leaving
			# the label as the plain sentence the customer reads.
			label = internal
			if n is not None:
				detail = _plural(n, "row")
		else:
			label = f"Read {doctype}" + (f", {_plural(n, 'row')}" if n is not None else "")
	elif name == "get_doc":
		doctype = args.get("doctype") or "document"
		target = args.get("name")
		internal = internal_doctype_label(doctype)
		# A record NAME is model-supplied: it is whatever the delegate guessed, and a
		# wrong guess is exactly what produced "Read Company ignore" on the bench. So
		# the name is only ever shown once the document actually came back.
		resolved = isinstance(result, dict) and bool(result)
		if internal:
			# Never the record name here either: an internal row's name is an opaque
			# hash that means nothing to the customer and states nothing true.
			label = internal
		elif not target and isinstance(args.get("names"), list):
			n = len(args["names"])
			label = f"Read {doctype}, {_plural(n, 'document')}"
		elif _is_single(doctype):
			# A Single's name IS its doctype - "Read Stock Settings", never twice.
			label = f"Read {doctype}"
		elif resolved and target:
			label = f"Read {doctype} {target}"
		elif resolved:
			label = f"Read {doctype}"
		else:
			# The lookup did not resolve. Say what was attempted, not what was read;
			# the error itself lands in `detail` (see the api.py step hook).
			label = f"Looked up {doctype}"
	elif name == "run_report":
		label = f"Ran report {args.get('report_name') or ''}".strip()
		n = _n_rows(result)
		if n is not None:
			detail = _plural(n, "row")
	elif name == "query":
		doctype = _query_doctype(args)
		internal = internal_doctype_label(doctype)
		if internal:
			label = internal
		else:
			label = f"Queried {doctype}".strip() if doctype else "Ran a query"
		n = _n_rows(result)
		if n is not None:
			detail = _plural(n, "row")
	elif name == "get_balance_on":
		account = args.get("account") or args.get("party") or ""
		label = f"Checked balance for {account}".strip() if account else "Checked a balance"
	elif name == "record_agent_run":
		n = _n_rows(args.get("findings"))
		if n is None and isinstance(result, dict):
			n = result.get("findings_count")
		label = f"Recorded {_plural(int(n), 'finding')}" if isinstance(n, int) else "Recorded findings"
	elif name == "save_agent_dashboard":
		label = "Saved dashboard"
	else:
		label = _title_case(name)

	return _clip(label, LABEL_MAX), _clip(detail, DETAIL_MAX)
