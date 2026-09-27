"""Tests for jarvis.chat.llm_switch (jarvis#1425 follow-up, Task 4).

Holds a proxy<->direct switch until no reply is in flight (or a 5-minute cap),
tells every open chat, then applies. Covers: begin/try_apply/end lifecycle,
idempotent retarget of an already-applied switch, the cap, the reconcile poller
hook, the maintenance-hold surfacing (boot_payload + persist_from_connection
still reading only the operator mirror), the promote_next hold, and the
settle_turn / settle_conversation_dispatching triggers.

``jarvis.chat.llm_switch._inflight`` (not ``admission._shard_inflight``) is the
patch seam here: admission's own 180s freshness window under-counts a legacy
streaming reply mid a long tool call (traced against turn_handler's assistant
batcher - see llm_switch.py's module comment), so llm_switch widens that leg to
900s via its own ``_inflight()`` wrapper instead of reusing ``_shard_inflight``
directly. See task-4-report.md for the trace.

``frappe.flags.in_test`` is on for the whole suite (FrappeTestCase), so
``begin()``/``_enqueue`` always take the inline branch - no ``frappe.db.commit()``
is needed to observe their effects, mirroring how the existing
``_enqueue_pool_sync`` / ``_enqueue_handover`` tests rely on the same flag.
"""

import time
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import maintenance_notice
from jarvis.chat import admission, llm_switch


class _LlmSwitchTestCase(FrappeTestCase):
	def setUp(self):
		frappe.cache().delete_value(llm_switch.KEY)
		maintenance_notice.persist({"active": False})
		self._orig_status = frappe.db.get_value("Jarvis Settings", "Jarvis Settings", "last_sync_status")

	def tearDown(self):
		frappe.cache().delete_value(llm_switch.KEY)
		maintenance_notice.persist({"active": False})
		frappe.db.set_single_value(
			"Jarvis Settings", {"last_sync_status": self._orig_status or ""}, update_modified=False
		)


class TestBeginAndTryApply(_LlmSwitchTestCase):
	def test_begin_stores_record_broadcasts_and_applies_when_idle(self):
		with (
			patch.object(llm_switch, "_inflight", return_value=0),
			patch("frappe.enqueue") as mock_enqueue,
			patch("frappe.publish_realtime") as mock_pub,
		):
			llm_switch.begin("jarvis.tests.fake_job", foo="bar")

		mock_pub.assert_called_once_with(llm_switch.EVENT, {"state": "switching"})
		mock_enqueue.assert_called_once()
		args, kwargs = mock_enqueue.call_args
		self.assertEqual(args[0], "jarvis.tests.fake_job")
		self.assertEqual(kwargs["foo"], "bar")
		self.assertEqual(kwargs["queue"], "long")
		self.assertTrue(kwargs["now"])
		self.assertFalse(kwargs["enqueue_after_commit"])
		rec = llm_switch.status()
		self.assertTrue(rec["applied"])

	def test_begin_waits_while_replies_in_flight(self):
		with (
			patch.object(llm_switch, "_inflight", return_value=2),
			patch("frappe.enqueue") as mock_enqueue,
			patch("frappe.publish_realtime"),
		):
			llm_switch.begin("jarvis.tests.fake_job")
			mock_enqueue.assert_not_called()
			self.assertFalse(llm_switch.status()["applied"])

			with patch.object(llm_switch, "_inflight", return_value=0):
				applied = llm_switch.try_apply()

			self.assertTrue(applied)
			mock_enqueue.assert_called_once()
			self.assertTrue(llm_switch.status()["applied"])

	def test_cap_applies_after_deadline(self):
		with (
			patch.object(llm_switch, "_inflight", return_value=1),
			patch("frappe.enqueue") as mock_enqueue,
			patch("frappe.publish_realtime"),
		):
			llm_switch.begin("jarvis.tests.fake_job")
			mock_enqueue.assert_not_called()

			rec = llm_switch.status()
			rec["deadline"] = time.time() - 1
			frappe.cache().set_value(llm_switch.KEY, rec, expires_in_sec=llm_switch.TTL_S)

			# inflight is STILL 1 (nobody drained) - only the past deadline should
			# force the apply through.
			applied = llm_switch.try_apply()

		self.assertTrue(applied)
		mock_enqueue.assert_called_once()

	def test_try_apply_runs_once(self):
		with (
			patch.object(llm_switch, "_inflight", return_value=0),
			patch("frappe.enqueue") as mock_enqueue,
			patch("frappe.publish_realtime"),
		):
			llm_switch.begin("jarvis.tests.fake_job")
			mock_enqueue.assert_called_once()

			second = llm_switch.try_apply()

		self.assertFalse(second)
		mock_enqueue.assert_called_once()  # still just the one from begin()

	def test_begin_while_applied_parks_next_and_does_not_enqueue(self):
		"""2026 review fix wave ("retarget race", Important): a retarget that
		arrives while the current run is already APPLIED (its job is running
		RIGHT NOW) must NOT force-enqueue a second job for the same container -
		it parks the newer config in rec["next"] instead. No second banner
		either - the switch is already showing one."""
		with (
			patch.object(llm_switch, "_inflight", return_value=0),
			patch("frappe.enqueue") as mock_enqueue,
			patch("frappe.publish_realtime") as mock_pub,
		):
			llm_switch.begin("jarvis.tests.job_a")
			mock_pub.assert_called_once()
			mock_enqueue.assert_called_once()
			mock_pub.reset_mock()
			mock_enqueue.reset_mock()

			# The retargeting caller's own pending stamp, exactly as any real
			# enqueue path writes before reaching begin() - see
			# TestHandoffPreservesPendingStatus for dedicated coverage of this
			# field; here it just has to be captured into "next" too.
			frappe.db.set_single_value(
				"Jarvis Settings",
				{"last_sync_status": "pending: provisioning container (pool)"},
				update_modified=False,
			)
			with patch.object(llm_switch, "_inflight", return_value=5):
				llm_switch.begin("jarvis.tests.job_b", other="kw")

		mock_pub.assert_not_called()  # no second "switching" banner
		mock_enqueue.assert_not_called()  # no second, uncoordinated enqueue
		rec = llm_switch.status()
		self.assertTrue(rec["applied"])  # job_a's run is untouched
		self.assertEqual(rec["job"], "jarvis.tests.job_a")
		self.assertEqual(
			rec["next"],
			{
				"job": "jarvis.tests.job_b",
				"job_kwargs": {"other": "kw"},
				"pending_status": "pending: provisioning container (pool)",
			},
		)

	def test_finish_releases_a_parked_next_as_a_fresh_run(self):
		"""The running job's own finish() call is what actually hands off a
		parked `next` - a fresh run_id, applied stays True, enqueued - with NO
		second "switching" broadcast (the switch never stopped being active
		from the caller's point of view)."""
		with (
			patch.object(llm_switch, "_inflight", return_value=0),
			patch("frappe.enqueue"),
			patch("frappe.publish_realtime"),
		):
			llm_switch.begin("jarvis.tests.job_a")
		run_id_a = llm_switch.status()["run_id"]

		with patch.object(llm_switch, "_inflight", return_value=5):
			llm_switch.begin("jarvis.tests.job_b", other="kw")

		with (
			patch("frappe.enqueue") as mock_enqueue,
			patch("frappe.publish_realtime") as mock_pub,
		):
			llm_switch.finish(run_id_a, "ok: moved")

		mock_pub.assert_not_called()  # hand-off, not end - no "done" broadcast
		mock_enqueue.assert_called_once()
		args, kwargs = mock_enqueue.call_args
		self.assertEqual(args[0], "jarvis.tests.job_b")
		self.assertEqual(kwargs["other"], "kw")
		rec = llm_switch.status()
		self.assertTrue(rec["applied"])
		self.assertEqual(rec["job"], "jarvis.tests.job_b")
		self.assertIsNone(rec["next"])
		self.assertNotEqual(rec["run_id"], run_id_a)  # a FRESH run

	def test_finish_with_no_next_ends_the_switch(self):
		with (
			patch.object(llm_switch, "_inflight", return_value=0),
			patch("frappe.enqueue"),
			patch("frappe.publish_realtime"),
		):
			llm_switch.begin("jarvis.tests.job_a")
		run_id = llm_switch.status()["run_id"]

		with (
			patch("frappe.publish_realtime") as mock_pub,
			patch.object(admission, "promote_next") as mock_promote,
		):
			llm_switch.finish(run_id, "ok: moved")

		self.assertIsNone(llm_switch.status())
		mock_pub.assert_called_once_with(llm_switch.EVENT, {"state": "done", "outcome": "ok: moved"})
		mock_promote.assert_called_once()

	def test_finish_with_a_stale_run_id_does_nothing(self):
		with (
			patch.object(llm_switch, "_inflight", return_value=0),
			patch("frappe.enqueue"),
			patch("frappe.publish_realtime"),
		):
			llm_switch.begin("jarvis.tests.job_a")

		with (
			patch("frappe.publish_realtime") as mock_pub,
			patch.object(admission, "promote_next") as mock_promote,
		):
			llm_switch.finish("some-other-run-id", "ok: moved")

		self.assertIsNotNone(llm_switch.status())  # untouched
		mock_pub.assert_not_called()
		mock_promote.assert_not_called()

	def test_finish_with_none_run_id_does_nothing(self):
		with patch("frappe.publish_realtime") as mock_pub:
			llm_switch.finish(None, "ok: moved")

		mock_pub.assert_not_called()

	def test_released_job_id_carries_the_run_suffix(self):
		"""rq dedup treats a STARTED job like a QUEUED one, so reusing a
		job_id across two releases could silently drop the newer one - the
		run_id suffix makes every release's job_id unique."""
		with (
			patch.object(llm_switch, "_inflight", return_value=0),
			patch("frappe.enqueue") as mock_enqueue,
			patch("frappe.publish_realtime"),
		):
			llm_switch.begin("jarvis.tests.fake_job", job_id="jarvis_settings_sync:pool", deduplicate=True)

		run_id = llm_switch.status()["run_id"]
		self.assertIsNotNone(run_id)
		kwargs = mock_enqueue.call_args.kwargs
		self.assertEqual(kwargs["job_id"], f"jarvis_settings_sync:pool:{run_id[:8]}")
		self.assertEqual(kwargs["llm_switch_run_id"], run_id)

	def test_begin_under_lock_contention_enqueues_directly(self):
		"""CR-1: if the 2s wait for the jarvis_llm_switch lock expires, the
		job must not be dropped - it runs untracked by the switch, exactly as
		a non-switch apply would (its own plain job_id, no run token, no
		switch record ever created)."""
		fake_lock = MagicMock()
		fake_lock.__enter__ = MagicMock(return_value=False)
		fake_lock.__exit__ = MagicMock(return_value=False)
		with (
			patch("jarvis._redis_lock.redis_lock", return_value=fake_lock),
			patch("frappe.enqueue") as mock_enqueue,
			patch("frappe.log_error") as mock_log,
		):
			llm_switch.begin("jarvis.tests.fake_job", job_id="jarvis_settings_sync:pool", deduplicate=True)

		mock_log.assert_called_once()
		mock_enqueue.assert_called_once()
		args, kwargs = mock_enqueue.call_args
		self.assertEqual(args[0], "jarvis.tests.fake_job")
		self.assertEqual(kwargs["job_id"], "jarvis_settings_sync:pool")  # unsuffixed
		self.assertNotIn("llm_switch_run_id", kwargs)
		self.assertIsNone(llm_switch.status())  # no record was ever created


class TestHandoffPreservesPendingStatus(_LlmSwitchTestCase):
	"""2026 review, second pass (Critical): releasing a parked ``next`` must
	re-stamp last_sync_status back to a pending string. The run being handed
	off just wrote its OWN terminal status ("ok ..."/"failed: ...") over
	last_sync_status before calling finish() - without the re-stamp, a
	poller's reconcile() running right after the hand-off (every held-chat
	poll runs one via maintenance_notice.check()) would read that STALE
	terminal status, match it against the freshly-minted run_id finish() just
	stored, and wrongly end the switch while the handed-off job is still
	queued or running."""

	def test_a_hand_off_re_stamps_pending_so_reconcile_does_not_end_it_early(self):
		# A begins and applies (job A running) - its own caller stamped a
		# pending status first, exactly as every real enqueue path does.
		frappe.db.set_single_value(
			"Jarvis Settings",
			{"last_sync_status": "pending: provisioning container (pool)"},
			update_modified=False,
		)
		with (
			patch.object(llm_switch, "_inflight", return_value=0),
			patch("frappe.enqueue"),
			patch("frappe.publish_realtime"),
		):
			llm_switch.begin("jarvis.tests.job_a", job_id="jarvis_settings_sync:pool", deduplicate=True)
		run_id_a = llm_switch.status()["run_id"]

		# B retargets while A is still running - a fresh save stamped its OWN
		# pending status before this begin() call, same as any real caller.
		frappe.db.set_single_value(
			"Jarvis Settings",
			{"last_sync_status": "pending: provisioning container (pool)"},
			update_modified=False,
		)
		with patch.object(llm_switch, "_inflight", return_value=5):
			llm_switch.begin(
				"jarvis.tests.job_b", other="kw", job_id="jarvis_settings_sync:pool", deduplicate=True
			)
		self.assertEqual(
			llm_switch.status()["next"]["pending_status"], "pending: provisioning container (pool)"
		)

		# A's worker finishes and writes ITS OWN terminal status over
		# last_sync_status, then calls finish() - this is the hand-off.
		frappe.db.set_single_value(
			"Jarvis Settings", {"last_sync_status": "ok (pool_update via admin)"}, update_modified=False
		)
		with (
			patch("frappe.enqueue") as mock_enqueue,
			patch("frappe.publish_realtime") as mock_pub,
		):
			llm_switch.finish(run_id_a, "ok (pool_update via admin)")

		mock_pub.assert_not_called()  # hand-off, not end - no "done" broadcast
		mock_enqueue.assert_called_once()
		rec = llm_switch.status()
		self.assertIsNotNone(rec)
		run_id_b = rec["run_id"]
		self.assertNotEqual(run_id_b, run_id_a)
		self.assertEqual(rec["job"], "jarvis.tests.job_b")
		current_status = frappe.db.get_value("Jarvis Settings", "Jarvis Settings", "last_sync_status")
		self.assertTrue(current_status.startswith("pending"), current_status)

		# A reconcile() poll right after the hand-off must NOT end the switch -
		# last_sync_status correctly reads pending again, not A's stale "ok".
		with (
			patch("frappe.publish_realtime") as mock_pub2,
			patch.object(admission, "promote_next") as mock_promote2,
		):
			llm_switch.reconcile()
		mock_pub2.assert_not_called()
		mock_promote2.assert_not_called()
		self.assertIsNotNone(llm_switch.status())

		# B's own terminal write + finish(run_id_b) ends the switch for real.
		frappe.db.set_single_value(
			"Jarvis Settings", {"last_sync_status": "ok (pool_update via admin)"}, update_modified=False
		)
		with (
			patch("frappe.publish_realtime") as mock_pub3,
			patch.object(admission, "promote_next") as mock_promote3,
		):
			llm_switch.finish(run_id_b, "ok (pool_update via admin)")
		mock_pub3.assert_called_once()
		mock_promote3.assert_called_once()
		self.assertIsNone(llm_switch.status())

	def test_a_parked_handover_preserves_its_attempt_string(self):
		"""The fallback ("pending: applying") must never actually be needed
		for a real handover park - the attempt suffix ("pending: handover to
		direct (attempt N)") the handover's own enqueue path stamps must
		survive the park/release round trip untouched, since
		_handover_attempt() parses it later to bound retries."""
		from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import _PENDING_HANDOVER_STATUS

		attempt_status = f"{_PENDING_HANDOVER_STATUS} (attempt 2)"
		frappe.db.set_single_value(
			"Jarvis Settings",
			{"last_sync_status": "pending: provisioning container (pool)"},
			update_modified=False,
		)
		with (
			patch.object(llm_switch, "_inflight", return_value=0),
			patch("frappe.enqueue"),
			patch("frappe.publish_realtime"),
		):
			llm_switch.begin("jarvis.tests.job_a", job_id="jarvis_settings_sync:pool", deduplicate=True)
		run_id_a = llm_switch.status()["run_id"]

		frappe.db.set_single_value(
			"Jarvis Settings", {"last_sync_status": attempt_status}, update_modified=False
		)
		with patch.object(llm_switch, "_inflight", return_value=5):
			llm_switch.begin(
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_handover_via_admin",
				job_id="jarvis_settings_sync:handover",
				deduplicate=True,
			)

		self.assertEqual(llm_switch.status()["next"]["pending_status"], attempt_status)

		frappe.db.set_single_value(
			"Jarvis Settings", {"last_sync_status": "ok (pool_update via admin)"}, update_modified=False
		)
		with (
			patch("frappe.enqueue"),
			patch("frappe.publish_realtime"),
		):
			llm_switch.finish(run_id_a, "ok (pool_update via admin)")

		self.assertEqual(
			frappe.db.get_value("Jarvis Settings", "Jarvis Settings", "last_sync_status"), attempt_status
		)


class TestNeverRaises(_LlmSwitchTestCase):
	"""Fix round 1 (2026 review): begin()'s after-commit callback, try_apply() (which
	backs a whitelisted poller via reconcile()) and reconcile() itself must never
	raise - frappe's CallbackManager does not wrap after_commit callbacks in
	try/except, and a raise out of a whitelisted endpoint is a 500 in front of the
	user. Also covers the enqueue-failure reset (Fix 4): a switch must not get stuck
	"applied" forever when the enqueue call itself blows up."""

	def test_begin_after_commit_path_survives_inflight_error(self):
		"""``_begin_now`` is exactly what the after_commit callback runs (inline here
		only because frappe.flags.in_test makes begin() take the inline branch - the
		function under test is identical either way)."""
		with (
			patch.object(llm_switch, "_inflight", side_effect=RuntimeError("redis down")),
			patch("frappe.enqueue") as mock_enqueue,
			patch("frappe.publish_realtime"),
			patch("frappe.log_error") as mock_log,
		):
			llm_switch.begin("jarvis.tests.fake_job")  # must not raise

		mock_enqueue.assert_not_called()
		mock_log.assert_called()
		self.assertFalse(llm_switch.status()["applied"])

	def test_reconcile_survives_a_lock_error(self):
		with (
			patch("jarvis._redis_lock.redis_lock", side_effect=RuntimeError("redis down")),
			patch("frappe.log_error") as mock_log,
		):
			llm_switch.reconcile()  # must not raise

		mock_log.assert_called()

	def test_enqueue_failure_resets_applied_and_a_later_try_apply_enqueues(self):
		with (
			patch.object(llm_switch, "_inflight", return_value=0),
			patch("frappe.publish_realtime"),
			patch("frappe.enqueue", side_effect=[RuntimeError("boom"), None]) as mock_enqueue,
			patch("frappe.log_error") as mock_log,
		):
			llm_switch.begin("jarvis.tests.fake_job")

			self.assertFalse(llm_switch.status()["applied"])
			mock_log.assert_called()

			second = llm_switch.try_apply()

		self.assertTrue(second)
		self.assertEqual(mock_enqueue.call_count, 2)
		self.assertTrue(llm_switch.status()["applied"])


class TestEnd(_LlmSwitchTestCase):
	def test_end_clears_and_broadcasts_and_promotes(self):
		with (
			patch.object(llm_switch, "_inflight", return_value=0),
			patch("frappe.enqueue"),
			patch("frappe.publish_realtime"),
		):
			llm_switch.begin("jarvis.tests.fake_job")
		self.assertIsNotNone(llm_switch.status())

		with (
			patch("frappe.publish_realtime") as mock_pub,
			patch.object(admission, "promote_next") as mock_promote,
		):
			llm_switch.end("ok: moved")

		self.assertIsNone(llm_switch.status())
		mock_pub.assert_called_once_with(llm_switch.EVENT, {"state": "done", "outcome": "ok: moved"})
		mock_promote.assert_called_once()

	def test_end_is_a_noop_without_an_active_record(self):
		with (
			patch("frappe.publish_realtime") as mock_pub,
			patch.object(admission, "promote_next") as mock_promote,
		):
			llm_switch.end("ok: moved")

		mock_pub.assert_not_called()
		mock_promote.assert_not_called()


class TestPersistentCacheKey(_LlmSwitchTestCase):
	"""Live e2e2 finding (2026-09-27): frappe.clear_cache()'s "everything" branch
	deletes every site-prefixed redis key except one matched by the
	``persistent_cache_keys`` hook - pump.py's watchdog stamping a __default via
	frappe.db.set_default triggers exactly this full clear via
	frappe.defaults._clear_cache, wiping an active switch record mid-hold or
	mid-apply. hooks.py must list ``jarvis:llm_switch``."""

	def test_clear_cache_does_not_wipe_an_active_switch_record(self):
		rec = {
			"started_at": time.time(),
			"deadline": time.time() + 300,
			"job": "jarvis.tests.fake_job",
			"job_kwargs": {},
			"applied": False,
			"run_id": None,
			"next": None,
		}
		frappe.cache().set_value(llm_switch.KEY, rec, expires_in_sec=llm_switch.TTL_S)
		frappe.cache().set_value("jarvis:test_non_persistent_probe", "x")
		try:
			frappe.clear_cache()
			self.assertEqual(
				llm_switch.status(), rec, "the hook must keep clear_cache() from wiping the switch record"
			)
			self.assertIsNone(
				frappe.cache().get_value("jarvis:test_non_persistent_probe"),
				"an ordinary key must still be cleared - proves the hook, not a no-op clear",
			)
		finally:
			llm_switch._reset_for_tests()


class TestReconcile(_LlmSwitchTestCase):
	def _applied_record(self, *, run_id="test-run-1", next_apply=None):
		rec = {
			"started_at": time.time(),
			"deadline": time.time() + llm_switch.CAP_S,
			"job": "jarvis.tests.fake_job",
			"job_kwargs": {},
			"applied": True,
			"run_id": run_id,
			"next": next_apply,
		}
		frappe.cache().set_value(llm_switch.KEY, rec, expires_in_sec=llm_switch.TTL_S)

	def test_status_self_clears_after_terminal_sync_status(self):
		for status in ("ok: applied", "failed: fleet unreachable"):
			with self.subTest(status=status):
				self._applied_record()
				frappe.db.set_single_value(
					"Jarvis Settings", {"last_sync_status": status}, update_modified=False
				)
				with patch.object(llm_switch, "end") as mock_end:
					llm_switch.reconcile()
				mock_end.assert_called_once_with(status)

	def test_reconcile_leaves_pending_switch_alone(self):
		self._applied_record()
		frappe.db.set_single_value(
			"Jarvis Settings", {"last_sync_status": "pending: provisioning container"}, update_modified=False
		)
		with patch.object(llm_switch, "end") as mock_end:
			llm_switch.reconcile()
		mock_end.assert_not_called()

	def test_reconcile_hands_off_to_a_parked_next(self):
		"""2026 review fix wave: reconcile() uses the SAME hand-off logic as a
		worker's own finish() call - a terminal status with a parked `next`
		releases it as a fresh run instead of ending."""
		self._applied_record(next_apply={"job": "jarvis.tests.job_b", "job_kwargs": {"other": "kw"}})
		frappe.db.set_single_value(
			"Jarvis Settings", {"last_sync_status": "ok: applied"}, update_modified=False
		)

		with (
			patch("frappe.enqueue") as mock_enqueue,
			patch("frappe.publish_realtime") as mock_pub,
		):
			llm_switch.reconcile()

		mock_pub.assert_not_called()  # hand-off, not end - no "done" broadcast
		mock_enqueue.assert_called_once()
		args, kwargs = mock_enqueue.call_args
		self.assertEqual(args[0], "jarvis.tests.job_b")
		self.assertEqual(kwargs["other"], "kw")
		new_rec = llm_switch.status()
		self.assertTrue(new_rec["applied"])
		self.assertEqual(new_rec["job"], "jarvis.tests.job_b")
		self.assertIsNone(new_rec["next"])
		self.assertNotEqual(new_rec["run_id"], "test-run-1")
		self.assertIsNotNone(llm_switch.status())


class TestSelfHealLostRelease(_LlmSwitchTestCase):
	"""jarvis#1425 review, third pass, "lost release": a released job's enqueue
	can be lost after the record already reads applied:True (a rollback after
	enqueue_after_commit registered it, a worker killed before it ever
	started, a redis blip) - without this, the record is stuck applied:True
	with nothing behind it until the full 20-minute TTL."""

	_RQ_JOB_ID = "jarvis_settings_sync:pool:abcd1234"

	def _lost_release_record(self, *, released_at: float, run_id: str = "stale-run") -> None:
		rec = {
			"started_at": time.time() - 100,
			"deadline": time.time() + 200,
			"job": "jarvis.tests.fake_job",
			"job_kwargs": {"job_id": "jarvis_settings_sync:pool"},
			"applied": True,
			"run_id": run_id,
			"next": None,
			"released_at": released_at,
			"rq_job_id": self._RQ_JOB_ID,
		}
		frappe.cache().set_value(llm_switch.KEY, rec, expires_in_sec=llm_switch.TTL_S)

	def _stamp_pending_status(self) -> None:
		frappe.db.set_single_value(
			"Jarvis Settings", {"last_sync_status": "pending: admin applying config"}, update_modified=False
		)

	def test_reconcile_reheals_a_lost_release_past_grace_with_no_rq_job(self):
		"""The end-to-end path the coordinator asked for: reconcile() (every held
		chat's 60s check and get_llm_sync_status already call it) re-releases
		under a fresh run_id and a real new enqueue when the old job is simply
		gone. Flips both inline flags off (see test_lone_direct_handover.py's
		established pattern) since inline mode has no rq job to lose."""
		self._lost_release_record(released_at=time.time() - llm_switch.RELEASE_GRACE_S - 1)
		self._stamp_pending_status()
		original_in_test = frappe.flags.in_test
		original_inline = frappe.flags.get("run_admin_sync_inline")
		frappe.flags.in_test = False
		frappe.flags.run_admin_sync_inline = False
		try:
			with (
				patch.object(llm_switch, "_inflight", return_value=0),
				patch("frappe.utils.background_jobs.get_job_status", return_value=None) as get_status,
				patch("frappe.enqueue") as mock_enqueue,
				patch("frappe.log_error") as mock_log,
			):
				llm_switch.reconcile()
			get_status.assert_called_once_with(self._RQ_JOB_ID)
		finally:
			frappe.flags.in_test = original_in_test
			frappe.flags.run_admin_sync_inline = original_inline
			frappe.db.after_commit.reset()  # the real enqueue was mocked away; nothing to run
		mock_log.assert_called_once()
		self.assertIn("llm_switch", mock_log.call_args.kwargs.get("title", ""))
		mock_enqueue.assert_called_once()
		new_rec = llm_switch.status()
		self.assertTrue(new_rec["applied"], "the re-release must land as a fresh applied run")
		self.assertNotEqual(new_rec["run_id"], "stale-run", "healing must mint a FRESH run_id")

	def test_heal_leaves_a_queued_job_alone(self):
		self._lost_release_record(released_at=time.time() - llm_switch.RELEASE_GRACE_S - 1)
		self._stamp_pending_status()
		rec = llm_switch.status()
		with patch("frappe.utils.background_jobs.get_job_status", return_value="queued"):
			healed = llm_switch._heal_lost_release_locked(rec)
		self.assertIsNone(healed, "a queued job is alive; nothing to heal")
		self.assertEqual(llm_switch.status(), rec, "an untouched record must be byte-identical")

	def test_heal_leaves_a_fresh_release_alone(self):
		"""Within RELEASE_GRACE_S: rq dequeue latency is normal, not a loss."""
		self._lost_release_record(released_at=time.time() - 1)
		self._stamp_pending_status()
		rec = llm_switch.status()
		with patch("frappe.utils.background_jobs.get_job_status") as get_status:
			healed = llm_switch._heal_lost_release_locked(rec)
			get_status.assert_not_called()  # never even worth the rq round-trip
		self.assertIsNone(healed)
		self.assertEqual(llm_switch.status(), rec)

	def test_heal_leaves_a_terminal_status_alone(self):
		"""A terminal last_sync_status means finish()/end() already own this (or
		reconcile()'s own terminal-status branch will) - healing must never
		race that by re-releasing behind its back."""
		self._lost_release_record(released_at=time.time() - llm_switch.RELEASE_GRACE_S - 1)
		frappe.db.set_single_value(
			"Jarvis Settings", {"last_sync_status": "ok (restart via admin)"}, update_modified=False
		)
		rec = llm_switch.status()
		with patch("frappe.utils.background_jobs.get_job_status", return_value=None):
			healed = llm_switch._heal_lost_release_locked(rec)
		self.assertIsNone(healed)
		self.assertEqual(llm_switch.status(), rec)

	def test_heal_is_skipped_entirely_in_inline_mode(self):
		"""frappe.flags.in_test is True for the whole suite - a real regression
		here would make every OTHER test in this file a false negative for
		self-heal, so this pins the skip explicitly rather than relying on it
		as an incidental side effect elsewhere."""
		self._lost_release_record(released_at=time.time() - llm_switch.RELEASE_GRACE_S - 1)
		self._stamp_pending_status()
		rec = llm_switch.status()
		with patch("frappe.utils.background_jobs.get_job_status") as get_status:
			healed = llm_switch._heal_lost_release_locked(rec)
			get_status.assert_not_called()
		self.assertIsNone(healed)


class TestBootPayload(_LlmSwitchTestCase):
	def test_boot_payload_reports_switch(self):
		with patch.object(llm_switch, "is_active", return_value=True):
			payload = maintenance_notice.boot_payload()
		self.assertEqual(payload, {"active": True, "message": llm_switch.MESSAGE})

	def test_operator_mirror_wins_over_switch(self):
		maintenance_notice.persist({"active": True, "message": "up"})
		with patch.object(llm_switch, "is_active", return_value=True):
			payload = maintenance_notice.boot_payload()
		self.assertEqual(payload, {"active": True, "message": "up"})

	def test_boot_payload_inactive_when_neither_holds(self):
		with patch.object(llm_switch, "is_active", return_value=False):
			payload = maintenance_notice.boot_payload()
		self.assertEqual(payload, {"active": False, "message": ""})

	def test_persist_from_connection_reads_mirror_not_switch(self):
		"""persist_from_connection must read only the operator mirror - a switch is
		bench-local and the control plane has no notion of it. Patching
		boot_payload to explode proves the old-CP branch no longer calls it."""
		with patch(
			"jarvis.maintenance_notice.boot_payload", side_effect=AssertionError("must not call boot_payload")
		):
			maintenance_notice.persist_from_connection({})  # no marker -> old-CP clear branch
		self.assertFalse(maintenance_notice._mirror_payload()["active"])


class TestInflightWidenedWindow(FrappeTestCase):
	"""Direct coverage of the documented deviation from the brief: llm_switch does
	NOT reuse admission._shard_inflight as-is (it under-counts a legacy streaming
	reply mid a long tool call - see llm_switch.py's module comment and
	task-4-report.md). It calls the two admission primitives itself and widens the
	legacy leg's freshness cutoff to 900s instead of admission's own 180s."""

	def test_inflight_widens_the_legacy_freshness_window(self):
		with (
			patch.object(admission, "_turn_dispatching_count", return_value=0) as mock_turn,
			patch.object(admission, "_legacy_streaming_count", return_value=0) as mock_legacy,
		):
			llm_switch._inflight()

		mock_turn.assert_called_once_with(admission.DEFAULT_RELAY_TARGET)
		mock_legacy.assert_called_once()
		args, kwargs = mock_legacy.call_args
		self.assertEqual(args[0], admission.DEFAULT_RELAY_TARGET)
		self.assertIn("fresh_cutoff", kwargs)
		# Wider window -> an EARLIER cutoff than admission's own 180s default.
		self.assertLess(
			frappe.utils.get_datetime(kwargs["fresh_cutoff"]),
			frappe.utils.get_datetime(admission._fresh_cutoff()),
		)


class TestPromoteNextHold(FrappeTestCase):
	def test_promote_next_holds_during_switch(self):
		with patch.object(llm_switch, "is_active", return_value=True):
			result = admission.promote_next()
		self.assertEqual(result, 0)


class TestSettleTriggersTryApply(FrappeTestCase):
	def test_settle_turn_triggers_try_apply(self):
		with (
			patch.object(admission, "admission_enabled", return_value=False),
			patch.object(llm_switch, "try_apply") as mock_try_apply,
		):
			admission.settle_turn("no-such-run-id", "done")
		mock_try_apply.assert_called_once()

	def test_settle_conversation_dispatching_triggers_try_apply_with_admission_disabled(self):
		"""CR-7 reverted (2026 review, second pass): with admission_enabled()
		False (today's default), settle_conversation_dispatching returns
		before ever reaching settle_turn - its OWN finally is the ONLY
		trigger on this path, so a prior "cleanup" round that removed it (on
		the mistaken theory that settle_turn always runs it) meant a held
		switch would never start once the last reply finished on the
		recovery path. Restored."""
		with (
			patch.object(admission, "admission_enabled", return_value=False),
			patch.object(llm_switch, "try_apply") as mock_try_apply,
		):
			admission.settle_conversation_dispatching("conv-1", "done")

		mock_try_apply.assert_called_once()

	def test_settle_conversation_dispatching_with_admission_enabled_still_triggers_try_apply(self):
		"""The restored call also fires when admission IS enabled and a
		dispatching run_id is found - settle_turn's own trigger and this
		one both run (try_apply is idempotent, so the extra call is
		harmless, not a real duplicate)."""
		with (
			patch.object(admission, "admission_enabled", return_value=True),
			patch("frappe.db.get_value", return_value="turn-1"),
			patch.object(admission, "settle_turn") as mock_settle_turn,
			patch.object(llm_switch, "try_apply") as mock_try_apply,
		):
			admission.settle_conversation_dispatching("conv-1", "done")

		mock_settle_turn.assert_called_once_with("turn-1", "done", error=None)
		# settle_turn is mocked away here (it would trigger try_apply itself
		# too, in real code - see test_settle_turn_triggers_try_apply) - this
		# asserts settle_conversation_dispatching's OWN restored trigger
		# still fires regardless.
		mock_try_apply.assert_called_once()
