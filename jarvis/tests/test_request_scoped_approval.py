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
	"""The upfront detector (approval_phrases.contains_confirm_all_directive) + the
	typed-path predicate (is_explicit_confirm_all): only an EXPLICIT blanket approval,
	as a trailing clause (upfront) or the whole message (typed), arms - never a bare
	go-ahead, an incidental phrase in prose, an un-/re- prefix, a negation, an exception
	qualifier, a question, or a pasted document."""

	def _d(self, text):
		return approval_phrases.contains_confirm_all_directive(text)

	def test_detects_compound_trailing_directive(self):
		self.assertTrue(self._d("create 3 todos and submit them, confirm all"))
		self.assertTrue(self._d("please just do everything"))
		self.assertTrue(self._d("make the invoices, approve all"))
		self.assertTrue(self._d("create the new batch and submit it, confirm all"))

	def test_ignores_question_and_negation(self):
		self.assertFalse(self._d("should I confirm all?"))
		self.assertFalse(self._d("don't do everything"))
		self.assertFalse(self._d("do not confirm all of them"))
		# broadened negations (review): never / cannot / can't
		self.assertFalse(self._d("never confirm all without asking me first"))
		self.assertFalse(self._d("I cannot confirm all 50 items"))
		self.assertFalse(self._d("can't do everything right now"))

	def test_word_boundary_rejects_un_re_prefix(self):
		# "undo everything" / "redo it all" must NOT match "do everything" / "do it all".
		self.assertFalse(self._d("undo everything I just did"))
		self.assertFalse(self._d("redo it all"))
		self.assertFalse(self._d("redo everything"))

	def test_incidental_midsentence_phrase_does_not_arm(self):
		# the directive is not a trailing clause here - ordinary prose.
		self.assertFalse(self._d("do everything needed to ship it"))
		self.assertFalse(self._d("confirm all the details are right"))
		self.assertFalse(self._d("please confirm all totals look correct"))

	def test_exception_qualifier_does_not_arm(self):
		self.assertFalse(self._d("create the orders, confirm all except the tagging"))
		self.assertFalse(self._d("do everything but not the emails"))

	def test_length_cap_blocks_a_pasted_block(self):
		pasted = ("the customer wrote a long note here " * 20) + ", confirm all"
		self.assertGreater(len(pasted), approval_phrases.MAX_DIRECTIVE_LEN)
		self.assertFalse(self._d(pasted))

	def test_bare_directive_is_not_a_compound_arm(self):
		# a bare whole-message directive is the TYPED path's job (is_explicit_confirm_all);
		# the upfront compound detector must not arm on it (nothing bundled to scope).
		self.assertFalse(self._d("confirm all"))
		self.assertFalse(self._d("do everything"))

	def test_plain_request_is_not_a_directive(self):
		self.assertFalse(self._d("create a todo for me"))
		self.assertFalse(self._d(""))

	def test_is_explicit_confirm_all_whole_message(self):
		ex = approval_phrases.is_explicit_confirm_all
		for yes in ("confirm all", "do everything", "all", "both", "approve all of them", "yes to all"):
			self.assertTrue(ex(yes), msg=yes)
		# bare go-aheads sweep visible cards but must NOT arm the request scope
		for no in ("yes", "y", "ok", "okay", "go", "go ahead", "sure", "do it", "proceed", "confirm 1"):
			self.assertFalse(ex(no), msg=no)


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
		# a NEW compound "confirm all" (trailing directive) resets the prior then re-arms
		res = self._send(conv, "start a fresh batch of orders, confirm all")
		self.assertTrue(res["ok"])
		self.assertEqual(_flag(conv), 1, "the upfront arm rides AFTER the save's reset")

	def test_plain_message_leaves_it_disarmed(self):
		conv = _make_conv(TEST_USER)
		res = self._send(conv, "what is the total outstanding?")
		self.assertTrue(res["ok"])
		self.assertEqual(_flag(conv), 0)

	def test_delegated_send_with_directive_does_not_arm(self):
		# A delegated re-entry (scheduler / approval-resume / File Box) whose text happens
		# to carry "confirm all" must NOT arm - no human approved anything (review).
		conv = _make_conv(TEST_USER)
		self.assertEqual(_flag(conv), 0)
		orig = frappe.flags.get("jarvis_delegated_send")
		frappe.flags.jarvis_delegated_send = True
		try:
			self._send(conv, "create the orders, confirm all")
		finally:
			frappe.flags.jarvis_delegated_send = orig
		self.assertEqual(_flag(conv), 0, "a delegated send never arms the request run")

	def test_background_send_with_directive_does_not_arm(self):
		conv = _make_conv(TEST_USER)
		from jarvis.tests._transport_helpers import provision_legacy_site

		provision_legacy_site(self)
		with patch("jarvis.chat.api._ensure_session_key", return_value="agent:fake"):
			with patch("frappe.enqueue"):
				chat_api.send_message(conv, "create the orders, confirm all", background=1)
		self.assertEqual(_flag(conv), 0, "a background (unattended) send never arms")


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

	def _strand(self, conv):
		"""Arm + freeze the sliding timestamp past the reap window."""
		api._request_autorun_arm(conv, "")
		stranded = frappe.utils.add_to_date(
			frappe.utils.now_datetime(), seconds=-(3 * api._REQUEST_AUTORUN_TTL_S)
		)
		frappe.db.set_value(CONV, conv, "request_autorun_at", stranded, update_modified=False)
		frappe.db.commit()

	def test_reaper_reaps_a_null_timestamp_flag(self):
		from jarvis.chat import session_lifecycle

		conv = _make_conv(TEST_USER)
		api._request_autorun_arm(conv, "")
		frappe.db.set_value(CONV, conv, "request_autorun_at", None, update_modified=False)
		frappe.db.commit()
		session_lifecycle.reap_stranded_request_autorun()
		self.assertEqual(_flag(conv), 0, "a NULL-timestamp armed flag is malformed and reapable")

	def test_reaper_keeps_a_flag_with_a_live_turn(self):
		from jarvis.chat import session_lifecycle

		conv = _make_conv(TEST_USER)
		self._strand(conv)
		# a streaming turn = a live run that may legitimately still be auto-executing
		frappe.get_doc(
			{
				"doctype": "Jarvis Chat Message",
				"conversation": conv,
				"seq": 1,
				"role": "assistant",
				"streaming": 1,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		session_lifecycle.reap_stranded_request_autorun()
		self.assertEqual(_flag(conv), 1, "a stranded-looking flag with a live turn is NOT reaped")

	def test_reaper_keeps_a_flag_with_a_pending_card(self):
		import time

		from jarvis.chat import session_lifecycle

		conv = _make_conv(TEST_USER)
		self._strand(conv)
		# a run PAUSED on a parked (brake) card is a legit pause, not stranded
		pending_confirm.mint(
			conversation=conv,
			owner=TEST_USER,
			tool="delete_doc",
			args={"doctype": "ToDo", "name": "x"},
			run_id="",
			exec_user=TEST_USER,
			preview={"summary": "delete"},
			expires_at=int(time.time()) + 900,
		)
		session_lifecycle.reap_stranded_request_autorun()
		self.assertEqual(_flag(conv), 1, "a flag paused on a pending card is NOT reaped")


class TestRequestAutorunTypedSweepArm(FrappeTestCase):
	"""The typed 'confirm all' arm (the primary Layer-B usage): an EXPLICIT blanket
	approval over parked cards arms the request scope; a bare 'yes' / a numbered pick /
	an empty sweep does NOT."""

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

	def _park_reversible(self, conv):
		import time

		return pending_confirm.mint(
			conversation=conv,
			owner=TEST_USER,
			tool="create_doc",
			args={"doctype": "ToDo", "values": {"description": "ra-sweep"}},
			run_id="",
			exec_user=TEST_USER,
			preview={"summary": "create a ToDo"},
			expires_at=int(time.time()) + 900,
		)

	def test_confirm_all_arms_after_a_real_sweep(self):
		conv = _make_conv(TEST_USER)
		self._park_reversible(conv)
		with patch("jarvis.api.dispatch_confirmed", return_value={"ok": True, "data": {}}):
			with patch("frappe.enqueue"):
				chat_api._typed_confirmation(TEST_USER, conv, "confirm all", [])
		self.assertEqual(_flag(conv), 1, "an explicit 'confirm all' sweep arms the request run")

	def test_bare_yes_sweeps_but_does_not_arm(self):
		conv = _make_conv(TEST_USER)
		self._park_reversible(conv)
		with patch("jarvis.api.dispatch_confirmed", return_value={"ok": True, "data": {}}):
			with patch("frappe.enqueue"):
				chat_api._typed_confirmation(TEST_USER, conv, "yes", [])
		self.assertEqual(_flag(conv), 0, "a bare 'yes' confirms the card but does NOT arm the request")

	def test_confirm_all_nothing_parked_does_not_arm(self):
		conv = _make_conv(TEST_USER)
		res = chat_api._typed_confirmation(TEST_USER, conv, "confirm all", [])
		self.assertIsNone(res, "nothing to sweep -> falls through")
		self.assertEqual(_flag(conv), 0, "an empty sweep does not arm")

	def test_numbered_pick_does_not_arm(self):
		conv = _make_conv(TEST_USER)
		token = self._park_reversible(conv)
		with patch("jarvis.api.dispatch_confirmed", return_value={"ok": True, "data": {}}):
			with patch("frappe.enqueue"):
				chat_api._typed_confirmation(TEST_USER, conv, "confirm 1", [token])
		self.assertEqual(_flag(conv), 0, "a numbered pick approves only that card, not the request")

	def test_confirm_all_over_displayed_tokens_arms(self):
		conv = _make_conv(TEST_USER)
		token = self._park_reversible(conv)
		with patch("jarvis.api.dispatch_confirmed", return_value={"ok": True, "data": {}}):
			with patch("frappe.enqueue"):
				chat_api._typed_confirmation(TEST_USER, conv, "confirm all", [token])
		self.assertEqual(_flag(conv), 1, "'confirm all' over the displayed cards arms the request run")


class TestRequestAutorunOverCapMessage(FrappeTestCase):
	"""F16 over-cap bounce wording is uncarded-run-aware for request_autorun too, not
	only skill_autorun - telling the model to 'confirm each one' is wrong under an
	armed request."""

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

	def test_over_cap_message_is_autorun_aware(self):
		from jarvis.tools._bulk import _MAX_BATCH

		conv = _make_conv(TEST_USER)
		api._request_autorun_arm(conv, "")
		docs = [{"doctype": "ToDo", "values": {"description": f"o{i}"}} for i in range(_MAX_BATCH + 1)]
		r = api._run_tool("create_docs", {"docs": docs}, conversation=conv)
		self.assertFalse(r["ok"])
		self.assertIn("too many records", r["error"]["message"])
		self.assertNotIn(
			"confirm each one",
			r["error"]["message"],
			"under request_autorun there is no card to confirm - the wording must not say so",
		)
		self.assertIn("nothing", r["error"]["message"])


class TestRequestReceiptLabel(FrappeTestCase):
	"""A request-scoped uncarded write labels its receipt auto_applied with NO armer
	name (the user approved it) - the consume-once marker drives the outcome, and the
	armed_by_* fields stay empty. Mirrors macro/skill's TestArmedReceiptProvenance."""

	MSG = "Jarvis Chat Message"

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

		_agent_run_ctx.take_request_autorun_applied()
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER}, pluck="name"):
			frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_request_scoped_write_labels_auto_applied_without_an_armer(self):
		from jarvis.tools import _agent_run_ctx

		conv = _make_conv(TEST_USER)
		sk = "sk-request-receipt-ok"
		frappe.db.set_value(CONV, conv, "session_key", sk, update_modified=False)
		_agent_run_ctx.set_request_autorun_applied()
		api._persist_and_publish_tool_call(
			session_key=sk,
			tool="submit_doc",
			args={"doctype": "ToDo", "name": "x"},
			result={"ok": True, "data": {}},
			tool_call_id="tc-req-1",
		)
		row = frappe.get_all(
			self.MSG,
			filters={"conversation": conv, "role": "tool", "tool_call_id": "tc-req-1"},
			fields=["action_outcome", "armed_by_macro", "armed_by_skill"],
		)
		self.assertEqual(len(row), 1)
		self.assertEqual(row[0].action_outcome, "auto_applied")
		self.assertFalse(row[0].armed_by_macro, "no external armer name for a user-approved request")
		self.assertFalse(row[0].armed_by_skill)
		# Consumed: a later ordinary receipt must NOT inherit the label.
		self.assertFalse(_agent_run_ctx.take_request_autorun_applied())

	def test_failed_request_scoped_write_labels_failed(self):
		from jarvis.tools import _agent_run_ctx

		conv = _make_conv(TEST_USER)
		sk = "sk-request-receipt-fail"
		frappe.db.set_value(CONV, conv, "session_key", sk, update_modified=False)
		_agent_run_ctx.set_request_autorun_applied()
		api._persist_and_publish_tool_call(
			session_key=sk,
			tool="submit_doc",
			args={"doctype": "ToDo", "name": "x"},
			result={"ok": False, "error": {"message": "boom"}},
			tool_call_id="tc-req-2",
		)
		row = frappe.get_all(
			self.MSG,
			filters={"conversation": conv, "tool_call_id": "tc-req-2"},
			fields=["action_outcome"],
		)
		self.assertEqual(len(row), 1)
		self.assertEqual(row[0].action_outcome, "failed", "a failed uncarded request write is still surfaced")


class TestRequestAutorunContinuationDoesNotReset(FrappeTestCase):
	"""AC-req-scope: a hidden continuation ([System] Applied) rides _enqueue_turn, never
	send_message, so it does NOT reset request_autorun - the create->submit chain keeps
	its approval. (A genuine new human message via send_message DOES reset; that is
	TestRequestAutorunSendMessage.)"""

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

	def test_hidden_continuation_keeps_the_flag(self):
		from jarvis.tests._transport_helpers import provision_legacy_site

		provision_legacy_site(self)
		conv = _make_conv(TEST_USER)
		frappe.db.set_value(CONV, conv, "session_key", "agent:cont", update_modified=False)
		api._request_autorun_arm(conv, "orig-msg")
		# A continuation turn (hidden) - the create->submit chain's next hop.
		with patch("frappe.enqueue"):
			chat_api._enqueue_turn(conv, "[System] Applied: created the ToDo. Continue.", hidden=True)
		self.assertEqual(_flag(conv), 1, "a hidden continuation must NOT reset the request-scoped approval")
