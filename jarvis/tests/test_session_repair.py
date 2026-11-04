"""Tests for the pre-send session self-repair (jarvis.chat.session_repair, #753).

The helper probes a conversation's gateway session before a send and un-archives
it (``sessions.patch``) so a chat that the gateway would refuse is healed instead of
stuck. It is ARCHIVED-ONLY: a tombstone needs ``sessions.reset`` (``operator.admin``),
which the bench does not hold, so it makes no mutating call. Most tests drive a fake session
object that returns real row dicts; ``TestWireOrder`` runs a real ``AgentSession``
over the scripted websocket so the frames and their order are the real ones.

Every rule has a test that fails when the rule is broken: absent row means proceed,
tombstone means no mutation, a temporary error re-raises, a definitive refusal is
REFUSED, nothing but a gateway error (and rq's job-timeout) escapes, the kill switch
tokens, and one bounded re-describe after ``session-changed``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from rq.timeouts import JobTimeoutException

from jarvis.chat import session_repair
from jarvis.chat.session_repair import Repair, ensure_session_usable
from jarvis.chat.socket_io import SendNotStarted, SendUncertain
from jarvis.exceptions import AgentUnreachableError
from jarvis.tests.test_chat_agent_client import _session_with_scripted_frames

KEY = "agent:main:dashboard:conv-secret-key-1"
SID = "sid-1"
CONF = "jarvis_session_repair_enabled"
REFUSED_TITLE = "jarvis.chat.session_repair.refused"
FAILED_TITLE = "jarvis.chat.session_repair.probe_failed"
PROBE_REFUSED_TITLE = "jarvis.chat.session_repair.probe_refused"


def _row(**kw) -> dict:
	row = {"key": KEY, "sessionId": SID, "archived": False, "archivedAt": None}
	row.update(kw)
	return row


def _archived(**kw) -> dict:
	fields = {"archived": True, "archivedAt": 1700000000000, "archiveReason": "stale-dashboard", **kw}
	return _row(**fields)


def _tomb(**kw) -> dict:
	return _row(restartRecoveryStatus="tombstoned", **kw)


def _refusal(code="INVALID_REQUEST", method="sessions.patch", details=None) -> AgentUnreachableError:
	return AgentUnreachableError(
		f"{method} rejected: {code}: Session {KEY} is nope",
		code=code,
		details=details,
		rejected_method=method,
	)


def _changed() -> AgentUnreachableError:
	return _refusal(details={"reason": "session-changed"})


class _Sess:
	"""Scripted stand-in for AgentSession. Each effects item is a value to return or an
	exception instance to raise; an exhausted restore list means success. ``calls``
	records ("name", args...) in order, ``waking`` joins it. There is deliberately no
	``reset_session``: the helper must never need one."""

	def __init__(self, describe=(), restore=()):
		self.calls: list[tuple] = []
		self._describe = list(describe)
		self._restore = list(restore)

	@staticmethod
	def _next(effects):
		if not effects:
			return None
		item = effects.pop(0)
		if isinstance(item, BaseException):
			raise item
		return item

	def describe_session(self, key, *, timeout_s=5):
		self.calls.append(("describe", key))
		return self._next(self._describe)

	def restore_session(self, key, session_id, *, timeout_s=5):
		self.calls.append(("restore", key, session_id))
		return self._next(self._restore)

	def names(self):
		return [c[0] for c in self.calls]

	def mutating(self):
		return [c for c in self.calls if c[0] != "describe"]


def _conv(key=KEY):
	return SimpleNamespace(session_key=key)


class _Harness(FrappeTestCase):
	"""Common capture of the on_waking callback, Error Log rows and the INFO logger."""

	def setUp(self):
		super().setUp()
		self.info_lines: list[str] = []
		self.errors: list[dict] = []
		self.logger = MagicMock()
		self.logger.info.side_effect = lambda *a, **k: self.info_lines.append(a[0] % a[1:] if a[1:] else a[0])
		p1 = patch("jarvis.chat.latency.get_logger", return_value=self.logger)
		p2 = patch(
			"frappe.log_error",
			side_effect=lambda *a, **k: self.errors.append(
				{"title": k.get("title"), "message": k.get("message")}
			),
		)
		p1.start()
		p2.start()
		self.addCleanup(p1.stop)
		self.addCleanup(p2.stop)
		self.waking = MagicMock()

	def run_helper(self, sess, conv=None, **kw):
		kw.setdefault("on_waking", self.waking)
		kw.setdefault("path", "pump")
		kw.setdefault("run_id", "run-1")
		return ensure_session_usable(sess, conv if conv is not None else _conv(), **kw)

	def titles(self):
		return [e["title"] for e in self.errors]

	def assert_no_key_anywhere(self):
		for text in [*self.info_lines, *(str(e) for e in self.errors)]:
			self.assertNotIn(KEY, text)
			self.assertNotIn("conv-secret-key-1", text)


class TestLadder(_Harness):
	def test_healthy_probes_only(self):
		for row in (_row(), _row(archived=False, archivedAt=None, restartRecoveryStatus="ok")):
			with self.subTest(row=row):
				sess = _Sess(describe=[row])
				self.assertIs(self.run_helper(sess), Repair.HEALTHY)
				self.assertEqual(sess.calls, [("describe", KEY)])
		self.waking.assert_not_called()
		self.assertEqual(self.errors, [])

	def test_absent_row_proceeds_without_repair_or_error_log(self):
		# A key whose first send never reached the gateway, or an eagerly-minted macro
		# key: the gateway creates the session on the first send, so this is not a fault.
		for row in (None, "gone", ["x"], 0):
			with self.subTest(row=row):
				sess = _Sess(describe=[row])
				self.assertIs(self.run_helper(sess), Repair.ABSENT)
				self.assertEqual(sess.names(), ["describe"])
		self.waking.assert_not_called()
		self.assertEqual(self.errors, [])

	def test_archived_flag_restores_with_expected_session_id(self):
		sess = _Sess(describe=[_archived()])
		self.assertIs(self.run_helper(sess), Repair.RESTORED)
		self.assertEqual(sess.calls, [("describe", KEY), ("restore", KEY, SID)])
		self.assertEqual(self.errors, [])

	def test_archived_at_alone_counts_as_archived(self):
		sess = _Sess(describe=[_row(archivedAt=1700000000000)])
		self.assertIs(self.run_helper(sess), Repair.RESTORED)
		self.assertEqual(sess.names(), ["describe", "restore"])

	def test_archived_true_without_archived_at_restores(self):
		# Mutation target: detection that looked only at archivedAt would call this healthy.
		sess = _Sess(describe=[_row(archived=True, archivedAt=None)])
		self.assertIs(self.run_helper(sess), Repair.RESTORED)
		self.assertEqual(sess.calls, [("describe", KEY), ("restore", KEY, SID)])
		bare = _row(archived=True)
		del bare["archivedAt"]
		sess = _Sess(describe=[bare])
		self.assertIs(self.run_helper(sess), Repair.RESTORED)

	def test_falsy_archived_at_values_are_healthy(self):
		for value in (0, "", False, None):
			with self.subTest(archivedAt=value):
				sess = _Sess(describe=[_row(archived=False, archivedAt=value)])
				self.assertIs(self.run_helper(sess), Repair.HEALTHY)
				self.assertEqual(sess.names(), ["describe"])
		self.waking.assert_not_called()

	def test_non_true_archived_values_are_not_archived(self):
		# Only the literal True counts; a truthy string or 1 is not the gateway's flag.
		for value in ("true", 1, "yes"):
			with self.subTest(archived=value):
				sess = _Sess(describe=[_row(archived=value, archivedAt=None)])
				self.assertIs(self.run_helper(sess), Repair.HEALTHY)

	def test_on_waking_called_once_before_the_restore(self):
		sess = _Sess(describe=[_archived()])
		self.waking.side_effect = lambda: sess.calls.append(("waking",))
		self.assertIs(self.run_helper(sess), Repair.RESTORED)
		self.assertEqual(sess.names(), ["describe", "waking", "restore"])

	def test_on_waking_that_raises_never_blocks_the_repair(self):
		self.waking.side_effect = RuntimeError("publish failed")
		sess = _Sess(describe=[_archived()])
		self.assertIs(self.run_helper(sess), Repair.RESTORED)
		self.assertEqual(sess.names(), ["describe", "restore"])
		self.assertEqual(self.errors, [])

	def test_tombstoned_is_unrepairable_with_no_mutation_waking_or_error_log(self):
		# Tombstone alone, and tombstone + archived: the tombstone check runs first, so
		# restoring would be pointless (the send fails on the tombstone anyway).
		for row in (_tomb(), _archived(restartRecoveryStatus="tombstoned")):
			with self.subTest(row=row):
				sess = _Sess(describe=[row])
				self.assertIs(self.run_helper(sess), Repair.UNREPAIRABLE)
				self.assertEqual(sess.calls, [("describe", KEY)])
				self.assertEqual(sess.mutating(), [])
		self.waking.assert_not_called()
		self.assertEqual(self.errors, [])
		self.assertEqual(len(self.info_lines), 2)
		for line in self.info_lines:
			self.assertIn("outcome=unrepairable", line)
			self.assertIn("recovery=tombstoned", line)
			self.assertIn("step=classify", line)

	def test_tombstoned_archived_row_without_a_session_id_is_still_just_unrepairable(self):
		sess = _Sess(describe=[_archived(restartRecoveryStatus="tombstoned", sessionId=None)])
		self.assertIs(self.run_helper(sess), Repair.UNREPAIRABLE)
		self.assertEqual(self.errors, [])

	def test_archived_without_a_string_session_id_is_refused_without_restore(self):
		for sid in (None, "", 12, ["x"]):
			with self.subTest(sid=sid):
				self.errors.clear()
				self.waking.reset_mock()
				sess = _Sess(describe=[_archived(sessionId=sid)])
				self.assertIs(self.run_helper(sess), Repair.REFUSED)
				self.assertEqual(sess.names(), ["describe"])
				self.assertEqual(self.titles(), [REFUSED_TITLE])
				self.waking.assert_not_called()
		self.assert_no_key_anywhere()

	def test_archived_row_missing_session_id_key_is_refused(self):
		row = _archived()
		del row["sessionId"]
		sess = _Sess(describe=[row])
		self.assertIs(self.run_helper(sess), Repair.REFUSED)
		self.assertNotIn("restore", sess.names())

	def test_magicmock_session_never_restores(self):
		# Existing tests hand the pipeline MagicMock sessions; a MagicMock describe
		# result is not a dict, so it must read as ABSENT and trigger nothing.
		sess = MagicMock()
		self.assertIs(self.run_helper(sess), Repair.ABSENT)
		sess.restore_session.assert_not_called()
		sess.reset_session.assert_not_called()
		self.waking.assert_not_called()


class TestSessionChanged(_Harness):
	def test_re_describe_then_restore_with_the_fresh_session_id(self):
		sess = _Sess(
			describe=[_archived(), _archived(sessionId="sid-2")],
			restore=[_changed()],
		)
		self.assertIs(self.run_helper(sess), Repair.RESTORED)
		self.assertEqual(
			sess.calls,
			[
				("describe", KEY),
				("restore", KEY, SID),
				("describe", KEY),
				("restore", KEY, "sid-2"),
			],
		)
		self.waking.assert_called_once_with()
		self.assertEqual(self.errors, [])

	def test_fresh_row_no_longer_archived_goes_on_without_a_second_restore(self):
		sess = _Sess(describe=[_archived(), _row(sessionId="sid-2")], restore=[_changed()])
		self.assertIs(self.run_helper(sess), Repair.HEALTHY)
		self.assertEqual(sess.names(), ["describe", "restore", "describe"])

	def test_fresh_row_tombstoned_is_unrepairable_without_another_mutation(self):
		for fresh in (
			_tomb(sessionId="sid-2"),
			_archived(sessionId="sid-2", restartRecoveryStatus="tombstoned"),
		):
			with self.subTest(fresh=fresh):
				self.info_lines.clear()
				sess = _Sess(describe=[_archived(), fresh], restore=[_changed()])
				self.assertIs(self.run_helper(sess), Repair.UNREPAIRABLE)
				# The one restore that lost the race, then a describe, and nothing more.
				self.assertEqual(sess.names(), ["describe", "restore", "describe"])
				self.assertIn("step=re-describe", self.info_lines[0])
				self.assertIn("recovery=tombstoned", self.info_lines[0])
		self.assertEqual(self.errors, [])

	def test_fresh_row_gone_is_absent(self):
		sess = _Sess(describe=[_archived(), None], restore=[_changed()])
		self.assertIs(self.run_helper(sess), Repair.ABSENT)
		self.assertEqual(sess.names(), ["describe", "restore", "describe"])

	def test_fresh_row_without_session_id_is_refused(self):
		sess = _Sess(describe=[_archived(), _archived(sessionId=None)], restore=[_changed()])
		self.assertIs(self.run_helper(sess), Repair.REFUSED)
		self.assertEqual(sess.names(), ["describe", "restore", "describe"])

	def test_second_session_changed_is_refused_and_bounded(self):
		sess = _Sess(
			describe=[_archived(), _archived(sessionId="sid-2"), _archived(sessionId="sid-3")],
			restore=[_changed(), _changed()],
		)
		self.assertIs(self.run_helper(sess), Repair.REFUSED)
		# One re-describe and one retry, never a third describe or restore.
		self.assertEqual(sess.names(), ["describe", "restore", "describe", "restore"])
		self.assertEqual(self.titles(), [REFUSED_TITLE])
		self.assertIn("session-changed", self.errors[0]["message"])
		self.assert_no_key_anywhere()

	def test_info_line_reflects_the_fresh_row_after_a_re_describe(self):
		sess = _Sess(
			describe=[
				_archived(),
				_archived(sessionId="sid-2", archiveReason="active-session-cap", restartRecoveryStatus="ok"),
			],
			restore=[_changed()],
		)
		self.assertIs(self.run_helper(sess), Repair.RESTORED)
		line = self.info_lines[0]
		self.assertIn("archive_reason=active-session-cap", line)
		self.assertNotIn("stale-dashboard", line)
		self.assertIn("recovery=ok", line)

	def test_info_line_drops_the_archive_reason_when_the_fresh_row_is_not_archived(self):
		sess = _Sess(describe=[_archived(), _row(sessionId="sid-2")], restore=[_changed()])
		self.run_helper(sess)
		self.assertNotIn("archive_reason=", self.info_lines[0])

	def test_temporary_error_on_the_second_restore_reraises(self):
		err = AgentUnreachableError("sessions.patch timed out", code="ack-timeout")
		sess = _Sess(
			describe=[_archived(), _archived(sessionId="sid-2")],
			restore=[_changed(), err],
		)
		with self.assertRaises(AgentUnreachableError) as cm:
			self.run_helper(sess)
		self.assertIs(cm.exception, err)
		self.assertEqual(sess.names(), ["describe", "restore", "describe", "restore"])
		self.assertEqual(self.errors, [])

	def test_temporary_error_on_the_re_describe_reraises(self):
		boom = AgentUnreachableError("x", code="ack-timeout")
		sess = _Sess(describe=[_archived(), boom], restore=[_changed()])
		with self.assertRaises(AgentUnreachableError) as cm:
			self.run_helper(sess)
		self.assertIs(cm.exception, boom)

	def test_definitive_refusal_on_the_re_describe_is_refused(self):
		sess = _Sess(
			describe=[_archived(), _refusal(method="sessions.describe", code="FORBIDDEN")],
			restore=[_changed()],
		)
		self.assertIs(self.run_helper(sess), Repair.REFUSED)
		self.assertEqual(self.titles(), [REFUSED_TITLE])


class TestGatewayErrors(_Harness):
	TEMPORARY = (
		lambda: AgentUnreachableError("x", code="UNAVAILABLE", rejected_method="sessions.describe"),
		lambda: AgentUnreachableError("sessions.describe timed out", code="ack-timeout"),
		lambda: SendNotStarted("busy", reason="busy"),
		lambda: SendUncertain("write failed"),
		lambda: AgentUnreachableError("transport"),
	)

	def test_temporary_probe_errors_are_failsoft_and_silent(self):
		for make in self.TEMPORARY:
			with self.subTest(err=repr(make())):
				sess = _Sess(describe=[make()])
				self.assertIs(self.run_helper(sess), Repair.FAILSOFT)
				self.assertEqual(sess.names(), ["describe"])
		self.assertEqual(self.errors, [])
		self.waking.assert_not_called()

	def test_rejected_method_with_no_code_or_agent_timeout_is_temporary(self):
		for code in (None, "", "AGENT_TIMEOUT"):
			with self.subTest(code=code):
				err = AgentUnreachableError("x", code=code, rejected_method="sessions.describe")
				sess = _Sess(describe=[err])
				self.assertIs(self.run_helper(sess), Repair.FAILSOFT)
				self.assertEqual(self.errors, [])
				# On the restore it re-raises like any other temporary error.
				err2 = AgentUnreachableError("x", code=code, rejected_method="sessions.patch")
				sess = _Sess(describe=[_archived()], restore=[err2])
				with self.assertRaises(AgentUnreachableError) as cm:
					self.run_helper(sess)
				self.assertIs(cm.exception, err2)
				self.assertEqual(self.errors, [])

	def test_definitive_probe_refusal_is_failsoft_with_a_probe_refused_row(self):
		for code in ("FORBIDDEN", "INVALID_REQUEST", "NOT_PAIRED"):
			with self.subTest(code=code):
				self.errors.clear()
				err = _refusal(method="sessions.describe", code=code, details={"reason": "scope-needed"})
				sess = _Sess(describe=[err])
				self.assertIs(self.run_helper(sess), Repair.FAILSOFT)
				self.assertEqual(sess.names(), ["describe"])
				self.assertEqual(self.titles(), [PROBE_REFUSED_TITLE])
				self.assertEqual(self.errors[0]["message"], f"step=probe code={code} reason=scope-needed")
		self.waking.assert_not_called()
		self.assert_no_key_anywhere()

	def test_temporary_error_on_restore_reraises_the_same_error(self):
		for make in self.TEMPORARY:
			with self.subTest(err=repr(make())):
				err = make()
				sess = _Sess(describe=[_archived()], restore=[err])
				with self.assertRaises(AgentUnreachableError) as cm:
					self.run_helper(sess)
				self.assertIs(cm.exception, err)
				self.assertEqual(sess.names(), ["describe", "restore"])
		self.assertEqual(self.errors, [])

	def test_temporary_error_still_writes_one_info_line(self):
		sess = _Sess(describe=[_archived()], restore=[AgentUnreachableError("x", code="ack-timeout")])
		with self.assertRaises(AgentUnreachableError):
			self.run_helper(sess)
		self.assertEqual(len(self.info_lines), 1)
		self.assertIn("session_repair outcome=unreachable step=restore ", self.info_lines[0])

	def test_definitive_refusal_on_restore_is_refused_with_error_log(self):
		for code in ("INVALID_REQUEST", "FORBIDDEN", "NOT_PAIRED"):
			with self.subTest(code=code):
				self.errors.clear()
				sess = _Sess(describe=[_archived()], restore=[_refusal(code=code)])
				self.assertIs(self.run_helper(sess), Repair.REFUSED)
				self.assertEqual(sess.names(), ["describe", "restore"])
				self.assertEqual(self.titles(), [REFUSED_TITLE])
				msg = self.errors[0]["message"]
				self.assertIn("restore", msg)
				self.assertIn(code, msg)
		self.assert_no_key_anywhere()

	def test_refusal_reason_and_code_are_sanitised_into_the_message(self):
		hostile = _refusal(code="INVALID_REQUEST", details={"reason": f"see {KEY} now\nplease"})
		sess = _Sess(describe=[_archived()], restore=[hostile])
		self.assertIs(self.run_helper(sess), Repair.REFUSED)
		self.assert_no_key_anywhere()
		self.assertNotIn("\n", self.errors[0]["message"])
		self.assertIn("reason=-", self.errors[0]["message"])


class TestNeverRaises(_Harness):
	SECRET = f"boom while holding {KEY}"

	def _assert_failsoft(self, sess, conv=None):
		self.assertIs(self.run_helper(sess, conv), Repair.FAILSOFT)
		self.assertEqual(self.titles(), [FAILED_TITLE])
		self.assert_no_key_anywhere()
		self.assertNotIn("boom", self.errors[0]["message"])
		self.assertEqual(len(self.info_lines), 1)

	def test_unexpected_exception_in_describe(self):
		self._assert_failsoft(_Sess(describe=[RuntimeError(self.SECRET)]))

	def test_unexpected_exception_in_restore(self):
		sess = _Sess(describe=[_archived()], restore=[ValueError(self.SECRET)])
		self._assert_failsoft(sess)
		self.assertEqual(sess.names(), ["describe", "restore"])

	def test_unexpected_exception_in_classification(self):
		class _BadRow(dict):
			def get(self, *a, **k):
				raise TypeError(KEY)

		self._assert_failsoft(_Sess(describe=[_BadRow(sessionId=SID)]))

	def test_unexpected_exception_in_the_session_changed_branch(self):
		sess = _Sess(describe=[_archived(), RuntimeError(self.SECRET)], restore=[_changed()])
		self._assert_failsoft(sess)

	def test_base_exceptions_are_not_swallowed(self):
		# Ctrl-C and interpreter shutdown must still stop the worker.
		for exc in (KeyboardInterrupt(), SystemExit(1)):
			with self.subTest(exc=type(exc).__name__):
				with self.assertRaises(type(exc)):
					self.run_helper(_Sess(describe=[exc]))
		with self.assertRaises(KeyboardInterrupt):
			self.run_helper(_Sess(describe=[_archived()], restore=[KeyboardInterrupt()]))
		self.assertEqual(self.errors, [])

	def test_rq_job_timeout_propagates_from_every_broad_except(self):
		# JobTimeoutException is an Exception subclass, so each swallowing site has to
		# let it through or a timed-out job would carry on past its deadline.
		timeout = JobTimeoutException("Task exceeded maximum timeout value (180 seconds)")
		cases = {
			"describe": lambda: self.run_helper(_Sess(describe=[timeout])),
			"restore": lambda: self.run_helper(_Sess(describe=[_archived()], restore=[timeout])),
			"re-describe": lambda: self.run_helper(
				_Sess(describe=[_archived(), timeout], restore=[_changed()])
			),
			"on_waking": lambda: self.run_helper(
				_Sess(describe=[_archived()]), on_waking=MagicMock(side_effect=timeout)
			),
		}
		for name, call in cases.items():
			with self.subTest(site=name), self.assertRaises(JobTimeoutException) as cm:
				call()
			self.assertIs(cm.exception, timeout)
		self.assertEqual(self.errors, [])

	def test_rq_job_timeout_propagates_from_log_error(self):
		timeout = JobTimeoutException("Task exceeded maximum timeout value (180 seconds)")
		with patch("frappe.log_error", side_effect=timeout), self.assertRaises(JobTimeoutException):
			self.run_helper(_Sess(describe=[_archived()], restore=[_refusal()]))

	def test_job_timeout_is_recognised_by_class_name_when_rq_is_unavailable(self):
		class JobTimeoutException(Exception):
			pass

		with patch.object(session_repair, "_JobTimeout", None):
			self.assertTrue(session_repair._is_job_timeout(JobTimeoutException("x")))
			self.assertFalse(session_repair._is_job_timeout(RuntimeError("x")))
			with self.assertRaises(JobTimeoutException):
				self.run_helper(_Sess(describe=[JobTimeoutException("x")]))

	def test_logging_failures_never_escape(self):
		self.logger.info.side_effect = OSError("disk full")
		with patch("frappe.log_error", side_effect=OSError("db down")):
			self.assertIs(
				self.run_helper(_Sess(describe=[_archived()], restore=[_refusal()])), Repair.REFUSED
			)
			self.assertIs(self.run_helper(_Sess(describe=[RuntimeError("x")])), Repair.FAILSOFT)

	def test_conv_without_a_usable_key_never_raises(self):
		for conv in (
			None,
			SimpleNamespace(),
			SimpleNamespace(session_key=None),
			SimpleNamespace(session_key=5),
		):
			with self.subTest(conv=conv):
				sess = _Sess()
				self.assertIs(
					ensure_session_usable(sess, conv, on_waking=self.waking, path="pump"), Repair.SKIPPED
				)
				self.assertEqual(sess.calls, [])


class TestSkips(_Harness):
	def test_empty_key_is_skipped(self):
		sess = _Sess(describe=[_archived()])
		self.assertIs(self.run_helper(sess, _conv("")), Repair.SKIPPED)
		self.assertEqual(sess.calls, [])

	def test_collab_key_is_skipped(self):
		sess = _Sess(describe=[_archived()])
		key = "agent:main:dashboard:collab:abc"
		self.assertIs(self.run_helper(sess, _conv(key)), Repair.SKIPPED)
		self.assertEqual(sess.calls, [])

	def test_each_off_token_skips_with_zero_calls(self):
		for token in ("", 0, "0", False, "false", "off", "no", " OFF ", "False"):
			with self.subTest(token=token), patch.dict(frappe.conf, {CONF: token}):
				sess = _Sess(describe=[_archived()])
				self.assertIs(self.run_helper(sess), Repair.SKIPPED)
				self.assertEqual(sess.calls, [])
				self.assertFalse(session_repair.session_repair_enabled())

	def test_on_tokens_and_absent_probe(self):
		for token in (1, "1", True, "true", "on", "yes"):
			with self.subTest(token=token), patch.dict(frappe.conf, {CONF: token}):
				sess = _Sess(describe=[_row()])
				self.assertIs(self.run_helper(sess), Repair.HEALTHY)
				self.assertEqual(sess.names(), ["describe"])
		with patch.dict(frappe.conf, {}):
			frappe.conf.pop(CONF, None)
			self.assertTrue(session_repair.session_repair_enabled())
			sess = _Sess(describe=[_row()])
			self.assertIs(self.run_helper(sess), Repair.HEALTHY)
			self.assertEqual(sess.names(), ["describe"])

	def test_skip_still_logs_one_info_line(self):
		self.run_helper(_Sess(), _conv(""))
		self.assertEqual(len(self.info_lines), 1)
		self.assertIn("outcome=skipped", self.info_lines[0])


class TestLogging(_Harness):
	def test_info_line_format(self):
		self.run_helper(_Sess(describe=[_archived()]), path="legacy", run_id="run-9")
		self.assertEqual(len(self.info_lines), 1)
		line = self.info_lines[0]
		self.assertRegex(
			line,
			r"^session_repair outcome=restored step=restore probe_ms=\d+ repair_ms=\d+ path=legacy "
			r"run_id=run-9 archive_reason=stale-dashboard$",
		)
		self.logger.info.assert_called_once()

	def test_info_line_carries_recovery_status_and_handles_missing_run_id(self):
		self.run_helper(_Sess(describe=[_tomb()]), run_id=None)
		line = self.info_lines[0]
		self.assertIn("outcome=unrepairable", line)
		self.assertIn("run_id=-", line)
		self.assertIn("recovery=tombstoned", line)

	def test_step_names_the_step_that_decided_the_outcome(self):
		cases = [
			(_Sess(describe=[RuntimeError("x")]), "failsoft", "probe"),
			(_Sess(describe=[None]), "absent", "probe"),
			(_Sess(describe=[_row()]), "healthy", "classify"),
			(_Sess(describe=[_tomb()]), "unrepairable", "classify"),
			(_Sess(describe=[_archived(sessionId=None)]), "refused", "classify"),
			(_Sess(describe=[_archived()]), "restored", "restore"),
			(_Sess(describe=[_archived()], restore=[_refusal()]), "refused", "restore"),
			(_Sess(describe=[_archived(), None], restore=[_changed()]), "absent", "re-describe"),
			(
				_Sess(describe=[_archived(), _archived(sessionId="s2")], restore=[_changed(), _changed()]),
				"refused",
				"restore",
			),
		]
		for sess, outcome, step in cases:
			with self.subTest(outcome=outcome, step=step):
				self.info_lines.clear()
				self.run_helper(sess)
				self.assertIn(f"outcome={outcome} step={step} ", self.info_lines[0])

	def test_repair_ms_covers_the_whole_call_and_probe_ms_only_the_describe(self):
		clock = iter([100.0, 100.0, 100.01, 100.5])  # call start, probe start, probe end, call end
		with patch.object(session_repair, "time", SimpleNamespace(monotonic=lambda: next(clock))):
			self.run_helper(_Sess(describe=[_row()]))
		self.assertIn("probe_ms=10 repair_ms=500 ", self.info_lines[0])

	def test_one_line_per_call_for_every_outcome(self):
		cases = [
			_Sess(describe=[_row()]),
			_Sess(describe=[None]),
			_Sess(describe=[_archived()]),
			_Sess(describe=[RuntimeError("x")]),
			_Sess(describe=[_archived()], restore=[_refusal()]),
		]
		for sess in cases:
			self.info_lines.clear()
			self.run_helper(sess)
			self.assertEqual(len(self.info_lines), 1)

	def test_gateway_supplied_fields_cannot_smuggle_the_key_into_the_line(self):
		self.run_helper(_Sess(describe=[_archived(archiveReason=KEY)]))
		self.assert_no_key_anywhere()
		self.assertEqual(len(self.info_lines), 1)

	def test_no_key_in_any_line_across_all_outcomes(self):
		cases = [
			_Sess(describe=[_row()]),
			_Sess(describe=[_archived()]),
			_Sess(describe=[_tomb()]),
			_Sess(describe=[_archived()], restore=[_refusal()]),
			_Sess(describe=[_refusal(method="sessions.describe", code="FORBIDDEN")]),
			_Sess(describe=[RuntimeError(KEY)]),
			_Sess(describe=[_archived(), _archived(sessionId="s2")], restore=[_changed(), _changed()]),
		]
		for sess in cases:
			self.run_helper(sess)
		self.assertTrue(self.info_lines)
		self.assert_no_key_anywhere()

	def test_repair_values_are_stable_strings(self):
		self.assertEqual(
			{r.value for r in Repair},
			{"skipped", "healthy", "absent", "restored", "unrepairable", "refused", "failsoft"},
		)
		self.assertEqual(Repair.UNREPAIRABLE, "unrepairable")
		self.assertFalse(hasattr(Repair, "RESET"))
		self.assertFalse(hasattr(session_repair, "CONTEXT_LOST_LINE"))


class TestWireOrder(_Harness):
	"""Through a real AgentSession over the scripted websocket: the frames the
	gateway would see, in order."""

	def _methods(self, sess):
		return [(f["method"], f["params"]) for f in sess._ws.sent]

	def test_archived_sends_describe_then_patch(self):
		sess = _session_with_scripted_frames(
			[
				{"type": "res", "ok": True, "payload": {"session": _archived()}},
				{"type": "res", "ok": True, "payload": {}},
			]
		)
		self.assertIs(self.run_helper(sess), Repair.RESTORED)
		self.assertEqual(
			self._methods(sess),
			[
				("sessions.describe", {"key": KEY}),
				("sessions.patch", {"key": KEY, "archived": False, "expectedSessionId": SID}),
			],
		)
		self.waking.assert_called_once_with()
		self.assert_no_key_anywhere()

	def test_tombstoned_sends_only_the_describe(self):
		for row in (_tomb(), _archived(restartRecoveryStatus="tombstoned")):
			with self.subTest(row=row):
				sess = _session_with_scripted_frames(
					[{"type": "res", "ok": True, "payload": {"session": row}}]
				)
				self.assertIs(self.run_helper(sess), Repair.UNREPAIRABLE)
				self.assertEqual(self._methods(sess), [("sessions.describe", {"key": KEY})])

	def test_session_changed_over_the_wire_re_describes_once(self):
		changed = {
			"type": "res",
			"ok": False,
			"error": {"code": "INVALID_REQUEST", "message": "x", "details": {"reason": "session-changed"}},
		}
		sess = _session_with_scripted_frames(
			[
				{"type": "res", "ok": True, "payload": {"session": _archived()}},
				changed,
				{"type": "res", "ok": True, "payload": {"session": _archived(sessionId="sid-2")}},
				{"type": "res", "ok": True, "payload": {}},
			]
		)
		self.assertIs(self.run_helper(sess), Repair.RESTORED)
		self.assertEqual(
			[m for m, _ in self._methods(sess)],
			["sessions.describe", "sessions.patch", "sessions.describe", "sessions.patch"],
		)
		self.assertEqual(self._methods(sess)[-1][1]["expectedSessionId"], "sid-2")

	def test_healthy_and_absent_send_exactly_one_frame(self):
		for payload in ({"session": _row()}, {"session": None}):
			with self.subTest(payload=payload):
				sess = _session_with_scripted_frames([{"type": "res", "ok": True, "payload": payload}])
				self.run_helper(sess)
				self.assertEqual([m for m, _ in self._methods(sess)], ["sessions.describe"])

	def test_gateway_refusal_over_the_wire_is_refused(self):
		sess = _session_with_scripted_frames(
			[
				{"type": "res", "ok": True, "payload": {"session": _archived()}},
				{"type": "res", "ok": False, "error": {"code": "INVALID_REQUEST", "message": "no"}},
			]
		)
		self.assertIs(self.run_helper(sess), Repair.REFUSED)
		self.assertEqual(self.titles(), [REFUSED_TITLE])

	def test_ack_timeout_over_the_wire_on_restore_reraises(self):
		# No second frame is scripted, so sessions.patch never gets a reply.
		sess = _session_with_scripted_frames(
			[{"type": "res", "ok": True, "payload": {"session": _archived()}}]
		)
		sess._request = _fast(sess._request)
		with self.assertRaises(AgentUnreachableError) as cm:
			self.run_helper(sess)
		self.assertEqual(cm.exception.code, "ack-timeout")
		self.assertEqual([m for m, _ in self._methods(sess)], ["sessions.describe", "sessions.patch"])


def _fast(real):
	"""Shrink every request's timeout so a missing reply fails quickly."""

	def _req(method, params, *, timeout_s, **kw):
		return real(method, params, timeout_s=min(timeout_s, 0.05), **kw)

	return _req
