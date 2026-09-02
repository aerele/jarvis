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

	MAX(seq)+1 rather than a row COUNT: a step is never deleted, but a count
	would silently renumber from 1 if one ever were, and two rows sharing a seq
	is exactly the ambiguity the column exists to remove. The gateway drives one
	tool call at a time per run, so no lock is needed; ``list_run_steps`` orders
	by ``seq, creation`` anyway so even a tie reads in insert order."""
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

	``owner`` pins the row to the human who owns the run. It has to be passed
	explicitly and applied AFTER the insert, exactly like
	``agent_activity.log_activity``: every caller here runs impersonated as the
	run-as user (the plugin dispatcher) or as the scheduler's Administrator, and
	``insert()`` stamps ``owner = frappe.session.user`` - so without this the
	owner-scoped (``if_owner``) timeline would be invisible to the very customer
	it is for.

	NEVER raises: a failed narration is logged and swallowed."""
	if not run_name:
		return None
	try:
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
		label = f"Read {doctype}" + (f", {_plural(n, 'row')}" if n is not None else "")
	elif name == "get_doc":
		doctype = args.get("doctype") or "document"
		target = args.get("name")
		if not target and isinstance(args.get("names"), list):
			n = len(args["names"])
			label = f"Read {doctype}, {_plural(n, 'document')}"
		else:
			label = f"Read {doctype}" + (f" {target}" if target else "")
	elif name == "run_report":
		label = f"Ran report {args.get('report_name') or ''}".strip()
		n = _n_rows(result)
		if n is not None:
			detail = _plural(n, "row")
	elif name == "query":
		doctype = _query_doctype(args)
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
