"""Tests for request-scoped "confirm all" (design Layer B, action-card overhaul).

A typed "confirm all" / "do everything" arms ``request_autorun`` for the CURRENT
request's fan-out: covered writes then run uncarded, the BRAKE still cards, a new
top-level human message resets it, the first error re-cards, Halt / stop_run clears
it, and a stranded flag falls out past the sliding TTL (inline gate + hourly reaper).
The flag is unforgeable via a generic save (controller guard); only the server's raw
arm sets it.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import api as chat_api
from jarvis.chat import approval_phrases, pending_confirm
from jarvis.tests._conv_helpers import (
	CONV,
	NON_ADMIN_USER,
	_ensure_non_admin_user,
	_make_conv,
)
from jarvis.tests.test_chat_api import (
	TEST_USER,
	_cleanup_user_conversations,
	_ensure_test_user,
)


def _flag(conv: str) -> int:
	return int(frappe.db.get_value(CONV, conv, "request_autorun") or 0)


class TestConfirmAllDirective(FrappeTestCase):
	"""The upfront detector (approval_phrases.contains_confirm_all_directive): a
	compound message that bundles a request with 'confirm all' arms the run; a plain
	request, a question, or a negation does not."""

	def test_detects_compound_directive(self):
		self.assertTrue(
			approval_phrases.contains_confirm_all_directive("create 3 todos and submit them, confirm all")
		)
		self.assertTrue(approval_phrases.contains_confirm_all_directive("please just do everything"))
		self.assertTrue(approval_phrases.contains_confirm_all_directive("make the invoices, approve all"))

	def test_ignores_question_and_negation(self):
		self.assertFalse(approval_phrases.contains_confirm_all_directive("should I confirm all?"))
		self.assertFalse(approval_phrases.contains_confirm_all_directive("don't do everything"))
		self.assertFalse(approval_phrases.contains_confirm_all_directive("do not confirm all of them"))

	def test_plain_request_is_not_a_directive(self):
		self.assertFalse(approval_phrases.contains_confirm_all_directive("create a todo for me"))
		self.assertFalse(approval_phrases.contains_confirm_all_directive(""))


class TestRequestAutorunGuard(FrappeTestCase):
	"""AC-req-guard: the flag is owner-writable with no permlevel, so a non-admin owner
	must not flip it 0 -> 1 through a generic save; only the server's raw arm does."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_non_admin_user()

	def setUp(self):
		self._orig = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._orig)
		for conv in frappe.get_all(CONV, filters={"owner": NON_ADMIN_USER}, pluck="name"):
			frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_non_admin_owner_save_cannot_arm(self):
		conv = _make_conv(NON_ADMIN_USER)
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(CONV, conv)
		doc.request_autorun = 1
		with self.assertRaises(frappe.PermissionError):
			doc.save()
		self.assertEqual(_flag(conv), 0)

	def test_raw_arm_bypasses_the_guard(self):
		conv = _make_conv(NON_ADMIN_USER)
		api._request_autorun_arm(conv, "")
		self.assertEqual(_flag(conv), 1)

	def test_disable_is_always_allowed_for_owner(self):
		conv = _make_conv(NON_ADMIN_USER)
		api._request_autorun_arm(conv, "")  # server-armed
		frappe.set_user(NON_ADMIN_USER)
		doc = frappe.get_doc(CONV, conv)
		doc.request_autorun = 0
		doc.save()  # 1 -> 0 must not raise
		self.assertEqual(_flag(conv), 0)


class TestRequestAutorunGate(FrappeTestCase):
	"""The gate branch: an armed request runs COVERED writes uncarded (provenance
	'request'), the BRAKE still parks, a stale TTL re-cards, the first ok:False and Halt
	clear the flag, and a covered success slides the timestamp."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		from jarvis.tools import _agent_run_ctx

		_agent_run_ctx.take_request_autorun_applied()  # clear any test-set marker
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
			pending_confirm.clear_for_conversation(TEST_USER, conv)
			frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		for name in frappe.get_all("ToDo", filters={"owner": TEST_USER}, pluck="name"):
			frappe.delete_doc("ToDo", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_armed_covered_write_runs_uncarded(self):
		conv = _make_conv(TEST_USER)
		api._request_autorun_arm(conv, "")
		with patch("jarvis.api.dispatch_confirmed", return_value={"ok": True, "data": {}}) as disp:
			r = api._run_tool(
				"create_doc",
				{"doctype": "ToDo", "values": {"description": "ra-uncarded"}},
				conversation=conv,
			)
		self.assertTrue(disp.called, "a covered write runs uncarded when the request is armed")
		self.assertEqual(disp.call_args.kwargs.get("provenance"), "request")
		self.assertNotEqual((r.get("data") or {}).get("status"), "pending_confirmation")

	def test_armed_brake_still_parks(self):
		conv = _make_conv(TEST_USER)
		api._request_autorun_arm(conv, "")
		todo = frappe.get_doc({"doctype": "ToDo", "description": "ra-brake"}).insert(ignore_permissions=True)
		with patch("jarvis.api.dispatch_confirmed") as disp:
			r = api._run_tool("delete_doc", {"doctype": "ToDo", "name": todo.name}, conversation=conv)
		self.assertEqual(r["data"]["status"], "pending_confirmation", "a brake tool still cards")
		self.assertFalse(disp.called)

	def test_expired_ttl_recards(self):
		conv = _make_conv(TEST_USER)
		api._request_autorun_arm(conv, "")
		stale = frappe.utils.add_to_date(
			frappe.utils.now_datetime(), seconds=-(api._REQUEST_AUTORUN_TTL_S + 60)
		)
		frappe.db.set_value(CONV, conv, "request_autorun_at", stale, update_modified=False)
		with patch("jarvis.api.dispatch_confirmed") as disp:
			r = api._run_tool(
				"create_doc",
				{"doctype": "ToDo", "values": {"description": "ra-ttl"}},
				conversation=conv,
			)
		self.assertEqual(r["data"]["status"], "pending_confirmation", "a stale flag re-cards")
		self.assertFalse(disp.called)

	def test_first_error_clears_the_flag(self):
		conv = _make_conv(TEST_USER)
		api._request_autorun_arm(conv, "")
		with patch("jarvis.api.dispatch_confirmed", return_value={"ok": False, "error": {"message": "boom"}}):
			r = api._run_tool(
				"create_doc",
				{"doctype": "ToDo", "values": {"description": "ra-err"}},
				conversation=conv,
			)
		self.assertFalse(r["ok"])
		self.assertIn("re-approve", r["error"]["message"])
		self.assertEqual(_flag(conv), 0, "the first covered ok:False ends the request run")

	def test_halt_cancel_gate_clears_the_flag(self):
		from jarvis.chat import turn_message_binding

		conv = _make_conv(TEST_USER)
		api._request_autorun_arm(conv, "")
		turn_message_binding.request_run_cancel(conv)
		try:
			with patch("jarvis.api.dispatch_confirmed") as disp:
				r = api._run_tool(
					"create_doc",
					{"doctype": "ToDo", "values": {"description": "ra-halt"}},
					conversation=conv,
				)
			self.assertFalse(r["ok"])
			self.assertFalse(disp.called, "Halt refuses the covered write without executing")
			self.assertEqual(_flag(conv), 0)
		finally:
			turn_message_binding.clear_run_cancel(conv)

	def test_covered_success_slides_the_timestamp(self):
		conv = _make_conv(TEST_USER)
		api._request_autorun_arm(conv, "")
		within = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-120)
		frappe.db.set_value(CONV, conv, "request_autorun_at", within, update_modified=False)
		with patch("jarvis.api.dispatch_confirmed", return_value={"ok": True, "data": {}}):
			api._run_tool(
				"create_doc",
				{"doctype": "ToDo", "values": {"description": "ra-slide"}},
				conversation=conv,
			)
		new_at = frappe.utils.get_datetime(frappe.db.get_value(CONV, conv, "request_autorun_at"))
		self.assertGreater(new_at, frappe.utils.get_datetime(within), "a covered write slides the TTL")
		self.assertEqual(_flag(conv), 1, "a successful covered write keeps the run armed")


class TestRequestAutorunSendMessage(FrappeTestCase):
	"""Reset + upfront arm through send_message: a genuine new top-level message clears a
	prior request's flag; a compound 'confirm all' send arms this request; a reset then
	re-arm on one message lands armed (the arm rides AFTER the save's reset)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		_cleanup_user_conversations(TEST_USER)
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
			pending_confirm.clear_for_conversation(TEST_USER, conv)
			frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _send(self, conv, message):
		from jarvis.tests._transport_helpers import provision_legacy_site

		provision_legacy_site(self)
		with patch("jarvis.chat.api._ensure_session_key", return_value="agent:fake"):
			with patch("frappe.enqueue"):
				return chat_api.send_message(conv, message)

	def test_new_top_level_message_resets_the_flag(self):
		conv = _make_conv(TEST_USER)
		api._request_autorun_arm(conv, "")
		res = self._send(conv, "just create one todo please")  # no directive
		self.assertTrue(res["ok"])
		self.assertEqual(_flag(conv), 0, "a genuine new message ends the prior request run")

	def test_upfront_confirm_all_arms_the_flag(self):
		conv = _make_conv(TEST_USER)
		self.assertEqual(_flag(conv), 0)
		res = self._send(conv, "create 3 todos and submit them, confirm all")
		self.assertTrue(res["ok"])
		self.assertEqual(_flag(conv), 1, "an upfront 'confirm all' arms the request run")

	def test_reset_then_rearm_on_one_message_lands_armed(self):
		conv = _make_conv(TEST_USER)
		api._request_autorun_arm(conv, "prior")  # a prior request left it armed
		res = self._send(conv, "now do everything for this new batch")
		self.assertTrue(res["ok"])
		self.assertEqual(_flag(conv), 1, "the upfront arm rides AFTER the save's reset")

	def test_plain_message_leaves_it_disarmed(self):
		conv = _make_conv(TEST_USER)
		res = self._send(conv, "what is the total outstanding?")
		self.assertTrue(res["ok"])
		self.assertEqual(_flag(conv), 0)


class TestRequestAutorunStopRun(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
			pending_confirm.clear_for_conversation(TEST_USER, conv)
			frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_stop_run_clears_the_flag(self):
		conv = _make_conv(TEST_USER)
		api._request_autorun_arm(conv, "")
		chat_api.stop_run(conv)  # no session_key -> returns after the sweep that clears it
		self.assertEqual(_flag(conv), 0, "Halt / stop_run ends the request run")


class TestRequestAutorunReaper(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)

	def tearDown(self):
		frappe.set_user(self._orig)
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
			frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_reaper_clears_a_stranded_flag(self):
		from jarvis.chat import session_lifecycle

		conv = _make_conv(TEST_USER)
		api._request_autorun_arm(conv, "")
		stranded = frappe.utils.add_to_date(
			frappe.utils.now_datetime(), seconds=-(3 * api._REQUEST_AUTORUN_TTL_S)
		)
		frappe.db.set_value(CONV, conv, "request_autorun_at", stranded, update_modified=False)
		frappe.db.commit()
		reaped = session_lifecycle.reap_stranded_request_autorun()
		self.assertGreaterEqual(reaped, 1)
		self.assertEqual(_flag(conv), 0, "a stranded flag past the reap window is cleared")

	def test_reaper_keeps_a_fresh_flag(self):
		from jarvis.chat import session_lifecycle

		conv = _make_conv(TEST_USER)
		api._request_autorun_arm(conv, "")  # fresh timestamp
		frappe.db.commit()
		session_lifecycle.reap_stranded_request_autorun()
		self.assertEqual(_flag(conv), 1, "a live/fresh request run is never reaped")
