"""Throwaway one-shot sessions must not be deleted while their run is live.

Issue #525. The three throwaway kinds (auto-title, pattern polish, prefix
prewarm) each mint a session, run one agent turn on it and then reclaim it.
They used to call ``sessions.delete`` the instant their own call returned,
which is NOT the same instant the run ends:

- ``stream_agent_turn`` returns on the run's lifecycle-end frame, while
  openclaw is still finalising the session file;
- it RAISES on every error path with the run still going server side;
- ``fire_agent`` (prewarm) never waits at all.

Deleting there renamed the session file out from under a live run. These tests
pin the fix: every reclaim first asks the gateway whether a run is still active
(``sessions.list`` -> ``hasActiveRun``, the same signal the orphan sweep uses)
and withholds the delete while it is, leaving the session to that sweep. They
also pin the cost shape - one probe and no sleep when the run is already done,
a backoff only when the gateway says it is live - because polish's reclaim runs
inside a synchronous whitelisted request and title's holds one of only three
pooled connections.

Issue #535 then found the half that probe could not reach: openclaw ACCEPTS a
run a median 670ms before it STARTS one, and ``hasActiveRun`` is false for the
whole of that window, so a reclaim firing inside it deletes a session whose run
is about to begin. ``TestUnstartedRunIsNotDeleted`` below pins the second half
of the rule - a caller that never saw the run END hands over the time it fired,
and an idle answer is only believed once that has aged past
``RUN_START_GRACE_S`` or a probe has actually caught the run.

SCOPE NOTE: the transport is stubbed here, as everywhere else in this suite, so
these tests assert INTENT - which RPCs the bench issues and in what order. They
cannot observe what openclaw does with a delete that lands mid-run; that was
established from the tenant container's own session files and gateway log, not
from a test.
"""

from __future__ import annotations

import contextlib
import time
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import session_lifecycle

SKEY = "agent:main:dashboard:throwaway-1"


def _stub_pool_session(*, active_probes, reply="A Nice Title", raises=None):
	"""A pooled OpenclawSession whose ``is_run_active`` answers ``active_probes``
	in order (a bare bool repeats forever)."""
	sess = MagicMock()
	sess.create_session.return_value = SKEY
	if isinstance(active_probes, bool):
		sess.is_run_active.return_value = active_probes
	else:
		sess.is_run_active.side_effect = list(active_probes)
	if raises is not None:
		sess.stream_agent_turn.side_effect = raises
	else:
		sess.stream_agent_turn.return_value = iter([{"kind": "assistant", "text": reply}])
	return sess


@contextlib.contextmanager
def _checkout_yielding(sess):
	@contextlib.contextmanager
	def _cm(_gateway_url):
		yield sess

	with patch("jarvis.chat.openclaw_session_pool.checkout", _cm):
		yield


def _no_sleep():
	"""Drop the retry backoff so a busy-session probe loop runs at test speed."""
	return patch.object(session_lifecycle, "RECLAIM_PROBE_DELAY_S", 0)


class TestReclaimThrowawaySession(FrappeTestCase):
	"""The shared helper both the minters and the sweep agree on."""

	def test_active_run_is_never_deleted(self):
		sess = _stub_pool_session(active_probes=True)

		with _no_sleep():
			reclaimed = session_lifecycle.reclaim_throwaway_session(sess, SKEY, logger_name="jarvis.tests")

		self.assertFalse(reclaimed)
		sess.delete_session.assert_not_called()
		self.assertEqual(
			sess.is_run_active.call_count,
			session_lifecycle.RECLAIM_PROBE_ATTEMPTS,
			"the helper must exhaust its probe budget before giving up",
		)

	def test_deletes_once_the_run_clears(self):
		"""Giving up on a busy session must not become a leak: as soon as the
		gateway reports the run finished, the session goes.

		The call_count assertion is load-bearing. Without it this passes against
		an unconditional delete AND against an inverted probe (which would fire
		on the first True), so it would pin nothing."""
		sess = _stub_pool_session(active_probes=[True, True, False])

		with _no_sleep():
			reclaimed = session_lifecycle.reclaim_throwaway_session(sess, SKEY, logger_name="jarvis.tests")

		self.assertTrue(reclaimed)
		sess.delete_session.assert_called_once_with(SKEY)
		self.assertEqual(sess.is_run_active.call_count, 3, "the delete must wait for the third, idle probe")
		self.assertEqual(
			[c[0] for c in sess.method_calls],
			["is_run_active", "is_run_active", "is_run_active", "delete_session"],
			"every probe must precede the delete",
		)

	def test_idle_session_is_deleted_without_waiting(self):
		"""polish's reclaim runs inside a synchronous whitelisted request and
		title's holds one of three pooled connections, so the common case must
		cost one probe and no sleep at all."""
		sess = _stub_pool_session(active_probes=False)

		with patch.object(session_lifecycle.time, "sleep") as sleep:
			reclaimed = session_lifecycle.reclaim_throwaway_session(sess, SKEY, logger_name="jarvis.tests")

		self.assertTrue(reclaimed)
		self.assertEqual(sess.is_run_active.call_count, 1)
		sleep.assert_not_called()

	def test_busy_session_backs_off_between_probes(self):
		"""The wait only appears when the gateway says the run is live."""
		sess = _stub_pool_session(active_probes=[True, False])

		with patch.object(session_lifecycle.time, "sleep") as sleep:
			session_lifecycle.reclaim_throwaway_session(sess, SKEY, logger_name="jarvis.tests")

		sleep.assert_called_once_with(session_lifecycle.RECLAIM_PROBE_DELAY_S)

	def test_probe_failure_defers_to_the_sweep(self):
		"""An unreadable gateway must not be answered with a blind delete."""
		sess = _stub_pool_session(active_probes=True)
		sess.is_run_active.side_effect = RuntimeError("gateway blip")

		with _no_sleep():
			reclaimed = session_lifecycle.reclaim_throwaway_session(sess, SKEY, logger_name="jarvis.tests")

		self.assertFalse(reclaimed)
		sess.delete_session.assert_not_called()

	def test_delete_failure_never_raises(self):
		sess = _stub_pool_session(active_probes=False)
		sess.delete_session.side_effect = RuntimeError("gateway blip")

		with _no_sleep():
			reclaimed = session_lifecycle.reclaim_throwaway_session(sess, SKEY, logger_name="jarvis.tests")

		self.assertFalse(reclaimed)

	def test_empty_key_is_a_noop(self):
		sess = _stub_pool_session(active_probes=False)

		with _no_sleep():
			self.assertFalse(
				session_lifecycle.reclaim_throwaway_session(sess, "", logger_name="jarvis.tests")
			)

		sess.delete_session.assert_not_called()
		sess.is_run_active.assert_not_called()


class TestUnstartedRunIsNotDeleted(FrappeTestCase):
	"""Issue #535: the window the probe alone cannot close.

	openclaw ACCEPTS a run before it STARTS one, and sessions.list reports
	"accepted, not started" and "finished" identically - hasActiveRun is false in
	both. Measured on jarvis-pool-bf4097 over 269 sessions, as the gap between
	the session file's creation stamp and the run's own session.started
	trajectory event: p50 0.67s, p90 2.90s, p95 4.84s. A reclaim that fires
	inside that window therefore reads "idle" and deletes the session out from
	under a run that is about to begin.

	The fix is not more probing - the signal has not been written yet - but a
	caller admitting it never saw the run END, by handing over the time it fired.
	"""

	def test_an_accepted_but_unstarted_run_is_not_deleted(self):
		sess = _stub_pool_session(active_probes=False)

		with _no_sleep():
			reclaimed = session_lifecycle.reclaim_throwaway_session(
				sess, SKEY, logger_name="jarvis.tests", fired_at=time.time()
			)

		self.assertFalse(reclaimed)
		sess.delete_session.assert_not_called()

	def test_an_old_run_reported_idle_is_still_reclaimed_at_once(self):
		"""The guard must not become a leak. Past the grace, an idle answer is
		what it has always been - a finished run - and costs one probe."""
		sess = _stub_pool_session(active_probes=False)

		with patch.object(session_lifecycle.time, "sleep") as sleep:
			reclaimed = session_lifecycle.reclaim_throwaway_session(
				sess,
				SKEY,
				logger_name="jarvis.tests",
				fired_at=time.time() - (session_lifecycle.RUN_START_GRACE_S + 60),
			)

		self.assertTrue(reclaimed)
		sess.delete_session.assert_called_once_with(SKEY)
		self.assertEqual(sess.is_run_active.call_count, 1)
		sleep.assert_not_called()

	def test_a_run_caught_active_is_reclaimed_inside_the_grace(self):
		"""Positive evidence beats the clock. Once a probe has actually SEEN the
		run, a later idle answer is a real finished-signal, so the grace stops
		applying and the session goes immediately."""
		sess = _stub_pool_session(active_probes=[True, False])

		with _no_sleep():
			reclaimed = session_lifecycle.reclaim_throwaway_session(
				sess, SKEY, logger_name="jarvis.tests", fired_at=time.time()
			)

		self.assertTrue(reclaimed)
		sess.delete_session.assert_called_once_with(SKEY)
		self.assertEqual(sess.is_run_active.call_count, 2)

	def test_the_guard_declines_rather_than_waiting_the_grace_out(self):
		"""It refuses to guess, it does not block. polish's reclaim runs inside a
		synchronous whitelisted request; spending RUN_START_GRACE_S there to
		reach a delete the orphan sweep does for free would be the worse trade."""
		sess = _stub_pool_session(active_probes=False)

		started = time.time()
		with patch.object(session_lifecycle, "RECLAIM_PROBE_DELAY_S", 0):
			session_lifecycle.reclaim_throwaway_session(
				sess, SKEY, logger_name="jarvis.tests", fired_at=time.time()
			)

		self.assertLess(time.time() - started, session_lifecycle.RUN_START_GRACE_S)


class TestAutoTitleReclaim(FrappeTestCase):
	"""jarvis/chat/title.py::_generate_via_gateway"""

	def _generate(self, sess):
		from jarvis.chat import title

		with _checkout_yielding(sess), _no_sleep():
			return title._generate_via_gateway(
				"ws://gw.example",
				"Show me last month's overdue invoices",
				model="gpt-5.5",
				provider=None,
			)

	def test_title_session_is_not_deleted_while_its_run_is_live(self):
		"""The regression. stream_agent_turn returning is not the run ending -
		openclaw is still writing the session file - so a title turn whose run
		the gateway still reports as active must be left alone."""
		sess = _stub_pool_session(active_probes=True)

		title_text = self._generate(sess)

		sess.delete_session.assert_not_called()
		self.assertEqual(title_text, "A Nice Title", "the title must still be returned")

	def test_title_session_is_reclaimed_once_the_run_ends(self):
		sess = _stub_pool_session(active_probes=[True, False])

		title_text = self._generate(sess)

		sess.delete_session.assert_called_once_with(SKEY)
		self.assertEqual(sess.is_run_active.call_count, 2, "the delete must wait for the idle probe")
		self.assertEqual(title_text, "A Nice Title")

	def test_failed_title_turn_does_not_delete_a_live_session(self):
		"""The worst of the old paths: stream_agent_turn raises precisely
		BECAUSE the run is still going (a WS drop leaves openclaw running), and
		the finally deleted the session anyway."""
		from jarvis.exceptions import OpenclawUnreachableError

		sess = _stub_pool_session(
			active_probes=True,
			raises=OpenclawUnreachableError("agent turn timed out before lifecycle end"),
		)

		with patch("jarvis.chat.title.frappe.log_error"):
			title_text = self._generate(sess)

		sess.delete_session.assert_not_called()
		self.assertEqual(title_text, "", "a failed title turn still falls back to derive_title")

	def test_a_title_run_that_has_not_started_yet_is_not_deleted(self):
		"""Issue #535. Same raise path as above, but the gateway has not started
		the run yet, so hasActiveRun is FALSE and the probe cannot tell it from a
		finished run. Only the fire time can, and the finally must hand it over.

		This is the shape the probe-only fix (#530) still gets wrong: a WS drop
		or a connect error raises within a few hundred ms of the fire, well
		inside the measured 670ms median start delay."""
		from jarvis.exceptions import OpenclawUnreachableError

		sess = _stub_pool_session(
			active_probes=False,
			raises=OpenclawUnreachableError("connection closed before the first frame"),
		)

		with patch("jarvis.chat.title.frappe.log_error"):
			title_text = self._generate(sess)

		sess.delete_session.assert_not_called()
		self.assertEqual(title_text, "")


class TestPatternPolishReclaim(FrappeTestCase):
	"""jarvis/learning/polish.py::_run_gateway_turn"""

	def _run(self, sess):
		from jarvis.learning import polish

		settings = MagicMock()
		settings.agent_url = "http://gw.example"
		with (
			_checkout_yielding(sess),
			_no_sleep(),
			patch("jarvis.learning.polish.frappe.get_single", return_value=settings),
			patch(
				"jarvis.chat.turn_handler._resolve_model_and_provider",
				return_value=("gpt-5.5", None),
			),
		):
			return polish._run_gateway_turn("- rewrite this bullet")

	def test_polish_session_is_not_deleted_while_its_run_is_live(self):
		sess = _stub_pool_session(active_probes=True, reply="- a polished bullet")

		text = self._run(sess)

		sess.delete_session.assert_not_called()
		self.assertEqual(text, "- a polished bullet")

	def test_polish_session_is_reclaimed_once_the_run_ends(self):
		sess = _stub_pool_session(active_probes=[True, False], reply="- a polished bullet")

		self._run(sess)

		sess.delete_session.assert_called_once_with(SKEY)
		self.assertEqual(sess.is_run_active.call_count, 2, "the delete must wait for the idle probe")

	def test_failed_polish_turn_does_not_delete_a_live_session(self):
		"""polish's finally has title's exact shape and its exact bug history,
		so it gets title's raise-path test too."""
		from jarvis.exceptions import OpenclawUnreachableError

		sess = _stub_pool_session(
			active_probes=True,
			raises=OpenclawUnreachableError("agent turn timed out before lifecycle end"),
		)

		with patch("jarvis.learning.polish.frappe.log_error"):
			text = self._run(sess)

		sess.delete_session.assert_not_called()
		self.assertEqual(text, "", "a failed polish turn still falls back to the template text")

	def test_a_polish_run_that_has_not_started_yet_is_not_deleted(self):
		"""Issue #535, polish's copy of title's raise-path gap: the gateway
		reports the session idle because the run has not started, not because it
		finished."""
		from jarvis.exceptions import OpenclawUnreachableError

		sess = _stub_pool_session(
			active_probes=False,
			raises=OpenclawUnreachableError("connection closed before the first frame"),
		)

		with patch("jarvis.learning.polish.frappe.log_error"):
			text = self._run(sess)

		sess.delete_session.assert_not_called()
		self.assertEqual(text, "")


class TestPrewarmReclaim(FrappeTestCase):
	"""jarvis/chat/prewarm.py::_reclaim_previous"""

	def tearDown(self):
		from jarvis.chat import prewarm

		frappe.cache().delete_value(prewarm._warm_cooldown_key())
		frappe.cache().delete_value(prewarm._warm_last_key())

	def test_predecessor_with_a_live_run_is_left_for_the_sweep(self):
		"""Two warms can pass the get-then-set cooldown check in the same
		instant, which makes "the predecessor" a session minted milliseconds
		ago whose fire-and-forget warm turn is still running."""
		from jarvis.chat import prewarm

		sess = _stub_pool_session(active_probes=True)

		with _no_sleep():
			prewarm._reclaim_previous(sess, "sk_previous", "sk_current")

		sess.delete_session.assert_not_called()

	def test_finished_predecessor_is_still_reclaimed(self):
		from jarvis.chat import prewarm

		sess = _stub_pool_session(active_probes=False)

		with _no_sleep():
			prewarm._reclaim_previous(sess, "sk_previous", "sk_current")

		sess.delete_session.assert_called_once_with("sk_previous")
		sess.is_run_active.assert_called_once_with("sk_previous")

	def test_own_session_is_never_reclaimed(self):
		from jarvis.chat import prewarm

		sess = _stub_pool_session(active_probes=False)

		with _no_sleep():
			prewarm._reclaim_previous(sess, "sk_same", "sk_same")

		sess.delete_session.assert_not_called()
		sess.is_run_active.assert_not_called()

	def test_a_predecessor_whose_run_has_not_started_is_left_alone(self):
		"""Issue #535: the same instant-apart race, but with the gateway
		answering hasActiveRun=FALSE because it has not started the predecessor's
		run yet. The probe waves this one through; the remembered fire time is
		what stops it."""
		from jarvis.chat import prewarm

		sess = _stub_pool_session(active_probes=False)

		with _no_sleep():
			prewarm._reclaim_previous(sess, {"key": "sk_previous", "fired_at": time.time()}, "sk_current")

		sess.delete_session.assert_not_called()

	def test_a_predecessor_from_a_cooldown_ago_is_reclaimed(self):
		"""The normal case, which must stay immediate: the cooldown has elapsed,
		so the pointer is minutes old and an idle answer means finished."""
		from jarvis.chat import prewarm

		sess = _stub_pool_session(active_probes=False)

		with _no_sleep():
			prewarm._reclaim_previous(
				sess, {"key": "sk_previous", "fired_at": time.time() - 600}, "sk_current"
			)

		sess.delete_session.assert_called_once_with("sk_previous")

	def test_a_malformed_pointer_is_a_noop(self):
		from jarvis.chat import prewarm

		sess = _stub_pool_session(active_probes=False)

		with _no_sleep():
			prewarm._reclaim_previous(sess, {"fired_at": time.time()}, "sk_current")
			prewarm._reclaim_previous(sess, None, "sk_current")

		sess.delete_session.assert_not_called()
		sess.is_run_active.assert_not_called()
