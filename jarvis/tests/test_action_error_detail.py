"""Enriched action-error envelope + apply_action contract (2026-07-10).

A failed ERP write used to surface as a flat, misleading "permission denied":
Frappe raises a BARE ``PermissionError`` (``str(e) == ""``) with the human text
in ``frappe.flags.error_message`` and the specific blocker (e.g. a User
Permission on a link value) msgprinted into ``message_log`` - all of which the
old ``_dispatch_and_wrap`` discarded. These tests pin:

- ``err()`` stays 2-key by default, gains optional ``detail``/``hint``;
- ``_translate_write_error`` harvests the safe reason (and dedupes/masks);
- ``apply_action`` returns the SAME ``{ok:false, error}`` envelope (not a raw
  403) and rolls back so "No changes were saved" is truthful.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis._responses import err
from jarvis.chat.actions_api import apply_action
from jarvis.exceptions import PermissionDeniedError

# Frappe's User-Permission link denial, verbatim shape (permissions.py:445).
_USER_PERM_REASON = (
	"You are not allowed to access this Timesheet record because it is linked to "
	"Employee 'HR-EMP-00007' in field Employee"
)


class TestErrEnvelope(FrappeTestCase):
	def test_err_backward_compatible_shape(self):
		# Un-enriched failures keep the exact 2-key shape existing consumers
		# (admin_client, the agent transcript, tests) branch on.
		self.assertEqual(err("X", "y"), {"ok": False, "error": {"code": "X", "message": "y"}})

	def test_err_adds_detail_and_hint_only_when_present(self):
		self.assertEqual(
			err("X", "y", detail="d", hint="h")["error"],
			{"code": "X", "message": "y", "detail": "d", "hint": "h"},
		)
		self.assertNotIn("detail", err("X", "y", detail="", hint="")["error"])
		self.assertNotIn("hint", err("X", "y", detail="", hint="")["error"])


class TestTranslateWriteError(FrappeTestCase):
	def setUp(self):
		frappe.local.message_log = []
		frappe.flags.pop("error_message", None)
		self.addCleanup(lambda: frappe.flags.pop("error_message", None))
		self.addCleanup(lambda: setattr(frappe.local, "message_log", []))

	def test_record_level_permission_enriched_from_message_log_and_flags(self):
		mark = api._msglog_mark()
		frappe.msgprint(_USER_PERM_REASON)
		frappe.flags.error_message = (
			"You need the 'create' permission on <b>Timesheet</b> to perform this action."
		)
		env = api._translate_write_error(frappe.PermissionError(), mark)
		self.assertFalse(env["ok"])
		e = env["error"]
		self.assertEqual(e["code"], "PermissionDeniedError")
		# message promoted from flags (a bare PermissionError has str(e) == "")...
		self.assertIn("create' permission", e["message"])
		self.assertNotIn("<b>", e["message"])  # ...HTML-stripped
		# detail = the specific User-Permission blocker
		self.assertIn("linked to Employee", e["detail"])
		# and the User-Permission-specific hint (not the generic one)
		self.assertIn("User Permission", e["hint"])
		# harvested entry popped so it can't ALSO ride out as _server_messages
		self.assertEqual(api._msglog_mark(), mark)

	def test_doctype_level_denial_masked_no_detail(self):
		# Frappe clears message_log when the user lacks doctype access, so nothing
		# is harvested -> no detail leaked, but flags still informs `message`.
		mark = api._msglog_mark()
		frappe.flags.error_message = (
			"You need the 'create' permission on <b>Secret DocType</b> to perform this action."
		)
		env = api._translate_write_error(frappe.PermissionError(), mark)
		self.assertEqual(env["error"]["code"], "PermissionDeniedError")
		self.assertNotIn("detail", env["error"])
		self.assertIn("create' permission", env["error"]["message"])

	def test_validation_detail_deduped_when_equal_to_message(self):
		mark = api._msglog_mark()
		msg = "Value missing for Timesheet: Employee"
		frappe.msgprint(msg)
		env = api._translate_write_error(frappe.ValidationError(msg), mark)
		self.assertEqual(env["error"]["code"], "InvalidArgumentError")
		self.assertEqual(env["error"]["message"], msg)
		self.assertNotIn("detail", env["error"])  # would just repeat the message

	def test_validation_keeps_distinct_detail(self):
		mark = api._msglog_mark()
		frappe.msgprint("Employee is a mandatory field")
		env = api._translate_write_error(frappe.ValidationError("Could not save the timesheet"), mark)
		self.assertEqual(env["error"]["code"], "InvalidArgumentError")
		self.assertIn("mandatory", env["error"]["detail"])

	def test_html_stripped_from_str_e_message(self):
		# frappe.throw("... <b>Employee</b> ...") puts literal tags in str(e); the
		# card message must be clean text, not markup.
		mark = api._msglog_mark()
		env = api._translate_write_error(
			frappe.ValidationError("Please set <b>Employee</b> before saving"), mark
		)
		self.assertNotIn("<b>", env["error"]["message"])
		self.assertIn("Employee", env["error"]["message"])

	def test_duplicate_entry_gets_clean_message(self):
		# DuplicateEntryError's str(e) is a (doctype, name, IntegrityError) repr -
		# never surface the internal driver/table text.
		mark = api._msglog_mark()
		env = api._translate_write_error(
			frappe.DuplicateEntryError("Customer", "ACME", ValueError("1062 duplicate key")), mark
		)
		self.assertEqual(env["error"]["code"], "InvalidArgumentError")
		self.assertIn("already exists", env["error"]["message"])
		self.assertIn("Customer", env["error"]["message"])
		self.assertNotIn("1062", env["error"]["message"])
		self.assertNotIn("IntegrityError", env["error"]["message"])

	def test_jarvis_error_passthrough(self):
		env = api._translate_write_error(
			PermissionDeniedError("no create permission on ToDo"), api._msglog_mark()
		)
		self.assertEqual(env["error"]["code"], "PermissionDeniedError")
		self.assertEqual(env["error"]["message"], "no create permission on ToDo")

	def test_unexpected_exception_returns_none(self):
		# None signals the caller to re-raise -> a real bug stays a 500, never
		# enveloped, never leaks a traceback.
		self.assertIsNone(api._translate_write_error(ValueError("boom"), api._msglog_mark()))

	def test_outgoing_email_error_preserves_setup_message(self):
		message = "Please setup default outgoing <b>Email Account</b>"
		mark = api._msglog_mark()
		frappe.msgprint(message)
		env = api._translate_write_error(frappe.OutgoingEmailError(message), mark)
		self.assertIsNotNone(env)
		self.assertFalse(env["ok"])
		self.assertEqual(env["error"]["code"], "OutgoingEmailError")
		self.assertEqual(env["error"]["message"], "Please setup default outgoing Email Account")
		self.assertIn("administrator", env["error"]["hint"])
		self.assertIn("Email Account", env["error"]["hint"])
		self.assertNotIn("detail", env["error"])
		self.assertEqual(api._msglog_mark(), mark)

	def test_outgoing_email_error_without_message_has_a_useful_fallback(self):
		env = api._translate_write_error(frappe.OutgoingEmailError(), api._msglog_mark())
		self.assertIsNotNone(env)
		self.assertEqual(env["error"]["message"], "Outgoing email is not configured.")


class TestDispatchAndWrapEnrichment(FrappeTestCase):
	def setUp(self):
		frappe.local.message_log = []
		frappe.flags.pop("error_message", None)
		self.addCleanup(lambda: frappe.flags.pop("error_message", None))
		self.addCleanup(lambda: setattr(frappe.local, "message_log", []))

	def test_missing_email_account_surfaces_after_single_and_batch_confirmation(self):
		from frappe.core.doctype.communication.communication import Communication

		from jarvis.chat.actions_api import _dispatch_call

		for batch in (False, True):
			with self.subTest(batch=batch):
				doc = frappe.get_doc({"doctype": "ToDo", "description": "email-error-test"}).insert()
				self.addCleanup(lambda name=doc.name: frappe.db.delete("ToDo", {"name": name}))
				message = {
					"doctype": "ToDo",
					"name": doc.name,
					"recipients": ["first@example.invalid", "second@example.invalid"],
					"subject": f"Email error test {doc.name}",
					"content": "Synthetic email; delivery is forbidden.",
				}
				args = {"messages": [message, dict(message)]} if batch else message
				frappe.publish_realtime("email-test-prior", {"marker": doc.name}, after_commit=True)
				prior_events = list(frappe.local._realtime_log)
				with (
					patch.object(Communication, "get_outgoing_email_account", return_value=None),
					patch.object(Communication, "send_email") as send,
					patch("frappe.sendmail", side_effect=AssertionError("real email forbidden")),
					patch("frappe.enqueue", return_value=None),
					patch("frappe.log_error") as crash_log,
					patch("jarvis.api.audit.record") as audit,
					patch("jarvis.agent_audit.record_write"),
				):
					env = _dispatch_call(
						{"tool": "send_email", "args": args, "exec_user": frappe.session.user},
						"test-email-token",
						None,
						"confirm dispatch crashed",
					)
				self.assertFalse(env["ok"])
				self.assertEqual(env["error"]["code"], "OutgoingEmailError")
				self.assertIn("missing email account", env["error"]["message"])
				self.assertIn("Email Account", env["error"]["hint"])
				self.assertFalse(frappe.db.exists("Communication", {"subject": message["subject"]}))
				self.assertTrue(frappe.db.exists("ToDo", doc.name))
				self.assertEqual(frappe.local._realtime_log, prior_events)
				send.assert_not_called()
				crash_log.assert_not_called()
				self.assertFalse(audit.call_args.kwargs["ok"])
				self.assertEqual(audit.call_args.kwargs["error_code"], "OutgoingEmailError")

	def test_known_failure_restores_callbacks_and_an_absent_realtime_log(self):
		from frappe.realtime import clear_realtime_log

		clear_realtime_log()
		queues = ("before_commit", "after_commit", "before_rollback", "after_rollback")
		before = {name: tuple(getattr(frappe.db, name)._functions) for name in queues}

		def fail(tool, args):
			for name in queues:
				getattr(frappe.db, name).add(lambda: None)
			frappe.publish_realtime("failed-email-event", {}, after_commit=True)
			raise frappe.OutgoingEmailError("No outgoing email account")

		with (
			patch("jarvis.api.dispatch", side_effect=fail),
			patch("jarvis.api.audit.record"),
			patch("jarvis.agent_audit.record_write"),
		):
			env = api.dispatch_confirmed("send_email", {})
		self.assertFalse(env["ok"])
		self.assertFalse(hasattr(frappe.local, "_realtime_log"))
		for name in queues:
			self.assertEqual(tuple(getattr(frappe.db, name)._functions), before[name])

	def test_permission_failure_enriched_via_dispatch(self):
		def fake(tool, args):
			frappe.msgprint(
				"You are not allowed to access this Sales Invoice record because it "
				"is linked to Company 'ACME' in field Company"
			)
			frappe.flags.error_message = (
				"You need the 'write' permission on <b>Sales Invoice</b> to perform this action."
			)
			raise frappe.PermissionError()

		with patch("jarvis.api.dispatch", side_effect=fake):
			env = api._dispatch_and_wrap(
				"update_doc", {"doctype": "Sales Invoice", "name": "x"}, is_write=True
			)
		self.assertFalse(env["ok"])
		self.assertEqual(env["error"]["code"], "PermissionDeniedError")
		self.assertIn("linked to Company", env["error"]["detail"])
		self.assertIn("User Permission", env["error"]["hint"])

	def test_unexpected_exception_reraised(self):
		with patch("jarvis.api.dispatch", side_effect=ValueError("boom")):
			with self.assertRaises(ValueError):
				api._dispatch_and_wrap("update_doc", {"doctype": "X"}, is_write=True)

	def test_savepoint_rolls_back_partial_write_on_known_failure(self):
		# A tool that half-applied (wrote a row) before raising a KNOWN error must
		# be undone before the {ok:false} envelope is returned - else Frappe's
		# end-of-request commit would persist the partial write (e.g. a submit
		# whose on_submit hook throws after docstatus=1 was written).
		marker = "jarvis-sp-rollback-marker"
		frappe.local.conf["disable_global_search"] = 1
		self.addCleanup(lambda: frappe.local.conf.pop("disable_global_search", None))

		def half_apply(tool, args):
			frappe.get_doc({"doctype": "ToDo", "description": marker}).insert(ignore_permissions=True)
			raise frappe.ValidationError("blew up after writing")

		with patch("jarvis.api.dispatch", side_effect=half_apply):
			env = api._dispatch_and_wrap("update_doc", {"doctype": "ToDo"}, is_write=True)
		self.assertFalse(env["ok"])
		self.assertEqual(env["error"]["code"], "InvalidArgumentError")
		self.assertFalse(frappe.db.exists("ToDo", {"description": marker}))

	def test_savepoint_keeps_write_on_success(self):
		# The savepoint must NOT undo a successful write (it is released, not
		# rolled back) - the row persists in the transaction as before.
		marker = "jarvis-sp-success-marker"
		frappe.local.conf["disable_global_search"] = 1
		self.addCleanup(lambda: frappe.local.conf.pop("disable_global_search", None))
		self.addCleanup(
			lambda: frappe.db.exists("ToDo", {"description": marker})
			and frappe.db.delete("ToDo", {"description": marker})
		)

		def apply_ok(tool, args):
			doc = frappe.get_doc({"doctype": "ToDo", "description": marker}).insert(ignore_permissions=True)
			return {"name": doc.name}

		with patch("jarvis.api.dispatch", side_effect=apply_ok):
			env = api._dispatch_and_wrap("update_doc", {"doctype": "ToDo"}, is_write=True)
		self.assertTrue(env["ok"])
		self.assertTrue(frappe.db.exists("ToDo", {"description": marker}))


class TestApplyActionContract(FrappeTestCase):
	def setUp(self):
		frappe.flags.pop("error_message", None)
		self.addCleanup(lambda: frappe.flags.pop("error_message", None))

	def _conv(self) -> str:
		conv = frappe.get_doc(
			{
				"doctype": "Jarvis Conversation",
				"title": "err-ux test",
			}
		).insert(ignore_permissions=True)
		# Tolerant cleanup: a real rollback under test may have already dropped it.
		self.addCleanup(
			lambda: frappe.db.exists("Jarvis Conversation", conv.name)
			and frappe.delete_doc("Jarvis Conversation", conv.name, force=True, ignore_permissions=True)
		)
		return conv.name

	def test_failed_create_returns_envelope_not_raise(self):
		def boom(doctype, values):
			frappe.flags.error_message = (
				"You need the 'create' permission on <b>ToDo</b> to perform this action."
			)
			raise frappe.PermissionError()

		with (
			patch("jarvis.tools.create_doc.create_doc", side_effect=boom),
			patch.object(frappe.db, "rollback") as rb,
		):
			r = apply_action(
				frappe.as_json(
					{
						"verb": "create",
						"doctype": "ToDo",
						"values": {"description": "should not persist"},
						"conversation": self._conv(),
					}
				)
			)
		self.assertFalse(r["ok"])  # enveloped, NOT a raised 403
		self.assertEqual(r["error"]["code"], "PermissionDeniedError")
		self.assertIn("create' permission", r["error"]["message"])
		self.assertTrue(rb.called)  # rolled back -> nothing saved

	def test_failed_create_audited_as_failure(self):
		with (
			patch("jarvis.tools.create_doc.create_doc", side_effect=frappe.PermissionError()),
			patch.object(frappe.db, "rollback"),
			patch("jarvis.chat.actions_api.audit.record") as rec,
		):
			apply_action(
				frappe.as_json(
					{
						"verb": "create",
						"doctype": "ToDo",
						"values": {"description": "x"},
						"conversation": self._conv(),
					}
				)
			)
		self.assertTrue(rec.called)
		self.assertFalse(rec.call_args.kwargs["ok"])
		self.assertIn("apply_action", rec.call_args.kwargs["tool"])

	def test_unexpected_error_reraises(self):
		with (
			patch("jarvis.tools.create_doc.create_doc", side_effect=ValueError("boom")),
			patch.object(frappe.db, "rollback"),
		):
			with self.assertRaises(ValueError):
				apply_action(
					frappe.as_json(
						{
							"verb": "create",
							"doctype": "ToDo",
							"values": {"description": "x"},
							"conversation": self._conv(),
						}
					)
				)

	def test_create_then_submit_failure_rolls_back_the_create(self):
		# The multi-tool case: create succeeds (a real row is inserted), submit
		# fails -> apply_action rolls back the WHOLE thing (REAL rollback here) so
		# the SPA's "No changes were saved" line is truthful. submit_doc is mocked
		# to fail so the create is the only real write, and we assert it vanished.
		marker = "err-ux-rollback-marker-xyz"
		self.assertFalse(frappe.db.exists("ToDo", {"description": marker}))
		# global_search's queue path asserts in tests on an unseeded site; skip it
		# (built-in conf guard) so the real insert -> rollback effect runs cleanly.
		frappe.local.conf["disable_global_search"] = 1
		self.addCleanup(lambda: frappe.local.conf.pop("disable_global_search", None))
		with patch(
			"jarvis.tools.submit_doc.submit_doc", side_effect=frappe.ValidationError("submit blocked")
		):
			r = apply_action(
				frappe.as_json(
					{
						"verb": "create",
						"doctype": "ToDo",
						"values": {"description": marker},
						"submit": 1,
						"conversation": self._conv(),
					}
				)
			)
		self.assertFalse(r["ok"])
		self.assertEqual(r["error"]["code"], "InvalidArgumentError")
		# the successful create was undone - nothing persisted
		self.assertFalse(frappe.db.exists("ToDo", {"description": marker}))
