"""Shared dry-run sandbox for the preview paths (api._run_preview, preview_doc).

MariaDB facts this encodes: a real COMMIT releases all savepoints (so commits
are neutralized for the duration); a full-transaction abort (deadlock) also
releases them; and a savepoint rollback does NOT clear the before/after-commit
callback queues, so webhook/notification enqueues for the rolled-back doc are
dropped here or they would fire on the request's next real commit.

A bare ROLLBACK releases all savepoints too, exactly like a COMMIT, so a hook's
``frappe.db.rollback()`` with no savepoint is redirected to the sandbox's own
savepoint for the duration (see ``_Sandbox.rollback`` below). HRMS does this in
create_salary_slips_for_employees when a slip fails: rollback, write
``status: Failed``, commit. Unredirected, that rollback ended the sandbox's
transaction - taking with it whatever the request had written before the dry
run - and the ``Failed`` write that followed was real.

Background jobs ARE sandboxed. frappe.enqueue pushes to Redis immediately, which
no DB rollback undoes: a submit hook that enqueues (Payroll Entry's
create_salary_slips for more than 30 employees) used to start real work while
the confirmation card was merely being built. Frappe has no switch to defer or
suppress enqueue, but every path in frappe, erpnext and hrms - frappe.enqueue,
enqueue_doc, a direct import of enqueue - resolves ``get_queue`` from
frappe.utils.background_jobs at call time. That one name is wrapped ONCE per
process, the first time a sandbox runs, and never restored: the wrapper is a
pass-through unless THIS REQUEST is inside a sandbox (a frappe.local attribute,
so it cannot leak onto a reused worker thread), in which case it hands back a
queue that records the job and sends nothing. Never restoring is deliberate:
swap-and-restore lets another thread call a wrapper whose original has just
been put back. (A module that imported ``get_queue`` BY NAME before the wrap
keeps the original. Two Frappe modules do, doctor and the system health
report, and neither enqueues.)

One job is sent for real even from a dry run: see ``SURVIVES_DRY_RUN``.

The request is left as it was found. Frappe keeps per-request structures
(``_realtime_log``, ``_webhook_queue``, ``_link_count``, and jarvis's own
LLM-trigger queue) and registers the callback that flushes one only when it
CREATES it. So dropping the dry run's callbacks while leaving its structures
would silence a real write later in the same request: it would add to one that
nothing flushes. On exit they are put back as they were, the rollback callbacks
the dry run registered are run (they undo what SQL cannot: a written file, a
cache entry), and a document lock the dry run took (``Document.queue_action``, a
file under the site's locks/ folder that Frappe releases only after a JOB) is
released. The order of all that is in ``_Sandbox.exit``.

Still NOT sandboxed: HTTP or email sent directly inside a hook; a realtime
event published immediately (``after_commit=False``); a write to a
non-transactional table (Error Log is MyISAM on a stock site); DDL as the
dry run's FIRST statement, which commits implicitly and takes the savepoint
with it (the sandbox then fails closed, see ``PreviewSandboxLost``, but what the
request wrote before is committed; DDL after any write makes Frappe raise
``ImplicitCommitError`` before the ALTER, refused as a structure change, and the
structure doctypes never reach a dry run: ``jarvis.tools._write_risk``); and ``enqueue(deduplicate=True)`` deleting a FINISHED job of the
same id, which Frappe does before it ever asks for the queue.

Frappe internals this leans on. None is public API; each was read in the
version-15 and version-16 source and must be re-read when either moves:
``CallbackManager._functions`` (a deque, consumed from the left by ``run()``),
``frappe.local.locked_documents`` (where ``Document.lock`` records a lock),
``Database._disable_transaction_control`` (version-16 only), the request
structures named above, and the qualname ``enqueue.<locals>.enqueue_call`` of
the closure ``frappe.enqueue`` parks on after_commit. The tests in
test_preview_sandbox_enqueue.py fail on either version if one of them changes.
"""

import logging
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from functools import wraps
from typing import NamedTuple

import frappe
from frappe.utils import background_jobs
from rq import Queue

from jarvis.exceptions import PreviewSandboxLost

# Jobs that exist to OUTLIVE a rollback, by method. A Jarvis Trigger that blocks
# a save enqueues its Blocked audit row immediately, on purpose, because the
# block rolls the save back (triggers/engine.py, _run_script_action). A chat
# create / update is dry-run at park, the trigger raises inside the sandbox, and
# a dropped job would leave the blocked attempt recorded nowhere. So this one is
# sent for real - but only when it is enqueued IMMEDIATELY: the same method
# carries Success / Failed activities with enqueue_after_commit, and those
# belong to a save the dry run rolls back. Everything in this set runs
# unconfirmed, and is left out of ``will_queue``. Add a method here only, and
# only together with a test that pins why it must outlive the rollback.
SURVIVES_DRY_RUN = frozenset({"jarvis.triggers.engine.write_activity"})

# What rq must be asked to run for a job to survive: Frappe's own job runner.
# version-15 passes the function, version-16 its dotted path.
_EXECUTE_JOB = "frappe.utils.background_jobs.execute_job"

# ``Document.queue_action``: the action itself (a submit) runs as this job.
QUEUED_ACTION = "frappe.model.document.execute_action"

# Dropped like any other job, but never announced: Frappe enqueues this on EVERY
# delete (delete_doc.py, both versions) to clear the deleted document's comments,
# shares and to-dos. Announcing it would put "confirming starts a background job"
# on every delete and bury the jobs that matter. By method, like the sets above.
_NOT_ANNOUNCED = frozenset({"frappe.model.delete_doc.delete_dynamic_links"})

# What ``will_queue`` may carry. Job names can hold caller-supplied text
# (ERPNext builds some from document names) and the preview is stored on the
# pending record and in the transcript, so both the count and each name are
# bounded.
WILL_QUEUE_MAX = 20
WILL_QUEUE_NAME_MAX = 140

# Request-local structures whose flush callback is registered only on creation.
_REQUEST_STATE = ("_realtime_log", "_webhook_queue", "_jarvis_trigger_llm_queue", "_link_count")
_ABSENT = object()

# frappe.local attribute: absent / None outside a sandbox, the DroppedJobs of
# the dry run inside one. Request-scoped, like jarvis_dispatch_depth.
_LOCAL_DROPPED_JOBS = "jarvis_preview_dropped_jobs"

_LOGGER = "jarvis.preview"
LOST_TITLE = "jarvis preview sandbox: savepoint lost"
DISPLACED_TITLE = "jarvis preview sandbox: get_queue guard was displaced"

# The words the model relays when a dry run could not be undone cleanly.
_LOST_MESSAGE = (
	"This change could not be checked safely, so it was stopped: no confirmation card was created "
	"and nothing more was done. If the change alters database structure (for example a Custom "
	"Field), part of it may already be saved, so check before retrying. Retry this exact call at "
	"most once; if it fails again, tell the user and stop."
)

_install_lock = threading.Lock()
# The wrapper this process installed, or None before the first sandbox.
# "Installed" means THIS object is what get_queue resolves to (or what a wrapper
# built on it wraps), never an attribute: functools.wraps copies attributes.
_guard = None
# A displaced guard is reported once per process: an app that re-patches
# get_queue per request must not write an Error Log row per dry run.
_displacement_reported = False


class DroppedJobs(list):
	"""Names of the jobs a dry run did not send, in the order the real run
	would send them. One live list per request, shared by nested sandboxes
	(which is why its flags live here and not on one sandbox)."""

	# A hook called a bare frappe.db.rollback() and was redirected: its code
	# took a failure path, so the dry run may not show what Confirm will do.
	hook_rolled_back = False
	# True only while the sandbox replays after-commit enqueues to record them.
	replaying_after_commit = False
	# A lost savepoint has been reported for this dry run: once is enough when
	# sandboxes are nested (they all lose the same transaction).
	lost_reported = False

	def __init__(self, *args):
		super().__init__(*args)
		# The method each job really runs, in step with the names. A job's
		# NAME is the caller's to choose (frappe.enqueue takes job_name, and a
		# Server Script can pass one), so nothing is decided on it.
		self.methods = []

	def record(self, name: str, method: str) -> None:
		self.append(name)
		self.methods.append(method)


class WillQueue(NamedTuple):
	"""What a preview says about the jobs Confirm will start."""

	names: list  # distinct, cleaned, at most WILL_QUEUE_MAX plus a "+N more" marker
	jobs: int  # how many jobs Confirm will start
	kinds: int  # how many distinct names they have
	queued_action: bool  # one of them is the action itself (Document.queue_action)


def _dropped_jobs() -> DroppedJobs | None:
	"""The current request's dry run, or None outside a sandbox."""
	return getattr(frappe.local, _LOCAL_DROPPED_JOBS, None)


def _log(level: int, message: str, *, exc_info: bool = False) -> None:
	"""Write one line to the ``jarvis.preview`` log. NEVER raises: these calls
	sit between a dry run and its rollback, and a log file that cannot be
	written must not decide whether the rollback happens.

	frappe.logger hands out loggers at ERROR on a production bench (WARNING on
	a dev server), which would discard the two lines that matter: a job sent
	for real from a dry run, and a hook's rollback being redirected. So the
	level is pinned no higher than WARNING, the way chat/latency.py and
	telemetry.py pin theirs. Per-job "not sent" lines stay INFO: written only
	when the operator lowers the level."""
	try:
		logger = frappe.logger(_LOGGER)
		if logger.level == 0 or logger.level > logging.WARNING:
			logger.setLevel(logging.WARNING)
		logger.log(level, message, exc_info=exc_info)
	except Exception:
		pass


def _quoted(value) -> str:
	"""``value`` for a log line: clipped, and repr()-ed so that a newline in a
	job or document name cannot start a line of its own."""
	return repr(str(value)[:WILL_QUEUE_NAME_MAX])


def _who() -> str:
	"""``site=... user=...`` (and the tool, when this request is a tool call)
	for a log line. Never raises."""
	try:
		who = f"site={frappe.local.site} user={frappe.session.user}"
		tool = (getattr(frappe.local, "form_dict", None) or {}).get("tool")
		return f"{who} tool={_quoted(tool)}" if tool else who
	except Exception:
		return "site=? user=?"


def _job_target(queue_args: dict) -> str:
	"""`` doctype=... name=...`` when the job's own arguments name a document
	(enqueue_doc and queue_action do), else an empty string."""
	inner = queue_args.get("kwargs")
	if not isinstance(inner, dict):
		return ""
	doctype = inner.get("doctype") or inner.get("__doctype")
	name = inner.get("name") or inner.get("__name")
	return f" doctype={_quoted(doctype)} name={_quoted(name)}" if doctype else ""


def _method_name(method) -> str:
	if isinstance(method, str):
		return method
	return f"{getattr(method, '__module__', '?')}.{getattr(method, '__qualname__', repr(method))}"


def _clean_name(name) -> str:
	"""A job name as the model may read it: one line, no control characters
	(names are caller-supplied text), clipped."""
	text = "".join(ch if ch.isprintable() else " " for ch in str(name))
	return " ".join(text.split())[:WILL_QUEUE_NAME_MAX]


def will_queue_summary(dropped: list) -> WillQueue:
	"""What to tell a preview's reader about the jobs a dry run dropped.

	Jarvis's own ``SURVIVES_DRY_RUN`` methods are left out: a trigger's Success
	/ Failed activity is enqueued after commit on every save of a triggered
	doctype, and is bookkeeping, not work the user is starting. So is Frappe's
	own clean-up after a delete (``_NOT_ANNOUNCED``). ``jobs`` counts every
	remaining job, ``kinds`` their distinct names (500 salary-slip jobs are one
	kind).

	Both that and ``queued_action`` are decided on the METHOD a job runs
	(``DroppedJobs.methods``), never on its name: a job that merely calls itself
	``jarvis.triggers.engine.write_activity`` is still announced. A list with no
	methods on record hides nothing and flags nothing."""
	methods = getattr(dropped, "methods", ())
	jobs = [(name, methods[i] if i < len(methods) else None) for i, name in enumerate(dropped)]
	quiet = SURVIVES_DRY_RUN | _NOT_ANNOUNCED
	announced = [(_clean_name(name), method) for name, method in jobs if method not in quiet]
	kinds = list(dict.fromkeys(name for name, _ in announced))
	names = kinds[:WILL_QUEUE_MAX]
	if len(kinds) > WILL_QUEUE_MAX:
		names.append(f"+{len(kinds) - WILL_QUEUE_MAX} more")
	queued_action = any(method == QUEUED_ACTION for _, method in announced)
	return WillQueue(names, len(announced), len(kinds), queued_action)


class _DroppedJob:
	"""Stand-in for the rq Job a caller may read ``.id`` from."""

	def __init__(self, job_id=None):
		self.id = job_id or "jarvis-preview-dropped"

	def get_id(self):
		return self.id


def _built_by_frappe(func) -> bool:
	"""Whether rq is asked to run Frappe's own execute_job, which is what
	frappe.enqueue always passes and what reads the envelope."""
	return func is background_jobs.execute_job or func == _EXECUTE_JOB


def _real_method(queue_args: dict, func) -> str:
	"""The method the job will really run. The envelope's ``method`` counts
	only for a job Frappe built; for anything else rq runs ``func`` itself,
	whatever the kwargs claim."""
	if _built_by_frappe(func):
		return _method_name(queue_args.get("method") or "?")
	return _method_name(func) if func is not None else "?"


def _survives(method: str, func, dropped: DroppedJobs | None) -> bool:
	"""Whether this enqueue is sent for real: only a job Frappe's own enqueue
	built, for a method in SURVIVES_DRY_RUN. Naming the audit method in a job's
	kwargs or as its job_name, or handing it to rq directly, sends nothing."""
	return (
		dropped is not None
		and not dropped.replaying_after_commit
		and _built_by_frappe(func)
		and method in SURVIVES_DRY_RUN
	)


class _DroppingQueue(Queue):
	"""A real rq Queue that never sends.

	A real one, not a stub: Frappe READS the queue before using it
	(``_check_queue_size`` reads ``q.count``, ``get_jobs`` reads ``q.jobs``), and
	an AttributeError from a stub is not among the exceptions _pending_preview
	catches, so the park itself would fail. Reads still go to Redis; only the
	two methods that push are overridden. ``enqueue_many`` would still push;
	nothing in Frappe, ERPNext or HRMS calls it.
	"""

	def enqueue_call(self, *args, **kwargs):
		# Frappe passes its own envelope as ``kwargs=``; job_name is the dotted
		# method name there, already made readable by frappe.enqueue.
		queue_args = kwargs.get("kwargs") or {}
		func = args[0] if args else kwargs.get("func")
		method = _real_method(queue_args, func)
		name = queue_args.get("job_name") or _method_name(queue_args.get("method") or "?")
		name = name if isinstance(name, str) else _method_name(name)
		what = f"{_who()} job={_quoted(name)}{_job_target(queue_args)}"
		dropped = _dropped_jobs()
		if _survives(method, func, dropped):
			_log(logging.WARNING, f"dry run: job sent for real (it must outlive a rollback) {what}")
			try:
				real = Queue(self.name, connection=self.connection, is_async=self.is_async)
				return real.enqueue_call(*args, **kwargs)
			except Exception:
				# The caller is a trigger about to re-raise its own
				# ValidationError ("blocked by ..."). A Redis failure here must
				# not become the error the user sees instead.
				_log(logging.ERROR, f"dry run: could not send the job {what}", exc_info=True)
				return _DroppedJob(kwargs.get("job_id"))
		if dropped is not None:
			dropped.record(name, method)
		_log(logging.INFO, f"dry run: job not sent {what}")
		return _DroppedJob(kwargs.get("job_id"))

	def enqueue_job(self, job, *args, **kwargs):
		return job


def _is_installed() -> bool:
	"""Whether get_queue is our wrapper, or a wrapper built on it. A third
	party that wraps ours with functools.wraps still calls ours, and its
	``__wrapped__`` chain leads back to it; one that merely carries our
	attributes does not."""
	function, hops = background_jobs.get_queue, 0
	while function is not None and hops < 16:
		if function is _guard:
			return True
		function, hops = getattr(function, "__wrapped__", None), hops + 1
	return False


def _install_guard() -> None:
	"""Wrap background_jobs.get_queue once per process. Idempotent, and a
	tripwire: called on every sandbox entry, it re-installs (and reports, once)
	if something has put an unguarded get_queue back since. Never raises for
	that."""
	global _guard
	if _is_installed():
		return
	with _install_lock:
		if _is_installed():
			return
		real = background_jobs.get_queue
		displaced = _guard is not None

		@wraps(real)
		def get_queue(*args, **kwargs):
			q = real(*args, **kwargs)
			if _dropped_jobs() is None:
				# Outside a dry run: a job a Jarvis tool call enqueues runs under the
				# write-risk guard in the worker too (a pass-through otherwise).
				from jarvis.tools._write_risk import guard_queue

				return guard_queue(q)
			return _DroppingQueue(q.name, connection=q.connection, is_async=q.is_async)

		background_jobs.get_queue = _guard = get_queue
	if displaced:
		_report_displaced(real)


def _report_displaced(found) -> None:
	global _displacement_reported
	if _displacement_reported:
		return
	_displacement_reported = True
	message = (
		"frappe.utils.background_jobs.get_queue was replaced after the preview sandbox wrapped it "
		f"(found {found!r}). The wrap has been re-installed. Until it was, a dry run in this process "
		"could have queued real background jobs. Reported once per process."
	)
	_log(logging.ERROR, message)
	try:
		frappe.log_error(title=DISPLACED_TITLE, message=message)
	except Exception:
		pass  # reporting must never fail a dry run


def _report_lost(dropped: DroppedJobs | None) -> None:
	"""One Error Log row (a MyISAM table: it outlives the rollback a caller may
	still do) and one log line, under a title an operator can search for. Once
	per dry run: nested sandboxes all lose the same transaction."""
	if dropped is not None:
		if dropped.lost_reported:
			return
		dropped.lost_reported = True
	message = (
		f"A dry run's savepoint was gone when the preview sandbox came to roll back ({_who()}). The "
		"transaction was rolled back in full and the park was refused. Either a deadlock aborted the "
		"transaction, or the previewed write ran DDL (a Custom Field, a DocType), which commits "
		"implicitly: in that case whatever the request and the dry run wrote before the DDL is saved."
	)
	_log(logging.ERROR, message)
	try:
		frappe.log_error(title=LOST_TITLE, message=message)
	except Exception:
		pass  # reporting must never replace the refusal


def _run_added_since(manager, count_at_entry: int, *, swallow: bool = False) -> None:
	"""Run (and consume) only the callbacks added to ``manager`` after the
	first ``count_at_entry``. Nothing inside a sandbox can consume the earlier
	ones (commit is neutralized, rollback is redirected), so they are still the
	leading entries of the queue.

	One at a time, as Frappe's ``run()`` does: a callback is taken off the
	queue only when its turn comes, so when one raises the rest are still
	queued (and one that registers another gets it run too). ``swallow``: at
	sandbox exit a failing callback is logged and the rest still run, because
	the cleanup after it must happen; for a hook's own rollback it propagates,
	as it would from Frappe's."""
	functions = manager._functions
	while len(functions) > count_at_entry:
		function = functions[count_at_entry]
		del functions[count_at_entry]
		try:
			function()
		except Exception:
			if not swallow:
				raise
			_log(logging.ERROR, "dry run: a rollback callback failed at sandbox exit", exc_info=True)


def _is_dropped_enqueue(function) -> bool:
	"""Frappe's own ``enqueue_call`` closure (what ``enqueue_after_commit``
	puts on after_commit) holding a dropping queue: the only callback the
	sandbox will ever invoke, because invoking it records and sends nothing."""
	if getattr(function, "__module__", None) != background_jobs.__name__:
		return False
	if not getattr(function, "__qualname__", "").endswith("enqueue.<locals>.enqueue_call"):
		return False
	for cell in getattr(function, "__closure__", None) or ():
		try:
			if isinstance(cell.cell_contents, _DroppingQueue):
				return True
		except ValueError:  # an empty cell
			continue
	return False


def _record_after_commit_jobs(db, count_at_entry: int, dropped: DroppedJobs) -> None:
	"""Name the jobs the dry run enqueued with ``enqueue_after_commit``.

	Such a job never reaches the queue during a dry run: frappe.enqueue parks a
	closure on after_commit and returns, and the sandbox discards that closure
	unrun. ``Document.queue_action`` enqueues this way by default, so without
	this the jobs that matter most (a Stock Reconciliation over 100 rows) were
	missing from will_queue. Only Frappe's enqueue closures are invoked, never
	another callback, only those added since ``count_at_entry`` (an outer
	sandbox names its own, once), and ``replaying_after_commit`` keeps even a
	SURVIVES_DRY_RUN method from being sent: an after-commit job of a rolled
	back transaction is never sent by Frappe either."""
	if not hasattr(db, "after_commit"):
		return
	dropped.replaying_after_commit = True
	try:
		for function in list(db.after_commit._functions)[count_at_entry:]:
			if _is_dropped_enqueue(function):
				try:
					function()
				except Exception:
					_log(logging.ERROR, "dry run: could not name an after-commit job", exc_info=True)
	finally:
		dropped.replaying_after_commit = False


def _snapshot_request_state() -> dict:
	saved = {}
	for name in _REQUEST_STATE:
		value = getattr(frappe.local, name, _ABSENT)
		# A copy of the same type: _link_count is a defaultdict and must stay one.
		saved[name] = value.copy() if isinstance(value, list | dict) else value
	return saved


def _restore_request_state(saved: dict) -> None:
	"""Same rule api._run_tool applies to ``_realtime_log`` around a failed
	write: absent before means absent after, so the next real write creates
	the structure and registers its flush again."""
	local = frappe.local
	for name, value in saved.items():
		if value is _ABSENT:
			if hasattr(local, name):
				delattr(local, name)
		else:
			setattr(local, name, value)


def _release_locks_taken_since(held_at_entry: list) -> None:
	"""Unlock the documents the dry run locked. ``frappe.local.locked_documents``
	is where Document.lock records them; Frappe itself walks it only in
	after_job, never after a web request."""
	locked = getattr(frappe.local, "locked_documents", None) or []
	for doc in [d for d in locked if not any(d is held for held in held_at_entry)]:
		try:
			doc.unlock()
		except Exception:
			_log(logging.ERROR, "dry run: could not release a document lock", exc_info=True)


def _no_commit(*args, **kwargs) -> None:
	"""``frappe.db.commit`` for the duration of a sandbox."""


class _Sandbox:
	"""One dry run: what the request looked like when it began, and the steps
	that put it back. ``preview_sandbox`` is the only caller."""

	def __init__(self):
		db = self.db = frappe.db
		self.real_commit = db.commit
		self.real_rollback = db.rollback
		# Frappe 16 only. sql_ddl zeroes it and restores it without a finally, so a
		# DDL the dry run refuses inside a doc_events handler leaves it at -1 (the
		# handler's finally decrements): every later commit and full rollback of the
		# request would then be silently skipped. Put back first thing at exit.
		self.transaction_control = getattr(db, "_disable_transaction_control", None)
		self.commit_queues = {
			name: tuple(getattr(db, name)._functions)
			for name in ("before_commit", "after_commit")
			if hasattr(db, name)
		}
		self.rollback_counts = {
			name: len(getattr(db, name)._functions)
			for name in ("before_rollback", "after_rollback")
			if hasattr(db, name)
		}
		self.request_state = _snapshot_request_state()
		self.locks_held = list(getattr(frappe.local, "locked_documents", None) or [])
		self.outermost = _dropped_jobs() is None
		self.savepoint = "jps_" + frappe.generate_hash(length=10)
		self.savepoint_set = False
		self.savepoint_lost = False
		self.returned_normally = False
		self.dropped = None

	def enter(self) -> DroppedJobs:
		"""Neutralise commit, redirect rollback, start dropping jobs, set the
		savepoint. A failure part-way is undone by ``exit``, which the caller
		runs whatever happens here."""
		self.db.commit = _no_commit
		self.db.rollback = self.rollback
		if self.outermost:
			setattr(frappe.local, _LOCAL_DROPPED_JOBS, DroppedJobs())
		self.dropped = _dropped_jobs()
		self.db.savepoint(self.savepoint)
		self.savepoint_set = True
		return self.dropped

	def rollback(self, *, save_point=None, **kwargs):
		"""``frappe.db.rollback`` for the duration. Same keyword-only signature
		(``chain`` exists on Frappe 16 only, hence **kwargs).

		A rollback to a NAMED savepoint is the caller's own nested transaction
		and passes straight through. So does a bare rollback where Frappe 16
		has transaction control disabled (it runs doc_events hooks that way):
		the real one warns and does nothing there, and so must this.

		A bare rollback means "undo this transaction". Inside a sandbox the
		hook's transaction IS the dry run, so it becomes a rollback to the
		sandbox's savepoint, which survives ROLLBACK TO SAVEPOINT and so keeps
		the sandbox closed for whatever the hook writes next. Everything else
		Frappe's rollback does is kept, scoped the same way: the commit
		callbacks the dry run queued are dropped, the rollback callbacks it
		registered run (they undo its non-DB effects: a written file, a cache
		entry), and the value cache is cleared by the savepoint branch. The
		request's own callbacks, from before the sandbox, are left alone: its
		transaction was not rolled back. No ``begin()``: the transaction never
		ended.

		The dropped-jobs list is deliberately NOT cleared. A job is recorded at
		the moment the real run would have pushed it to Redis, and a rollback
		does not pull a job back out of Redis: in the real run it is queued
		regardless, so will_queue must still name it. (A job enqueued with
		enqueue_after_commit is not in the list until the sandbox exits, and
		this rollback drops its callback first, as Frappe's own reset would:
		it is never reported, because it would never be sent.)

		The redirect is recorded (``hook_rolled_back``): code that rolls back
		has taken a failure path, and the preview says so.

		If the savepoint is already gone (a deadlock aborted the transaction)
		this raises 1305 into the hook, and whether the hook swallows it or
		not ``exit`` fails the park. That is the safe outcome: carrying on
		would write for real.
		"""
		if save_point or getattr(self.db, "_disable_transaction_control", 0):
			return self.real_rollback(save_point=save_point, **kwargs)
		if self.dropped is not None:
			self.dropped.hook_rolled_back = True
		_log(
			logging.WARNING, f"dry run: a bare frappe.db.rollback() was redirected to the savepoint {_who()}"
		)
		self._restore_commit_queues()
		self._run_rollback_callbacks("before_rollback")
		self.real_rollback(save_point=self.savepoint)
		self._run_rollback_callbacks("after_rollback")

	def exit(self, returned_normally: bool) -> None:
		"""Undo the dry run and put the request back. THE exit order, and why:

		1. Name the after-commit jobs (only when the body returned normally:
		   nobody reads the list otherwise). First, while the request flag is
		   still set, so the replayed enqueues are dropped and not sent.
		2. Run the before_rollback callbacks the dry run registered. Commit is
		   STILL neutralised and rollback still redirected: these run before
		   the SQL rollback, and one that commits (a Script Manager can
		   register one) would make the unconfirmed document real.
		3. ROLLBACK TO SAVEPOINT. Steps 1 and 2 cannot prevent it: every step
		   runs whatever the ones before it did. If the savepoint is gone,
		   that is remembered for step 8.
		4. Run the dry run's after_rollback callbacks (they undo what SQL
		   cannot: a written file, a cache entry). Commit still neutralised.
		5. Put the before / after_commit queues back: a savepoint rollback does
		   not clear them, and the dry run's webhooks and enqueues must not
		   fire on the request's next real commit.
		6. Put the request-local structures back, so a real write later in
		   this request registers its flush again.
		7. Release the document locks the dry run took.
		8. Fail closed if the savepoint was lost: roll the transaction back in
		   full (with a bare ROLLBACK if Frappe's own did not send one, see
		   ``_roll_back_raw``). After 5 and 6, so Frappe's own rollback empties the queues it
		   owns, and before 9, because the request's own before_rollback
		   callbacks run inside it ahead of the SQL: a commit there would keep
		   what the dry run wrote after the savepoint went.
		9. Give commit and rollback back.
		10. Clear the request flag (the outermost sandbox only). LAST, and
		   nothing may skip it: left set, every later real enqueue of this
		   request would be silently dropped.

		0. (Before 1) put Frappe 16's ``_disable_transaction_control`` back to its
		   value at entry, so the rollbacks below are not skipped.

		Then, with the request clean: re-raise the first error a step raised.
		Otherwise, if the savepoint was lost, report it (one Error Log row)
		and raise PreviewSandboxLost, unless the body raised: then its own
		error is the one the caller sees, and the loss is still reported."""
		self.returned_normally = returned_normally
		first_error = None
		for step in (
			self._restore_transaction_control,
			self._name_after_commit_jobs,
			self._run_before_rollback,
			self._roll_back_to_savepoint,
			self._run_after_rollback,
			self._restore_commit_queues,
			self._restore_request_state,
			self._release_locks,
			self._roll_back_in_full_if_lost,
			self._restore_commit_and_rollback,
			self._clear_request_flag,
		):
			try:
				step()
			except BaseException as e:  # the steps after it must still run
				first_error = first_error or e
		if first_error is not None:
			_log(logging.ERROR, f"dry run: a cleanup step failed at sandbox exit: {first_error!r}")
			raise first_error
		if not self.savepoint_lost:
			return
		_report_lost(self.dropped)
		if returned_normally:
			raise PreviewSandboxLost(_LOST_MESSAGE)

	def _restore_transaction_control(self) -> None:
		if self.transaction_control is not None:
			self.db._disable_transaction_control = self.transaction_control

	def _name_after_commit_jobs(self) -> None:
		if self.returned_normally and self.dropped is not None:
			count_at_entry = len(self.commit_queues.get("after_commit", ()))
			_record_after_commit_jobs(self.db, count_at_entry, self.dropped)

	def _run_rollback_callbacks(self, name: str, *, swallow: bool = False) -> None:
		if name in self.rollback_counts:
			_run_added_since(getattr(self.db, name), self.rollback_counts[name], swallow=swallow)

	def _run_before_rollback(self) -> None:
		self._run_rollback_callbacks("before_rollback", swallow=True)

	def _roll_back_to_savepoint(self) -> None:
		if not self.savepoint_set:
			return  # entry failed before the body ran: there is nothing to undo
		try:
			self.real_rollback(save_point=self.savepoint)
		except BaseException as e:
			# The savepoint went with the transaction it belonged to (a deadlock
			# abort, an implicit commit by DDL), so what the dry run wrote after
			# that sits in a LIVE transaction the caller is about to commit.
			# This used to be ``except Exception: pass``.
			self.savepoint_lost = True
			if not isinstance(e, Exception):
				raise

	def _run_after_rollback(self) -> None:
		self._run_rollback_callbacks("after_rollback", swallow=True)

	def _restore_commit_queues(self) -> None:
		for name, functions in self.commit_queues.items():
			getattr(self.db, name)._functions = deque(functions)

	def _restore_request_state(self) -> None:
		_restore_request_state(self.request_state)

	def _release_locks(self) -> None:
		_release_locks_taken_since(self.locks_held)

	def _roll_back_in_full_if_lost(self) -> None:
		"""A full rollback discards exactly what the dry run wrote after the
		savepoint went: the request's own earlier writes were already lost
		(abort) or already committed (DDL) at that moment."""
		if not self.savepoint_lost:
			return
		try:
			# Frappe 16 only warns while transaction control is disabled (it
			# runs doc_events hooks that way): do not mistake that for a rollback.
			if not getattr(self.db, "_disable_transaction_control", 0):
				self.real_rollback()
				return
		except Exception:
			# Frappe runs before_rollback callbacks BEFORE it sends the SQL, so
			# one of the request's own that raises means no ROLLBACK was sent.
			_log(logging.ERROR, "dry run: full rollback after a lost savepoint failed", exc_info=True)
		self._roll_back_raw()

	def _roll_back_raw(self) -> None:
		"""The statements themselves, as ``Database.rollback`` sends them
		(``sql("rollback")`` then ``begin()``, both versions), with what it
		does around them that cannot raise: the commit callbacks of the
		transaction that is gone are dropped."""
		try:
			for name in self.commit_queues:
				getattr(self.db, name).reset()
			self.db.sql("rollback")
			self.db.begin()
			self.db.value_cache.clear()
			_log(logging.ERROR, f"dry run: rolled back with a bare ROLLBACK after a lost savepoint {_who()}")
		except Exception:
			_log(logging.ERROR, "dry run: bare ROLLBACK after a lost savepoint failed", exc_info=True)

	def _restore_commit_and_rollback(self) -> None:
		self.db.commit = self.real_commit
		self.db.rollback = self.real_rollback

	def _clear_request_flag(self) -> None:
		if self.outermost:
			setattr(frappe.local, _LOCAL_DROPPED_JOBS, None)


@contextmanager
def preview_sandbox() -> Iterator[DroppedJobs]:
	"""Run the body with every DB effect rolled back and nothing queued.

	Yields the names of the jobs it did not send. The list is LIVE (a job
	enqueued with enqueue_after_commit is added only as the sandbox exits, so
	read it after the ``with``) and is the same object for nested sandboxes.

	Raises PreviewSandboxLost on exit if the savepoint was lost under a body
	that returned normally. A caller that catches JarvisError or Exception
	around this must let that one through: see its docstring."""
	_install_guard()
	sandbox = _Sandbox()
	returned_normally = False
	try:
		# Entered inside the try: a failure there (the savepoint could not be
		# set) must still restore commit and rollback and clear the flag.
		yield sandbox.enter()
		returned_normally = True
	finally:
		sandbox.exit(returned_normally)
