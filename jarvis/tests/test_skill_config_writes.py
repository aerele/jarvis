"""A write to a skill or a learned rule asks in every mode.

What these records say is what later chats do, so "confirm all", an approved skill
run and auto mode never apply a change to one unseen: the call parks a card, as it
does on an armed macro's run (``test_macro_owner_arming``). Other writes in those
modes are unaffected.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import pending_confirm
from jarvis.exceptions import InvalidArgumentError
from jarvis.tests._conv_helpers import CONV, _make_conv
from jarvis.tests.test_chat_api import TEST_USER, _cleanup_user_conversations, _ensure_test_user
from jarvis.tests.test_skill_approve_and_run import (
	ADMIN_USER,
	_ensure_admin_user,
	_make_skill,
	_stamp_autorun,
)
from jarvis.tools import _write_risk as wr

SKILL = "Jarvis Custom Skill"
PATTERN = "Jarvis Learned Pattern"


class TestTheSet(FrappeTestCase):
	def test_every_doctype_in_the_set_exists(self):
		for dt in wr.SKILL_CONFIG_DOCTYPES:
			with self.subTest(doctype=dt):
				self.assertTrue(frappe.db.exists("DocType", dt))

	def test_the_skill_and_its_child_tables_are_in_it(self):
		# ``api._SKILL_DOCTYPES`` keeps its own meaning (a skill and its child tables:
		# the armed-macro read rules pluck ``parent`` from the other two).
		self.assertLess(api._SKILL_DOCTYPES, wr.SKILL_CONFIG_DOCTYPES)
		self.assertEqual(len(api._SKILL_DOCTYPES), 3)

	def test_each_one_is_sensitive_with_its_own_line(self):
		for dt in wr.SKILL_CONFIG_DOCTYPES:
			with self.subTest(doctype=dt):
				self.assertEqual(wr.risk_class(dt), "instructions")
				self.assertEqual(wr.risk_class(dt.lower()), "instructions")
				calls = {
					"create_doc": {"doctype": dt, "values": {"x": 1}},
					"create_docs": {"docs": [{"doctype": dt, "values": {"x": 1}}]},
					"update_doc": {"doctype": dt, "name": "n", "changes": {"x": 1}},
					"delete_doc": {"doctype": dt, "name": "n"},
				}
				for tool, args in calls.items():
					self.assertEqual(wr.check(tool, args), "sensitive", tool)
		self.assertEqual(
			wr.risk_line("update_doc", {"doctype": SKILL, "name": "n", "changes": {"instructions": "x"}}),
			"This changes what your assistant is told to do.",
		)


class TestWhatCountsAsAWrite(FrappeTestCase):
	def test_any_tool_that_names_one(self):
		for tool, args in (
			("share_doc", {"doctype": SKILL, "name": "n", "user": "a@example.com"}),
			("assign_to", {"doctype": PATTERN, "name": "n", "users": ["a@example.com"]}),
			("add_comment", {"reference_doctype": SKILL, "reference_name": "n", "content": "c"}),
			("attach_to_doc", {"target_doctype": "jarvis custom skill ", "target_name": "n"}),
			("add_tag", {"dt": "Jarvis Learned Pattern", "dn": "n", "tag": "t"}),
			("create_doc", {"doctype": "ToDo", "values": {"parenttype": "Jarvis Custom Skill Share"}}),
			(
				"create_docs",
				{
					"docs": [
						{"doctype": "ToDo", "values": {"description": "plain"}},
						{"doctype": "Jarvis Shared Skill Slug", "values": {"slug": "x"}},
					]
				},
			),
			(
				"run_method",
				{"method": "frappe.client.set_value", "args": {"doctype": PATTERN, "name": "n"}},
			),
			(
				"run_method",
				{"method": "frappe.client.save", "args": {"doc": '{"doctype": "Jarvis Custom Skill"}'}},
			),
			# A learning sweep writes learned patterns: a person sees the call that starts one.
			("run_method", {"method": "jarvis.chat.personalise_api.anything", "args": {}}),
			("run_method", {"method": "jarvis.chat.voice_notes_api.anything", "args": {}}),
		):
			with self.subTest(tool=tool, args=args):
				self.assertTrue(api._writes_skill_config(tool, args))

	def test_other_writes_do_not_count(self):
		for tool, args in (
			("create_doc", {"doctype": "ToDo", "values": {"description": "Jarvis Custom Skill"}}),
			("share_doc", {"doctype": "ToDo", "name": "n", "user": "a@example.com"}),
			("run_method", {"method": "frappe.client.get_value", "args": {"doctype": "ToDo"}}),
			("run_method", {"method": "jarvis.chat.custom_skills.something", "args": {}}),
		):
			with self.subTest(tool=tool, args=args):
				self.assertFalse(api._writes_skill_config(tool, args))

	def test_a_doctype_that_is_not_text_is_refused(self):
		with self.assertRaises(InvalidArgumentError):
			api._writes_skill_config("update_doc", {"doctype": [SKILL], "name": "n"})

	def test_a_method_the_tool_refuses_is_found_wherever_it_is_named(self):
		denied = "jarvis.chat.custom_skills_api.update_custom_skill"
		for args in (
			{"method": denied, "args": {}},
			{"method": f" {denied}", "args": {}},
			# A whitelisted helper that calls the method it is handed.
			{"method": "frappe.model.mapper.make_mapped_doc", "args": {"method": denied, "source_name": "n"}},
			{
				"method": "some.helper",
				"args": {"calls": [{"method": "jarvis.chat.learned_api.batch_approve"}]},
			},
		):
			with self.subTest(args=args):
				self.assertTrue(api._calls_a_denied_method(args))
		for args in (
			{"method": "frappe.client.get_value", "args": {"doctype": "ToDo"}},
			{"method": "erpnext.x.make_y", "args": {"note": "see jarvis.chat.custom_skills_api.x"}},
			{"method": "jarvis.chat.custom_skills.something", "args": {}},
		):
			with self.subTest(args=args):
				self.assertIsNone(api._calls_a_denied_method(args))

	def test_what_switches_approve_and_run_on(self):
		on = (
			("update_doc", {"doctype": SKILL, "name": "n", "changes": {"allow_approve_run": 1}}),
			(
				"update_doc",
				{"doctype": " jarvis custom skill", "name": "n", "changes": {"allow_approve_run": "true"}},
			),
			(
				"update_doc",
				{"doctype": SKILL, "updates": [{"name": "n", "changes": {"allow_approve_run": "1"}}]},
			),
			("create_doc", {"doctype": SKILL, "values": {"skill_name": "x", "allow_approve_run": True}}),
			("create_docs", {"docs": [{"doctype": SKILL, "values": {"allow_approve_run": "Yes"}}]}),
			(
				"run_method",
				{
					"method": "frappe.client.set_value",
					"args": {"doctype": SKILL, "name": "n", "fieldname": "allow_approve_run", "value": 1},
				},
			),
		)
		off = (
			("update_doc", {"doctype": SKILL, "name": "n", "changes": {"allow_approve_run": 0}}),
			("update_doc", {"doctype": SKILL, "name": "n", "changes": {"allow_approve_run": "false"}}),
			("update_doc", {"doctype": SKILL, "name": "n", "changes": {"instructions": "x"}}),
			("update_doc", {"doctype": "ToDo", "name": "n", "changes": {"allow_approve_run": 1}}),
			(
				"run_method",
				{"method": "frappe.client.set_value", "args": {"doctype": SKILL, "fieldname": "enabled"}},
			),
		)
		for tool, args in on:
			with self.subTest(on=args):
				self.assertTrue(api._arms_a_skill(tool, args))
		for tool, args in off:
			with self.subTest(off=args):
				self.assertFalse(api._arms_a_skill(tool, args))


class _ModeBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)
		self._sweep()
		self.skill = _make_skill(TEST_USER, name="cfgwrite-own")

	def tearDown(self):
		frappe.set_user(self._orig)
		self._sweep()

	@staticmethod
	def _sweep():
		from jarvis.tools import _agent_run_ctx

		frappe.db.rollback()
		_agent_run_ctx.take_request_autorun_applied()
		_agent_run_ctx.take_armed_by_skill()
		for conv in frappe.get_all(
			CONV, filters={"owner": ["in", [TEST_USER, ADMIN_USER]]}, fields=["name", "owner"]
		):
			pending_confirm.clear_for_conversation(conv.owner, conv.name)
			frappe.delete_doc(CONV, conv.name, force=True, ignore_permissions=True)
		for name in frappe.get_all(SKILL, filters={"skill_name": ["like", "cfgwrite-%"]}, pluck="name"):
			frappe.delete_doc(SKILL, name, force=True, ignore_permissions=True)
		for name in frappe.get_all("ToDo", filters={"owner": TEST_USER}, pluck="name"):
			frappe.delete_doc("ToDo", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	# One conversation per mode, each already writing without a card.
	def _confirm_all(self):
		conv = _make_conv(TEST_USER)
		api._request_autorun_arm(conv, "")
		return conv

	def _skill_run(self):
		conv = _make_conv(TEST_USER)
		_stamp_autorun(conv)
		return conv

	def _auto_mode(self):
		if not frappe.get_meta(CONV).has_field("auto_mode"):
			self.skipTest("auto_mode is not migrated on this site")
		conv = _make_conv(TEST_USER)
		frappe.db.set_value(CONV, conv, "auto_mode", 1, update_modified=False)
		return conv

	MODES = ("confirm_all", "skill_run", "auto_mode")

	def _in_each_mode(self, check):
		"""Run ``check(conv)`` once per mode, each in its own subTest."""
		for mode in self.MODES:
			with self.subTest(mode=mode):
				check(getattr(self, f"_{mode}")())


class TestEveryModeAsks(_ModeBase):
	def test_an_ordinary_write_still_runs_without_a_card(self):
		# Non-vacuity: each of these conversations really is writing unasked.
		def check(conv):
			with patch("jarvis.api.dispatch_confirmed", return_value={"ok": True, "data": {}}) as disp:
				res = api._run_tool(
					"create_doc",
					{"doctype": "ToDo", "values": {"description": "cfgwrite"}},
					conversation=conv,
				)
			self.assertTrue(disp.called, res)

		self._in_each_mode(check)

	def test_changing_a_skill_parks_a_card_that_says_why(self):
		def check(conv):
			with patch("jarvis.api.dispatch_confirmed") as disp:
				res = api._run_tool(
					"update_doc",
					{"doctype": SKILL, "name": self.skill, "changes": {"instructions": "something else"}},
					conversation=conv,
				)
			data = res.get("data") or {}
			self.assertEqual(data.get("status"), "pending_confirmation", res)
			self.assertFalse(disp.called)
			self.assertEqual(
				(data.get("preview") or {}).get("risk_line"), wr.RISK_LINES["instructions"], data
			)
			self.assertEqual(frappe.db.get_value(SKILL, self.skill, "instructions"), "do the thing")

		self._in_each_mode(check)

	def test_a_lower_case_doctype_is_the_same_skill(self):
		def check(conv):
			with patch("jarvis.api.dispatch_confirmed") as disp:
				res = api._run_tool(
					"update_doc",
					{"doctype": " jarvis custom skill", "name": self.skill, "changes": {"enabled": 0}},
					conversation=conv,
				)
			self.assertFalse(disp.called, res)
			self.assertEqual((res.get("data") or {}).get("status"), "pending_confirmation", res)
			self.assertEqual(int(frappe.db.get_value(SKILL, self.skill, "enabled")), 1)

		self._in_each_mode(check)

	def test_other_tools_that_name_a_skill_park_too(self):
		calls = (
			("share_doc", {"doctype": SKILL, "name": self.skill, "user": "Administrator"}),
			(
				"run_method",
				{
					"method": "frappe.client.set_value",
					"args": {"doctype": SKILL, "name": self.skill, "fieldname": "enabled", "value": 0},
				},
			),
		)
		for tool, args in calls:

			def check(conv, tool=tool, args=args):
				with patch("jarvis.api.dispatch_confirmed") as disp:
					res = api._run_tool(tool, args, conversation=conv)
				self.assertEqual((res.get("data") or {}).get("status"), "pending_confirmation", (tool, res))
				self.assertFalse(disp.called)

			self._in_each_mode(check)

	def test_a_method_the_tool_refuses_is_refused_before_a_card(self):
		# A card for one could only fail at Confirm.
		calls = (
			{"method": "jarvis.chat.custom_skills_api.update_custom_skill", "args": {"name": self.skill}},
			{"method": "jarvis.chat.learned_api.batch_approve", "args": {"names": ["x"]}},
			{
				"method": "frappe.model.mapper.make_mapped_doc",
				"args": {"method": "jarvis.chat.custom_skills_api.delete_custom_skill", "source_name": "x"},
			},
		)
		for args in calls:

			def check(conv, args=args):
				with patch("jarvis.api.dispatch_confirmed") as disp:
					res = api._run_tool("run_method", args, conversation=conv)
				self.assertFalse(res.get("ok"), res)
				self.assertIn("cannot be called from a tool", str(res))
				self.assertFalse(disp.called)
				self.assertFalse(pending_confirm.has_live_card(TEST_USER, conv))

			self._in_each_mode(check)
			check(_make_conv(TEST_USER))  # and in a chat that asks for everything

	def test_a_doctype_that_is_not_text_is_a_tool_error(self):
		def check(conv):
			with patch("jarvis.api.dispatch_confirmed") as disp:
				res = api._run_tool(
					"share_doc",
					{"doctype": [SKILL], "name": self.skill, "user": "Administrator"},
					conversation=conv,
				)
			self.assertFalse(res.get("ok"), res)
			self.assertFalse(disp.called)

		self._in_each_mode(check)

	def test_arguments_too_deep_to_read_park_a_card(self):
		conv = self._confirm_all()
		with (
			patch.object(api, "_writes_skill_config", side_effect=RecursionError),
			patch("jarvis.api.dispatch_confirmed") as disp,
		):
			res = api._run_tool(
				"create_doc", {"doctype": "ToDo", "values": {"description": "cfgwrite"}}, conversation=conv
			)
		self.assertFalse(disp.called, res)
		self.assertEqual((res.get("data") or {}).get("status"), "pending_confirmation", res)

	def test_a_read_is_never_scanned(self):
		conv = self._confirm_all()
		with patch.object(api, "_writes_skill_config") as scan:
			api._run_tool("get_schema", {"doctype": "ToDo"}, conversation=conv)
		scan.assert_not_called()


class TestRowsChangeThroughTheirParent(_ModeBase):
	"""A share or role row is written when its skill is saved, where the skill's own
	rules decide who may change it. A row written as a document of its own is refused."""

	SHARE = "Jarvis Custom Skill Share"

	def _share_row(self):
		return {
			"doctype": self.SHARE,
			"parenttype": SKILL,
			"parentfield": "shared_with",
			"parent": self.skill,
			"user": "Administrator",
		}

	def _rows(self):
		return frappe.get_all(self.SHARE, filters={"parent": self.skill}, pluck="name")

	def test_the_three_child_tables_carry_the_rule(self):
		from jarvis.permissions import ChangedThroughParentOnly

		for dt in (
			"Jarvis Custom Skill Share",
			"Jarvis Custom Skill Allowed Role",
			"Jarvis Learned Pattern Role",
		):
			with self.subTest(doctype=dt):
				self.assertTrue(issubclass(type(frappe.new_doc(dt)), ChangedThroughParentOnly))

	def test_saving_the_skill_still_writes_its_rows(self):
		from jarvis.chat import custom_skills_api

		out = custom_skills_api.share_custom_skill(self.skill, ["Administrator"])
		self.assertEqual(out["data"]["count"], 1)
		self.assertEqual(len(self._rows()), 1)
		custom_skills_api.share_custom_skill(self.skill, [])
		self.assertEqual(self._rows(), [])

	def test_a_row_inserted_by_itself_is_refused(self):
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(self._share_row()).insert()
		self.assertEqual(self._rows(), [])

	def test_a_row_saved_or_deleted_by_itself_is_refused(self):
		from jarvis.chat import custom_skills_api

		custom_skills_api.share_custom_skill(self.skill, ["Administrator"])
		(row,) = self._rows()
		doc = frappe.get_doc(self.SHARE, row)
		doc.user = TEST_USER
		with self.assertRaises(frappe.PermissionError):
			doc.save()
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc(self.SHARE, row)
		self.assertEqual(frappe.db.get_value(self.SHARE, row, "user"), "Administrator")

	def test_server_code_that_says_so_may_write_a_row(self):
		# A patch or a fixture: it bypasses permissions on purpose and says so.
		frappe.get_doc(self._share_row()).insert(ignore_permissions=True)
		(row,) = self._rows()
		frappe.delete_doc(self.SHARE, row, ignore_permissions=True)
		self.assertEqual(self._rows(), [])


class TestAnUnseenWriteIsRefusedAtTheSave(_ModeBase):
	"""The gate parks a call whose arguments name one of these records. A write the
	arguments could not show (a method, an import, a hook) is refused at the save."""

	def test_under_a_write_nobody_was_shown(self):
		doc = frappe.get_doc(SKILL, self.skill)
		doc.instructions = "something else"
		with wr.guard_scope([], brake=True):
			self.assertTrue(wr.uncarded_write())
			with self.assertRaises(frappe.PermissionError):
				doc.save()
			with self.assertRaises(frappe.PermissionError):
				frappe.delete_doc(SKILL, self.skill)
			for dt in ("Jarvis Skill Promotion Request", "Jarvis Shared Skill Slug", PATTERN):
				with self.subTest(doctype=dt):
					with self.assertRaises(frappe.PermissionError):
						frappe.new_doc(dt).run_method("validate")
					with self.assertRaises(frappe.PermissionError):
						frappe.new_doc(dt).run_method("on_trash")
		self.assertFalse(wr.uncarded_write())
		self.assertEqual(frappe.db.get_value(SKILL, self.skill, "instructions"), "do the thing")

	def test_a_write_a_person_confirmed_goes_through(self):
		doc = frappe.get_doc(SKILL, self.skill)
		doc.instructions = "something else"
		with wr.guard_scope([]):
			self.assertFalse(wr.uncarded_write())
			doc.save()
		self.assertEqual(frappe.db.get_value(SKILL, self.skill, "instructions"), "something else")


class TestTheAssistantDoesNotArm(_ModeBase):
	"""Switching "Allow Approve & run" on decides what later runs do unasked. A person
	does that on the skill's page; a tool call never does, whoever the chat belongs to."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_admin_user()

	def _in_a_tool_call(self):
		prev = getattr(frappe.local, "jarvis_dispatch_depth", 0)
		frappe.local.jarvis_dispatch_depth = prev + 1
		self.addCleanup(setattr, frappe.local, "jarvis_dispatch_depth", prev)

	def _armed(self):
		return int(frappe.db.get_value(SKILL, self.skill, "allow_approve_run") or 0)

	def test_an_admin_arms_it_on_the_page(self):
		frappe.set_user(ADMIN_USER)
		doc = frappe.get_doc(SKILL, self.skill)
		doc.allow_approve_run = 1
		doc.save(ignore_permissions=True)
		self.assertEqual(self._armed(), 1)

	def test_a_tool_call_does_not_even_for_an_admin(self):
		frappe.set_user(ADMIN_USER)
		self._in_a_tool_call()
		doc = frappe.get_doc(SKILL, self.skill)
		doc.allow_approve_run = 1
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)
		self.assertEqual(self._armed(), 0)

	def test_nor_does_a_job_a_tool_call_queued(self):
		# Such a job runs under the guard's scope, with no dispatch depth of its own.
		frappe.set_user(ADMIN_USER)
		doc = frappe.get_doc(SKILL, self.skill)
		doc.allow_approve_run = 1
		with wr.guard_scope([]):
			with self.assertRaises(frappe.PermissionError):
				doc.save(ignore_permissions=True)
		self.assertEqual(self._armed(), 0)

	def test_the_call_is_refused_before_a_card_exists(self):
		# A card the user could only confirm into an error would be worse than a refusal.
		frappe.set_user(ADMIN_USER)
		calls = {
			"update_doc": {"doctype": SKILL, "name": self.skill, "changes": {"allow_approve_run": 1}},
			"update_doc (many)": {
				"doctype": SKILL,
				"updates": [{"name": self.skill, "changes": {"allow_approve_run": "1"}}],
			},
			"create_doc": {
				"doctype": "jarvis custom skill",
				"values": {
					"skill_name": "cfgwrite-armed",
					"description": "d",
					"instructions": "i",
					"allow_approve_run": True,
				},
			},
		}
		for label, args in calls.items():
			with self.subTest(call=label):
				conv = _make_conv(ADMIN_USER)
				res = api._run_tool(label.split(" ")[0], args, conversation=conv)
				self.assertFalse(res.get("ok"), res)
				self.assertIn("skill's own page", str(res))
				self.assertIn("Leave allow_approve_run out", str(res))
				self.assertFalse(pending_confirm.has_live_card(ADMIN_USER, conv))
		self.assertEqual(self._armed(), 0)

	def test_switching_it_off_from_chat_still_parks_a_card(self):
		frappe.db.set_value(SKILL, self.skill, "allow_approve_run", 1)
		conv = _make_conv(TEST_USER)
		res = api._run_tool(
			"update_doc",
			{"doctype": SKILL, "name": self.skill, "changes": {"allow_approve_run": 0}},
			conversation=conv,
		)
		self.assertEqual((res.get("data") or {}).get("status"), "pending_confirmation", res)

	def test_a_tool_call_may_edit_or_switch_off_an_armed_skill(self):
		frappe.db.set_value(SKILL, self.skill, "allow_approve_run", 1)
		self._in_a_tool_call()
		doc = frappe.get_doc(SKILL, self.skill)
		doc.description = "edited while armed"
		doc.save()
		self.assertEqual(self._armed(), 1)
		doc.allow_approve_run = 0
		doc.save()
		self.assertEqual(self._armed(), 0)
