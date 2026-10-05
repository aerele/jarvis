"""A macro's owner arms it, on the macro form only (``JarvisMacro._guard_arming``).

What is pinned here:

* the owner arms and disarms their own macro through ``macros_api.update_macro`` and
  ``create_macro``; nobody else arms it, an admin included; a held macro cannot be
  armed; ``get_macro`` says whether the viewer may (``can_arm``) and why not;
* both endpoints are POST only and refused inside a tool call;
* arming is refused from every other route: the assistant's ``update_doc`` and
  ``create_doc``, ``run_method`` reaching ``frappe.client``, a bulk update of 20 or
  more documents (its background job), a confirmation card the user approved, the
  Desk form and its Duplicate, and Data Import; a payload cannot carry the form's
  marker;
* an armed macro can be switched off, taken off its schedule and disarmed from any of
  those routes; its steps, its summary, switching it on and its schedule cannot be
  changed there; an unarmed macro is edited from a tool as before;
* before the migrate (no hold field) the rule from before applies: only a Jarvis
  Admin arms, still only their own macro and only on the form;
* the notice names every tool an armed macro runs without asking, and every one that
  still asks.

The tests where a plain owner arms need the hold field and skip on a site that has
not migrated it. A refusal is tested on a Jarvis Admin's own macro, which the owner
rule allows with or without the field, so only the route can be what refuses it.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.handler import is_valid_http_method
from frappe.tests.utils import FrappeTestCase
from frappe.utils import set_request

from jarvis import api
from jarvis.chat import actions_api, macros_api, pending_confirm
from jarvis.exceptions import PermissionDeniedError
from jarvis.jarvis.doctype.jarvis_macro import jarvis_macro as controller
from jarvis.permissions import ensure_jarvis_admin_role
from jarvis.tests._conv_helpers import _make_conv
from jarvis.tests._pending_action_helpers import ensure_user
from jarvis.tools import registry
from jarvis.tools.run_method import run_method

MACRO = "Jarvis Macro"
CONV = "Jarvis Conversation"
OWNER = "macro-arm-owner@example.com"
OTHER = "macro-arm-other@example.com"
ADMIN = "macro-arm-admin@example.com"
USERS = (OWNER, OTHER, ADMIN)
PATH = "jarvis.chat.macros_api."
EM_DASH = chr(0x2014)
_MISSING = object()


def _columns() -> bool:
	return controller.hold_fields_exist()


def _form_only() -> str:
	return controller.arm_on_form_only()


class ArmingBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Users are created as Administrator: a plain session cannot insert a User.
		frappe.set_user("Administrator")
		ensure_jarvis_admin_role()
		ensure_user(OWNER)
		ensure_user(OTHER)
		ensure_user(ADMIN, roles=("Jarvis User", "Jarvis Admin"))
		if "System Manager" in frappe.get_roles(ADMIN):
			frappe.get_doc("User", ADMIN).remove_roles("System Manager")
			frappe.db.commit()

	def setUp(self):
		self._orig = frappe.session.user
		self._purge()
		self.addCleanup(self._purge)

	def tearDown(self):
		frappe.set_user(self._orig)

	def _purge(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("Jarvis Settings", None, "disable_armed_skip", 0, update_modified=False)
		for owner in USERS:
			for dt in (MACRO, CONV):
				for name in frappe.get_all(dt, filters={"owner": owner}, pluck="name"):
					frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _macro(self, owner, tag="m", *, armed=False, steps=("first prompt",), **fields):
		"""An unarmed macro of ``owner``, saved the plain way; ``armed`` and any other
		field are written raw after it: these are the state a test starts from."""
		frappe.set_user(owner)
		doc = frappe.get_doc(
			{
				"doctype": MACRO,
				"macro_name": f"arm-{tag}",
				"steps": [{"prompt": p} for p in steps],
			}
		).insert(ignore_permissions=True)
		raw = {**fields, **({"skip_confirmation": 1} if armed else {})}
		if raw:
			frappe.db.set_value(MACRO, doc.name, raw, update_modified=False)
		frappe.db.commit()
		return doc.name

	def _armed(self, macro) -> int:
		return frappe.utils.cint(frappe.db.get_value(MACRO, macro, "skip_confirmation"))

	def _row(self, macro):
		return frappe.db.get_value(
			MACRO,
			macro,
			["enabled", "schedule_enabled", "skip_confirmation", "schedule_time", "merged_prompt"],
			as_dict=True,
		)

	def _prompts(self, macro):
		return [s.prompt for s in frappe.get_doc(MACRO, macro).steps]


class WithColumns(ArmingBase):
	def setUp(self):
		if not _columns():
			self.skipTest("the site has not migrated the hold fields (bench migrate)")
		super().setUp()


# --------------------------------------------------------------------------- #
# The owner arms it on the form
# --------------------------------------------------------------------------- #
class TestTheOwnerArms(WithColumns):
	def test_the_owner_arms_and_disarms_through_update_macro(self):
		macro = self._macro(OWNER)
		frappe.set_user(OWNER)
		macros_api.update_macro(macro, skip_confirmation=1)
		self.assertEqual(self._armed(macro), 1)
		macros_api.update_macro(macro, skip_confirmation=0)
		self.assertEqual(self._armed(macro), 0)

	def test_the_owner_creates_an_armed_macro(self):
		frappe.set_user(OWNER)
		name = macros_api.create_macro("arm-new", steps=[{"prompt": "do it"}], skip_confirmation=1)["data"][
			"name"
		]
		self.assertEqual(self._armed(name), 1)
		self.assertEqual(frappe.db.get_value(MACRO, name, "owner"), OWNER)
		self.assertEqual(macros_api.get_macro(name)["skip_confirmation"], 1)

	def test_the_owner_edits_an_armed_macro_on_the_form_and_it_stays_armed(self):
		# Moved here from a bare doc.save(): the form is where an armed macro's steps,
		# summary, switch and schedule change.
		macro = self._macro(OWNER, armed=True, enabled=0)
		frappe.set_user(OWNER)
		macros_api.update_macro(
			macro,
			steps=[{"prompt": "do a different thing"}],
			merged_prompt="my own summary",
			merged_prompt_edited=1,
			enabled=1,
			schedule_enabled=1,
			schedule_time="07:30",
		)
		self.assertEqual(self._prompts(macro), ["do a different thing"])
		row = self._row(macro)
		self.assertEqual((row.skip_confirmation, row.enabled, row.schedule_enabled), (1, 1, 1))
		self.assertEqual(row.merged_prompt, "my own summary")

	def test_nobody_else_arms_it_an_admin_included(self):
		macro = self._macro(OWNER)
		for who in (OTHER, ADMIN, "Administrator"):
			with self.subTest(who=who):
				frappe.set_user(who)
				with self.assertRaises(frappe.PermissionError):
					macros_api.update_macro(macro, skip_confirmation=1)
				frappe.db.rollback()
		self.assertEqual(self._armed(macro), 0)

	def test_the_controller_refuses_a_non_owner_even_with_the_forms_marker(self):
		# Administrator passes the write permission check: the owner rule is the
		# controller's own, not the permission system's.
		macro = self._macro(OWNER)
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.PermissionError) as raised:
			macros_api.update_macro(macro, skip_confirmation=1)
		self.assertIn("Only the macro's owner", str(raised.exception))
		self.assertEqual(self._armed(macro), 0)

	def test_a_non_owner_cannot_edit_an_armed_macros_steps_on_the_form(self):
		macro = self._macro(OWNER, armed=True)
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.PermissionError):
			macros_api.update_macro(macro, steps=[{"prompt": "someone else's words"}])
		self.assertEqual(self._prompts(macro), ["first prompt"])

	def test_a_held_macro_cannot_be_armed(self):
		macro = self._macro(OWNER, admin_hold=1, admin_hold_reason="Too many emails")
		frappe.set_user(OWNER)
		self.assertEqual(macros_api.get_macro(macro)["can_arm"], 0)
		with self.assertRaises(controller.MacroOnHoldError):
			macros_api.update_macro(macro, skip_confirmation=1)
		self.assertEqual(self._armed(macro), 0)


class TestCanArm(WithColumns):
	def test_the_owner_may_and_the_form_is_told_what_it_means(self):
		macro = self._macro(OWNER)
		frappe.set_user(OWNER)
		got = macros_api.get_macro(macro)
		self.assertEqual((got["can_arm"], got["arm_blocked_reason"]), (1, ""))
		self.assertEqual(got["arm_notice"], macros_api.arm_notice())
		self.assertEqual(
			macros_api.get_new_macro_arming(),
			{"can_arm": 1, "arm_blocked_reason": "", "arm_notice": macros_api.arm_notice()},
		)

	def test_anyone_else_may_not_and_is_told_why(self):
		macro = self._macro(OWNER)
		frappe.set_user("Administrator")  # the one non-owner who can open it
		got = macros_api.get_macro(macro)
		self.assertEqual(got["can_arm"], 0)
		self.assertIn("owner", got["arm_blocked_reason"])

	def test_the_site_kill_switch_is_said(self):
		frappe.db.set_value("Jarvis Settings", None, "disable_armed_skip", 1, update_modified=False)
		macro = self._macro(OWNER)
		frappe.set_user(OWNER)
		got = macros_api.get_macro(macro)
		self.assertEqual(got["can_arm"], 0)
		self.assertIn("switched off for this whole site", got["arm_blocked_reason"])


# --------------------------------------------------------------------------- #
# Before the migrate: the rule from before
# --------------------------------------------------------------------------- #
class TestWithoutTheHoldField(ArmingBase):
	def _no_field(self):
		target = patch.object(controller, "hold_fields_exist", return_value=False)
		target.start()
		self.addCleanup(target.stop)

	def test_a_plain_owner_cannot_arm_and_is_told_so(self):
		self._no_field()
		macro = self._macro(OWNER)
		frappe.set_user(OWNER)
		got = macros_api.get_macro(macro)
		self.assertEqual(got["can_arm"], 0)
		self.assertIn("Jarvis Admin", got["arm_blocked_reason"])
		self.assertEqual(macros_api.get_new_macro_arming()["can_arm"], 0)
		with self.assertRaises(frappe.PermissionError):
			macros_api.update_macro(macro, skip_confirmation=1)
		frappe.db.rollback()
		with self.assertRaises(frappe.PermissionError):
			macros_api.create_macro("arm-new", steps=[{"prompt": "x"}], skip_confirmation=1)
		self.assertEqual(self._armed(macro), 0)
		self.assertFalse(frappe.db.exists(MACRO, {"owner": OWNER, "macro_name": "arm-new"}))

	def test_a_jarvis_admin_still_arms_their_own_macro_on_the_form(self):
		self._no_field()
		macro = self._macro(ADMIN)
		frappe.set_user(ADMIN)
		self.assertEqual(macros_api.get_macro(macro)["can_arm"], 1)
		macros_api.update_macro(macro, skip_confirmation=1)
		self.assertEqual(self._armed(macro), 1)


# --------------------------------------------------------------------------- #
# The two endpoints: POST only, never from a tool
# --------------------------------------------------------------------------- #
class TestTheEndpoints(ArmingBase):
	@staticmethod
	def _restore_request(prev):
		if prev is _MISSING:
			if hasattr(frappe.local, "request"):
				del frappe.local.request
		else:
			frappe.local.request = prev

	def test_both_are_post_only(self):
		prev = getattr(frappe.local, "request", _MISSING)
		self.addCleanup(self._restore_request, prev)
		for fn in (macros_api.create_macro, macros_api.update_macro):
			with self.subTest(fn=fn.__name__):
				self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[fn], ["POST"])
				set_request(method="GET", path=f"/api/method/{PATH}{fn.__name__}")
				with self.assertRaises(frappe.PermissionError):
					is_valid_http_method(fn)

	def _in_a_tool(self, fn, **kwargs):
		"""Call ``fn`` as the body of a registered tool: the real ``registry.dispatch``
		holds the depth counter as it does for a model's tool call."""
		probe = "zz_macro_arm_probe"

		def body():
			try:
				fn(**kwargs)
			except Exception as e:
				return e
			return None

		with (
			patch.dict(registry._TOOLS, {probe: body}),
			patch.dict(registry._ACCEPTS_VAR_KW, {probe: False}),
			patch.dict(registry._ACCEPTED_PARAMS, {probe: set()}),
		):
			return registry.dispatch(probe, {})

	def test_both_are_refused_inside_a_tool_call(self):
		macro = self._macro(ADMIN)
		frappe.set_user(ADMIN)
		for fn, kwargs in (
			(macros_api.update_macro, {"name": macro, "skip_confirmation": 1}),
			(
				macros_api.create_macro,
				{"macro_name": "arm-tool", "steps": [{"prompt": "x"}], "skip_confirmation": 1},
			),
		):
			with self.subTest(fn=fn.__name__):
				err = self._in_a_tool(fn, **kwargs)
				self.assertIsInstance(err, frappe.PermissionError)
				self.assertIn("inside a tool", str(err))
		self.assertEqual(self._armed(macro), 0)
		self.assertFalse(frappe.db.exists(MACRO, {"owner": ADMIN, "macro_name": "arm-tool"}))

	def test_and_through_run_method(self):
		macro = self._macro(ADMIN)
		frappe.set_user(ADMIN)
		with self.assertRaises(PermissionDeniedError):
			run_method(PATH + "update_macro", {"name": macro, "skip_confirmation": 1})
		self.assertEqual(self._armed(macro), 0)


# --------------------------------------------------------------------------- #
# Every other route is refused arming
# --------------------------------------------------------------------------- #
class TestNoOtherRouteArms(ArmingBase):
	"""On a Jarvis Admin's own macro, which the owner rule allows with or without the
	hold field: what refuses is the route."""

	def _refused(self, fn, macro=None):
		with self.assertRaises((frappe.PermissionError, PermissionDeniedError)) as raised:
			fn()
		self.assertIn("only on the macro's own page", str(raised.exception))
		frappe.db.rollback()
		if macro:
			self.assertEqual(self._armed(macro), 0)

	def test_the_assistants_update_doc_and_create_doc(self):
		macro = self._macro(ADMIN)
		frappe.set_user(ADMIN)
		self._refused(
			lambda: registry.dispatch(
				"update_doc", {"doctype": MACRO, "name": macro, "changes": {"skip_confirmation": 1}}
			),
			macro,
		)
		self._refused(
			lambda: registry.dispatch(
				"create_doc",
				{
					"doctype": MACRO,
					"values": {
						"macro_name": "arm-made",
						"steps": [{"prompt": "x"}],
						"skip_confirmation": 1,
						"flags": {controller.FORM_FLAG: True},
					},
				},
			)
		)
		self.assertFalse(frappe.db.exists(MACRO, {"owner": ADMIN, "macro_name": "arm-made"}))

	def test_run_method_reaching_frappe_client(self):
		macro = self._macro(ADMIN)
		frappe.set_user(ADMIN)
		self._refused(
			lambda: run_method(
				"frappe.client.set_value",
				{"doctype": MACRO, "name": macro, "fieldname": "skip_confirmation", "value": 1},
			),
			macro,
		)
		doc = frappe.get_doc(MACRO, macro).as_dict()
		doc.update({"skip_confirmation": 1, "flags": {controller.FORM_FLAG: True}})
		self._refused(lambda: run_method("frappe.client.save", {"doc": json.dumps(doc, default=str)}), macro)
		self._refused(
			lambda: run_method(
				"frappe.client.insert",
				{
					"doc": {
						"doctype": MACRO,
						"macro_name": "arm-rest",
						"steps": [{"prompt": "x"}],
						"skip_confirmation": 1,
					}
				},
			)
		)

	def test_rest_and_the_desk_form(self):
		macro = self._macro(ADMIN)
		frappe.set_user(ADMIN)
		self._refused(lambda: frappe.client.set_value(MACRO, macro, "skip_confirmation", 1), macro)
		doc = frappe.get_doc(MACRO, macro).as_dict()
		doc.update({"skip_confirmation": 1, "flags": {controller.FORM_FLAG: True}})
		from frappe.desk.form.save import savedocs

		self._refused(lambda: savedocs(json.dumps(doc, default=str), "Save"), macro)

	def test_a_desk_duplicate_of_an_armed_macro(self):
		macro = self._macro(ADMIN, armed=True)
		frappe.set_user(ADMIN)
		copy = frappe.copy_doc(frappe.get_doc(MACRO, macro))
		copy.macro_name = "arm-copy"
		self._refused(copy.insert)
		self.assertFalse(frappe.db.exists(MACRO, {"owner": ADMIN, "macro_name": "arm-copy"}))

	def test_data_import(self):
		# The doctype does not allow imports at all, and the row an importer would
		# build (new_doc + update with the file's values) cannot carry the marker.
		self.assertFalse(frappe.get_meta(MACRO).allow_import)
		frappe.set_user("Administrator")
		importer = frappe.get_doc(
			{"doctype": "Data Import", "reference_doctype": MACRO, "import_type": "Insert New Records"}
		)
		with self.assertRaises(frappe.ValidationError):
			importer.validate_doctype()
		frappe.set_user(ADMIN)
		row = frappe.new_doc(MACRO)
		row.update(
			{
				"macro_name": "arm-import",
				"steps": [{"prompt": "x"}],
				"skip_confirmation": 1,
				"flags": {controller.FORM_FLAG: True},
			}
		)
		self.assertFalse(row.flags.get(controller.FORM_FLAG))
		self._refused(row.insert)

	def test_a_bulk_update_of_twenty_runs_as_a_background_job_and_arms_nothing(self):
		from frappe.desk.doctype.bulk_update import bulk_update

		macros = [self._macro(ADMIN, f"b{i}") for i in range(20)]
		frappe.set_user(ADMIN)
		with patch("frappe.enqueue") as enqueue:
			bulk_update.submit_cancel_or_update_docs(
				MACRO, macros, action="update", data={"skip_confirmation": 1}
			)
		(job,) = enqueue.call_args_list
		# What the worker runs.
		kwargs = {k: v for k, v in job.kwargs.items() if k not in ("queue", "timeout")}
		failed = job.args[0](**kwargs)
		self.assertEqual(sorted(failed), sorted(macros))
		self.assertEqual([m for m in macros if self._armed(m)], [])

	def test_an_approved_confirmation_card(self):
		macro = self._macro(ADMIN)
		conv = _make_conv(ADMIN)
		frappe.set_user(ADMIN)
		args = {"doctype": MACRO, "name": macro, "changes": {"skip_confirmation": 1}}
		# The park's own dry run already refuses it: no card is offered.
		refused = api._run_tool("update_doc", args, conversation=conv)
		self.assertFalse(refused["ok"])
		self.assertIn("only on the macro's own page", refused["error"]["message"])
		self.assertEqual(pending_confirm.list_for_owner(ADMIN, conversation=conv), [])
		# A card that got parked anyway (its dry run skipped, or parked before this
		# rule) is refused when the user approves it.
		with patch.object(api, "_run_preview", return_value={}):
			parked = api._run_tool("update_doc", args, conversation=conv)
		self.assertEqual((parked.get("data") or {}).get("status"), "pending_confirmation", parked)
		[card] = pending_confirm.list_for_owner(ADMIN, conversation=conv)
		res = actions_api.confirm_tool(card["token"], conversation=conv)
		self.assertFalse(res["ok"])
		self.assertEqual(self._armed(macro), 0)


# --------------------------------------------------------------------------- #
# An armed macro off the form: the safe direction only
# --------------------------------------------------------------------------- #
class TestAnArmedMacroOffTheForm(ArmingBase):
	def _armed_on(self, tag="on"):
		return self._macro(OWNER, tag, armed=True, enabled=1, schedule_enabled=1, schedule_time="09:00:00")

	def _routes(self, macro):
		"""``{route: apply(changes)}``, each saving the macro as its owner."""

		def set_value(changes):
			return frappe.client.set_value(MACRO, macro, changes)

		def tool(changes):
			return registry.dispatch("update_doc", {"doctype": MACRO, "name": macro, "changes": changes})

		return {"frappe.client.set_value": set_value, "update_doc": tool}

	def test_it_can_be_switched_off_unscheduled_and_disarmed_from_anywhere(self):
		for route in ("frappe.client.set_value", "update_doc"):
			for field in ("enabled", "schedule_enabled", "skip_confirmation"):
				with self.subTest(route=route, field=field):
					macro = self._armed_on(f"{route}-{field}")
					frappe.set_user(OWNER)
					self._routes(macro)[route]({field: 0})
					frappe.db.commit()
					self.assertEqual(self._row(macro)[field], 0)
					self._purge()

	def test_what_it_does_and_when_it_runs_cannot_be_changed_there(self):
		for route in ("frappe.client.set_value", "update_doc"):
			for what in ("steps", "summary", "schedule"):
				with self.subTest(route=route, what=what):
					macro = self._armed_on(f"{route}-{what}")
					step = frappe.get_doc(MACRO, macro).steps[0].name
					changes = {
						"steps": {"steps": [{"name": step, "prompt": "something else"}]},
						"summary": {"merged_prompt": "a summary written elsewhere"},
						"schedule": {"schedule_time": "03:00:00"},
					}[what]
					frappe.set_user(OWNER)
					with self.assertRaises((frappe.PermissionError, PermissionDeniedError)) as raised:
						self._routes(macro)[route](changes)
					self.assertIn(what, str(raised.exception))
					frappe.db.rollback()
					row = self._row(macro)
					self.assertEqual(self._prompts(macro), ["first prompt"])
					self.assertEqual((row.merged_prompt or "", str(row.schedule_time)), ("", "9:00:00"))
					self._purge()

	def test_it_cannot_be_switched_on_or_scheduled_there(self):
		for route in ("frappe.client.set_value", "update_doc"):
			for field in ("enabled", "schedule_enabled"):
				with self.subTest(route=route, field=field):
					macro = self._macro(OWNER, f"{route}-{field}", armed=True, enabled=0)
					frappe.set_user(OWNER)
					with self.assertRaises((frappe.PermissionError, PermissionDeniedError)):
						self._routes(macro)[route]({field: 1})
					frappe.db.rollback()
					self.assertEqual(self._row(macro)[field], 0)
					self._purge()

	def test_switching_it_off_and_changing_the_steps_in_one_save_still_needs_the_form(self):
		macro = self._armed_on()
		frappe.set_user(OWNER)
		with self.assertRaises(frappe.PermissionError):
			frappe.client.set_value(MACRO, macro, {"enabled": 0, "steps": [{"prompt": "other"}]})
		frappe.db.rollback()
		# Disarming with it is the safe direction: the macro is no longer armed.
		frappe.client.set_value(MACRO, macro, {"skip_confirmation": 0, "steps": [{"prompt": "other"}]})
		self.assertEqual(self._prompts(macro), ["other"])

	def test_the_rest_of_an_armed_macro_is_free(self):
		macro = self._armed_on()
		frappe.set_user(OWNER)
		registry.dispatch(
			"update_doc",
			{"doctype": MACRO, "name": macro, "changes": {"description": "renamed", "stop_on_error": 0}},
		)
		self.assertEqual(frappe.db.get_value(MACRO, macro, "description"), "renamed")
		self.assertEqual(self._armed(macro), 1)


class TestAnUnarmedMacroFromATool(ArmingBase):
	def test_the_assistant_still_creates_and_edits_unarmed_macros(self):
		frappe.set_user(OWNER)
		made = registry.dispatch(
			"create_doc",
			{"doctype": MACRO, "values": {"macro_name": "arm-tool-made", "steps": [{"prompt": "one"}]}},
		)
		step = frappe.get_doc(MACRO, made["name"]).steps[0].name
		registry.dispatch(
			"update_doc",
			{
				"doctype": MACRO,
				"name": made["name"],
				"changes": {
					"steps": [{"name": step, "prompt": "two"}],
					"enabled": 1,
					"schedule_enabled": 1,
					"schedule_time": "06:00:00",
				},
			},
		)
		row = self._row(made["name"])
		self.assertEqual((row.enabled, row.schedule_enabled, row.skip_confirmation), (1, 1, 0))
		self.assertEqual(self._prompts(made["name"]), ["two"])


# --------------------------------------------------------------------------- #
# The notice
# --------------------------------------------------------------------------- #
class TestTheNotice(FrappeTestCase):
	def _tools(self, table):
		tools = [t for _phrase, ts in table for t in ts]
		self.assertEqual(len(tools), len(set(tools)), "a tool is in two rows")
		return set(tools)

	def test_every_tool_an_armed_macro_runs_uncarded_is_named(self):
		self.assertEqual(self._tools(macros_api._ARM_NOTICE_SKIPS), set(api._COVERED))

	def test_every_tool_that_still_asks_is_named(self):
		self.assertEqual(self._tools(macros_api._ARM_NOTICE_ASKS), set(api._BRAKE))

	def test_the_text(self):
		self.assertEqual(
			macros_api.arm_notice(),
			"This macro will create, change and submit records, add and change comments, tags and "
			"attachments, update wiki pages, share and assign documents (and remove shares and "
			"assignments), run workflow actions, send email, run imports and call server methods "
			"without asking you first, including when it runs on a schedule with nobody watching. "
			"Deleting, cancelling and amending records, creating skills and calling connectors "
			"still ask, and stop the run.",
		)
		self.assertNotIn(EM_DASH, macros_api.arm_notice())
