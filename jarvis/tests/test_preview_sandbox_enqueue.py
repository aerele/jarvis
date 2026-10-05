"""A dry run must never queue background work, and a hook cannot end it.

preview_sandbox rolls back DB writes, but frappe.enqueue pushes to Redis at
once, so a submit hook that enqueues (Payroll Entry.create_salary_slips for
more than 30 employees) started real work when the confirmation card was
merely built. And a hook that calls a bare frappe.db.rollback() (the same
Payroll Entry code, when a slip fails) ended the sandbox's transaction, so
whatever it wrote next was real. These tests pin both suppressions, what a
dry run leaves behind on the request (nothing), and their limits.

Nothing here touches a shared queue: rq.Queue.enqueue_job is the single point
where rq pushes to Redis, so patching it records what WOULD have been sent and
sends nothing. A test that takes a document lock removes it.
"""

import functools
import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import background_jobs
from rq import Queue

from jarvis.exceptions import JarvisError, PreviewSandboxLost
from jarvis.tests.test_filebox_held import _Base as _FileBoxBase
from jarvis.tests.test_filebox_held import _supplier
from jarvis.tools import _preview_sandbox as sandbox_module
from jarvis.tools._preview_sandbox import preview_sandbox, will_queue_summary

PING = "frappe.ping"
BLOCKED_AUDIT = "jarvis.triggers.engine.write_activity"
THIS = "jarvis.tests.test_preview_sandbox_enqueue"
CALLBACK_QUEUES = ("before_commit", "after_commit", "before_rollback", "after_rollback")
REQUEST_LISTS = ("_realtime_log", "_webhook_queue", "_jarvis_trigger_llm_queue", "_link_count")
QUEUED_ACTION = "frappe.model.document.execute_action"
DELETE_HOUSEKEEPING = "frappe.model.delete_doc.delete_dynamic_links"
LOST_TITLE = "jarvis preview sandbox: savepoint lost"


@contextmanager
def sent_to_redis():
	"""Yield the list of jobs rq would have pushed. Nothing reaches Redis."""
	sent = []

	def record(self, job, *args, **kwargs):
		sent.append(job)
		return job

	with patch.object(Queue, "enqueue_job", record):
		yield sent


def _job_names(sent):
	return [(job.kwargs or {}).get("job_name") for job in sent]


def _lose_the_savepoint():
	"""What a deadlock abort or an implicit commit (DDL) does to the sandbox:
	the transaction its savepoint belonged to is gone."""
	frappe.db.sql("rollback")
	frappe.db.begin()


def _boom():
	raise RuntimeError("callback failed")


@contextmanager
def preview_log(level=logging.ERROR):
	"""Yield the records the ``jarvis.preview`` logger really emits. ``level`` is
	what the logger starts at: ERROR is what frappe.logger gives a production
	bench (frappe/utils/logger.py, ``default_log_level``), so a record seen at
	that setting is a line an operator will find in the log file."""
	logger = frappe.logger("jarvis.preview")
	records = []

	class Capture(logging.Handler):
		def emit(self, record):
			records.append(record)

	handler, before = Capture(), logger.level
	logger.setLevel(level)
	logger.addHandler(handler)
	try:
		yield records
	finally:
		logger.removeHandler(handler)
		logger.setLevel(before)


def _lines(records, level):
	return [record.getMessage() for record in records if record.levelno == level]


def _todo(description):
	return frappe.get_doc({"doctype": "ToDo", "description": description}).insert(ignore_permissions=True)


def _todo_exists(description):
	return bool(frappe.db.exists("ToDo", {"description": description}))


@contextmanager
def doc_event(doctype, event, handler):
	"""Hook ``handler`` (a dotted path in this module) to one doc event, the way
	an app's hooks.py would, for the duration."""
	hooks = frappe.get_doc_hooks()
	with patch.dict(hooks, {doctype: {**hooks.get(doctype, {}), event: [f"{THIS}.{handler}"]}}):
		yield


# doc_events handlers, resolved by dotted path like any app's.
def _hook_enqueues(doc, method=None):
	frappe.enqueue(PING)
	frappe.enqueue("frappe.utils.now", enqueue_after_commit=True)


def _hook_rolls_back_writes_and_commits(doc, method=None):
	# What HRMS create_salary_slips_for_employees does when a slip fails.
	frappe.db.rollback()
	frappe.db.set_value("ToDo", doc.name, "status", "Cancelled", update_modified=False)
	frappe.db.commit()


class _IsolatedRequestState(FrappeTestCase):
	"""Each test starts with empty frappe.db callback queues and none of the
	request-local lists, and gets the runner's own back afterwards. So a test
	can RUN a queue (``frappe.db.after_commit.run()``) and fire only what it
	added, and nothing a test registers can leak into the runner's commit."""

	def setUp(self):
		super().setUp()
		db, local = frappe.db, frappe.local
		queues = {name: getattr(db, name)._functions for name in CALLBACK_QUEUES}
		lists = {name: getattr(local, name) for name in REQUEST_LISTS if hasattr(local, name)}
		for name in CALLBACK_QUEUES:
			getattr(db, name)._functions = deque()
		for name in lists:
			delattr(local, name)

		def restore():
			for name, functions in queues.items():
				getattr(db, name)._functions = functions
			for name in REQUEST_LISTS:
				if hasattr(local, name):
					delattr(local, name)
			for name, value in lists.items():
				setattr(local, name, value)

		self.addCleanup(restore)
		# Registered last, so it runs first: some tests here really do roll
		# the test transaction back when the fix is absent.
		self.addCleanup(frappe.db.rollback)


class TestPreviewSandboxEnqueue(_IsolatedRequestState):
	def test_enqueue_reaches_redis_outside_a_sandbox(self):
		# The control: proves sent_to_redis sees a real enqueue, so the
		# zeroes below mean "suppressed", not "the probe is blind".
		with sent_to_redis() as sent:
			frappe.enqueue(PING)
		self.assertEqual(_job_names(sent), [PING])

	def test_frappe_enqueue_is_suppressed(self):
		with sent_to_redis() as sent, preview_sandbox() as dropped:
			frappe.enqueue(PING)
		self.assertEqual(sent, [])
		self.assertEqual(dropped, [PING])

	def test_direct_import_enqueue_is_suppressed(self):
		from frappe.utils.background_jobs import enqueue

		with sent_to_redis() as sent, preview_sandbox() as dropped:
			enqueue(PING)
		self.assertEqual(sent, [])
		self.assertEqual(dropped, [PING])

	def test_enqueue_doc_is_suppressed(self):
		with sent_to_redis() as sent, preview_sandbox() as dropped:
			frappe.enqueue_doc("User", "Administrator", "validate")
		self.assertEqual(sent, [])
		self.assertEqual(dropped, ["frappe.utils.background_jobs.run_doc_method"])

	def test_a_callable_is_reported_by_its_dotted_name(self):
		with sent_to_redis() as sent, preview_sandbox() as dropped:
			frappe.enqueue(frappe.ping)
		self.assertEqual(sent, [])
		self.assertEqual(dropped, [PING])

	def test_enqueue_after_commit_is_reported_and_never_fires(self):
		# Document.queue_action enqueues this way by default, so this is the
		# shape of the jobs that matter most (a 100-row Stock Reconciliation).
		ran = []
		with sent_to_redis() as sent:
			with preview_sandbox() as dropped:
				frappe.enqueue(PING, enqueue_after_commit=True)
				frappe.db.after_commit.add(lambda: ran.append("not an enqueue"))
				self.assertEqual(dropped, [])  # nothing handed to the queue yet
			# Reported without the caller doing anything ...
			self.assertEqual(dropped, [PING])
			# ... no other callback of the dry run was run to find that out ...
			self.assertEqual(ran, [])
			# ... and the request's real commit has nothing left to send.
			frappe.db.after_commit.run()
		self.assertEqual(sent, [])
		self.assertEqual(ran, [])

	def test_enqueue_after_commit_in_a_nested_sandbox_is_reported(self):
		with sent_to_redis() as sent, preview_sandbox() as outer:
			with preview_sandbox():
				frappe.enqueue(PING, enqueue_after_commit=True)
			self.assertEqual(outer, [PING])
		self.assertEqual(sent, [])
		self.assertEqual(outer, [PING])

	def test_an_outer_sandboxes_after_commit_job_is_named_once_and_by_the_outer(self):
		# The inner sandbox replays only what IT added. Replaying the whole
		# queue would name the outer's job at the inner exit and again at the
		# outer's: a count of two for one job.
		with sent_to_redis() as sent, preview_sandbox() as outer:
			frappe.enqueue(PING, enqueue_after_commit=True)
			with preview_sandbox():
				pass
			self.assertEqual(outer, [])
		self.assertEqual(outer, [PING])
		self.assertEqual(sent, [])

	def test_an_after_commit_job_from_before_the_sandbox_is_left_for_the_request(self):
		with sent_to_redis() as sent:
			frappe.enqueue(PING, enqueue_after_commit=True)
			with preview_sandbox() as dropped:
				pass
			self.assertEqual(dropped, [])
			self.assertEqual(sent, [])
			# Still owed, and sent by the request's own commit.
			frappe.db.after_commit.run()
		self.assertEqual(_job_names(sent), [PING])

	def test_enqueue_after_commit_is_not_reported_when_the_body_raises(self):
		# Nobody reads the list then; the callback is discarded unrun.
		seen = []
		with sent_to_redis() as sent:
			with self.assertRaises(ValueError):
				with preview_sandbox() as dropped:
					seen.append(dropped)
					frappe.enqueue(PING, enqueue_after_commit=True)
					raise ValueError("boom")
			frappe.db.after_commit.run()
		self.assertEqual(sent, [])
		self.assertEqual(seen[0], [])

	def test_enqueue_works_again_after_the_sandbox(self):
		with sent_to_redis() as sent:
			with preview_sandbox():
				frappe.enqueue(PING)
			frappe.enqueue(PING)
		self.assertEqual(len(sent), 1)

	def test_restored_even_when_the_body_raises(self):
		with sent_to_redis() as sent:
			with self.assertRaises(ValueError):
				with preview_sandbox():
					raise ValueError("boom")
			frappe.enqueue(PING)
		self.assertEqual(len(sent), 1)

	def test_nested_sandboxes(self):
		with sent_to_redis() as sent:
			with preview_sandbox() as outer:
				with preview_sandbox() as inner:
					frappe.enqueue(PING)
				self.assertIs(inner, outer)
				frappe.enqueue(PING)
			self.assertEqual(sent, [])
			self.assertEqual(outer, [PING, PING])
			frappe.enqueue(PING)
		self.assertEqual(len(sent), 1)

	def test_other_threads_still_enqueue(self):
		site = frappe.local.site
		inside, finished = threading.Event(), threading.Event()
		errors = []

		def other():
			try:
				frappe.init(site=site)
				frappe.connect()
				self.assertTrue(inside.wait(10), "main thread never entered the sandbox")
				frappe.enqueue(PING)
			except Exception as e:  # surfaced below; a swallowed error would pass vacuously
				errors.append(e)
			finally:
				frappe.destroy()
				finished.set()

		with sent_to_redis() as sent:
			# daemon + joined before the patch is lifted: a stalled thread can
			# never push a real job after sent_to_redis is gone.
			t = threading.Thread(target=other, daemon=True)
			t.start()
			try:
				with preview_sandbox():
					inside.set()
					self.assertTrue(finished.wait(20), "second thread did not finish")
			finally:
				inside.set()
				t.join(30)
				self.assertFalse(t.is_alive(), "second thread is still running")
		self.assertEqual(errors, [])
		self.assertEqual(_job_names(sent), [PING])

	def test_dropped_job_looks_like_a_job(self):
		with sent_to_redis(), preview_sandbox():
			job = frappe.enqueue(PING, job_id="jarvis-dropped-id")
			bare = background_jobs.get_queue("default").enqueue_call(PING)
		self.assertEqual(type(job).__name__, "_DroppedJob")
		# The id the real job would have had: callers key progress and dedup on it.
		self.assertEqual(job.id, background_jobs.create_job_id("jarvis-dropped-id"))
		self.assertEqual(job.get_id(), job.id)
		self.assertEqual(bare.id, "jarvis-preview-dropped")

	def test_queue_can_still_be_read_inside_the_sandbox(self):
		# Through the module attribute, as Frappe's own callers resolve it: a
		# name imported before the guard was installed would be the original.
		with preview_sandbox():
			q = background_jobs.get_queue("default")
			self.assertEqual(type(q).__name__, "_DroppingQueue")
			self.assertIsInstance(q.count, int)
		self.assertIs(type(background_jobs.get_queue("default")), Queue)


class TestGuardInstall(_IsolatedRequestState):
	"""The get_queue wrap: installed once, and a tripwire if something replaces
	it. "Installed" is identity with the wrapper this process built, never an
	attribute: functools.wraps copies attributes onto whatever wraps ours."""

	def setUp(self):
		super().setUp()
		with preview_sandbox():
			pass
		self.installed = background_jobs.get_queue
		self.real = self.installed.__wrapped__

	@contextmanager
	def never_installed(self):
		"""A process that has not run a sandbox yet: the original function, and
		no wrapper on record (so the tripwire has nothing to report)."""
		with (
			patch.object(background_jobs, "get_queue", self.real),
			patch.object(sandbox_module, "_guard", None),
			patch.object(sandbox_module, "_displacement_reported", False),
		):
			yield

	def test_install_is_idempotent(self):
		for _ in range(3):
			with preview_sandbox():
				pass
		self.assertIs(background_jobs.get_queue, self.installed)
		self.assertIs(sandbox_module._guard, self.installed)
		self.assertIsNot(self.real, self.installed)

	def test_racing_installs_build_one_wrapper(self):
		# The window between "not installed" and "installed" is held open, so
		# without the lock (or its second check) every thread is inside it at
		# once and each builds its own wrapper.
		real_wraps = sandbox_module.wraps
		barrier = threading.Barrier(8)
		built, errors = [], []

		def slow_wraps(wrapped, *args, **kwargs):
			built.append(wrapped)
			time.sleep(0.3)
			return real_wraps(wrapped, *args, **kwargs)

		def install():
			try:
				barrier.wait(10)
				sandbox_module._install_guard()
			except Exception as e:
				errors.append(e)

		with self.never_installed(), patch.object(sandbox_module, "wraps", slow_wraps):
			threads = [threading.Thread(target=install, daemon=True) for _ in range(8)]
			for t in threads:
				t.start()
			for t in threads:
				t.join(30)
			guard = background_jobs.get_queue
			self.assertEqual(errors, [])
			self.assertEqual(built, [self.real])
			self.assertIs(guard, sandbox_module._guard)
			self.assertIs(guard.__wrapped__, self.real)
		self.assertIs(background_jobs.get_queue, self.installed)

	def test_a_mock_does_not_read_as_installed(self):
		self.assertTrue(sandbox_module._is_installed())
		with patch.object(background_jobs, "get_queue", MagicMock()):
			self.assertFalse(sandbox_module._is_installed())

	def test_a_displaced_guard_is_reinstalled_and_reported(self):
		with (
			sent_to_redis() as sent,
			patch.object(background_jobs, "get_queue", self.real),  # something put the original back
			patch.object(sandbox_module, "_guard", self.installed),
			patch.object(sandbox_module, "_displacement_reported", False),
			patch.object(frappe, "log_error") as log_error,
		):
			with preview_sandbox() as dropped:
				frappe.enqueue(PING)
			self.assertIs(background_jobs.get_queue, sandbox_module._guard)
			self.assertIsNot(background_jobs.get_queue, self.installed)
			self.assertIs(background_jobs.get_queue.__wrapped__, self.real)
		self.assertEqual(sent, [])
		self.assertEqual(dropped, [PING])
		self.assertEqual(log_error.call_count, 1)
		self.assertIn("get_queue", log_error.call_args.kwargs["title"])
		self.assertIs(background_jobs.get_queue, self.installed)

	def test_a_displacement_is_reported_once_per_process(self):
		with (
			sent_to_redis() as sent,
			patch.object(background_jobs, "get_queue", self.real),
			patch.object(sandbox_module, "_guard", self.installed),
			patch.object(sandbox_module, "_displacement_reported", False),
			patch.object(frappe, "log_error") as log_error,
		):
			for _ in range(3):
				background_jobs.get_queue = self.real  # displaced again, every time
				with preview_sandbox() as dropped:
					frappe.enqueue(PING)
				# Re-installed every time ...
				self.assertEqual(dropped, [PING])
		self.assertEqual(sent, [])
		# ... reported once: an app that re-patches per request must not write
		# an Error Log row per dry run.
		self.assertEqual(log_error.call_count, 1)

	def test_a_wrapper_built_on_ours_is_accepted(self):
		ours = self.installed

		@functools.wraps(ours)
		def theirs(*args, **kwargs):
			return ours(*args, **kwargs)

		with (
			sent_to_redis() as sent,
			patch.object(background_jobs, "get_queue", theirs),
			patch.object(frappe, "log_error") as log_error,
		):
			with preview_sandbox() as dropped:
				frappe.enqueue(PING)
			self.assertIs(background_jobs.get_queue, theirs)
		self.assertEqual(sent, [])
		self.assertEqual(dropped, [PING])
		log_error.assert_not_called()

	def test_a_function_that_only_carries_our_attributes_is_not_us(self):
		# What functools.update_wrapper leaves on a wrapper that was built on
		# ours and then re-pointed at the original: every attribute of ours,
		# and no call to ours.
		real = self.real

		def theirs(*args, **kwargs):
			return real(*args, **kwargs)

		theirs.__dict__.update(self.installed.__dict__)
		theirs.__wrapped__ = real
		with (
			sent_to_redis() as sent,
			patch.object(background_jobs, "get_queue", theirs),
			patch.object(sandbox_module, "_guard", self.installed),
			patch.object(sandbox_module, "_displacement_reported", False),
			patch.object(frappe, "log_error") as log_error,
		):
			self.assertFalse(sandbox_module._is_installed())
			with preview_sandbox() as dropped:
				frappe.enqueue(PING)
		self.assertEqual(sent, [])
		self.assertEqual(dropped, [PING])
		self.assertEqual(log_error.call_count, 1)


class TestBlockedTriggerAuditSurvives(_IsolatedRequestState):
	"""A Jarvis Trigger that blocks a save enqueues its Blocked audit row
	IMMEDIATELY, on purpose, so it outlives the rollback the block causes. A
	chat create_doc is dry-run at park; the trigger raises inside the sandbox.
	That one job must still be sent for real, or the blocked attempt is
	recorded nowhere."""

	def _blocking_trigger(self):
		from jarvis.triggers import engine

		with patch(
			"jarvis.jarvis.doctype.jarvis_trigger.jarvis_trigger.is_safe_exec_enabled", return_value=True
		):
			trigger = frappe.get_doc(
				{
					"doctype": "Jarvis Trigger",
					"trigger_name": f"sandbox-{frappe.generate_hash(length=8)}",
					"target_doctype": "ToDo",
					"doc_event": "validate",
					"condition": 'doc.description == "jarvis-blocked"',
					"action_type": "Script",
					"script_body": 'frappe.throw("blocked by the sandbox test")',
				}
			).insert(ignore_permissions=True)
		engine.clear_cache()
		# The row goes with the test transaction; the engine's cache must not
		# keep naming it.
		self.addCleanup(engine.clear_cache)
		return trigger

	def test_blocked_audit_job_is_sent_from_a_parked_create(self):
		from jarvis import api

		with sent_to_redis() as sent:
			trigger = self._blocking_trigger()
			del sent[:]  # whatever creating the fixture enqueued is not under test
			with (
				patch("frappe.utils.safe_exec.is_safe_exec_enabled", return_value=True),
				doc_event("ToDo", "before_validate", "_hook_enqueues"),
			):
				with self.assertRaises(frappe.ValidationError):
					api._run_preview(
						"create_doc", {"doctype": "ToDo", "values": {"description": "jarvis-blocked"}}
					)
		# Exactly that one job; the hook's own two enqueues were dropped.
		self.assertEqual(_job_names(sent), [BLOCKED_AUDIT])
		audit = sent[0].kwargs["kwargs"]
		self.assertEqual((audit["status"], audit["trigger"]), ("Blocked", trigger.name))
		self.assertFalse(_todo_exists("jarvis-blocked"))

	def test_the_same_method_enqueued_after_commit_is_not_sent(self):
		# Success / Failed activities use the same method with
		# enqueue_after_commit: they belong to a save that was rolled back.
		with sent_to_redis() as sent, preview_sandbox() as dropped:
			frappe.enqueue(BLOCKED_AUDIT, enqueue_after_commit=True, status="Success")
		self.assertEqual(sent, [])
		self.assertEqual(dropped, [BLOCKED_AUDIT])

	def test_a_survivor_is_not_reported_as_will_queue(self):
		with sent_to_redis() as sent, preview_sandbox() as dropped:
			frappe.enqueue(BLOCKED_AUDIT, status="Blocked")
			frappe.enqueue(PING)
		self.assertEqual(_job_names(sent), [BLOCKED_AUDIT])
		self.assertEqual(dropped, [PING])

	def test_a_failed_push_does_not_replace_the_triggers_own_error(self):
		# The trigger enqueues its audit row and then re-raises its
		# ValidationError. A Redis failure at the push must not become the
		# error the user sees instead of "blocked by ...".
		def redis_is_down(self, job, *args, **kwargs):
			raise ConnectionError("redis is down")

		with patch.object(Queue, "enqueue_job", redis_is_down), preview_log() as records:
			with preview_sandbox() as dropped:
				job = frappe.enqueue(BLOCKED_AUDIT, status="Blocked")
		self.assertEqual(type(job).__name__, "_DroppedJob")
		self.assertEqual(dropped, [])
		errors = _lines(records, logging.ERROR)
		self.assertEqual(len(errors), 1)
		self.assertIn(BLOCKED_AUDIT, errors[0])

	def test_only_a_job_frappe_itself_built_can_survive(self):
		# The method name alone is caller-supplied text. What rq will RUN must
		# be Frappe's execute_job, or anything could ride out of a dry run by
		# naming the audit method in its kwargs.
		with sent_to_redis() as sent, preview_sandbox() as dropped:
			queue = background_jobs.get_queue("default")
			queue.enqueue_call(PING, kwargs={"method": BLOCKED_AUDIT, "job_name": "smuggled"})
			queue.enqueue_call(frappe.ping, kwargs={"method": BLOCKED_AUDIT})
			# Handing rq the audit method itself is not Frappe's enqueue either.
			queue.enqueue_call(BLOCKED_AUDIT, kwargs={"job_name": "direct"})
		self.assertEqual(sent, [])
		self.assertEqual(dropped, ["smuggled", BLOCKED_AUDIT, "direct"])


class TestRequestStateAfterADryRun(_IsolatedRequestState):
	"""Frappe registers a flush callback only when it CREATES one of its
	request-local lists. The sandbox removes the dry run's callbacks, so it must
	remove the dry run's lists too, or a real write later in the same request
	appends to a list nothing will ever flush: its webhooks, realtime events
	and Jarvis LLM triggers silently never fire (approvals_api does a dry run
	and then the real dispatch in one request)."""

	def _flush_realtime(self):
		emitted = []
		with sent_to_redis(), patch("frappe.realtime.emit_via_redis", lambda *args: emitted.append(args)):
			frappe.db.after_commit.run()
		return emitted

	def test_realtime_events_of_a_real_write_after_a_dry_run_still_fire(self):
		with preview_sandbox():
			dry = _todo("jarvis-rs-dry")
		real = _todo("jarvis-rs-real")
		emitted = self._flush_realtime()
		names = [message.get("name") for event, message, room in emitted if isinstance(message, dict)]
		self.assertIn(real.name, names)
		self.assertNotIn(dry.name, names)

	def test_realtime_events_queued_before_a_dry_run_are_kept(self):
		frappe.publish_realtime("jarvis_rs_before", {"n": 1}, user="Administrator", after_commit=True)
		with preview_sandbox():
			frappe.publish_realtime("jarvis_rs_dry", {"n": 2}, user="Administrator", after_commit=True)
		frappe.publish_realtime("jarvis_rs_after", {"n": 3}, user="Administrator", after_commit=True)
		self.assertEqual(
			[event for event, message, room in self._flush_realtime()],
			["jarvis_rs_before", "jarvis_rs_after"],
		)

	def test_webhooks_of_a_real_write_after_a_dry_run_are_still_flushed(self):
		from frappe.integrations.doctype.webhook import _add_webhook_to_queue, flush_webhook_execution_queue

		hook = frappe._dict(name="jarvis-rs-webhook")
		with preview_sandbox():
			_add_webhook_to_queue(hook, frappe._dict(name="dry"))
		_add_webhook_to_queue(hook, frappe._dict(name="real"))
		self.assertIn(flush_webhook_execution_queue, frappe.db.after_commit._functions)
		self.assertEqual([entry.doc.name for entry in frappe.local._webhook_queue], ["real"])

	def test_webhooks_queued_before_a_dry_run_are_kept(self):
		from frappe.integrations.doctype.webhook import _add_webhook_to_queue

		hook = frappe._dict(name="jarvis-rs-webhook")
		_add_webhook_to_queue(hook, frappe._dict(name="before"))
		with preview_sandbox():
			_add_webhook_to_queue(hook, frappe._dict(name="dry"))
		self.assertEqual([entry.doc.name for entry in frappe.local._webhook_queue], ["before"])

	def test_llm_triggers_of_a_real_write_after_a_dry_run_are_still_flushed(self):
		from jarvis.triggers import engine

		row = {"name": "jarvis-rs-trigger"}
		with patch.object(engine, "_llm_cap_reached", return_value=False):
			with preview_sandbox():
				engine._queue_llm_action(row, frappe._dict(doctype="ToDo", name="dry"), "on_update")
			engine._queue_llm_action(row, frappe._dict(doctype="ToDo", name="real"), "on_update")
		self.assertIn(engine._flush_llm_queue, frappe.db.after_commit._functions)
		self.assertEqual([job.docname for job in frappe.local._jarvis_trigger_llm_queue], ["real"])

	@contextmanager
	def _link_counting(self):
		"""Frappe counts links for one request in ten; make it every time."""
		with (
			patch("frappe.model.utils.link_count.random", return_value=0.99),
			patch.object(frappe.local, "request", MagicMock(), create=True),
		):
			yield

	def test_link_counts_of_a_real_write_after_a_dry_run_are_still_flushed(self):
		from frappe.model.utils.link_count import flush_local_link_count, notify_link_count

		with self._link_counting():
			with preview_sandbox():
				notify_link_count("Customer", "dry")
			self.assertFalse(hasattr(frappe.local, "_link_count"))
			notify_link_count("Customer", "real")
		self.assertIn(flush_local_link_count, frappe.db.after_commit._functions)
		self.assertEqual(dict(frappe.local._link_count), {("Customer", "real"): 1})

	def test_link_counts_from_before_a_dry_run_are_kept_as_they_were(self):
		from frappe.model.utils.link_count import notify_link_count

		with self._link_counting():
			notify_link_count("Customer", "before")
			with preview_sandbox():
				notify_link_count("Customer", "before")
				notify_link_count("Customer", "dry")
			self.assertEqual(dict(frappe.local._link_count), {("Customer", "before"): 1})
			# Still the counting dict Frappe made: a later link must not KeyError.
			notify_link_count("Customer", "after")
		self.assertEqual(frappe.local._link_count[("Customer", "after")], 1)

	def test_rollback_callbacks_of_the_dry_run_run_at_exit(self):
		# They undo what a rollback cannot: File.on_rollback removes the file a
		# dry run wrote, cache clears drop what it cached.
		ran = []
		frappe.db.after_rollback.add(lambda: ran.append("the request's own"))
		with preview_sandbox():
			frappe.db.before_rollback.add(lambda: ran.append("before"))
			frappe.db.after_rollback.add(lambda: ran.append("after"))
		self.assertEqual(ran, ["before", "after"])
		# The request's own was not consumed, and still runs when its turn comes.
		frappe.db.after_rollback.run()
		self.assertEqual(ran, ["before", "after", "the request's own"])

	def test_a_failing_rollback_callback_does_not_stop_the_cleanup(self):
		ran = []
		with sent_to_redis() as sent:
			with preview_sandbox():
				frappe.db.after_rollback.add(_boom)
				frappe.db.after_rollback.add(lambda: ran.append("next"))
				_todo("jarvis-rs-cb")
			frappe.enqueue(PING)
		self.assertEqual(ran, ["next"])
		self.assertFalse(_todo_exists("jarvis-rs-cb"))
		self.assertEqual(len(sent), 1)
		self.assertEqual(len(frappe.db.after_rollback._functions), 0)


class TestExitOrder(_IsolatedRequestState):
	"""Leaving a sandbox is a fixed sequence in which no step can skip a later
	one: whatever fails, the dry run is rolled back and the request gets its
	commit, its rollback and its real queue back."""

	def assert_request_is_clean(self, real_commit, real_rollback):
		self.assertIsNone(sandbox_module._dropped_jobs())
		self.assertEqual((frappe.db.commit, frappe.db.rollback), (real_commit, real_rollback))
		with sent_to_redis() as sent:
			frappe.enqueue(PING)
		self.assertEqual(_job_names(sent), [PING])

	def test_a_logger_that_cannot_write_never_keeps_the_dry_run(self):
		# An unwritable log file used to raise out of the exit AHEAD of the
		# rollback. preview_doc, the source mapper and collect_missing swallow
		# Exception, so the request went on to commit what the dry run wrote.
		real_commit, real_rollback = frappe.db.commit, frappe.db.rollback
		with sent_to_redis() as sent, patch.object(frappe, "logger", side_effect=OSError("read-only log")):
			try:
				with preview_sandbox() as dropped:
					_todo("jarvis-exit-log")
					frappe.enqueue(PING, enqueue_after_commit=True)  # logged at exit, by the replay
					frappe.db.after_rollback.add(_boom)  # logged at exit: a failing callback
			except Exception:  # what those three callers do
				pass
		self.assertFalse(_todo_exists("jarvis-exit-log"))
		self.assertEqual(sent, [])
		self.assertEqual(dropped, [PING])
		self.assert_request_is_clean(real_commit, real_rollback)

	def test_a_logger_that_cannot_write_does_not_fail_the_hook(self):
		with sent_to_redis() as sent, patch.object(frappe, "logger", side_effect=OSError("read-only log")):
			with preview_sandbox() as dropped:
				_todo("jarvis-exit-log-hook")
				frappe.db.rollback()  # logged: a redirected rollback
				frappe.enqueue(PING)  # logged: a dropped job
				frappe.enqueue(BLOCKED_AUDIT, status="Blocked")  # logged: sent for real
		self.assertFalse(_todo_exists("jarvis-exit-log-hook"))
		self.assertEqual(_job_names(sent), [BLOCKED_AUDIT])
		self.assertEqual(dropped, [PING])
		self.assertTrue(dropped.hook_rolled_back)

	def test_commit_stays_neutralised_while_the_dry_runs_rollback_callbacks_run(self):
		# before_rollback callbacks run BEFORE the savepoint rollback: one that
		# commits (a Script Manager can register one) would make the
		# unconfirmed document real if commit had already been put back.
		real_commit, real_rollback = frappe.db.commit, frappe.db.rollback
		seen = {}

		def look(when):
			seen[when] = (frappe.db.commit == real_commit, frappe.db.rollback == real_rollback)

		with preview_sandbox():
			_todo("jarvis-exit-commit")
			frappe.db.before_rollback.add(lambda: look("before"))
			frappe.db.after_rollback.add(lambda: look("after"))
		self.assertEqual(seen, {"before": (False, False), "after": (False, False)})
		self.assertFalse(_todo_exists("jarvis-exit-commit"))
		self.assert_request_is_clean(real_commit, real_rollback)

	def _fail_one_cleanup_step(self, step):
		# The flag is what makes get_queue drop: left set, every later REAL
		# enqueue of the request would be silently discarded.
		real_commit, real_rollback = frappe.db.commit, frappe.db.rollback

		def put_back():  # so that a failure here cannot fail every test after it
			frappe.db.commit, frappe.db.rollback = real_commit, real_rollback
			setattr(frappe.local, sandbox_module._LOCAL_DROPPED_JOBS, None)

		self.addCleanup(put_back)
		doc = _todo("jarvis-exit-step-lock")
		self.addCleanup(doc.unlock)
		with patch.object(sandbox_module, step, side_effect=RuntimeError("step failed")):
			with self.assertRaises(RuntimeError):
				with preview_sandbox():
					_todo("jarvis-exit-step")
					doc.lock()
		self.assertFalse(_todo_exists("jarvis-exit-step"))
		self.assert_request_is_clean(real_commit, real_rollback)
		return doc

	def test_a_failing_state_restore_cannot_skip_the_steps_after_it(self):
		doc = self._fail_one_cleanup_step("_restore_request_state")
		self.assertFalse(doc.is_locked)  # the step after it still ran

	def test_a_failing_lock_release_cannot_skip_the_steps_after_it(self):
		self._fail_one_cleanup_step("_release_locks_taken_since")

	def test_a_failing_callback_in_a_hooks_rollback_leaves_the_rest_queued(self):
		# Frappe's run() pops one callback, runs it, pops the next: when one
		# raises, the rest are still queued. Popping them all first threw the
		# rest away.
		ran = []
		with preview_sandbox():
			frappe.db.after_rollback.add(_boom)
			frappe.db.after_rollback.add(lambda: ran.append("next"))
			with self.assertRaises(RuntimeError):
				frappe.db.rollback()
			self.assertEqual(ran, [])
			self.assertEqual(len(frappe.db.after_rollback._functions), 1)
		self.assertEqual(ran, ["next"])

	def test_a_callback_registered_by_a_callback_runs_too(self):
		ran = []
		with preview_sandbox():
			frappe.db.after_rollback.add(
				lambda: frappe.db.after_rollback.add(lambda: ran.append("registered late"))
			)
		self.assertEqual(ran, ["registered late"])
		self.assertEqual(len(frappe.db.after_rollback._functions), 0)


class TestDocumentLock(_IsolatedRequestState):
	"""Document.queue_action locks the document (a file under the site's locks
	folder) before it enqueues. Frappe releases such locks after a JOB, never
	after a web request, so a lock taken by a dry run stayed and the real
	submit on Confirm was refused as locked."""

	def test_a_lock_taken_by_the_dry_run_is_released(self):
		doc = _todo("jarvis-lock")
		self.addCleanup(doc.unlock)
		with sent_to_redis() as sent, preview_sandbox() as dropped:
			doc.queue_action("save")
			self.assertTrue(doc.is_locked)
		self.assertFalse(doc.is_locked)
		self.assertNotIn(doc, frappe.local.locked_documents)
		self.assertEqual(sent, [])
		self.assertEqual(dropped, ["frappe.model.document.execute_action"])

	def test_a_lock_the_request_already_held_is_kept(self):
		mine, dry = _todo("jarvis-lock-mine"), _todo("jarvis-lock-dry")
		self.addCleanup(mine.unlock)
		self.addCleanup(dry.unlock)
		mine.lock()
		with preview_sandbox():
			dry.lock()
		self.assertTrue(mine.is_locked)
		self.assertFalse(dry.is_locked)

	def test_a_lock_is_released_when_the_body_raises(self):
		doc = _todo("jarvis-lock-raise")
		self.addCleanup(doc.unlock)
		with self.assertRaises(ValueError):
			with preview_sandbox():
				doc.lock()
				raise ValueError("boom")
		self.assertFalse(doc.is_locked)


class TestRunPreview(_IsolatedRequestState):
	"""The level the bug occurred at: a core doctype with a doc_events hook,
	driven through the function the park path calls. No HRMS needed."""

	def test_a_hook_that_enqueues(self):
		from jarvis import api

		todo = _todo("jarvis-rp-before")
		args = {"doctype": "ToDo", "name": todo.name, "changes": {"description": "jarvis-rp-after"}}
		with sent_to_redis() as sent, doc_event("ToDo", "on_update", "_hook_enqueues"):
			out = api._pending_preview("update_doc", args)
		self.assertEqual(sent, [])
		self.assertTrue(out["preview"])
		self.assertEqual(out["will_queue"], [PING, "frappe.utils.now"])
		self.assertIn("2 background jobs,", out["will_queue_note"])
		self.assertNotIn("hook_rolled_back", out)
		# The card shows ``note`` and nothing else of this: it has to stand alone.
		self.assertIn("Confirming will also start 2 background jobs", out["note"])
		self.assertNotIn("will_queue", out["note"])
		self.assertNotIn("background jobs were not started", out["note"])
		# A blocked save raises, so no note is ever built for it: the audit
		# record is not the card's business.
		self.assertNotIn("audit record", out["note"])
		self.assertIn("the change was checked", out["note"])
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "description"), "jarvis-rp-before")

	def test_a_hook_that_rolls_back_writes_and_commits(self):
		from jarvis import api

		todo = _todo("jarvis-rp-keep")
		args = {"doctype": "ToDo", "name": todo.name, "changes": {"description": "jarvis-rp-changed"}}
		with sent_to_redis() as sent, doc_event("ToDo", "on_update", "_hook_rolls_back_writes_and_commits"):
			out = api._pending_preview("update_doc", args)
		self.assertEqual(sent, [])
		self.assertTrue(out["preview"])
		# The row the request inserted before the dry run is still there, untouched.
		self.assertEqual(
			frappe.db.get_value("ToDo", todo.name, ["description", "status"]), ("jarvis-rp-keep", "Open")
		)
		# Frappe 16 runs doc_events hooks with transaction control disabled, so
		# the hook's rollback is a no-op there and there is nothing to flag.
		redirected = not hasattr(frappe.db, "_disable_transaction_control")
		self.assertEqual(out.get("hook_rolled_back", False), redirected)
		self.assertEqual("rolled back part-way" in out["note"], redirected)

	def test_nothing_queued_means_no_will_queue(self):
		from jarvis import api

		with sent_to_redis():
			out = api._run_preview("create_doc", {"doctype": "ToDo", "values": {"description": "jarvis-nq"}})
		self.assertTrue(out["preview"])
		self.assertNotIn("will_queue", out)
		self.assertNotIn("will_queue_note", out)
		self.assertNotIn("hook_rolled_back", out)
		self.assertNotIn("Confirming will also start", out["note"])
		self.assertNotIn("will_queue", out["note"])
		self.assertFalse(_todo_exists("jarvis-nq"))

	def test_a_bare_rollback_is_flagged_on_the_preview(self):
		from jarvis import api

		def tool(tool_name, args):
			frappe.db.rollback()
			return {"name": "x"}

		with patch.object(api, "dispatch", tool):
			out = api._run_preview("update_doc", {})
		self.assertIs(out["hook_rolled_back"], True)
		self.assertIn("rolled back part-way", out["note"])
		self.assertIn("may not reflect what Confirm will do", out["note"])

	def _preview_of(self, tool):
		from jarvis import api

		with sent_to_redis() as sent, patch.object(api, "dispatch", tool):
			out = api._run_preview("update_doc", {})
		self.assertEqual(sent, [])
		return out

	def test_will_queue_is_deduplicated_capped_and_clipped(self):
		def tool(tool_name, args):
			frappe.enqueue(PING, job_name="x" * 500)  # first, so it is inside the cap
			for _ in range(3):
				frappe.enqueue(PING)
			for i in range(25):
				frappe.enqueue(PING, job_name=f"job-{i:02d}")
			return {}

		out = self._preview_of(tool)
		listed = out["will_queue"]
		self.assertEqual(listed[:3], ["x" * 140, PING, "job-00"])
		self.assertEqual(len(listed), 21)
		self.assertEqual(listed[-1], "+7 more")
		# 29 jobs were dropped, of 27 names: the count is the jobs.
		self.assertIn("29 background jobs of 27 kinds,", out["will_queue_note"])
		self.assertIn("Confirming will also start 29 background jobs", out["note"])

	def test_one_job_reads_as_one_job(self):
		out = self._preview_of(lambda tool_name, args: frappe.enqueue(PING) and {})
		self.assertEqual(out["will_queue"], [PING])
		self.assertIn("1 background job,", out["will_queue_note"])
		self.assertIn("Confirming will also start 1 background job,", out["note"])
		self.assertNotIn("job(s)", out["will_queue_note"] + out["note"])

	def test_jarviss_own_trigger_activity_job_is_not_announced(self):
		# In production a trigger's Success / Failed activity is enqueued after
		# commit on EVERY save of a triggered doctype (in tests it is written
		# inline, so only this shape shows it): bookkeeping, not work the user
		# is starting.
		def tool(tool_name, args):
			frappe.enqueue(BLOCKED_AUDIT, enqueue_after_commit=True, status="Success")
			return {}

		out = self._preview_of(tool)
		self.assertNotIn("will_queue", out)
		self.assertNotIn("will_queue_note", out)
		self.assertNotIn("Confirming will also start", out["note"])

	def test_a_queued_action_is_called_out_as_not_validated(self):
		# Document.queue_action (a Stock Reconciliation over 100 rows): the dry
		# run only QUEUED the submit, so nothing about it was checked.
		todo = _todo("jarvis-rp-queued")
		self.addCleanup(todo.unlock)

		def tool(tool_name, args):
			frappe.get_doc("ToDo", todo.name).queue_action("save")
			return {}

		out = self._preview_of(tool)
		self.assertEqual(out["will_queue"], [QUEUED_ACTION])
		self.assertIn("NOT validated", out["will_queue_note"])
		self.assertIn("has not been validated here", out["note"])
		# The submit was only queued: the note must not also call it checked.
		self.assertNotIn("checked", out["note"])
		self.assertIn("every database write was rolled back", out["note"])
		# And said only then.
		plain = self._preview_of(lambda tool_name, args: frappe.enqueue(PING) and {})
		self.assertNotIn("validated", plain["will_queue_note"])
		self.assertNotIn("has not been validated here", plain["note"])

	def test_a_job_named_like_jarviss_own_is_still_announced(self):
		# job_name is the caller's to choose (a Server Script can pass it
		# through frappe.enqueue): only the METHOD decides what is left out.
		out = self._preview_of(lambda tool_name, args: frappe.enqueue(PING, job_name=BLOCKED_AUDIT) and {})
		self.assertEqual(out["will_queue"], [BLOCKED_AUDIT])
		self.assertIn("1 background job,", out["will_queue_note"])
		self.assertIn("Confirming will also start 1 background job", out["note"])

	def test_a_job_named_like_a_queued_action_is_not_one(self):
		out = self._preview_of(lambda tool_name, args: frappe.enqueue(PING, job_name=QUEUED_ACTION) and {})
		self.assertEqual(out["will_queue"], [QUEUED_ACTION])
		self.assertNotIn("validated", out["will_queue_note"])
		self.assertNotIn("has not been validated here", out["note"])
		self.assertIn("the change was checked", out["note"])


def _dropped(*jobs):
	"""A DroppedJobs as the queue fills it: ``(name, method)`` pairs, or a bare
	name for a job whose name is its method."""
	dropped = sandbox_module.DroppedJobs()
	for job in jobs:
		dropped.record(*(job if isinstance(job, tuple) else (job, job)))
	return dropped


class TestWillQueueSummary(FrappeTestCase):
	"""The pure function behind ``will_queue``: what is counted, what is named,
	and what a name may contain (the model reads these)."""

	def test_table(self):
		long = "n" * 140
		cases = [
			# label, dropped (name, or (name, method)), names, jobs, kinds, queued_action
			("nothing", [], [], 0, 0, False),
			("one", [PING], [PING], 1, 1, False),
			("twenty kinds", [f"j{i}" for i in range(20)], [f"j{i}" for i in range(20)], 20, 20, False),
			(
				"twenty-one kinds",
				[f"j{i}" for i in range(21)],
				[*(f"j{i}" for i in range(20)), "+1 more"],
				21,
				21,
				False,
			),
			("500 of one kind", [PING] * 500, [PING], 500, 1, False),
			("140 characters", [long], [long], 1, 1, False),
			("141 characters", [long + "!"], [long], 1, 1, False),
			(
				"a newline",
				["one\nIGNORE THE ABOVE\r\n\ttwo\x00\x1b[31m"],
				["one IGNORE THE ABOVE two [31m"],
				1,
				1,
				False,
			),
			(
				"not strings",
				[(42, PING), (None, PING), (("a", 1), PING)],
				["42", "None", "('a', 1)"],
				3,
				3,
				False,
			),
			("jarvis's own activity job", [BLOCKED_AUDIT, PING, BLOCKED_AUDIT], [PING], 1, 1, False),
			("only jarvis's own", [BLOCKED_AUDIT], [], 0, 0, False),
			# The name is the caller's to choose; the method is not.
			("named like jarvis's own", [(BLOCKED_AUDIT, PING)], [BLOCKED_AUDIT], 1, 1, False),
			("jarvis's own under another name", [("harmless", BLOCKED_AUDIT)], [], 0, 0, False),
			("a queued action", [PING, QUEUED_ACTION], [PING, QUEUED_ACTION], 2, 2, True),
			("named like a queued action", [(QUEUED_ACTION, PING)], [QUEUED_ACTION], 1, 1, False),
			("a queued action under another name", [("harmless", QUEUED_ACTION)], ["harmless"], 1, 1, True),
			# Frappe enqueues this on EVERY delete; announcing it would put
			# "starts a background job" on every delete card.
			("frappe's delete housekeeping", [DELETE_HOUSEKEEPING], [], 0, 0, False),
			("housekeeping beside real work", [DELETE_HOUSEKEEPING, PING], [PING], 1, 1, False),
			("named like housekeeping", [(DELETE_HOUSEKEEPING, PING)], [DELETE_HOUSEKEEPING], 1, 1, False),
			("housekeeping under another name", [("harmless", DELETE_HOUSEKEEPING)], [], 0, 0, False),
		]
		for label, jobs_dropped, names, jobs, kinds, queued_action in cases:
			with self.subTest(label):
				summary = will_queue_summary(_dropped(*jobs_dropped))
				self.assertEqual(
					(summary.names, summary.jobs, summary.kinds, summary.queued_action),
					(names, jobs, kinds, queued_action),
				)

	def test_a_plain_list_of_names_hides_nothing(self):
		# No method on record means nothing can be vouched for: every name is
		# announced and none is taken for a queued action.
		summary = will_queue_summary([BLOCKED_AUDIT, QUEUED_ACTION])
		self.assertEqual((summary.names, summary.jobs), ([BLOCKED_AUDIT, QUEUED_ACTION], 2))
		self.assertFalse(summary.queued_action)


class TestAuditTrail(_IsolatedRequestState):
	"""The two rare, security-relevant events of a dry run must be WRITTEN on a
	production bench, where frappe.logger hands out loggers at ERROR."""

	def test_a_job_sent_for_real_is_written_at_the_production_level(self):
		with sent_to_redis(), preview_log() as records, preview_sandbox():
			frappe.enqueue(BLOCKED_AUDIT, status="Blocked")
		lines = _lines(records, logging.WARNING)
		self.assertEqual(len(lines), 1)
		self.assertIn("sent for real", lines[0])
		self.assertIn(repr(BLOCKED_AUDIT), lines[0])
		self.assertIn(f"user={frappe.session.user}", lines[0])

	def test_a_redirected_rollback_is_written_at_the_production_level(self):
		with preview_log() as records, preview_sandbox():
			frappe.db.rollback()
		lines = _lines(records, logging.WARNING)
		self.assertEqual(len(lines), 1)
		self.assertIn("redirected", lines[0])

	def test_a_dropped_job_is_an_info_line(self):
		# One line per job would be thousands for a large payroll: written
		# only when the operator lowers the level.
		with sent_to_redis(), preview_log() as records, preview_sandbox():
			frappe.enqueue(PING)
		self.assertEqual(records, [])
		with sent_to_redis(), preview_log(logging.INFO) as records, preview_sandbox():
			frappe.enqueue_doc("User", "Administrator", "validate")
		lines = _lines(records, logging.INFO)
		self.assertEqual(len(lines), 1)
		self.assertIn("not sent", lines[0])
		# The job's own target, when Frappe's envelope carries one.
		self.assertIn("doctype='User' name='Administrator'", lines[0])

	def test_a_job_name_cannot_forge_a_log_line(self):
		forged = "x\n2026-01-01 00:00:00 WARNING jarvis.preview dry run: job sent for real " + "y" * 500
		with sent_to_redis(), preview_log(logging.INFO) as records, preview_sandbox():
			frappe.enqueue(PING, job_name=forged)
		(line,) = _lines(records, logging.INFO)
		self.assertNotIn("\n", line)
		self.assertNotIn("y" * 141, line)

	def test_the_tool_being_previewed_is_named_when_the_request_carries_it(self):
		with (
			sent_to_redis(),
			preview_log(logging.INFO) as records,
			patch.object(frappe.local, "form_dict", frappe._dict(tool="submit_doc")),
			preview_sandbox(),
		):
			frappe.enqueue(PING)
		(line,) = _lines(records, logging.INFO)
		self.assertIn("tool='submit_doc'", line)


class TestPreviewSandboxBareRollback(_IsolatedRequestState):
	"""A hook's ``frappe.db.rollback()`` with no savepoint used to end the
	sandbox's transaction: the savepoint went with it, the sandbox's own
	rollback then failed silently, and every later write of the dry run was
	real (HRMS create_salary_slips_for_employees does exactly this when a slip
	fails: rollback, write ``status: Failed``, commit)."""

	def test_bare_rollback_inside_the_sandbox_does_not_release_it(self):
		with preview_sandbox() as dropped:
			_todo("jarvis-br-first")
			self.assertFalse(dropped.hook_rolled_back)
			frappe.db.rollback()
			self.assertTrue(dropped.hook_rolled_back)
			self.assertFalse(_todo_exists("jarvis-br-first"))
			_todo("jarvis-br-second")
			frappe.db.commit()
		self.assertFalse(_todo_exists("jarvis-br-first"))
		self.assertFalse(_todo_exists("jarvis-br-second"))

	def test_bare_rollback_keeps_what_the_request_wrote_before_the_sandbox(self):
		_todo("jarvis-br-earlier")
		with preview_sandbox():
			frappe.db.rollback()
		self.assertTrue(_todo_exists("jarvis-br-earlier"))

	def test_rollback_to_a_named_savepoint_is_untouched(self):
		with preview_sandbox() as dropped:
			_todo("jarvis-br-kept")
			frappe.db.savepoint("jarvis_br_inner")
			_todo("jarvis-br-undone")
			frappe.db.rollback(save_point="jarvis_br_inner")
			self.assertTrue(_todo_exists("jarvis-br-kept"))
			self.assertFalse(_todo_exists("jarvis-br-undone"))
			self.assertFalse(dropped.hook_rolled_back)
		self.assertFalse(_todo_exists("jarvis-br-kept"))

	def test_real_rollback_is_back_after_the_sandbox(self):
		real = frappe.db.rollback
		with preview_sandbox():
			self.assertNotEqual(frappe.db.rollback, real)
		self.assertEqual(frappe.db.rollback, real)
		with self.assertRaises(ValueError):
			with preview_sandbox():
				raise ValueError("boom")
		self.assertEqual(frappe.db.rollback, real)
		_todo("jarvis-br-after")
		frappe.db.rollback()
		self.assertFalse(_todo_exists("jarvis-br-after"))

	def test_bare_rollback_in_a_nested_sandbox_stays_in_the_inner_one(self):
		with preview_sandbox():
			_todo("jarvis-br-outer")
			with preview_sandbox():
				_todo("jarvis-br-inner")
				frappe.db.rollback()
				self.assertFalse(_todo_exists("jarvis-br-inner"))
				self.assertTrue(_todo_exists("jarvis-br-outer"))
			self.assertTrue(_todo_exists("jarvis-br-outer"))
		self.assertFalse(_todo_exists("jarvis-br-outer"))

	def test_bare_rollback_does_not_forget_a_job_already_sent(self):
		# A real rollback does not pull a job back out of Redis, so the job
		# the real run WILL queue stays in the report.
		with sent_to_redis() as sent, preview_sandbox() as dropped:
			frappe.enqueue(PING)
			frappe.db.rollback()
		self.assertEqual(sent, [])
		self.assertEqual(dropped, [PING])

	def test_bare_rollback_forgets_a_job_waiting_for_the_commit(self):
		# Frappe's rollback empties after_commit: that job would never be sent.
		with sent_to_redis() as sent, preview_sandbox() as dropped:
			frappe.enqueue(PING, enqueue_after_commit=True)
			frappe.db.rollback()
		self.assertEqual(sent, [])
		self.assertEqual(dropped, [])

	def test_bare_rollback_drops_commit_callbacks_queued_by_the_dry_run(self):
		ran = []
		frappe.db.after_commit.add(lambda: ran.append("earlier"))
		with preview_sandbox():
			frappe.db.after_commit.add(lambda: ran.append("dry run"))
			frappe.db.rollback()
			# Frappe's rollback empties the queue; here only the dry run's
			# share goes, the request's own callback is still owed.
			self.assertEqual(len(frappe.db.after_commit._functions), 1)
		# And it is still RUN by the request's commit, not merely kept.
		frappe.db.after_commit.run()
		self.assertEqual(ran, ["earlier"])

	def test_bare_rollback_runs_only_the_dry_runs_rollback_callbacks(self):
		ran = []
		frappe.db.before_rollback.add(lambda: ran.append("before: earlier"))
		frappe.db.after_rollback.add(lambda: ran.append("after: earlier"))
		with preview_sandbox():
			frappe.db.before_rollback.add(lambda: ran.append("before: dry run"))
			frappe.db.after_rollback.add(lambda: ran.append("after: dry run"))
			frappe.db.rollback()
			self.assertEqual(ran, ["before: dry run", "after: dry run"])
			# Run once, like Frappe's own: a second rollback has nothing left.
			frappe.db.rollback()
		self.assertEqual(ran, ["before: dry run", "after: dry run"])
		# The request's own callbacks were not consumed: they belong to the
		# request's transaction, and run when IT is rolled back.
		frappe.db.before_rollback.run()
		frappe.db.after_rollback.run()
		self.assertEqual(ran, ["before: dry run", "after: dry run", "before: earlier", "after: earlier"])

	def test_bare_rollback_is_still_a_no_op_where_frappe_disables_it(self):
		# Frappe 16 runs doc_events hooks with transaction control disabled:
		# commit and rollback there warn and do nothing. The sandbox must not
		# turn that no-op into a rollback.
		if not hasattr(frappe.db, "_disable_transaction_control"):
			self.skipTest("this Frappe has no transaction-control switch (version-15)")
		with preview_sandbox() as dropped:
			_todo("jarvis-br-noop")
			frappe.db._disable_transaction_control += 1
			try:
				with self.assertWarns(Warning):
					frappe.db.rollback()
			finally:
				frappe.db._disable_transaction_control -= 1
			self.assertTrue(_todo_exists("jarvis-br-noop"))
			self.assertFalse(dropped.hook_rolled_back)
		self.assertFalse(_todo_exists("jarvis-br-noop"))


class _LostSavepoint(_IsolatedRequestState):
	"""Losing the savepoint writes an Error Log row. That table is MyISAM, so a
	real row would outlive the test: the call is recorded instead."""

	def setUp(self):
		super().setUp()
		patcher = patch.object(frappe, "log_error")
		self.log_error = patcher.start()
		self.addCleanup(patcher.stop)

	def lost_reports(self):
		return [call for call in self.log_error.call_args_list if call.kwargs.get("title") == LOST_TITLE]


class TestSavepointLost(_LostSavepoint):
	"""The sandbox's savepoint can be lost under it: a deadlock aborts the
	transaction, DDL commits implicitly. From then on the dry run writes for
	real, so the sandbox fails closed."""

	def test_a_hooks_bare_rollback_raises_when_the_savepoint_is_gone(self):
		with self.assertRaises(PreviewSandboxLost):
			with preview_sandbox():
				_lose_the_savepoint()
				with self.assertRaises(Exception) as caught:
					frappe.db.rollback()
				self.assertEqual(caught.exception.args[0], 1305)

	def test_a_clean_exit_without_the_savepoint_rolls_back_and_raises(self):
		real_commit, real_rollback = frappe.db.commit, frappe.db.rollback
		with preview_log() as records:
			with self.assertRaises(PreviewSandboxLost) as caught:
				with preview_sandbox():
					_lose_the_savepoint()
					_todo("jarvis-lost")  # a real write: nothing is left to roll it back to
		self.assertFalse(_todo_exists("jarvis-lost"))
		self.assertEqual((frappe.db.commit, frappe.db.rollback), (real_commit, real_rollback))
		self.assertIsNone(sandbox_module._dropped_jobs())
		# One Error Log row under a title an operator can search for, and one
		# line in the log file.
		self.assertEqual(len(self.lost_reports()), 1)
		self.assertEqual(len(_lines(records, logging.ERROR)), 1)
		# The words the model relays: what happened, what may be left, what to do.
		message = str(caught.exception)
		for phrase in ("no confirmation card was created", "may already be saved", "at most once"):
			self.assertIn(phrase, message)

	def test_it_is_a_jarvis_error_so_no_caller_turns_it_into_a_500(self):
		self.assertTrue(issubclass(PreviewSandboxLost, JarvisError))
		self.assertIs(sandbox_module.PreviewSandboxLost, PreviewSandboxLost)

	def test_commit_is_still_neutralised_during_the_full_rollback(self):
		# The request's own before_rollback callbacks run inside that full
		# rollback, before the SQL: one that commits would keep what the dry
		# run wrote after the savepoint went.
		real_commit = frappe.db.commit
		seen = []
		frappe.db.before_rollback.add(lambda: seen.append(frappe.db.commit == real_commit))
		with self.assertRaises(PreviewSandboxLost):
			with preview_sandbox():
				_lose_the_savepoint()
		self.assertEqual(seen, [False])
		self.assertEqual(frappe.db.commit, real_commit)

	def test_the_bodys_own_error_wins_and_the_writes_are_rolled_back(self):
		with self.assertRaises(ValueError):
			with preview_sandbox():
				_lose_the_savepoint()
				_todo("jarvis-lost-raise")
				raise ValueError("boom")
		self.assertFalse(_todo_exists("jarvis-lost-raise"))
		# The caller sees the body's error; the operator still gets the row.
		self.assertEqual(len(self.lost_reports()), 1)

	def test_a_loss_in_a_nested_sandbox_is_reported_once(self):
		with self.assertRaises(PreviewSandboxLost):
			with preview_sandbox():
				with preview_sandbox():
					_lose_the_savepoint()
		self.assertEqual(len(self.lost_reports()), 1)

	def _statements(self):
		"""Record every statement sent through frappe.db.sql from here on."""
		sent, real_sql = [], frappe.db.sql

		def sql(query, *args, **kwargs):
			sent.append(str(query).strip().lower())
			return real_sql(query, *args, **kwargs)

		patcher = patch.object(frappe.db, "sql", sql)
		patcher.start()
		self.addCleanup(patcher.stop)
		return sent

	def test_a_raising_callback_cannot_stop_the_full_rollback(self):
		# Frappe's rollback() runs before_rollback callbacks BEFORE it sends the
		# SQL: one of the request's own that raises means no ROLLBACK at all,
		# and what the dry run wrote after the savepoint went would be kept.
		frappe.db.before_rollback.add(_boom)
		with self.assertRaises(PreviewSandboxLost):
			with preview_sandbox():
				_lose_the_savepoint()
				_todo("jarvis-lost-callback")
				sent = self._statements()
		self.assertIn("rollback", sent)
		self.assertFalse(_todo_exists("jarvis-lost-callback"))
		self.assertEqual(len(self.lost_reports()), 1)

	def test_disabled_transaction_control_cannot_stop_the_full_rollback(self):
		# Frappe 16: rollback() only warns while transaction control is
		# disabled (it runs doc_events hooks that way).
		if not hasattr(frappe.db, "_disable_transaction_control"):
			self.skipTest("this Frappe has no transaction-control switch (version-15)")
		before = frappe.db._disable_transaction_control

		def put_back():
			frappe.db._disable_transaction_control = before

		self.addCleanup(put_back)
		with self.assertRaises(PreviewSandboxLost):
			with preview_sandbox():
				_lose_the_savepoint()
				_todo("jarvis-lost-disabled")
				frappe.db._disable_transaction_control = before + 1
				sent = self._statements()
		put_back()
		self.assertIn("rollback", sent)
		self.assertFalse(_todo_exists("jarvis-lost-disabled"))

	def test_a_savepoint_that_was_never_set_costs_the_request_nothing(self):
		real_commit, real_rollback = frappe.db.commit, frappe.db.rollback
		_todo("jarvis-lost-earlier")
		with sent_to_redis() as sent:
			with patch.object(frappe.db, "savepoint", side_effect=RuntimeError("no savepoint")):
				with self.assertRaises(RuntimeError):
					with preview_sandbox():
						pass
			self.assertTrue(_todo_exists("jarvis-lost-earlier"))
			# Nothing of the sandbox is left on the request: not the flag that
			# makes get_queue drop, not the neutralised commit or rollback.
			self.assertIsNone(sandbox_module._dropped_jobs())
			self.assertEqual((frappe.db.commit, frappe.db.rollback), (real_commit, real_rollback))
			frappe.enqueue(PING)
		self.assertEqual(_job_names(sent), [PING])
		self.assertEqual(self.lost_reports(), [])


class TestALostSandboxIsARefusal(_LostSavepoint):
	"""PreviewSandboxLost used to be a RuntimeError outside every caller's
	except tuple: a raw HTTP 500 to the model. Every path now ends in the
	refusal envelope, and none of them parks a card or reports the document as
	merely invalid."""

	def _tool_that_loses_the_savepoint(self, tool_name, args):
		_lose_the_savepoint()
		return {"name": "x"}

	def assert_refused(self, result):
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "ConfirmationUnavailableError")
		for phrase in ("no confirmation card was created", "may already be saved", "at most once"):
			self.assertIn(phrase, result["error"]["message"])
		self.assertEqual(len(self.lost_reports()), 1)

	def test_a_dry_run_on_park_tool_is_refused_and_not_parked(self):
		from jarvis import api

		with (
			patch.object(api, "dispatch", self._tool_that_loses_the_savepoint),
			patch("jarvis.chat.pending_confirm.mint") as mint,
		):
			result = api._run_tool("create_doc", {"doctype": "ToDo", "values": {"description": "x"}})
		self.assert_refused(result)
		mint.assert_not_called()

	def test_a_parked_submit_does_not_dispatch_at_preview(self):
		# #3: submit_doc is _NO_SANDBOX_PREVIEW - parking it builds a described-
		# intent card WITHOUT dispatching the tool, so the dry run (and any inline
		# on_submit side effect it would fire) never happens before the user
		# confirms. The savepoint-losing dispatch is therefore never reached; the
		# card parks normally.
		from jarvis import api

		# side_effect (not a bare replacement) keeps dispatch a MagicMock, so the
		# call can be asserted on; if it WERE reached it would still lose the savepoint.
		with (
			patch.object(api, "dispatch", side_effect=self._tool_that_loses_the_savepoint) as dispatch,
			patch("jarvis.chat.pending_confirm.mint") as mint,
		):
			result = api._run_tool("submit_doc", {"doctype": "ToDo", "name": "x"})
		dispatch.assert_not_called()
		mint.assert_called_once()
		self.assertTrue(result["ok"])

	def test_pending_preview_describes_submit_without_dispatching(self):
		# submit_doc now takes a described-intent preview: _pending_preview returns
		# the described dict and never calls dispatch, so a savepoint-losing dry run
		# cannot occur.
		from jarvis import api

		with patch.object(api, "dispatch", side_effect=self._tool_that_loses_the_savepoint) as dispatch:
			out = api._pending_preview("submit_doc", {"doctype": "ToDo", "name": "x"})
		dispatch.assert_not_called()
		self.assertTrue(out["described"])
		self.assertFalse(out["preview"])

	def test_destructive_single_doc_preview_never_dispatches(self):
		# #3 regression: a single-doc submit/cancel/delete/amend preview must be
		# described-intent (no sandbox dispatch), because their hooks fire inline,
		# non-rollback-able side effects (tax-portal HTTP, files removed from disk).
		# create_doc stays a sandboxed dry-run (pure DB, rolled back) as the control.
		from jarvis import api

		for tool in ("submit_doc", "cancel_doc", "delete_doc", "amend_doc"):
			with patch.object(api, "dispatch") as dispatch:
				out = api._pending_preview(tool, {"doctype": "ToDo", "name": "x"})
			dispatch.assert_not_called()
			self.assertTrue(out["described"], tool)
			self.assertFalse(out["preview"], tool)

		with patch.object(api, "dispatch", return_value={"name": "x"}) as dispatch:
			out = api._pending_preview("create_doc", {"doctype": "ToDo", "values": {"description": "x"}})
		dispatch.assert_called_once()
		self.assertTrue(out["preview"])

	def test_preview_doc_does_not_call_the_document_invalid(self):
		from jarvis.tools.preview_doc import preview_doc

		def insert(doc, *args, **kwargs):
			_lose_the_savepoint()

		with patch("frappe.model.document.Document.insert", insert):
			with self.assertRaises(PreviewSandboxLost):
				preview_doc("ToDo", {"description": "jarvis-lost-preview-doc"})

	def test_preview_doc_as_a_tool_is_a_clean_refusal(self):
		from jarvis import api

		def insert(doc, *args, **kwargs):
			_lose_the_savepoint()

		with patch("frappe.model.document.Document.insert", insert):
			result = api._run_tool(
				"preview_doc", {"doctype": "ToDo", "values": {"description": "jarvis-lost-tool"}}
			)
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "PreviewSandboxLost")
		self.assertIn("may already be saved", result["error"]["message"])

	def test_the_source_mapper_does_not_report_an_empty_mapping(self):
		from jarvis.tools import _source_mapper

		def mapper(source_name):
			_lose_the_savepoint()

		with (
			patch.object(_source_mapper, "resolve_mapper", return_value="x.y.z"),
			patch.object(frappe, "get_attr", return_value=mapper),
			patch.object(frappe, "has_permission", return_value=True),
		):
			with self.assertRaises(PreviewSandboxLost):
				_source_mapper.mapped_values("ToDo", "x", "ToDo")

	def test_the_approval_board_edit_gets_a_refusal_it_can_show(self):
		from jarvis import api
		from jarvis.chat import approvals_api, held_writes

		items = [{"op": "create", "doctype": "ToDo", "values": {"description": "x"}}]
		with (
			patch.object(api, "dispatch", self._tool_that_loses_the_savepoint),
			patch.object(held_writes, "collect_missing") as collect_missing,
		):
			result = approvals_api._edit_dry_run("create_doc", items[0], items)
		self.assertFalse(result["ok"])
		self.assertEqual(result["reason_code"], "invalid")
		self.assertIn("may already be saved", result["error"]["message"])
		# Not treated as a validation error: no second dry run to look for
		# missing fields.
		collect_missing.assert_not_called()
		self.assertEqual(len(self.lost_reports()), 1)


class TestALostSandboxInAFileBoxRun(_FileBoxBase):
	"""held_writes.apply answers a lost sandbox with its own retryable refusal
	(it rolls back and logs whatever went wrong in a hold). It must stay that:
	as a JarvisError, PreviewSandboxLost would otherwise be read by _hold as a
	validation error and sent looking for missing fields."""

	def test_the_hold_is_refused_as_unavailable(self):
		from jarvis.chat import held_writes

		conv = self.conv()
		supplier = _supplier("zz-fbh Lost Sandbox")
		with (
			patch("jarvis.api._run_preview", side_effect=PreviewSandboxLost("the savepoint was lost")),
			patch.object(held_writes, "collect_missing") as collect_missing,
			patch.object(frappe, "log_error") as log_error,
		):
			result = self.call("create_doc", supplier, conv)
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "ConfirmationUnavailableError")
		collect_missing.assert_not_called()
		self.assertEqual(self.rows(), [])
		titles = [call.kwargs.get("title") for call in log_error.call_args_list]
		self.assertIn("jarvis.file_box.hold_failed", titles)
