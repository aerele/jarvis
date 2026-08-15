"""Jarvis Triggers dispatch engine.

One wildcard ``doc_events`` handler (:func:`dispatch`, registered in hooks.py
for the eight :data:`SUPPORTED_EVENTS`) fires user-defined ``Jarvis Trigger``
rows. Two action kinds:

  * Script — the trigger's managed (always-disabled) Server Script runs
    SYNCHRONOUSLY inside the transaction via ``execute_doc``. A
    ``frappe.ValidationError`` raised by the script blocks the save (that is
    the feature); any other exception is fail-open. Core's server-script map
    skips the managed script (``disabled = 1``), so the engine is the only
    dispatcher and it fires exactly once.
  * LLM — queued per-request and flushed on ``frappe.db.after_commit`` into a
    background job (``jarvis.triggers.llm_action.run_llm_action``), mirroring
    frappe's webhook ``_add_webhook_to_queue`` / flush pattern including the
    keep-only-the-LAST-instance dedupe per (trigger, docname).

Everything logs to ``Jarvis Trigger Activity`` via :func:`_insert_activity`,
which must NEVER break a dispatch.

HOT-PATH PROPERTY: :func:`dispatch` runs on every doc event of every doctype,
so when the saved doctype has no triggers it must return after one cached-map
read (Redis on first touch, ``frappe.local.cache`` after) + a dict lookup —
no DB query, no imports.
"""

from __future__ import annotations

import time

import frappe
from frappe.utils import cint

TRIGGER = "Jarvis Trigger"
ACTIVITY = "Jarvis Trigger Activity"

# Registry cache key (frappe.cache() get_value/set_value — v15-safe; NOT
# frappe.client_cache, which older benches lack).
_CACHE_KEY = "jarvis:triggers_map"

# All doc events a trigger can attach to (order = UI order).
SUPPORTED_EVENTS = (
	"validate",
	"before_submit",
	"after_insert",
	"on_update",
	"on_submit",
	"on_cancel",
	"on_trash",
	"on_update_after_submit",
)

# LLM actions run after commit in a background job, so they only make sense on
# post-events — a validate/before_submit LLM action could never block the save
# it rides on (the reply lands after the transaction is gone).
LLM_EVENTS = frozenset(
	{
		"after_insert",
		"on_update",
		"on_submit",
		"on_cancel",
		"on_trash",
		"on_update_after_submit",
	}
)

# Only these events are meant to BLOCK a save when a Script action throws. On
# every other (post-)event a raised ``frappe.ValidationError`` — including the
# incidental subclasses like DoesNotExistError/MandatoryError that a buggy
# script can raise — is treated as fail-open, so a latent script bug can never
# roll back a user's committed-intent save.
BLOCKABLE_EVENTS = frozenset({"validate", "before_submit"})

# UI labels for the event picker (served by triggers_api.get_triggers_caps).
EVENT_LABELS = {
	"validate": "Before Save (blockable)",
	"before_submit": "Before Submit (blockable)",
	"after_insert": "After Insert",
	"on_update": "After Save",
	"on_submit": "After Submit",
	"on_cancel": "After Cancel",
	"on_trash": "Before Delete",
	"on_update_after_submit": "After Save (Submitted)",
}

# Our event name -> the managed Server Script's ``doctype_event`` value (the
# controller materializes with this mapping; core's EVENT_MAP is the inverse).
SCRIPT_EVENT_MAP = {
	"validate": "Before Save",
	"before_submit": "Before Submit",
	"after_insert": "After Insert",
	"on_update": "After Save",
	"on_submit": "After Submit",
	"on_cancel": "After Cancel",
	"on_trash": "Before Delete",
	"on_update_after_submit": "After Save (Submitted Document)",
}

# Recursion guard: a Script action that writes a doc fires doc events, which
# dispatch again. Depth 3 allows legitimate short chains but stops storms.
_MAX_TRIGGER_DEPTH = 3

_SNAPSHOT_CHAR_CAP = 12000
_SUMMARY_CHAR_CAP = 200
_DETAIL_CHAR_CAP = 20000


# The safe ``frappe.utils`` namespace exposed to conditions is a stateless bag
# of function refs (SAFE_DATA_UTILS); build it once per process rather than
# reconstructing the full safe-globals on every condition eval (~126 us saved
# per firing on a hot doctype).
_SAFE_UTILS = None


def eval_context(doc) -> dict:
	"""Condition-eval locals — exactly frappe's ``webhook.get_context`` shape
	({"doc": doc, "utils": <safe utils>}), so trigger conditions have webhook
	semantics. Imported lazily (the no-trigger hot path never needs safe_exec)
	and the utils namespace is process-cached."""
	global _SAFE_UTILS
	if _SAFE_UTILS is None:
		from frappe.utils.safe_exec import get_safe_globals

		_SAFE_UTILS = get_safe_globals().get("frappe").get("utils")
	return {"doc": doc, "utils": _SAFE_UTILS}


# --------------------------------------------------------------------------- #
# Registry cache
# --------------------------------------------------------------------------- #
def _build_triggers_map() -> dict:
	"""{doctype: {event: [row dicts]}} over all ENABLED Jarvis Trigger rows."""
	rows = frappe.get_all(
		TRIGGER,
		filters={"enabled": 1},
		fields=[
			"name",
			"trigger_name",
			"owner",
			"target_doctype",
			"doc_event",
			"condition",
			"action_type",
			"server_script",
			"llm_instruction",
			"llm_daily_cap",
		],
	)
	out: dict = {}
	for r in rows:
		out.setdefault(r.target_doctype, {}).setdefault(r.doc_event, []).append(dict(r))
	return out


# A short TTL backstops the map so a bust that races a concurrent rebuild
# (see clear_cache) can never leave a stale map cached indefinitely.
_CACHE_TTL_S = 300


def _triggers_map() -> dict:
	"""The cached registry ({} when there are no triggers). get_value keeps a
	per-request local copy, so repeated dispatches in one request cost a dict
	lookup, not a Redis round-trip.

	FAIL SAFE when the Jarvis Trigger doctype itself is unreachable (app code
	deployed but the site not yet migrated, or a DB restored to a pre-triggers
	state): the wildcard hook runs on EVERY doc event — login included — so a
	raise here would take down every request on the site. Triggers simply stay
	inert until migrate runs; the empty map is NOT cached in Redis so recovery
	is immediate after migration."""
	cached = frappe.cache().get_value(_CACHE_KEY)
	if cached is None:
		try:
			cached = _build_triggers_map()
		except Exception:
			return {}
		frappe.cache().set_value(_CACHE_KEY, cached, expires_in_sec=_CACHE_TTL_S)
	return cached


def _delete_cache_key() -> None:
	frappe.cache().delete_value(_CACHE_KEY)


def clear_cache() -> None:
	"""Bust the registry (trigger created/changed/deleted).

	Deletes now AND again on commit. A change lands via ``db_set`` +
	``clear_cache`` before the enclosing transaction commits, so a concurrent
	request in that window could rebuild the map from the still-OLD committed
	row and re-cache it. The after-commit delete forces the next rebuild to
	read the new committed state; the TTL on ``set_value`` is the final
	backstop. (after_commit is best-effort — outside a request/txn, e.g. a
	console call, the immediate delete alone suffices.)"""
	_delete_cache_key()
	try:
		frappe.db.after_commit.add(_delete_cache_key)
	except Exception:
		pass


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def dispatch(doc, method: str | None = None) -> None:
	"""``doc_events "*"`` entry point — frappe calls handler(doc, method).

	Guards (in order): bulk/system flags, unsupported event, no triggers for
	the doctype (the cheap early return), recursion depth. Then per matching
	trigger: evaluate the condition (eval errors are FAIL-OPEN — a broken
	condition writes a Failed activity and never breaks the user's save), and
	run/queue the action."""
	flags = frappe.local.flags
	if flags.in_install or flags.in_patch or flags.in_migrate or flags.in_import or flags.in_setup_wizard:
		return
	if method not in SUPPORTED_EVENTS:
		return
	by_event = _triggers_map().get(doc.doctype)
	if not by_event:
		return
	rows = by_event.get(method)
	if not rows:
		return
	depth = cint(flags.get("_jarvis_trigger_depth"))
	if depth >= _MAX_TRIGGER_DEPTH:
		return

	for row in rows:
		condition = row.get("condition")
		if condition:
			try:
				if not frappe.safe_eval(condition, eval_locals=eval_context(doc)):
					continue
			except Exception as e:
				_record_activity(
					_base_fields(row, doc, method),
					status="Failed",
					summary=f"condition error: {e}",
					detail=frappe.get_traceback(),
				)
				continue
		if row.get("action_type") == "Script":
			_run_script_action(row, doc, method, depth)
		elif row.get("action_type") == "LLM" and method in LLM_EVENTS:
			_queue_llm_action(row, doc, method)


def _base_fields(row: dict, doc, method: str) -> dict:
	"""Shared activity fields for one (trigger, doc, event) firing."""
	return {
		"trigger": row.get("name") or "",
		"trigger_label": row.get("trigger_name") or "",
		"target_doctype": doc.doctype,
		"target_docname": str(doc.name or ""),
		"doc_event": method or "",
		"action_type": row.get("action_type") or "",
		"event_user": frappe.session.user,
		"trigger_owner": row.get("owner") or "",
	}


def _run_script_action(row: dict, doc, method: str, depth: int) -> None:
	"""Run the managed Server Script synchronously, inside the transaction.

	On a BLOCKABLE event (validate/before_submit) a ``frappe.ValidationError``
	BLOCKS the save: the Blocked activity is enqueued IMMEDIATELY (the Redis
	push is not transactional, so it survives the rollback the re-raise causes)
	and the error re-raises to the caller with an identifying ``[Automation: …]``
	prefix. On any other event, or for any non-ValidationError exception, the
	action is FAIL-OPEN — a Failed activity is recorded and the user's save
	proceeds. Success/Failed activity is recorded via ``_record_activity``,
	which defers the DB insert + realtime publish to after the save COMMITS
	(``enqueue_after_commit``), keeping the user's save transaction lean and
	never publishing a firing that later rolls back."""
	flags = frappe.local.flags
	base = _base_fields(row, doc, method)
	# Pre-guard the managed script itself: a missing row must FAIL OPEN, but
	# frappe.DoesNotExistError subclasses ValidationError, so an unguarded
	# get_doc would land in the Blocked branch and block every matching save.
	script_name = row.get("server_script")
	if not script_name or not frappe.db.exists("Server Script", script_name):
		_record_activity(
			base,
			status="Failed",
			summary="managed server script missing (save not blocked)",
			detail=f"Server Script {script_name!r} not found for this trigger.",
		)
		return
	t0 = time.monotonic()
	flags._jarvis_trigger_depth = depth + 1
	try:
		script = frappe.get_doc("Server Script", script_name)
		script.execute_doc(doc)
	except frappe.ValidationError as e:
		duration_ms = int((time.monotonic() - t0) * 1000)
		if method in BLOCKABLE_EVENTS:
			# Intentional block. Enqueue Blocked IMMEDIATELY (survives the
			# rollback the re-raise triggers), then re-raise with an
			# identifying prefix so the blocked user knows which automation
			# stopped them (and an admin can find it).
			frappe.enqueue(
				"jarvis.triggers.engine.write_activity",
				queue="default",
				**base,
				status="Blocked",
				summary=str(e) or "blocked by trigger",
				detail=frappe.get_traceback(),
				duration_ms=duration_ms,
			)
			label = row.get("trigger_name") or "Trigger"
			if e.args and isinstance(e.args[0], str) and not e.args[0].startswith("[Automation:"):
				e.args = (f"[Automation: {label}] {e.args[0]}",) + e.args[1:]
			raise
		# Non-blockable event: a ValidationError here (often an incidental
		# subclass from a script bug) must NOT roll back the user's save.
		_record_activity(
			base,
			status="Failed",
			summary="script error (save not blocked)",
			detail=frappe.get_traceback(),
			duration_ms=duration_ms,
		)
	except Exception:
		_record_activity(
			base,
			status="Failed",
			summary="script error (save not blocked)",
			detail=frappe.get_traceback(),
			duration_ms=int((time.monotonic() - t0) * 1000),
		)
		frappe.log_error(
			title="Jarvis Trigger: script action failed",
			message=frappe.get_traceback(),
		)
	else:
		_record_activity(
			base,
			status="Success",
			summary="script ran",
			duration_ms=int((time.monotonic() - t0) * 1000),
		)
	finally:
		flags._jarvis_trigger_depth = depth


# --------------------------------------------------------------------------- #
# LLM action queue — mirrors frappe webhook's _add_webhook_to_queue / flush
# (after-commit flush; dedupe keeps only the LAST instance per key).
# --------------------------------------------------------------------------- #
def _snapshot_json(doc) -> str:
	"""The doc AT EVENT TIME as JSON: top-level keys starting with "_" are
	dropped, output clipped to the snapshot cap (LLM context, not archival)."""
	try:
		data = {k: v for k, v in doc.as_dict(convert_dates_to_str=True).items() if not str(k).startswith("_")}
		return frappe.as_json(data)[:_SNAPSHOT_CHAR_CAP]
	except Exception:
		return "{}"


def _drop_llm_queue() -> None:
	"""after_rollback cleanup: a rollback resets frappe's after_commit
	callbacks, so a lingering local queue would (a) hold stale jobs and
	(b) stop the next transaction from re-registering the flush."""
	if getattr(frappe.local, "_jarvis_trigger_llm_queue", None) is not None:
		del frappe.local._jarvis_trigger_llm_queue


def _llm_cap_reached(row: dict) -> bool:
	"""Cheap peek at today's per-trigger LLM counter so an over-cap trigger on
	a high-churn doctype stops paying the snapshot + enqueue cost on every save
	(the authoritative incr + the single Skipped marker still live in the job).
	Returns True only once the counter is STRICTLY over cap — i.e. after the
	job has already logged the cap+1 Skipped marker — so we never suppress that
	one marker. Never raises."""
	try:
		from jarvis.triggers.llm_action import _DEFAULT_DAILY_CAP, _cap_key

		cap = cint(row.get("llm_daily_cap")) or _DEFAULT_DAILY_CAP
		# The counter is a raw redis INCR value (llm_action uses cache.incr),
		# NOT a pickled set_value — read it with the raw GET, not get_value
		# (which pickle.loads and would raise on the plain integer).
		cache = frappe.cache()
		raw = cache.get(cache.make_key(_cap_key(row.get("name"))))
		used = int(raw) if raw is not None else 0
		return used > cap
	except Exception:
		return False


def _queue_llm_action(row: dict, doc, method: str) -> None:
	if _llm_cap_reached(row):
		return
	if getattr(frappe.local, "_jarvis_trigger_llm_queue", None) is None:
		frappe.local._jarvis_trigger_llm_queue = []
		frappe.db.after_commit.add(_flush_llm_queue)
		frappe.db.after_rollback.add(_drop_llm_queue)
	frappe.local._jarvis_trigger_llm_queue.append(
		frappe._dict(
			trigger=row.get("name"),
			doctype=doc.doctype,
			docname=str(doc.name or ""),
			event=method,
			snapshot_json=_snapshot_json(doc),
			fired_by=frappe.session.user,
		)
	)


def _flush_llm_queue() -> None:
	"""Enqueue all pending LLM evaluations (after commit).

	A trigger can fire multiple times on the same document in one transaction;
	the last queued snapshot is the document's final state for this DB
	transaction, so dedupe on (trigger, docname) keeping the LAST instance —
	exactly frappe's webhook flush."""
	if not getattr(frappe.local, "_jarvis_trigger_llm_queue", None):
		return

	uniq_keys = set()
	unique_last_instances = []

	# reverse
	frappe.local._jarvis_trigger_llm_queue.reverse()

	# deduplicate on (trigger, docname); the first hit in the reversed list is
	# the last queued instance
	for job in frappe.local._jarvis_trigger_llm_queue:
		key = (job.trigger, job.docname)
		if key not in uniq_keys:
			uniq_keys.add(key)
			unique_last_instances.append(job)

	# Clear the original queue so the next enqueue computation starts fresh.
	del frappe.local._jarvis_trigger_llm_queue

	# reverse again, to get back the original firing order
	unique_last_instances.reverse()

	for job in unique_last_instances:
		# NB: the event kwarg is named doc_event because frappe.enqueue
		# reserves `event` for itself (it would be swallowed, never reaching
		# the job function).
		frappe.enqueue(
			"jarvis.triggers.llm_action.run_llm_action",
			queue="default",
			timeout=180,
			trigger=job.trigger,
			doctype=job.doctype,
			docname=job.docname,
			doc_event=job.event,
			snapshot_json=job.snapshot_json,
			fired_by=job.fired_by,
		)


# --------------------------------------------------------------------------- #
# Activity log
# --------------------------------------------------------------------------- #
def _insert_activity(**fields) -> None:
	"""Insert one Jarvis Trigger Activity row (ignore_permissions) and
	best-effort notify the trigger's owner. Fully self-contained try/except:
	logging must NEVER break a dispatch (or the save it rides on)."""
	try:
		owner = fields.pop("trigger_owner", "") or ""
		frappe.get_doc(
			{
				"doctype": ACTIVITY,
				"trigger": fields.get("trigger") or "",
				"trigger_label": fields.get("trigger_label") or "",
				"target_doctype": fields.get("target_doctype") or "",
				"target_docname": fields.get("target_docname") or "",
				"doc_event": fields.get("doc_event") or "",
				"action_type": fields.get("action_type") or "",
				"status": fields.get("status") or "",
				"summary": (fields.get("summary") or "")[:_SUMMARY_CHAR_CAP],
				"detail": (fields.get("detail") or "")[:_DETAIL_CHAR_CAP],
				"duration_ms": cint(fields.get("duration_ms")),
				"event_user": fields.get("event_user") or frappe.session.user,
			}
		).insert(ignore_permissions=True)
		if owner:
			from jarvis.chat.events import publish_to_user

			publish_to_user(
				owner,
				{
					"kind": "trigger:activity",
					"trigger": fields.get("trigger") or "",
					"trigger_label": fields.get("trigger_label") or "",
					"status": fields.get("status") or "",
					# action_type lets the client notifier decide what to surface
					# (LLM "Success" findings warn; Script "Success" is quiet noise).
					"action_type": fields.get("action_type") or "",
				},
			)
	except Exception:
		try:
			frappe.log_error(
				title="Jarvis Trigger: activity write failed",
				message=frappe.get_traceback(),
			)
		except Exception:
			pass


def _record_activity(
	base: dict, *, status: str, summary: str = "", detail: str = "", duration_ms: int = 0
) -> None:
	"""Record a Success/Failed activity WITHOUT touching the user's save
	transaction: the insert + realtime publish are deferred to a background job
	that runs only after the save COMMITS (``enqueue_after_commit``). This keeps
	the hot save path free of the ~3 ms activity insert and never publishes a
	firing that later rolls back. Best-effort: enqueue failures must never break
	the dispatch (or the save it rides on).

	Blocked activities do NOT use this path — they must survive the rollback the
	block causes, so they enqueue immediately (see _run_script_action)."""
	if frappe.flags.in_test:
		# Synchronous inline insert in tests: keeps the run deterministic and
		# preserves the test transaction (no enqueue worker, no commit).
		try:
			_insert_activity(**base, status=status, summary=summary, detail=detail, duration_ms=duration_ms)
		except Exception:
			pass
		return
	try:
		frappe.enqueue(
			"jarvis.triggers.engine.write_activity",
			queue="default",
			enqueue_after_commit=True,
			**base,
			status=status,
			summary=summary,
			detail=detail,
			duration_ms=duration_ms,
		)
	except Exception:
		# Last resort: log inline rather than lose the record entirely. Still
		# guarded so a logging failure can't break the save.
		try:
			_insert_activity(**base, status=status, summary=summary, detail=detail, duration_ms=duration_ms)
		except Exception:
			pass


def write_activity(
	trigger: str = "",
	trigger_label: str = "",
	target_doctype: str = "",
	target_docname: str = "",
	doc_event: str = "",
	action_type: str = "",
	status: str = "",
	summary: str = "",
	detail: str = "",
	duration_ms: int = 0,
	event_user: str = "",
	trigger_owner: str = "",
) -> None:
	"""Background-job target for all activity writes (the enqueue_after_commit
	Success/Failed path AND the immediate Blocked path). Inserts the row + the
	realtime publish, then commits — it owns its own transaction."""
	_insert_activity(
		trigger=trigger,
		trigger_label=trigger_label,
		target_doctype=target_doctype,
		target_docname=target_docname,
		doc_event=doc_event,
		action_type=action_type,
		status=status,
		summary=summary,
		detail=detail,
		duration_ms=duration_ms,
		event_user=event_user,
		trigger_owner=trigger_owner,
	)
	frappe.db.commit()
