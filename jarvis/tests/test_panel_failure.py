"""R2-9 and the failure reference (round 2, J2b).

A failed save on the draft panel (``apply_action``): a fixable one keeps the panel
open with its field marks and tells the assistant ONCE that the user is fixing it
there (no card); a not fixable one closes the panel and the assistant explains. A
write that committed part-way reports partial, never "nothing was saved", on the
panel and on the legacy confirm path. A failed confirmation carries its own id as
``error.reference`` for support; it never reaches the assistant's prompt, and a
known failure writes no Error Log row."""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import actions_api, pending_confirm
from jarvis.chat import api as chat_api
from jarvis.chat import pending_actions as pa
from jarvis.tests._pending_action_helpers import MSG, OWNER, PendingActionTestMixin, as_user, fake_dispatch

FIXING_TEXT = "the user is fixing it in the panel"
FIXABLE_TEXT = "correct only the fields named in the failure"
NOT_FIXABLE_TEXT = "Retrying will not fix it"
UNVERIFIED_TEXT = "could NOT be verified"
BAD_USER = "nobody-j2b-panel@example.com"
HIGHLIGHTED = "highlighted fields"
# A legacy (Redis token) card: minted only with the pending-action cards flag off.
LEGACY = patch.dict(frappe.conf, {"jarvis_pa_chat_cards": 0})


def _deadlock(*_args, **_kwargs):
	raise frappe.QueryDeadlockError("Deadlock found when trying to get lock")


RULE = "Posting is not allowed in a closed period"  # no full stop: the toast adds one


def _business_rule(*_args, **_kwargs):
	"""A rule no value in the panel fixes (a plain ValidationError, like a closed period)."""
	raise frappe.ValidationError(RULE)


BUSINESS_RULE = patch("jarvis.tools.create_doc.create_doc", side_effect=_business_rule)


class _Hermetic:
	"""The continuation's seed message and Turn rows are written for real; starting the
	turn (the pump, or a legacy job) is stubbed, so the bench's queue depth never
	matters. Every ``frappe.log_error`` call during the test is recorded by title."""

	def setUp(self):
		super().setUp()
		for target in ("jarvis.chat.pump.ensure_pump", "jarvis.chat.pump.lpush_wake"):
			stub = patch(target, return_value={})
			stub.start()
			self.addCleanup(stub.stop)
		legacy = patch("jarvis.chat.api._dispatch_turn", return_value=None)
		legacy.start()
		self.addCleanup(legacy.stop)
		self.logged_errors: list[str] = []
		real_log_error = frappe.log_error

		def _record(*args, **kwargs):
			self.logged_errors.append(kwargs.get("title") or (args[0] if args else ""))
			return real_log_error(*args, **kwargs)

		recorder = patch("frappe.log_error", side_effect=_record)
		recorder.start()
		self.addCleanup(recorder.stop)


class _PanelBase(_Hermetic, PendingActionTestMixin, FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.conv = self.make_conv()

	def apply(
		self, values: dict, *, message: str = "draft-msg-1", verb="create", doctype="ToDo", **extra
	) -> dict:
		action = {
			"verb": verb,
			"doctype": doctype,
			"name": extra.pop("name", ""),
			"values": values,
			"conversation": self.conv,
			"message": message,
			**extra,
		}
		with as_user(OWNER):
			return actions_api.apply_action(frappe.as_json(action))

	def continuations(self) -> list[frappe._dict]:
		return frappe.get_all(
			MSG,
			filters={"conversation": self.conv, "origin": "continuation", "hidden": 1},
			fields=["name", "content"],
			order_by="seq asc",
		)

	def error_logs(self) -> list[str]:
		return list(self.logged_errors)

	def seeds(self) -> int:
		return frappe.db.count(MSG, {"conversation": self.conv, "origin": "continuation"})


class TestPanelFixable(_PanelBase):
	def test_keeps_the_marks_and_tells_the_assistant_once(self):
		first = self.apply({"priority": "High"})  # no description: a mandatory value is missing
		self.assertFalse(first["ok"])
		self.assertEqual(first["error"]["kind"], "fixable")
		self.assertEqual([f["fieldname"] for f in first["error"]["fields"]], ["description"])
		self.assertNotIn("closed", first, "the panel stays open")
		conts = self.continuations()
		self.assertEqual(len(conts), 1)
		self.assertIn(FIXING_TEXT, conts[0].content)
		self.assertNotIn(FIXABLE_TEXT, conts[0].content, "the assistant is not asked for a card")
		self.assertFalse(frappe.db.exists("Jarvis Pending Action", {"conversation": self.conv}), "no card")

		# A second failing save of the same draft (now a bad link) in the same turn.
		second = self.apply({"description": "pa-test panel", "allocated_to": BAD_USER})
		self.assertEqual((second["ok"], second["error"]["kind"]), (False, "fixable"), second)
		self.assertEqual(len(self.continuations()), 1, "never a second continuation")
		self.assertFalse(frappe.db.exists("ToDo", {"description": "pa-test panel"}))
		self.assertEqual(self.error_logs(), [], "a known failure writes no Error Log row")

	def test_another_draft_is_told_again(self):
		self.apply({"priority": "High"}, message="draft-a")
		self.apply({"priority": "High"}, message="draft-b")
		self.assertEqual(len(self.continuations()), 2)

	def test_without_a_draft_id_nothing_is_sent(self):
		# A repeat cannot be told from a new draft, so never risk one per click.
		self.apply({"priority": "High"}, message="")
		self.apply({"priority": "High"}, message="")
		self.assertEqual(self.continuations(), [])

	def test_a_continuation_that_could_not_be_sent_is_tried_again(self):
		with patch("jarvis.chat.api.enqueue_continuation", side_effect=RuntimeError("queue down")):
			self.apply({"priority": "High"})
		self.assertEqual(self.continuations(), [])
		self.apply({"priority": "High"})
		self.assertEqual(len(self.continuations()), 1)

	def test_the_only_field_emptied_is_a_missing_value_marked(self):
		# The panel sends no values at all: never the generic "non-empty dict" error.
		r = self.apply({})
		self.assertEqual((r["ok"], r["error"]["kind"]), (False, "fixable"), r)
		self.assertNotIn("closed", r)
		self.assertNotIn("non-empty", r["error"]["message"])
		self.assertEqual([f["fieldname"] for f in r["error"]["fields"]], ["description"])
		self.assertEqual(r["error"]["message"], "ToDo needs a value for Description.")
		self.assertEqual(len(self.continuations()), 1)
		self.assertIn(FIXING_TEXT, self.continuations()[0].content)

	def test_a_bad_link_marks_its_field(self):
		r = self.apply({"description": "pa-test link", "allocated_to": BAD_USER})
		self.assertEqual(r["error"]["kind"], "fixable")
		self.assertEqual(
			[(f["fieldname"], f.get("invalid")) for f in r["error"]["fields"]], [("allocated_to", 1)]
		)
		self.assertIn("Could not find", r["error"]["message"])

	def test_a_bad_type_checked_option_marks_its_field(self):
		r = self.apply({"description": "pa-test priority", "priority": "Urgent"})
		self.assertEqual(r["error"]["kind"], "fixable")
		self.assertEqual([f["fieldname"] for f in r["error"]["fields"]], ["priority"])

	def test_a_missing_value_and_a_bad_option_mark_both(self):
		# "Value missing" is raised first; the bad option behind it is marked too.
		r = self.apply({"priority": "Medium", "status": "Bogus"})
		self.assertNotIn("closed", r)
		marks = {f["fieldname"]: f.get("invalid") for f in r["error"]["fields"]}
		self.assertEqual(marks, {"description": None, "status": 1})
		self.assertEqual(
			r["error"]["message"],
			"Status has a value it does not accept. ToDo also needs a value for Description.",
		)

	def test_a_missing_value_it_cannot_fill_never_hides_a_bad_value_it_can(self):
		with patch("jarvis.chat.held_writes.missing_fields", return_value=None):
			r = self.apply({"priority": "Medium", "status": "Bogus"})
		self.assertNotIn("closed", r)
		self.assertEqual([(f["fieldname"], f.get("invalid")) for f in r["error"]["fields"]], [("status", 1)])

	def test_a_bad_link_marks_only_its_field(self):
		# Not a "Value missing" failure: required values are never asked for here (the
		# controller had not run yet), only the bad value is marked.
		r = self.apply({"priority": "Medium", "allocated_to": BAD_USER})
		self.assertNotIn("closed", r)
		self.assertEqual(
			[(f["fieldname"], f.get("invalid")) for f in r["error"]["fields"]], [("allocated_to", 1)]
		)

	def test_a_failing_link_lookup_never_turns_a_failure_into_a_crash(self):
		with patch("jarvis.chat.panel_fields._link_exists", side_effect=RuntimeError("db hiccup")):
			r = self.apply({"description": "pa-test lookup", "allocated_to": BAD_USER})
		self.assertEqual((r["ok"], r["error"]["kind"]), (False, "fixable"))

	def test_the_draft_is_not_re_run_unless_a_value_is_missing(self):
		with patch("jarvis.chat.panel_fields._fill_in_sandbox") as fill:
			self.apply({"description": "pa-test no rerun", "allocated_to": BAD_USER})
			fill.assert_not_called()
			self.apply({"priority": "High"})
			fill.assert_called_once()

	def test_a_full_queue_after_the_seed_still_tells_the_assistant_once(self):
		# The seed (and its queued Turn) is written and committed before the turn is
		# started; starting it can then fail on a full queue. The queued Turn is picked
		# up later (the pump's watchdog), so the claim stays: one seed across clicks.
		full = frappe.QueueOverloaded("Too many queued background jobs")
		with (
			patch("jarvis.chat.pump.ensure_pump", side_effect=full),  # after the Turn commit
			patch("jarvis.chat.api._dispatch_turn", side_effect=full),  # legacy: after the seed
		):
			for _ in range(3):
				r = self.apply({"priority": "High"})
				self.assertEqual((r["ok"], r["error"]["kind"]), (False, "fixable"))
		self.assertEqual(self.seeds(), 1)
		self.assertEqual(len(self.continuations()), 1)

	def test_an_orphaned_seed_never_multiplies(self):
		full = frappe.QueueOverloaded("Too many queued background jobs")
		with patch("jarvis.chat.admission.accept_or_queue", side_effect=full):
			for _ in range(3):
				self.apply({"priority": "High"})
				self.assertEqual(self.seeds(), 0)
		self.apply({"priority": "High"})  # the queue frees up: told now, once
		self.assertEqual(self.seeds(), 1)

	def test_nothing_written_releases_the_claim(self):
		with patch("jarvis.chat.admission._insert_seed", side_effect=RuntimeError("db down")):
			self.apply({"priority": "High"})
		self.assertEqual(self.seeds(), 0)
		self.apply({"priority": "High"})
		self.assertEqual(self.seeds(), 1)

	def test_an_update_of_a_record_that_is_gone_closes_the_panel(self):
		# DoesNotExistError stays fixable for the model (J2a), but no panel edit can
		# bring the record back: the panel closes and the assistant explains.
		r = self.apply({"description": "x"}, verb="update", name="pa-test-no-such-todo")
		self.assertEqual(r["error"]["kind"], "fixable")
		self.assertIs(r["closed"], True)
		self.assertNotIn("hint", r["error"], "no 'correct the value' on a closed panel")
		conts = self.continuations()
		self.assertEqual(len(conts), 1)
		self.assertIn(NOT_FIXABLE_TEXT, conts[0].content)
		self.assertNotIn(FIXING_TEXT, conts[0].content)

	def test_a_missing_value_the_panel_cannot_fill_closes_the_panel(self):
		# No field marks: the missing value is not one the panel can edit.
		# missing_fields answers None when a missing value cannot be filled in the panel.
		with patch("jarvis.chat.held_writes.missing_fields", return_value=None):
			r = self.apply({"priority": "High"})
		self.assertEqual(r["error"]["kind"], "fixable")
		self.assertNotIn("fields", r["error"])
		self.assertIs(r["closed"], True)
		self.assertIn(NOT_FIXABLE_TEXT, self.continuations()[0].content)

	def test_the_phone_card_cannot_be_edited_so_nothing_is_sent(self):
		r = self.apply({"priority": "High"}, editable=0)
		self.assertEqual((r["ok"], r["error"]["kind"]), (False, "fixable"))
		self.assertNotIn("closed", r)
		self.assertEqual(self.continuations(), [])

	def test_busy_keeps_the_panel_open_with_nothing_sent(self):
		with patch("jarvis.tools.create_doc.create_doc", side_effect=_deadlock):
			r = self.apply({"description": "pa-test busy"})
		self.assertEqual((r["ok"], r["error"]["kind"]), (False, "retry_later"))
		self.assertNotIn("closed", r)
		self.assertEqual(r["error"]["hint"], "Nothing was changed. Try again in a moment.")
		self.assertEqual(self.continuations(), [])


class TestPanelNotFixable(_PanelBase):
	def test_closes_the_panel_and_the_assistant_explains(self):
		with BUSINESS_RULE:
			r = self.apply({"description": "pa-test closed"})
		self.assertEqual((r["ok"], r["error"]["kind"]), (False, "not_fixable"), r)
		self.assertIs(r["closed"], True)
		self.assertNotIn("fields", r["error"])
		self.assertNotIn("hint", r["error"], "a closed panel invites no correction")
		conts = self.continuations()
		self.assertEqual(len(conts), 1)
		self.assertIn(NOT_FIXABLE_TEXT, conts[0].content)
		self.assertIn(RULE, conts[0].content)
		self.assertFalse(frappe.db.exists("ToDo", {"description": "pa-test closed"}))
		self.assertEqual(self.error_logs(), [])

	def test_says_whether_the_assistant_was_told(self):
		with BUSINESS_RULE:
			r = self.apply({"description": "pa-test told"})
		self.assertIs(r["assistant_told"], True)
		with (
			BUSINESS_RULE,
			patch("jarvis.chat.api.enqueue_continuation", side_effect=RuntimeError("queue down")),
		):
			r = self.apply({"description": "pa-test untold"})
		self.assertIs(r["closed"], True)
		self.assertIs(r["assistant_told"], False)

	def test_a_seed_without_its_turn_is_removed_and_not_told(self):
		# The turn machine's gate raised before it wrote the Turn: the committed seed
		# would never run, so it is removed and the person is told to ask in chat.
		full = frappe.QueueOverloaded("Too many queued background jobs")
		with BUSINESS_RULE, patch("jarvis.chat.admission.accept_or_queue", side_effect=full):
			r = self.apply({"description": "pa-test orphan"})
		self.assertIs(r["assistant_told"], False)
		self.assertEqual(self.seeds(), 0)

	def test_a_legacy_dispatch_that_fails_is_not_told(self):
		full = frappe.QueueOverloaded("Too many queued background jobs")
		with (
			BUSINESS_RULE,
			patch("jarvis.chat.admission.turn_machine_enabled", return_value=False),
			patch("jarvis.chat.api._dispatch_turn", side_effect=full),
		):
			r = self.apply({"description": "pa-test legacy"})
		self.assertIs(r["assistant_told"], False)
		self.assertEqual(self.seeds(), 0)

	def test_a_bad_option_frappe_checks_keeps_the_panel_open_marked(self):
		# ``status`` is left to Frappe's own Select check (J2a: not fixable for the
		# model), but the panel shows it as a choice the person can change.
		r = self.apply({"description": "pa-test status", "status": "Bogus"})
		self.assertEqual(r["error"]["kind"], "not_fixable")
		self.assertNotIn("closed", r)
		self.assertEqual([(f["fieldname"], f.get("invalid")) for f in r["error"]["fields"]], [("status", 1)])
		self.assertEqual(len(self.continuations()), 1)
		self.assertIn(FIXING_TEXT, self.continuations()[0].content)

	def test_a_refusal_keeps_the_guards_panel_reply(self):
		# Structure refusals (J1-guard) show their Desk path in the panel and send nothing.
		r = self.apply({"dt": "ToDo", "fieldname": "j2b_x", "fieldtype": "Data"}, doctype="Custom Field")
		self.assertEqual((r["ok"], r["error"]["code"]), (False, "structure_refused"))
		self.assertNotIn("closed", r)
		self.assertEqual(self.continuations(), [])


class TestPanelPartial(_PanelBase):
	def test_a_commit_before_the_failure_is_partial_not_nothing_saved(self):
		# The no-commit fence neutralises frappe.db.commit on the panel, so only a commit
		# that goes around it (a raw COMMIT) can keep half a write.
		def _commit_then_fail(doctype, values, **kw):
			frappe.get_doc({"doctype": "ToDo", "description": "pa-test partial half"}).insert()
			frappe.db.sql("commit")
			raise frappe.ValidationError("the second half failed")

		with patch("jarvis.tools.create_doc.create_doc", side_effect=_commit_then_fail):
			r = self.apply({"description": "pa-test partial"})
		self.assertFalse(r["ok"])
		self.assertEqual(r["outcome"], "partial")
		self.assertIs(r["closed"], True)
		self.assertIn("may already be saved", r["error"]["hint"])
		self.assertTrue(frappe.db.exists("ToDo", {"description": "pa-test partial half"}), "it did commit")
		conts = self.continuations()
		self.assertEqual(len(conts), 1)
		self.assertIn(UNVERIFIED_TEXT, conts[0].content)

	def test_a_clean_failure_is_not_partial(self):
		with BUSINESS_RULE:
			r = self.apply({"description": "pa-test clean"})
		self.assertNotIn("outcome", r)

	def test_a_fenced_commit_saves_nothing_so_it_is_not_partial(self):
		def _fenced_commit_then_fail(doctype, values, **kw):
			frappe.get_doc({"doctype": "ToDo", "description": "pa-test fenced half"}).insert()
			frappe.db.commit()  # a no-op inside the fence
			raise frappe.ValidationError("the second half failed")

		with patch("jarvis.tools.create_doc.create_doc", side_effect=_fenced_commit_then_fail):
			r = self.apply({"description": "pa-test fenced"})
		self.assertNotIn("outcome", r)
		self.assertFalse(frappe.db.exists("ToDo", {"description": "pa-test fenced half"}))

	def test_a_raw_commit_then_a_lock_timeout_is_partial(self):
		# A lock wait timeout rolls back only its statement: the savepoint survives,
		# so the probe still sees the raw commit before it.
		def _commit_then_timeout(doctype, values, **kw):
			frappe.get_doc({"doctype": "ToDo", "description": "pa-test timeout half"}).insert()
			frappe.db.sql("commit")
			raise frappe.QueryTimeoutError("Lock wait timeout exceeded")

		with patch("jarvis.tools.create_doc.create_doc", side_effect=_commit_then_timeout):
			r = self.apply({"description": "pa-test timeout"})
		self.assertEqual((r["error"]["kind"], r.get("outcome")), ("retry_later", "partial"))
		self.assertIs(r["closed"], True)

	def test_a_lock_timeout_alone_is_busy(self):
		def _timeout(doctype, values, **kw):
			raise frappe.QueryTimeoutError("Lock wait timeout exceeded")

		with patch("jarvis.tools.create_doc.create_doc", side_effect=_timeout):
			r = self.apply({"description": "pa-test timeout only"})
		self.assertEqual((r["error"]["kind"], "outcome" in r, "closed" in r), ("retry_later", False, False))

	def test_a_deadlock_is_busy_not_partial(self):
		def _real_deadlock(doctype, values, **kw):
			frappe.db._conn.rollback()  # InnoDB drops the transaction, savepoints included
			_deadlock()

		with patch("jarvis.tools.create_doc.create_doc", side_effect=_real_deadlock):
			r = self.apply({"description": "pa-test deadlock"})
		self.assertEqual((r["error"]["kind"], "outcome" in r, "closed" in r), ("retry_later", False, False))


class TestHintFitsTheKind(FrappeTestCase):
	def test_fixable_names_the_value_not_highlighted_fields(self):
		r = api._dispatch_and_wrap(
			"create_doc",
			{"doctype": "ToDo", "values": {"description": "j2b", "allocated_to": BAD_USER}},
			True,
		)
		self.assertEqual(r["error"]["kind"], "fixable")
		self.assertNotIn(HIGHLIGHTED, r["error"]["hint"], "nothing is highlighted on this path")
		self.assertEqual(r["error"]["hint"], api._FIXABLE_HINT)

	def test_a_business_rule_gets_no_field_hint(self):
		r = api._dispatch_and_wrap(
			"create_doc", {"doctype": "ToDo", "values": {"description": "j2b", "status": "Bogus"}}, True
		)
		self.assertEqual((r["error"]["code"], r["error"]["kind"]), ("InvalidArgumentError", "not_fixable"))
		self.assertNotIn("hint", r["error"])

	def test_busy_and_permission_keep_their_own_hints(self):
		self.assertEqual(
			api._hint_for("RetryLaterError", "", kind="retry_later"), api._ERROR_HINTS["RetryLaterError"]
		)
		self.assertIn("administrator", api._hint_for("PermissionDeniedError", "", kind="not_fixable"))
		# A caller with no kind (capability / wiki refusals) is unchanged.
		self.assertEqual(api._hint_for("FeatureDisabledError", ""), api._ERROR_HINTS["FeatureDisabledError"])


class TestRealInsufficientStockOnThePanel(FrappeTestCase):
	"""A real ERPNext business rule through the draft panel (create and submit)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if "erpnext" not in frappe.get_installed_apps():
			raise cls.skipException("ERPNext is not installed")
		from jarvis.tests import _erpnext_masters as masters

		frappe.set_user("Administrator")
		TestPanelOnErpnext.guard_masters(cls)
		masters.clear_cache_after_rollback(cls)
		cls.company = masters.ensure_ledger_company()
		masters.ensure_item("_J2A Stock Item", is_stock_item=1, valuation_rate=10)
		masters.ensure_stock_entry_type("Material Issue")
		# Its own fiscal year: never one another class left behind (a fresh CI site has none).
		masters.ensure_fiscal_years(frappe.utils.today())
		cls.warehouse = frappe.db.get_value("Warehouse", {"company": cls.company, "warehouse_name": "Stores"})
		allow = frappe.db.get_single_value("Stock Settings", "allow_negative_stock")
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 0)
		cls.addClassCleanup(frappe.db.set_single_value, "Stock Settings", "allow_negative_stock", allow)

	def test_closed_with_no_field_hint(self):
		conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "j2b stock"}).insert()
		real_rollback = frappe.db.rollback

		def _savepoint_only(*args, **kwargs):  # keep the class's masters (and this row)
			if args or kwargs.get("save_point"):
				return real_rollback(*args, **kwargs)

		values = {
			"company": self.company,
			"stock_entry_type": "Material Issue",
			"items": [
				{"item_code": "_J2A Stock Item", "qty": 5, "s_warehouse": self.warehouse, "basic_rate": 10}
			],
		}
		with (
			patch.object(frappe.db, "rollback", side_effect=_savepoint_only),
			patch("jarvis.chat.api.enqueue_continuation", return_value={}) as cont,
		):
			r = actions_api.apply_action(
				frappe.as_json(
					{
						"verb": "create",
						"doctype": "Stock Entry",
						"values": values,
						"submit": 1,
						"conversation": conv.name,
					}
				)
			)
		self.assertEqual((r["ok"], r["error"]["kind"]), (False, "not_fixable"), r)
		self.assertIn("needed in Warehouse", r["error"]["message"])
		self.assertNotIn(HIGHLIGHTED, r["error"].get("hint", ""))
		self.assertIs(r["closed"], True)
		self.assertEqual(cont.call_args.kwargs["outcome"], chat_api.OUTCOME_NOT_FIXABLE)


class TestFailureReference(_Hermetic, PendingActionTestMixin, FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.conv = self.make_conv()
		self.todo = self.make_todo()

	def park_update(self, changes: dict) -> str:
		args = {"doctype": "ToDo", "name": self.todo, "changes": changes}
		return self.park(self.conv, tool="update_doc", args=args, summary="Update a ToDo")

	def chip(self, name: str) -> dict:
		raw = frappe.db.get_value(MSG, {"conversation": self.conv, "tool_call_id": name}, "tool_result")
		return json.loads(raw) if raw else {}

	def prompts(self) -> str:
		return " ".join(
			frappe.get_all(MSG, filters={"conversation": self.conv, "hidden": 1}, pluck="content")
		)

	def test_a_failed_confirm_carries_its_own_id(self):
		name = self.park_update({"allocated_to": BAD_USER})
		with as_user(OWNER):
			out = pa.execute(name, conversation=self.conv)
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["reference"], name)
		self.assertEqual(self.chip(name)["error"]["reference"], name)
		self.assertIn(FIXABLE_TEXT, self.prompts())
		self.assertNotIn(name, self.prompts(), "the reference never reaches the assistant")
		self.assertEqual(self.logged_errors, [])

	def test_a_failure_that_never_ran_has_one_on_its_chip(self):
		name = self.park_update({"description": "pa-test stale"})
		frappe.db.sql(
			"UPDATE `tabToDo` SET modified = modified + INTERVAL 1 MICROSECOND WHERE name=%(n)s",
			{"n": self.todo},
		)
		frappe.db.commit()
		with as_user(OWNER):
			out = pa.execute(name, conversation=self.conv)
		self.assertEqual(out["reason_code"], "stale")
		self.assertEqual(self.chip(name)["error"]["reference"], name)
		self.assertNotIn(name, self.prompts())

	def test_a_crash_keeps_its_error_log_row_and_its_reference(self):
		def _boom(tool, args):
			raise ValueError("unexpected")

		name = self.park_update({"description": "pa-test crash"})
		with fake_dispatch(_boom), as_user(OWNER):
			out = pa.execute(name, conversation=self.conv)
		self.assertEqual(out["error"]["reference"], name)
		self.assertTrue(self.logged("dispatch_crashed"), "crash rows stay as today")

	def test_a_connectors_own_failure_carries_it_on_the_chip(self):
		# The call dispatched (outer ok) but the connector failed (inner ok false):
		# the row is Failed, so its chip carries the reference too. A bench that reads
		# only the outer ok (no api.envelope_ok, version-15) settles it as Executed.
		if not hasattr(api, "envelope_ok"):
			self.skipTest("this bench does not read a connector's inner failure")

		def _inner_failure(tool, args):
			return {"ok": False, "error": {"code": "transport_error", "message": "down"}}

		args = {"connector": "c1", "action": "b"}
		name = self.park(self.conv, tool="call_connector", args=args, summary="Call c1")
		with fake_dispatch(_inner_failure), as_user(OWNER):
			pa.execute(name, conversation=self.conv)
		self.assertEqual(self.row(name).status, "Failed")
		self.assertEqual(self.chip(name)["reference"], name)
		self.assertNotIn(name, self.prompts())

	def test_success_has_none(self):
		name = self.park_update({"description": "pa-test fine"})
		with as_user(OWNER):
			out = pa.execute(name, conversation=self.conv)
		self.assertTrue(out["ok"])
		self.assertNotIn("error", self.chip(name))

	def test_a_legacy_confirm_has_none(self):
		with LEGACY:
			token = pending_confirm.mint(
				conversation=self.conv,
				owner=OWNER,
				tool="update_doc",
				args={"doctype": "ToDo", "name": self.todo, "changes": {"allocated_to": BAD_USER}},
				run_id="j2b",
			)
		with as_user(OWNER):
			out = actions_api.confirm_tool(token, conversation=self.conv)
		self.assertFalse(out["ok"])
		self.assertNotIn("reference", out["error"])


class TestLegacyConfirmPartial(_Hermetic, PendingActionTestMixin, FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.conv = self.make_conv()
		self.todo = self.make_todo()

	def confirm(self, dispatch) -> dict:
		with LEGACY:
			token = pending_confirm.mint(
				conversation=self.conv,
				owner=OWNER,
				tool="update_doc",
				args={"doctype": "ToDo", "name": self.todo, "changes": {"description": "pa-test legacy"}},
				run_id="j2b",
			)
		with fake_dispatch(dispatch), as_user(OWNER):
			return actions_api.confirm_tool(token, conversation=self.conv)

	def chip_outcome(self) -> str:
		return frappe.db.get_value(
			MSG,
			{"conversation": self.conv, "role": "tool", "action_outcome": ["is", "set"]},
			"action_outcome",
		)

	def last_prompt(self) -> str:
		return frappe.get_all(
			MSG,
			filters={"conversation": self.conv, "hidden": 1},
			pluck="content",
			order_by="seq desc",
			limit=1,
		)[0]

	def test_a_commit_before_the_failure_is_partial(self):
		def _commit_then_fail(tool, args):
			frappe.db.commit()
			raise frappe.ValidationError("second half failed")

		out = self.confirm(_commit_then_fail)
		self.assertEqual((out["ok"], out["outcome"]), (False, "partial"))
		self.assertEqual(self.chip_outcome(), "partial")
		self.assertIn(UNVERIFIED_TEXT, self.last_prompt())

	def test_partial_never_says_nothing_was_saved_or_try_again(self):
		def _commit_then_deadlock(tool, args):
			frappe.db.commit()
			_deadlock()

		out = self.confirm(_commit_then_deadlock)
		self.assertEqual(out["outcome"], "partial")
		self.assertNotIn("nothing was saved", out["error"]["message"])
		self.assertIn("may already be saved", out["error"]["hint"])

	def test_a_raw_commit_the_sentinel_cannot_see_is_partial_too(self):
		def _raw_commit_then_fail(tool, args):
			frappe.db.sql("commit")  # no before_commit callbacks: only the savepoint sees it
			raise frappe.ValidationError("second half failed")

		out = self.confirm(_raw_commit_then_fail)
		self.assertEqual((out["ok"], out["outcome"]), (False, "partial"))
		self.assertEqual(self.chip_outcome(), "partial")

	def test_a_crash_after_a_commit_says_it_may_have_partly_run(self):
		def _commit_then_crash(tool, args):
			frappe.db.commit()
			raise ValueError("unexpected")

		out = self.confirm(_commit_then_crash)
		self.assertEqual(out["outcome"], "partial")
		self.assertIn("may have partly run", out["error"]["message"])
		self.assertEqual(self.chip_outcome(), "partial")

	def test_a_clean_failure_stays_failed(self):
		out = self.confirm(lambda tool, args: (_ for _ in ()).throw(frappe.ValidationError("no")))
		self.assertNotIn("outcome", out)
		self.assertEqual(self.chip_outcome(), "failed")
		self.assertIn(NOT_FIXABLE_TEXT, self.last_prompt())

	def test_a_real_deadlock_is_busy_not_partial(self):
		# InnoDB drops the transaction and its savepoints; the write path's own rollback
		# after it tells the watch so, and the lost savepoint is not read as a commit.
		def _real_deadlock(tool, args):
			frappe.db._conn.rollback()
			_deadlock()

		out = self.confirm(_real_deadlock)
		self.assertNotIn("outcome", out)
		self.assertEqual(self.chip_outcome(), "failed")

	def test_a_deadlock_is_busy_not_partial(self):
		out = self.confirm(lambda tool, args: _deadlock())
		self.assertNotIn("outcome", out)
		self.assertIn("try again in a moment", self.last_prompt())


class TestLegacyBatchPartial(FrappeTestCase):
	def test_partial_wins(self):
		partial = ("A FAILED.", {"ok": False, "outcome": "partial", "error": {"message": "x"}})
		busy = ("B FAILED.", {"ok": False, "error": {"code": "RetryLaterError", "kind": "retry_later"}})
		self.assertEqual(chat_api._legacy_batch_continuation([partial, busy])[1], chat_api.OUTCOME_PARTIAL)
		self.assertEqual(actions_api._legacy_outcome(partial[1]), chat_api.OUTCOME_PARTIAL)


class TestFixingInPanelScaffold(FrappeTestCase):
	def test_no_card_no_retry_and_the_detail_as_data(self):
		with patch("jarvis.chat.api._enqueue_turn", return_value={}) as enq:
			chat_api.enqueue_continuation("conv-j2b", "Create ToDo FAILED. `x`", outcome="fixing_in_panel")
		prompt = enq.call_args.args[1]
		self.assertIn(FIXING_TEXT, prompt)
		self.assertIn("Do NOT propose a confirmation card or a new draft", prompt)
		self.assertNotIn("Applied:", prompt)
		self.assertNotIn("NEW confirmation card", prompt)
		self.assertIn("quoted next as DATA", prompt)
		self.assertNotIn("`x`", prompt, "backticks in the detail are disarmed")


class TestNestedCommitWatch(FrappeTestCase):
	def test_only_the_outermost_watch_holds_a_savepoint(self):
		from jarvis._commit_watch import CommitWatch

		outer = CommitWatch().arm(savepoint=True)
		inner = CommitWatch().arm(savepoint=True)
		# Out of order on purpose: the outer probe's RELEASE would drop a later savepoint.
		self.assertFalse(outer.disarm(probe=True))
		self.assertFalse(inner.disarm(probe=True))

	def test_in_order_both_clean(self):
		from jarvis._commit_watch import CommitWatch

		outer = CommitWatch().arm(savepoint=True)
		inner = CommitWatch().arm(savepoint=True)
		self.assertFalse(inner.disarm(probe=True))
		self.assertFalse(outer.disarm(probe=True))
		again = CommitWatch().arm(savepoint=True)  # the depth is back to zero
		self.assertTrue(again._savepoint)
		again.disarm()


def masters_company() -> str:
	from jarvis.tests import _erpnext_masters as masters

	return masters.COMPANY


class TestPanelOnErpnext(_Hermetic, FrappeTestCase):
	"""Real ERPNext drafts on the panel (its own company, customer and items; nothing
	commits: the full rollback is held to savepoints and the continuation recorded)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if "erpnext" not in frappe.get_installed_apps():
			raise cls.skipException("ERPNext is not installed")
		from frappe.utils import add_days, today

		from jarvis.tests import _erpnext_masters as masters

		frappe.set_user("Administrator")
		TestPanelOnErpnext.guard_masters(cls)
		masters.clear_cache_after_rollback(cls)
		cls.company = masters.ensure_ledger_company()
		cls.item = masters.ensure_item("_J2A Service Item", is_stock_item=0, valuation_rate=10)
		masters.ensure_fiscal_years(today(), add_days(today(), 5))
		cls.customer = masters.ensure_customer("_J2B Panel Customer")
		cls.price_list = masters.ensure_selling_price_list()
		cls.today, cls.later = today(), add_days(today(), 5)

	@staticmethod
	def guard_masters(test_class) -> None:
		"""Fail the class when its company outlives the class rollback (a later suite
		would read it as the site's first company). A site that already had it (a dev
		bench) is not checked. Registered before the masters are made, so it runs after
		the rollback (class cleanups run last-in first-out)."""
		if frappe.db.exists("Company", masters_company()):
			return

		def _check():
			frappe.db.rollback()
			if frappe.db.exists("Company", masters_company()):
				raise AssertionError(f"{masters_company()} was committed past the class rollback")

		test_class.addClassCleanup(_check)

	def apply(self, doctype: str, values: dict) -> tuple[dict, list]:
		conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "j2b erpnext"}).insert()
		real_rollback = frappe.db.rollback

		def _savepoint_only(*args, **kwargs):
			if args or kwargs.get("save_point"):
				return real_rollback(*args, **kwargs)

		with (
			patch.object(frappe.db, "rollback", side_effect=_savepoint_only),
			patch("jarvis.chat.api.enqueue_continuation", return_value={}) as cont,
		):
			r = actions_api.apply_action(
				frappe.as_json(
					{
						"verb": "create",
						"doctype": doctype,
						"values": values,
						"conversation": conv.name,
						"message": "j2b-erp-" + frappe.generate_hash(length=6),
					}
				)
			)
		return r, [c.kwargs.get("outcome") for c in cont.call_args_list]

	@staticmethod
	def user_without_commit(email: str) -> str:
		"""A Jarvis User, inserted in the class's own transaction. Never through
		``ensure_user``: its commit would also commit this class's company and masters,
		which later tests (test_report_scope) then read as the site's first company."""
		from jarvis.permissions import ensure_jarvis_user_role

		ensure_jarvis_user_role()
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": email.split("@")[0],
					"send_welcome_email": 0,
					"user_type": "System User",
					"roles": [{"role": "Jarvis User"}],
				}
			).insert(ignore_permissions=True)
		return email

	def bad_customer_order(self) -> dict:
		return {
			"company": self.company,
			"customer": "NOPE-j2b-c3",
			"selling_price_list": self.price_list,
			"transaction_date": self.today,
			"delivery_date": self.later,
			"items": [{"item_code": self.item, "qty": 1, "rate": 10}],
		}

	def test_a_sales_order_with_a_bad_customer_stays_open_marked(self):
		r, outcomes = self.apply("Sales Order", self.bad_customer_order())
		self.assertEqual((r["ok"], r["error"]["kind"]), (False, "fixable"), r)
		self.assertNotIn("closed", r)
		self.assertEqual(
			[(f["fieldname"], f.get("invalid")) for f in r["error"]["fields"]], [("customer", 1)]
		)
		self.assertEqual(outcomes, [chat_api.OUTCOME_FIXING_IN_PANEL])

	def test_no_create_permission_closes_and_looks_nothing_up(self):
		# Frappe checks permission before links: a person who cannot create a Sales
		# Order is told so, the panel closes, and whether the customer exists stays
		# hidden (no field marks, no link lookup).
		from jarvis.chat import panel_fields

		user = self.user_without_commit("j2b-noperm@example.com")
		conv = frappe.get_doc({"doctype": "Jarvis Conversation", "title": "j2b perm"})
		with as_user(user):
			conv.insert(ignore_permissions=True)
		values = self.bad_customer_order()
		with (
			patch.object(panel_fields, "_link_exists", wraps=panel_fields._link_exists) as lookup,
			patch.object(frappe.db, "rollback"),
			patch("jarvis.chat.api.enqueue_continuation", return_value={}) as cont,
			as_user(user),
		):
			r = actions_api.apply_action(
				frappe.as_json(
					{
						"verb": "create",
						"doctype": "Sales Order",
						"values": values,
						"conversation": conv.name,
						"message": "j2b-perm",
					}
				)
			)
		self.assertEqual((r["ok"], r["error"]["code"]), (False, "PermissionDeniedError"), r)
		self.assertIs(r["closed"], True)
		self.assertNotIn("fields", r["error"])
		self.assertNotIn("marked", r["error"].get("hint", ""))
		lookup.assert_not_called()
		self.assertEqual(
			[c.kwargs.get("outcome") for c in cont.call_args_list], [chat_api.OUTCOME_NOT_FIXABLE]
		)

	def test_a_value_its_source_fetches_is_not_asked_for(self):
		# customer_name is fetched from customer, is_stock_item from the row's item_code.
		from jarvis.chat.panel_fields import _skip_fetched

		doc = frappe.new_doc("Sales Order")
		doc.customer = self.customer
		doc.append("items", {"item_code": self.item})
		found = [
			{"fieldname": "customer_name"},
			{"fieldname": "company"},
			{"fieldname": "is_stock_item", "parentfield": "items", "idx": 1},
		]
		self.assertEqual(_skip_fetched(doc, found), [{"fieldname": "company"}])

	def test_a_sales_invoice_with_a_bad_item_in_a_row_stays_open(self):
		r, outcomes = self.apply(
			"Sales Invoice",
			{
				"company": self.company,
				"customer": self.customer,
				"posting_date": self.today,
				"due_date": self.later,
				"items": [{"item_code": "NOPE-j2b-item", "qty": 1, "rate": 10}],
			},
		)
		self.assertEqual((r["ok"], r["error"]["kind"]), (False, "fixable"), r)
		self.assertNotIn("closed", r)
		self.assertIn("NOPE-j2b-item", r["error"]["message"])
		self.assertEqual(outcomes, [chat_api.OUTCOME_FIXING_IN_PANEL])


class TestFirstFailureClaim(FrappeTestCase):
	def panel(self):
		from jarvis.chat.panel_failure import PanelFailure

		return PanelFailure(f"conv-{frappe.generate_hash(length=8)}", "create_doc", {}, {})

	def test_once_per_draft_until_released(self):
		p = self.panel()
		self.assertTrue(p._first_failure_of("d1"))
		self.assertFalse(p._first_failure_of("d1"))
		self.assertTrue(p._first_failure_of("d2"))
		self.assertGreater(frappe.cache.ttl(frappe.cache.make_key(p._key("d1"))), 0)
		p._release("d1")
		self.assertTrue(p._first_failure_of("d1"))

	def test_no_draft_or_an_outage_sends_nothing(self):
		import redis

		p = self.panel()
		self.assertFalse(p._first_failure_of(""))
		with patch.object(
			frappe.cache, "execute_command", side_effect=redis.exceptions.ConnectionError("down")
		):
			self.assertFalse(p._first_failure_of("d3"))
