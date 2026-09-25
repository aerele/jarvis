"""File Box sheets, apply (T3 S2): one ordered background job, one resume.

``apply_sheet`` refuses with nothing claimed until every check passes, then claims
the sheet and enqueues ONE job (run here directly, as RQ would). The job creates
the records in dependency order as the dropper, all or nothing, answers the
linked questions and resumes the run once."""

from __future__ import annotations

import json
import statistics
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

import frappe

from jarvis.chat import (
	approvals_api,
	filebox,
	filebox_skills,
	held_parties,
	held_sheet_seal,
	held_sheets,
	held_writes,
)
from jarvis.chat import pending_actions as pa
from jarvis.chat.pending_actions import _reconcile, _seal, _sheet, _store
from jarvis.tests._pending_action_helpers import as_user
from jarvis.tests.test_filebox_sheet_seal import SM, _SealBase
from jarvis.tests.test_filebox_sheets import (
	AR,
	CONV,
	GROUP,
	OTHER,
	OWNER,
	PA,
	PARTY,
	TAX,
	UOM,
	WAITER,
	_address,
	_item,
	_question,
	_supplier,
)

AGENT_WRITE = "Jarvis Agent Write"


def _series():
	"""Suppliers named by naming series (links to a new one must be remapped)."""
	real = frappe.defaults.get_global_default

	def series(key, *a, **k):
		return "Naming Series" if key == "supp_master_name" else real(key, *a, **k)

	return patch("frappe.defaults.get_global_default", side_effect=series)


def _in_thread(fn, errors: list) -> threading.Thread:
	"""``fn()`` on its own DB connection (committed), started now."""
	site = frappe.local.site

	def run():
		frappe.init(site=site)
		frappe.connect()
		try:
			fn()
			frappe.db.commit()
		except Exception as e:  # pragma: no cover
			errors.append(e)
		finally:
			frappe.destroy()

	t = threading.Thread(target=run)
	t.start()
	return t


class _ApplyBase(_SealBase):
	def setUp(self):
		super().setUp()
		self._clear_apply()
		self.addCleanup(self._clear_apply)

	def _clear_apply(self):
		# The wipe deletes parents only: a re-created "zz-fbs ..." name would inherit rows.
		for parent in ("Address", "Item"):
			for df in frappe.get_meta(parent).get_table_fields():
				frappe.db.delete(df.options, {"parenttype": parent, "parent": ["like", "zz-fbs%"]})
		frappe.db.delete(AGENT_WRITE, {"actor": ["in", (OWNER, OTHER, SM)]})
		frappe.db.delete("Error Log", {"method": ["like", "jarvis.file_box.sheet_%"]})
		frappe.db.delete("User Permission", {"user": OWNER})
		frappe.db.commit()
		frappe.clear_cache(user=OWNER)

	# -- fixtures ---------------------------------------------------------------- #
	def sheet_of(self, *docs, questions=(), owner=OWNER):
		"""A sealed sheet holding ``docs`` (batches of up to 20) and ``questions``."""
		conv = self.conv(owner)
		rid = self.turn(conv)
		for start in range(0, len(docs), 20):
			batch = {"docs": list(docs[start : start + 20])}
			self.assert_added(self.call("create_doc", batch, conv, owner))
		for q in questions:
			self.assertTrue(self.call("create_doc", q, conv, owner)["ok"])
		[row] = [s for s in self.sheets(owner) if s.conversation == conv]
		self.set_turn(rid, state="done")
		self.assertEqual(held_sheet_seal.seal_turn(conv, rid), row.name)
		return conv, self.row(row.name)

	def routed(self, with_records=True):
		conv = self.conv()
		rid = self.turn(conv)
		if with_records:
			self.assert_added(self.call("create_doc", _supplier(), conv))
		with as_user(OWNER):
			_t, question = filebox_skills._file_routing(
				conv,
				filebox_skills.CONFLICT,
				"zz-fbs Which skill?",
				"Which skill should it follow?",
				["zz-skill-a", "zz-skill-b", filebox_skills.SKIP],
			)
			frappe.db.commit()
			sheet = held_sheets.link_to_sheet(conv, [question])
		self.set_turn(rid, state="done")
		held_sheet_seal.seal_turn(conv, rid)
		return conv, self.row(sheet), question

	def view(self, name, user=OWNER):
		with as_user(user):
			return approvals_api.get_pending_action(name)

	def decisions(self, name, records=None, answers=None, user=OWNER):
		d = self.view(name, user)
		return {
			"records": records or {},
			"answers": answers or {},
			"expected_card_sha256": d["card_sha256"],
			"record_count": d["record_count"],
		}

	def apply(self, name, records=None, answers=None, user=OWNER, payload=None):
		payload = payload or self.decisions(name, records, answers, user)
		with as_user(user):
			return approvals_api.apply_sheet(name, json.dumps(payload))

	def jobs(self):
		return [k for a, k in self.enqueued if a and a[0] == _sheet.JOB]

	def run_job(self, user=OWNER, index=-1):
		job = self.jobs()[index]
		self.assertEqual((job["queue"], job["job_id"]), ("long", job["token"]))
		with as_user(user):
			_sheet.run_apply(name=job["name"], token=job["token"])
		return self.row(job["name"])

	def applied(self, name, records=None, answers=None, user=OWNER):
		res = self.apply(name, records, answers, user)
		self.assertEqual((res["ok"], res.get("applying")), (True, True), res)
		return self.run_job(user)

	def answers_for(self, name, text="zz-fbs yes", approve=1):
		return {
			q: {"text": text, "approve": approve} for q in frappe.get_all(AR, {"sheet": name}, pluck="name")
		}

	def outcome(self, row):
		return json.loads(row.sheet_outcome)

	def assert_refused(self, res, code):
		self.assertFalse(res["ok"], res)
		self.assertEqual(res["reason_code"], code, res)

	def assert_unclaimed(self, name):
		row = self.row(name)
		self.assertEqual((row.status, row.apply_token, row.decided_by), ("Pending", None, None))
		self.assertEqual(self.jobs(), [])

	def suppliers(self):
		return frappe.get_all("Supplier", {"supplier_name": ["like", "zz-fbs%"]}, pluck="name")

	def logged(self, title):
		return frappe.get_all("Error Log", filters={"method": title}, pluck="error")

	def assert_value_free(self, row):
		self.assertEqual((row.apply_request, row.apply_errors), (None, None), row.status)


# --------------------------------------------------------------------------- #
# The preflight: every refusal claims nothing
# --------------------------------------------------------------------------- #
class TestPreflight(_ApplyBase):
	def test_only_the_owner_or_a_system_manager_may_apply(self):
		_c, row = self.sheet_of(_supplier())
		payload = self.decisions(row.name)
		self.assert_refused(self.apply(row.name, user=OTHER, payload=payload), "not_found")
		with as_user(OTHER):
			self.assert_refused(approvals_api.discard_sheet(row.name), "not_found")
		self.assert_unclaimed(row.name)

	def test_a_sheet_still_being_listed_is_refused(self):
		conv = self.conv()
		self.turn(conv)
		self.assert_added(self.call("create_doc", _supplier(), conv))
		name = self.sheet(conv).name
		payload = {"records": {}, "answers": {}, "expected_card_sha256": "x", "record_count": 1}
		self.assert_refused(self.apply(name, payload=payload), "collecting")

	def test_the_dropper_must_still_own_the_live_conversation_and_be_runnable(self):
		conv, row = self.sheet_of(_supplier())
		payload = self.decisions(row.name)
		frappe.db.set_value(CONV, conv, "owner", SM, update_modified=False)
		frappe.db.commit()
		self.assert_refused(self.apply(row.name, user=SM, payload=payload), "identity_refused")
		self.assert_refused(self.apply(row.name, payload=payload), "not_found")
		frappe.db.set_value(CONV, conv, "owner", OWNER, update_modified=False)
		frappe.db.set_value("User", OWNER, "enabled", 0)
		frappe.db.commit()
		try:
			self.assert_refused(self.apply(row.name, user=SM, payload=payload), "identity_refused")
		finally:
			frappe.db.set_value("User", OWNER, "enabled", 1)
			frappe.db.commit()
		self.assert_unclaimed(row.name)

	def test_an_armed_run_refuses(self):
		conv, row = self.sheet_of(_supplier())
		payload = self.decisions(row.name)
		frappe.db.set_value(CONV, conv, "skip_confirmation", 1, update_modified=False)
		frappe.db.commit()
		self.assert_refused(self.apply(row.name, payload=payload), "armed_run")
		self.assert_unclaimed(row.name)

	def test_a_view_older_than_the_sheet_is_refused(self):
		_c, row = self.sheet_of(_supplier(), _item("zz-fbs I1"))
		payload = self.decisions(row.name)
		for bad in ({"expected_card_sha256": "0" * 64}, {"record_count": 1}, {"record_count": None}):
			with self.subTest(bad=bad):
				self.assert_refused(self.apply(row.name, payload={**payload, **bad}), "changed")
		with as_user(OWNER):
			self.assert_refused(approvals_api.apply_sheet(row.name, "[1]"), "invalid")
		self.assert_unclaimed(row.name)

	def test_the_answers_must_be_exactly_the_sheets_pending_questions(self):
		conv, row = self.sheet_of(_supplier(), questions=[_question("zz-fbs q1"), _question("zz-fbs q2")])
		q1, q2 = frappe.get_all(AR, {"sheet": row.name}, pluck="name", order_by="creation")
		other_conv, other = self.sheet_of(_item("zz-fbs I9"), questions=[_question("zz-fbs q3")])
		[foreign] = frappe.get_all(AR, {"sheet": other.name}, pluck="name")
		ok = {"text": "zz-fbs yes", "approve": 1}
		for answers, bad in (
			({q1: ok}, q2),  # one missing
			({q1: ok, q2: ok, foreign: ok}, foreign),  # another sheet's question
			({q1: ok, q2: ok, "zz-fbs-nope": ok}, "zz-fbs-nope"),
			({q1: ok, q2: {"text": "  ", "approve": 1}}, q2),  # empty
			({q1: ok, q2: {"text": "x" * 1001, "approve": 1}}, q2),
		):
			with self.subTest(bad=bad):
				res = self.apply(row.name, answers=answers)
				self.assert_refused(res, "invalid")
				self.assertEqual(list(res["answer_errors"]), [bad])
		# A question the sheet no longer waits on is not one to answer.
		frappe.db.sql(f"UPDATE `tab{AR}` SET status='Dismissed', decision='x' WHERE name=%s", q2)
		frappe.db.commit()
		self.assert_refused(self.apply(row.name, answers={q1: ok, q2: ok}), "invalid")
		self.assert_unclaimed(row.name)
		self.assertTrue(self.apply(row.name, answers={q1: ok})["ok"])

	def test_a_routing_answer_must_be_an_offered_and_still_eligible_skill(self):
		conv, row, question = self.routed()
		with patch.object(filebox_skills, "_eligible_choice", side_effect=lambda c, s: s == "zz-skill-a"):
			for text in ("zz-free text", "zz-skill-b"):
				with self.subTest(text=text):
					res = self.apply(row.name, answers={question: {"text": text, "approve": 0}})
					self.assert_refused(res, "invalid")
					self.assertIn(question, res["answer_errors"])
			self.assert_unclaimed(row.name)
			self.assertTrue(self.apply(row.name, answers={question: {"text": "zz-skill-a"}})["ok"])
		plan = json.loads(self.row(row.name).apply_request)
		self.assertEqual(
			plan["answers"][question], {"text": "zz-skill-a", "approve": 1}, "an answer, never a rejection"
		)

	def test_use_existing_needs_a_readable_record_of_the_same_doctype(self):
		_c, row = self.sheet_of(_supplier())
		item = frappe.get_doc(_item("zz-fbs Other")["values"] | {"doctype": "Item"}).insert(
			ignore_permissions=True
		)
		hidden = frappe.get_doc({"doctype": "Supplier", "supplier_name": "zz-fbs Hidden"}).insert(
			ignore_permissions=True
		)
		seen = frappe.get_doc({"doctype": "Supplier", "supplier_name": "zz-fbs Seen"}).insert(
			ignore_permissions=True
		)
		frappe.get_doc(
			{"doctype": "User Permission", "user": OWNER, "allow": "Supplier", "for_value": seen.name}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		for target in ("", "zz-fbs Nobody", item.name, hidden.name):
			with self.subTest(target=target):
				res = self.apply(
					row.name, records={"0": {"action": "use_existing", "existing": target}}, user=SM
				)
				self.assert_refused(res, "invalid")
				self.assertEqual(list(res["errors"]), ["0"])
		self.assert_unclaimed(row.name)
		res = self.apply(row.name, records={"0": {"action": "use_existing", "existing": seen.name}}, user=SM)
		self.assertTrue(res["ok"], res)

	def test_an_edit_may_only_patch_what_the_held_rules_allow(self):
		frappe.db.delete("Error Log", {"method": "jarvis.pending_action.sheet_apply_crashed"})
		_c, row = self.sheet_of(_supplier(), _address())
		for records in (
			{"0": {"action": "edit", "values": {"tax_id": "29ZZZZZ9999Z1Z1"}}},  # identity: locked
			{"1": {"action": "edit", "values": {"links": [{"link_name": "zz-fbs Else"}]}}},  # a link to #1
			{"0": {"action": "edit", "values": {"owner": "x"}}},
			{"0": {"action": "edit", "values": {"zz_no_field": 1}}},
			{"0": {"action": "edit"}},
			{"0": {"action": "use_existing_or_not"}},
			{"7": {"action": "skip"}},
			{"²": {"action": "skip"}},  # a digit int() refuses
			{"١": {"action": "skip"}},  # a digit int() reads as 1
		):
			with self.subTest(records=records):
				res = self.apply(row.name, records=records)
				self.assert_refused(res, "invalid")
				self.assertEqual(list(res["errors"]), list(records))
		self.assert_unclaimed(row.name)
		self.assertEqual(self.logged("jarvis.pending_action.sheet_apply_crashed"), [])

	def test_an_update_record_takes_apply_edit_or_skip(self):
		existing = frappe.get_doc({"doctype": "Supplier", "supplier_name": "zz-fbs Old"}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		conv = self.conv()
		rid = self.turn(conv)
		change = {"doctype": "Supplier", "name": existing.name, "changes": {"supplier_details": "x"}}
		self.assert_added(self.call("update_doc", change, conv))
		self.set_turn(rid, state="done")
		name = held_sheet_seal.seal_turn(conv, rid)
		res = self.apply(name, records={"0": {"action": "use_existing", "existing": existing.name}})
		self.assert_refused(res, "invalid")

	def test_denied_runs_again_on_every_record(self):
		_c, row = self.sheet_of(_supplier(), _item("zz-fbs I1"))
		with patch.object(held_writes, "DENIED_MODULES", held_writes.DENIED_MODULES | {"Stock"}):
			res = self.apply(row.name)
		self.assert_refused(res, "invalid")
		self.assertEqual(list(res["errors"]), ["1"])
		self.assertIn("can't write Item", res["errors"]["1"])
		self.assert_unclaimed(row.name)

	def test_a_record_that_needs_a_fix_waits_for_an_edit_or_a_skip(self):
		_c, row = self.sheet_of(_supplier(), _item("zz-fbs I1"))
		structured = {"field": "item_group", "label": "Item Group", "message": "HSN/SAC Code is required"}
		for fix in ("HSN/SAC Code is required", structured):  # a legacy text entry too
			with self.subTest(fix=fix):
				self.set_row(row.name, needs_fix=json.dumps({"1": fix}))
				res = self.apply(row.name)
				self.assert_refused(res, "invalid")
				self.assertIn("needs a fix", res["errors"]["1"])
				self.assert_unclaimed(row.name)
		self.assertTrue(self.apply(row.name, records={"1": {"action": "skip"}})["ok"])

	def test_a_record_missing_fields_needs_them_filled_by_an_edit(self):
		_c, row = self.sheet_of(_supplier())
		self.set_row(
			row.name,
			needs_input=json.dumps([{"doc_index": 0, "doctype": "Supplier", "fieldname": "supplier_type"}]),
		)
		for records in ({}, {"0": {"action": "edit", "values": {"supplier_details": "x"}}}):
			with self.subTest(records=records):
				res = self.apply(row.name, records=records)
				self.assert_refused(res, "invalid")
				self.assertIn("Fill Supplier Type first", res["errors"]["0"])
		edit = {"0": {"action": "edit", "values": {"supplier_type": "Company"}}}
		self.assertTrue(self.apply(row.name, records=edit)["ok"])

	def test_a_double_apply_runs_once(self):
		_c, row = self.sheet_of(_supplier())
		payload = self.decisions(row.name)
		self.assertTrue(self.apply(row.name, payload=payload)["ok"])
		self.assert_refused(self.apply(row.name, payload=payload), "executing")
		self.run_job()
		self.assert_refused(self.apply(row.name, payload=payload), "already_handled")
		self.assertEqual(len(self.jobs()), 1)
		self.assertEqual(len(self.suppliers()), 1)


# --------------------------------------------------------------------------- #
# The job
# --------------------------------------------------------------------------- #
class TestApply(_ApplyBase):
	def test_the_records_are_created_in_order_as_the_dropper_and_links_follow_them(self):
		with _series():
			conv, row = self.sheet_of(
				_supplier(),
				_address(),
				_item("zz-fbs I1", supplier=PARTY),
				_item("zz-fbs I2"),
				_item("zz-fbs I3"),
				questions=[_question("zz-fbs q1")],
			)
			[question] = frappe.get_all(AR, {"sheet": row.name}, pluck="name")
			res = self.apply(row.name, answers={question: {"text": "zz-fbs Acme", "approve": 1}}, user=SM)
			self.assertEqual(
				res, {"ok": True, "applying": True, "reason_code": "applying", "pa_status": "Executing"}
			)
			self.assertEqual(self.row(row.name).status, "Executing")
			done = self.run_job(user=SM)
		[supplier] = self.suppliers()
		self.assertNotEqual(supplier, PARTY, "named by series: the links had to follow it")
		address = frappe.get_doc("Address", {"address_title": PARTY})
		self.assertEqual([(r.link_doctype, r.link_name) for r in address.links], [("Supplier", supplier)])
		i1 = frappe.get_doc("Item", "zz-fbs I1")
		self.assertEqual([r.supplier for r in i1.supplier_items], [supplier])
		created = [
			frappe.db.get_value(dt, n, "creation")
			for dt, n in (("Supplier", supplier), ("Address", address.name), ("Item", "zz-fbs I1"))
		]
		self.assertEqual(created, sorted(created), "dependencies first")
		self.assertEqual({frappe.db.get_value("Item", f"zz-fbs I{i}", "owner") for i in (1, 2, 3)}, {OWNER})
		self.assertEqual(frappe.db.get_value("Supplier", supplier, "owner"), OWNER, "created as the dropper")
		self.assertEqual((done.status, done.decided_by, done.settled), ("Executed", SM, 1))
		outcome = self.outcome(done)
		self.assertEqual(
			[(e["doctype"], e["action"], e.get("name")) for e in outcome["records"]],
			[
				("Supplier", "created", supplier),
				("Address", "created", address.name),
				("Item", "created", "zz-fbs I1"),
				("Item", "created", "zz-fbs I2"),
				("Item", "created", "zz-fbs I3"),
			],
		)
		self.assertNotIn(TAX, json.dumps(outcome), "no field values in the outcome")
		self.assertNotIn("1 Main Road", json.dumps(outcome))
		q = frappe.db.get_value(AR, question, ["status", "decision", "decided_by"], as_dict=True)
		self.assertEqual((q.status, q.decision, q.decided_by), ("Approved", "zz-fbs Acme", SM))
		self.assertEqual(len(self.resumes()), 1, "ONE resume")
		self.assertEqual(frappe.db.get_value(WAITER, {"parent": row.name}, "resume_state"), "due")
		steps = [p for _u, p in self.events if p.get("kind") == "sheet:progress" and p["name"] == row.name]
		self.assertEqual({p["state"] for p in steps}, {"applying", "applied"})
		self.assertEqual(steps[-1]["done"], 5)

	def test_use_existing_remaps_the_address_to_it(self):
		existing = frappe.get_doc({"doctype": "Supplier", "supplier_name": "zz-fbs Acme Old"}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		_c, row = self.sheet_of(_supplier(), _address())
		done = self.applied(row.name, records={"0": {"action": "use_existing", "existing": existing.name}})
		self.assertEqual(self.suppliers(), [existing.name])
		address = frappe.get_doc("Address", {"address_title": PARTY})
		self.assertEqual(address.links[0].link_name, existing.name)
		self.assertEqual(self.outcome(done)["records"][0]["action"], "existing")

	def test_an_edit_is_created_as_the_dropper_with_the_approvers_values(self):
		_c, row = self.sheet_of(_supplier(), _item("zz-fbs I1"))
		edit = {"1": {"action": "edit", "values": {"description": "zz-fbs edited"}}}
		with patch.object(held_sheets.held_edit, "patch_one", wraps=held_sheets.held_edit.patch_one) as rules:
			done = self.applied(row.name, records=edit, user=SM)
		self.assertEqual({c.args[3] for c in rules.call_args_list}, {OWNER}, "permlevel as the dropper")
		item = frappe.get_doc("Item", "zz-fbs I1")
		self.assertEqual((item.description, item.owner), ("zz-fbs edited", OWNER))
		self.assertEqual(done.status, "Executed")

	def test_skipping_a_record_skips_what_needs_it_transitively(self):
		group = {"doctype": "Item Group", "values": {"item_group_name": "zz-fbs GA", "is_group": 1}}
		sub = {
			"doctype": "Item Group",
			"values": {"item_group_name": "zz-fbs GB", "parent_item_group": "zz-fbs GA"},
		}
		_c, row = self.sheet_of(group, sub, _item("zz-fbs I1", item_group="zz-fbs GB"), _supplier())
		done = self.applied(row.name, records={"0": {"action": "skip"}})
		self.assertEqual(done.status, "Executed")
		self.assertFalse(frappe.db.exists("Item Group", "zz-fbs GA"))
		self.assertFalse(frappe.db.exists("Item Group", "zz-fbs GB"))
		self.assertFalse(frappe.db.exists("Item", "zz-fbs I1"))
		self.assertEqual(len(self.suppliers()), 1)
		records = self.outcome(done)["records"]
		self.assertEqual(
			[(e["action"], e.get("cascade", 0)) for e in records],
			[("skipped", 0), ("skipped", 1), ("skipped", 1), ("created", 0)],
		)
		self.assertIn("(it needs a skipped record)", held_sheet_seal.outcome_line(done.sheet_outcome))

	def test_a_flagged_record_a_skip_cascades_to_is_skipped_never_refused(self):
		group = {"doctype": "Item Group", "values": {"item_group_name": "zz-fbs GA", "is_group": 1}}
		sub = {
			"doctype": "Item Group",
			"values": {"item_group_name": "zz-fbs GB", "parent_item_group": "zz-fbs GA"},
		}
		_c, row = self.sheet_of(group, sub, _item("zz-fbs I1", item_group="zz-fbs GB"), _supplier())
		fix = {"field": None, "label": None, "message": "zz-fbs fix"}
		self.set_row(row.name, needs_fix=json.dumps({"2": fix}))  # needs #1, which needs #0
		res = self.apply(row.name, records={"0": {"action": "skip"}})
		self.assertTrue(res["ok"], res)
		records = self.outcome(self.run_job())["records"]
		self.assertEqual(
			[(e["action"], e.get("cascade", 0)) for e in records],
			[("skipped", 0), ("skipped", 1), ("skipped", 1), ("created", 0)],
		)

	def test_every_record_skipped_still_applies_the_answers(self):
		"""Only a routing Skip (or Skip all) discards: answered questions resume the run."""
		_c, row = self.sheet_of(_supplier(), questions=[_question()])
		done = self.applied(row.name, records={"0": {"action": "skip"}}, answers=self.answers_for(row.name))
		self.assertEqual((done.status, done.reason_code), ("Executed", ""))
		self.assertEqual(self.suppliers(), [])
		self.assertEqual([e["action"] for e in self.outcome(done)["records"]], ["skipped"])
		[question] = frappe.get_all(AR, {"sheet": row.name}, pluck="name")
		self.assertEqual(frappe.db.get_value(AR, question, "status"), "Approved")
		message = held_sheet_seal.resume_message(done)
		self.assertIn("applied this document's approval sheet", message)
		self.assertIn(f"Supplier {PARTY}: skipped", message)
		self.assertIn("] APPROVED: zz-fbs yes", message)

	def test_an_update_applies_and_a_stale_target_rolls_back(self):
		existing = frappe.get_doc({"doctype": "Supplier", "supplier_name": "zz-fbs Old"}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		for stale in (False, True):
			with self.subTest(stale=stale):
				conv = self.conv()
				rid = self.turn(conv)
				change = {
					"doctype": "Supplier",
					"name": existing.name,
					"changes": {"supplier_details": f"v{stale}"},
				}
				self.assert_added(self.call("update_doc", change, conv))
				self.set_turn(rid, state="done")
				name = held_sheet_seal.seal_turn(conv, rid)
				if stale:
					frappe.db.set_value("Supplier", existing.name, "website", "https://fbs.example")
					frappe.db.commit()
				done = self.applied(name)
				if stale:
					self.assertEqual(done.status, "Pending")
					self.assertEqual(json.loads(done.apply_errors), {"0": _store.REASON_TEXT["stale"]})
				else:
					self.assertEqual(done.status, "Executed")
					self.assertEqual(self.outcome(done)["records"][0]["action"], "updated")
				self.assertEqual(frappe.db.get_value("Supplier", existing.name, "supplier_details"), "vFalse")

	def test_a_create_whose_record_exists_now_uses_it(self):
		_c, row = self.sheet_of(_supplier(), _item("zz-fbs I1"), _address())
		frappe.get_doc({"doctype": "Supplier", "supplier_name": PARTY, "tax_id": TAX}).insert(
			ignore_permissions=True
		)
		frappe.get_doc({**_item("zz-fbs I1")["values"], "doctype": "Item"}).insert(ignore_permissions=True)
		frappe.db.commit()
		done = self.applied(row.name)
		records = self.outcome(done)["records"]
		self.assertEqual(
			[(e["action"], e.get("auto")) for e in records[:2]], [("existing", 1), ("existing", 1)]
		)
		self.assertEqual(records[2]["action"], "created")
		self.assertEqual(frappe.db.count("Item", {"item_code": "zz-fbs I1"}), 1)
		self.assertEqual(len(self.suppliers()), 1)

	def test_a_party_named_like_one_the_dropper_cannot_open_is_an_error_never_a_twin(self):
		with _series():
			_c, row = self.sheet_of(_supplier(tax=""))
			hidden = frappe.get_doc(
				{"doctype": "Supplier", "supplier_name": PARTY, "country": "Japan"}
			).insert(ignore_permissions=True)
			frappe.get_doc(
				{"doctype": "User Permission", "user": OWNER, "allow": "Country", "for_value": "India"}
			).insert(ignore_permissions=True)
			frappe.db.commit()
			done = self.applied(row.name)
		self.assertEqual(done.status, "Pending")
		error = json.loads(done.apply_errors)["0"]
		self.assertIn("This Supplier exists already, but the person who dropped the file can't", error)
		self.assertNotIn(hidden.name, error, "never the name of a record they can't open")
		self.assertEqual(self.suppliers(), [hidden.name])

	def test_a_party_named_like_a_readable_and_a_hidden_one_uses_the_readable_one(self):
		with _series():
			_c, row = self.sheet_of(_supplier(tax=""))
			hidden = frappe.get_doc(
				{"doctype": "Supplier", "supplier_name": PARTY, "country": "Japan"}
			).insert(ignore_permissions=True)  # the older: first when read past permissions
			seen = frappe.get_doc({"doctype": "Supplier", "supplier_name": PARTY, "country": "India"}).insert(
				ignore_permissions=True
			)
			frappe.get_doc(
				{"doctype": "User Permission", "user": OWNER, "allow": "Country", "for_value": "India"}
			).insert(ignore_permissions=True)
			frappe.db.commit()
			done = self.applied(row.name)
		self.assertEqual(done.status, "Executed")
		[entry] = self.outcome(done)["records"]
		self.assertEqual((entry["action"], entry["name"], entry.get("auto")), ("existing", seen.name, 1))
		self.assertEqual(sorted(self.suppliers()), sorted([hidden.name, seen.name]))

	def test_a_settle_failure_after_the_commit_is_not_a_crash(self):
		_c, row = self.sheet_of(_supplier())
		with patch.object(_sheet, "settle", side_effect=RuntimeError("zz settle")):
			done = self.applied(row.name)
		self.assertEqual((done.status, done.settled), ("Executed", 0))
		self.assertEqual(len(self.suppliers()), 1)
		for title in ("crashed", "lost", "failed"):
			self.assertEqual(self.logged(f"jarvis.file_box.sheet_apply_{title}"), [], title)
		self.assertTrue(self.logged("jarvis.file_box.sheet_apply_settle_failed"))

	def test_an_ended_sheet_keeps_no_decisions_or_errors(self):
		from erpnext.buying.doctype.supplier.supplier import Supplier

		edit = {"0": {"action": "edit", "values": {"supplier_details": "zz-fbs private"}}}
		_c, row = self.sheet_of(_supplier())
		self.assert_value_free(self.applied(row.name, records=edit))
		for end in ("discard", "stop"):
			with self.subTest(end=end):
				conv, row = self.sheet_of(_supplier(f"zz-fbs {end}", tax=""))
				with patch.object(Supplier, "validate", side_effect=frappe.ValidationError("zz-fbs private")):
					back = self.applied(row.name, records=edit)
				self.assertTrue(back.apply_request and back.apply_errors, "kept while Pending")
				with as_user(OWNER):
					if end == "discard":
						approvals_api.discard_sheet(row.name)
					else:
						pa.cancel_for_conversation(conv)
				done = self.row(row.name)
				self.assertEqual(done.status, "Discarded")
				self.assert_value_free(done)

	def test_an_existing_record_the_dropper_cannot_open_is_an_error(self):
		with _series():
			_c, row = self.sheet_of(_supplier())
			hidden = frappe.get_doc(
				{"doctype": "Supplier", "supplier_name": "zz-fbs Other", "tax_id": TAX}
			).insert(ignore_permissions=True)
			seen = frappe.get_doc({"doctype": "Supplier", "supplier_name": "zz-fbs Seen"}).insert(
				ignore_permissions=True
			)
			frappe.get_doc(
				{"doctype": "User Permission", "user": OWNER, "allow": "Supplier", "for_value": seen.name}
			).insert(ignore_permissions=True)
			frappe.db.commit()
			done = self.applied(row.name)
		self.assertEqual(done.status, "Pending")
		error = json.loads(done.apply_errors)["0"]
		self.assertIn("This Supplier exists already", error)
		self.assertNotIn(hidden.name, error)

	def test_a_duplicate_entry_is_resolved_again_never_assumed(self):
		_c, row = self.sheet_of(_supplier(), _item("zz-fbs I1"))
		_c, blind = self.sheet_of(_item("zz-fbs I1"))
		frappe.get_doc({**_item("zz-fbs I1")["values"], "doctype": "Item"}).insert(ignore_permissions=True)
		frappe.db.commit()
		real, calls = _sheet._existing, []

		def blind_once(record, values):
			calls.append(record["doctype"])
			return None if calls.count("Item") == 1 and record["doctype"] == "Item" else real(record, values)

		with patch.object(_sheet, "_existing", side_effect=blind_once):
			done = self.applied(row.name)
		self.assertEqual(calls.count("Item"), 2, "checked, clashed, then resolved again")
		self.assertEqual(self.outcome(done)["records"][1]["action"], "existing")
		# Nothing found on the second look: the record's error, nothing kept.
		with patch.object(_sheet, "_existing", return_value=None):
			done = self.applied(blind.name)
		self.assertEqual(done.status, "Pending")
		self.assertIn("already exists", json.loads(done.apply_errors)["0"])

	def test_any_error_rolls_everything_back_and_keeps_the_decisions(self):
		from erpnext.buying.doctype.supplier.supplier import Supplier
		from erpnext.stock.doctype.item.item import Item

		real_item = Item.validate

		def item_fails(doc):
			if doc.item_code == "zz-fbs I3":
				frappe.throw("zz-fbs I3 is <b>broken</b>")
			return real_item(doc)

		def supplier_fails(doc):
			frappe.throw("zz-fbs supplier broken")

		_c, row = self.sheet_of(
			_supplier(),
			_address(),
			_item("zz-fbs I1", supplier=PARTY),
			_item("zz-fbs I2"),
			_item("zz-fbs I3"),
			questions=[_question()],
		)
		answers = self.answers_for(row.name)
		edit = {"3": {"action": "edit", "values": {"description": "zz-fbs kept"}}}
		with patch.object(Item, "validate", item_fails), patch.object(Supplier, "validate", supplier_fails):
			done = self.applied(row.name, records=edit, answers=answers)
		self.assertEqual(
			(done.status, done.decided_by, done.decided_at, done.executing_at), ("Pending", None, None, None)
		)
		self.assertEqual(
			json.loads(done.apply_errors),
			{
				"0": "zz-fbs supplier broken",
				"1": "Blocked by #1: it needs that record.",
				"2": "Blocked by #1: it needs that record.",
				"4": "zz-fbs I3 is broken",
			},
		)
		self.assertEqual(self.suppliers(), [])
		self.assertFalse(frappe.db.exists("Address", {"address_title": PARTY}))
		self.assertFalse(frappe.db.exists("Item", {"item_code": ["like", "zz-fbs I%"]}), "I2 went too")
		self.assertEqual({q.status for q in self.questions(row.name)}, {"Pending"})
		plan = json.loads(done.apply_request)
		self.assertEqual(plan["records"]["3"], {"action": "edit", "values": {"description": "zz-fbs kept"}})
		view = self.view(row.name)
		self.assertEqual(view["decisions"]["records"]["3"]["values"], {"description": "zz-fbs kept"})
		self.assertEqual(view["errors"]["4"], "zz-fbs I3 is broken")
		self.assertEqual(view["can_act"], 1)
		self.assertEqual(self.resumes(), [])
		failed = frappe.get_all(AGENT_WRITE, {"provenance_name": row.name}, ["outcome", "ref_doctype"])
		self.assertEqual(
			sorted((f.outcome, f.ref_doctype) for f in failed), [("failed", "Item"), ("failed", "Supplier")]
		)
		# Fixed: skip the broken item, apply again.
		with patch.object(Item, "validate", item_fails):
			done = self.applied(row.name, records={**edit, "4": {"action": "skip"}}, answers=answers)
		self.assertEqual(done.status, "Executed")
		self.assertEqual(frappe.db.get_value("Item", "zz-fbs I2", "description"), "zz-fbs kept")
		self.assertIsNone(done.apply_errors)

	def test_a_crash_hands_it_back_and_alerts_once(self):
		for _ in range(2):
			_c, row = self.sheet_of(_supplier())
			with patch.object(_sheet, "_walk", side_effect=RuntimeError("zz boom")):
				done = self.applied(row.name)
			self.assertEqual(done.status, "Pending")
			self.assertEqual(json.loads(done.apply_errors), {"sheet": _sheet._CRASHED_TEXT})
			self.wipe_sheets()
		self.assertEqual(len(self.logged("jarvis.file_box.sheet_apply_crashed")), 2)
		self.assertEqual(len(self.logged("jarvis.file_box.sheet_apply_failed")), 1, "deduped")

	def wipe_sheets(self):
		from jarvis.tests.test_filebox_sheets import _wipe

		_wipe()
		self.enqueued.clear()

	def test_the_apply_drains_with_the_switch_off(self):
		_c, row = self.sheet_of(_supplier())
		with patch.object(held_sheets, "enabled", return_value=False):
			self.assertEqual(self.applied(row.name).status, "Executed")


# --------------------------------------------------------------------------- #
# Every exit is a compare-and-set on the token
# --------------------------------------------------------------------------- #
class TestToken(_ApplyBase):
	def test_a_stale_job_never_commits_over_a_new_claim(self):
		_c, row = self.sheet_of(_supplier(), _item("zz-fbs I1"))
		self.assertTrue(self.apply(row.name)["ok"])
		first = self.jobs()[0]["token"]
		_store.release_sheet(row.name, first, {"sheet": "reaped"})
		frappe.db.commit()
		self.assertTrue(self.apply(row.name)["ok"])
		second = self.jobs()[1]["token"]
		with as_user(OWNER):
			_sheet.run_apply(name=row.name, token=first)
		self.assertEqual((self.row(row.name).status, self.row(row.name).apply_token), ("Executing", second))
		self.assertEqual(self.suppliers(), [])
		self.assertTrue(self.logged("jarvis.file_box.sheet_apply_stale"))

	def test_a_claim_moved_mid_job_keeps_nothing(self):
		_c, row = self.sheet_of(_supplier(), _item("zz-fbs I1"))
		self.assertTrue(self.apply(row.name)["ok"])
		errors, real = [], _sheet._create

		def move_claim():
			frappe.db.sql(f"UPDATE `tab{PA}` SET apply_token='zz-other' WHERE name=%s", row.name)

		def racing(*a, **k):
			if not getattr(racing, "done", False):
				racing.done = True
				_in_thread(move_claim, errors).join(timeout=30)
			return real(*a, **k)

		with patch.object(_sheet, "_create", side_effect=racing):
			done = self.run_job()
		self.assertEqual(errors, [])
		self.assertEqual((done.status, done.apply_token), ("Executing", "zz-other"))
		self.assertEqual(self.suppliers(), [])
		self.assertFalse(frappe.db.exists("Item", "zz-fbs I1"))
		self.assertTrue(self.logged("jarvis.file_box.sheet_apply_lost"))
		self.assertEqual(self.resumes(), [])

	def test_a_bounce_never_hands_back_a_newer_claim(self):
		from erpnext.buying.doctype.supplier.supplier import Supplier

		_c, row = self.sheet_of(_supplier())
		self.assertTrue(self.apply(row.name)["ok"])
		errors = []

		def moved_then_fails(*a, **k):
			_in_thread(
				lambda: frappe.db.sql(f"UPDATE `tab{PA}` SET apply_token='zz-other' WHERE name=%s", row.name),
				errors,
			).join(timeout=30)
			raise frappe.ValidationError("zz no")

		with patch.object(Supplier, "validate", side_effect=moved_then_fails):
			done = self.run_job()
		self.assertEqual(errors, [])
		self.assertEqual((done.status, done.apply_token, done.apply_errors), ("Executing", "zz-other", None))
		self.assertTrue(self.logged("jarvis.file_box.sheet_apply_lost"))

	def test_questions_that_moved_after_the_claim_keep_nothing(self):
		_c, row = self.sheet_of(_supplier(), questions=[_question()])
		self.assertTrue(self.apply(row.name, answers=self.answers_for(row.name))["ok"])
		late = frappe.get_doc(
			{
				"doctype": AR,
				"title": "zz-fbs late",
				"question": "Late?",
				"conversation": row.conversation,
				"sheet": row.name,
				"source": "File Box",
				"status": "Pending",
			}
		)
		late.flags.jarvis_server_write = True
		late.insert(ignore_permissions=True)
		frappe.db.commit()
		done = self.run_job()
		self.assertEqual(done.status, "Pending")
		self.assertEqual(json.loads(done.apply_errors), {"sheet": _sheet._QUESTIONS_TEXT})
		self.assertEqual(self.suppliers(), [])
		self.assertEqual({q.status for q in self.questions(row.name)}, {"Pending"})


# --------------------------------------------------------------------------- #
# The reaper, Stop, and the operator
# --------------------------------------------------------------------------- #
class TestReaperAndStop(_ApplyBase):
	def executing(self, *docs, age=600):
		conv, row = self.sheet_of(*(docs or (_supplier(),)))
		self.assertTrue(self.apply(row.name)["ok"])
		self.set_row(
			row.name, executing_at=frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-age)
		)
		return conv, self.row(row.name)

	def test_a_sheet_whose_job_is_gone_goes_back_to_pending(self):
		from rq.job import JobStatus

		_c, gone = self.executing()
		_c, running = self.executing()
		_c, young = self.executing(age=10)
		status = {gone.apply_token: None, running.apply_token: JobStatus.STARTED, young.apply_token: None}
		with patch("frappe.utils.background_jobs.get_job_status", side_effect=lambda t: status[t]):
			self.assertEqual(_sheet.reap(), [gone.name])
			self.assertEqual(_sheet.reap(), [])
		back = self.row(gone.name)
		self.assertEqual((back.status, back.apply_token, back.decided_by), ("Pending", None, None))
		self.assertEqual(json.loads(back.apply_errors), {"sheet": _sheet.INTERRUPTED_TEXT})
		self.assertTrue(back.apply_request, "the decisions survive")
		self.assertEqual(
			(self.row(running.name).status, self.row(young.name).status), ("Executing", "Executing")
		)
		self.assertEqual(len(self.logged("jarvis.file_box.sheet_apply_interrupted")), 1)
		self.assertEqual(len(self.logged("jarvis.file_box.sheet_apply_failed")), 1)
		# Its late job finds nothing to do.
		with as_user(OWNER):
			_sheet.run_apply(name=gone.name, token=gone.apply_token)
		self.assertEqual(self.suppliers(), [])

	def test_the_generic_reaper_never_touches_a_sheet_and_the_reconciler_asks_rq(self):
		_c, row = self.executing(age=5000)
		self.assertEqual(_reconcile._reap_interrupted(), 0)
		self.assertEqual(self.row(row.name).status, "Executing")
		with patch("frappe.utils.background_jobs.get_job_status", return_value=None):
			out = _reconcile.reconcile()
		self.assertEqual(out["reap_sheets"], 1)
		self.assertEqual(self.row(row.name).status, "Pending")

	def test_a_probe_failure_never_hands_back(self):
		_c, row = self.executing()
		with patch("frappe.utils.background_jobs.get_job_status", side_effect=ConnectionError):
			self.assertEqual(_sheet.reap(), [])
		self.assertEqual(self.row(row.name).status, "Executing")

	def test_a_stop_during_a_successful_apply_keeps_the_records_without_a_resume(self):
		conv, row = self.sheet_of(_supplier(), questions=[_question()])
		self.assertTrue(self.apply(row.name, answers=self.answers_for(row.name))["ok"])
		with as_user(OWNER):
			pa.cancel_for_conversation(conv)
		mid = self.row(row.name)
		self.assertEqual((mid.status, mid.stop_requested), ("Executing", 1))
		self.assertEqual(frappe.db.count(WAITER, {"parent": row.name}), 1, "kept until the job ends")
		done = self.run_job()
		self.assertEqual((done.status, done.settled), ("Executed", 1))
		self.assert_value_free(done)
		self.assertEqual(len(self.suppliers()), 1)
		self.assertEqual(frappe.db.count(WAITER, {"parent": row.name}), 0)
		self.assertEqual(self.resumes(), [])

	def test_a_stop_during_an_apply_that_bounces_discards_it(self):
		from erpnext.buying.doctype.supplier.supplier import Supplier

		conv, row = self.sheet_of(_supplier(), questions=[_question()])
		self.assertTrue(self.apply(row.name, answers=self.answers_for(row.name))["ok"])
		with as_user(OWNER):
			pa.cancel_for_conversation(conv)
		with patch.object(Supplier, "validate", side_effect=frappe.ValidationError("zz no")):
			done = self.run_job()
		self.assertEqual((done.status, done.reason_code, done.settled), ("Discarded", "cancelled", 1))
		self.assert_value_free(done)
		self.assertEqual({q.status for q in self.questions(row.name)}, {"Dismissed"})
		self.assertEqual((self.suppliers(), self.resumes()), ([], []))

	def test_a_seal_broken_under_the_job_fails_it_and_creates_nothing(self):
		_c, row = self.sheet_of(_supplier())
		self.assertTrue(self.apply(row.name)["ok"])
		self.set_row(row.name, sealed_call="x")
		done = self.run_job()
		self.assertEqual((done.status, done.reason_code, done.settled), ("Failed", "unverifiable", 1))
		self.assert_value_free(done)
		self.assertEqual((self.suppliers(), len(self.resumes())), ([], 1))
		self.assertIn("could not be applied", held_sheet_seal.resume_message(done))

	def test_an_operator_fail_wins_over_the_running_job(self):
		_c, row = self.sheet_of(_supplier())
		self.assertTrue(self.apply(row.name)["ok"])
		with as_user(SM):
			pa.operator_fail(row.name)
		done = self.run_job()
		self.assertEqual((done.status, done.reason_code), ("Failed", "interrupted"))
		self.assertEqual(self.suppliers(), [])


# --------------------------------------------------------------------------- #
# Questions, routing, the resume, discard, audit, the ladder
# --------------------------------------------------------------------------- #
class TestResume(_ApplyBase):
	def resumed(self, name):
		sent = []
		with (
			as_user(SM),
			patch.object(
				approvals_api,
				"_resume_conversation",
				side_effect=lambda c, m, **k: sent.append((c, m)) or {"ok": True},
			),
		):
			self.assertEqual(held_writes.resume_waiters(name), 1)
			self.assertEqual(held_writes.resume_waiters(name), 0, "once")
		[(_c, message)] = sent
		return message

	def test_one_resume_quotes_every_outcome_and_every_answer(self):
		title = "zz-fbs Pick\nthe `vendor`"
		_c, row = self.sheet_of(
			_supplier(),
			_item("zz-fbs I1"),
			questions=[_question(title), _question("zz-fbs q2")],
		)
		q1, q2 = frappe.get_all(AR, {"sheet": row.name}, pluck="name", order_by="creation")
		answers = {q1: {"text": "zz Acme\n`run this`", "approve": 1}, q2: {"text": "no", "approve": 0}}
		done = self.applied(row.name, records={"1": {"action": "skip"}}, answers=answers)
		message = self.resumed(row.name)
		head, *lines = message.split("\n")
		self.assertIn("applied this document's approval sheet", head)
		self.assertIn("Each answer below applies to its own question only", head)
		self.assertIn("a draft may already exist", head)
		self.assertIn("If a master the draft needs was skipped, stop", head)
		self.assertEqual(head.count("`"), 2, "one DATA quote")
		self.assertIn(f"Supplier {PARTY}: created as {PARTY}", head)
		self.assertIn("Item zz-fbs I1: skipped", head)
		self.assertEqual(
			lines,
			[
				f"[Approval {q1} - zz-fbs Pick the 'vendor'] APPROVED: zz Acme 'run this'",
				f"[Approval {q2} - zz-fbs q2] REJECTED: no",
			],
		)
		self.assertEqual(done.status, "Executed")
		self.assertEqual(
			[frappe.db.get_value(AR, q, "status") for q in (q1, q2)], ["Approved", "Rejected"], "as the board"
		)

	def test_a_routing_answer_takes_effect_with_the_one_resume(self):
		conv, row, question = self.routed()
		with (
			patch.object(filebox_skills, "_eligible_choice", return_value=True),
			patch.object(filebox_skills, "_resume", side_effect=AssertionError("resumed on its own")),
		):
			done = self.applied(row.name, answers={question: {"text": "zz-skill-a"}})
		self.assertEqual(done.status, "Executed")
		self.assertEqual(frappe.db.get_value(CONV, conv, "filebox_skill_choice"), "zz-skill-a")
		self.assertEqual(
			frappe.db.get_value(AR, question, ["status", "decision"]), ("Approved", "zz-skill-a")
		)
		self.assertIn("] ANSWERED: zz-skill-a", self.resumed(row.name))

	def test_a_routing_skip_skips_the_whole_sheet(self):
		conv, row, question = self.routed()
		done = self.applied(
			row.name, records={"0": {"action": "create"}}, answers={question: {"text": filebox_skills.SKIP}}
		)
		self.assertEqual((done.status, done.reason_code), ("Discarded", "discarded"))
		self.assertEqual(self.suppliers(), [])
		self.assertFalse(frappe.db.get_value(CONV, conv, "filebox_skill_choice"))
		message = self.resumed(row.name)
		self.assertIn("skipped this document's approval sheet", message)
		self.assertIn(f"] ANSWERED: {filebox_skills.SKIP}", message)

	def test_a_questions_only_sheet_skipped_by_its_routing_answer_ends_skipped(self):
		_conv, row, question = self.routed(with_records=False)
		self.assertEqual(row.record_count, 0)
		done = self.applied(row.name, answers={question: {"text": filebox_skills.SKIP}})
		self.assertEqual((done.status, done.reason_code), ("Discarded", "discarded"))
		self.assertIn("skipped this document's approval sheet", self.resumed(row.name))

	def test_a_routing_only_sheet_never_spends_one_of_the_two_sheets(self):
		"""A skill conflict, then the masters, then a post-draft question: all three pause."""
		conv, row, question = self.routed(with_records=False)
		self.assertEqual(frappe.db.get_value(CONV, conv, "filebox_sheet_count"), 0)
		with patch.object(filebox_skills, "_eligible_choice", return_value=True):
			self.assertEqual(
				self.applied(row.name, answers={question: {"text": "zz-skill-a"}}).status, "Executed"
			)
		rid = self.turn(conv)
		self.assert_added(self.call("create_doc", _supplier(), conv))
		self.set_turn(rid, state="done")
		masters = held_sheet_seal.seal_turn(conv, rid)
		self.assertEqual(frappe.db.get_value(CONV, conv, "filebox_sheet_count"), 1)
		self.assertEqual(self.applied(masters).status, "Executed")
		self.turn(conv)
		res = self.call("create_doc", _question("zz-fbs after the draft"), conv)
		self.assertTrue(res["ok"], res)
		self.assertIn("answers it with the sheet", res["data"]["note"])
		self.assertEqual(frappe.db.get_value(CONV, conv, "filebox_sheet_count"), 2)

	def test_a_routing_skip_never_checks_the_edits(self):
		_conv, row, question = self.routed()
		edit = {"0": {"action": "edit", "values": {"owner": "x"}}}  # refused were it applied
		done = self.applied(row.name, records=edit, answers={question: {"text": filebox_skills.SKIP}})
		self.assertEqual((done.status, done.reason_code), ("Discarded", "discarded"))
		self.assertEqual(self.suppliers(), [])

	def test_a_routing_answer_is_checked_again_when_it_applies(self):
		conv, row, question = self.routed()
		with patch.object(filebox_skills, "_eligible_choice", return_value=True):
			self.assertTrue(self.apply(row.name, answers={question: {"text": "zz-skill-a"}})["ok"])
		with patch.object(filebox_skills, "_eligible_choice", return_value=None):
			done = self.run_job()
		self.assertEqual(done.status, "Pending")
		self.assertIn("no longer available", json.loads(done.apply_errors)[question])
		self.assertEqual(self.suppliers(), [])
		self.assertFalse(frappe.db.get_value(CONV, conv, "filebox_skill_choice"))
		self.assertEqual(frappe.db.get_value(AR, question, "status"), "Pending")

	def test_discard_all_skips_everything_and_resumes_once(self):
		_c, row = self.sheet_of(_supplier(), _item("zz-fbs I1"), questions=[_question()])
		with as_user(OWNER):
			res = approvals_api.discard_sheet(row.name)
		self.assertEqual((res["ok"], res["pa_status"]), (True, "Discarded"))
		done = self.row(row.name)
		self.assertEqual((done.status, done.decided_by, done.settled), ("Discarded", OWNER, 1))
		self.assertEqual({e["action"] for e in self.outcome(done)["records"]}, {"skipped"})
		self.assertEqual(
			{(q.status, q.decision) for q in self.questions(row.name)},
			{("Dismissed", held_sheet_seal.CLOSED_NOTE)},
		)
		self.assertEqual((self.suppliers(), len(self.resumes())), ([], 1))
		self.assertIn("skipped this document's approval sheet", self.resumed(row.name))
		with as_user(OWNER):
			self.assert_refused(approvals_api.discard_sheet(row.name), "already_handled")

	def test_every_written_record_is_audited_as_the_dropper(self):
		_c, row = self.sheet_of(_supplier(), _item("zz-fbs I1"), _item("zz-fbs I2"))
		self.applied(row.name, records={"2": {"action": "skip"}}, user=SM)
		rows = frappe.get_all(
			AGENT_WRITE,
			{"provenance_name": row.name},
			["actor", "tool", "outcome", "provenance", "ref_doctype", "ref_name"],
			order_by="ref_doctype",
		)
		self.assertEqual(
			[(r.actor, r.tool, r.outcome, r.provenance, r.ref_doctype, r.ref_name) for r in rows],
			[
				(OWNER, "create_doc", "applied", "approval", "Item", "zz-fbs I1"),
				(OWNER, "create_doc", "applied", "approval", "Supplier", PARTY),
			],
		)

	def test_a_legacy_hold_learns_what_a_sheet_just_created_never_what_it_skipped(self):
		_c, row = self.sheet_of(_supplier(), _item("zz-fbs I1"))
		self.applied(row.name, records={"1": {"action": "skip"}})
		created = held_parties.item_key({"op": "create", "name": "", **_supplier()})
		skipped = held_parties.item_key({"op": "create", "name": "", **_item("zz-fbs I1")})
		self.assertEqual(held_writes._find_match(OWNER, [created]), ("recent", row.name, False))
		self.assertEqual(held_writes._find_match(OWNER, [skipped]), (None, None, False))
		self.assertEqual(held_writes._find_match(OTHER, [created]), (None, None, False))

	def test_a_rerun_preamble_quotes_the_sheets_outcome(self):
		conv, row = self.sheet_of(_supplier())
		self.applied(row.name)
		preamble = filebox._rerun_preamble({"status": "no_draft", "skipped": 0}, None, [row.name])
		self.assertIn(f"decision: {row.summary}: Supplier {PARTY}: created as {PARTY} (executed)", preamble)


class TestLadderAndLocks(_ApplyBase):
	def test_an_applying_sheet_shows_its_progress_and_links_to_it(self):
		conv, row = self.sheet_of(_supplier(), _item("zz-fbs I1"))
		self.assertTrue(self.apply(row.name)["ok"])
		with as_user(OWNER):
			[r] = [r for r in filebox.list_inbound_page(page_length=100)["rows"] if r["name"] == conv]
		self.assertEqual((r["status"], r["result"]), ("applying", "Applying the approval sheet… 0/2"))
		self.assertEqual(r["result_link"], f"/approvals?held={row.name}")
		self.assertEqual(self.view(row.name)["progress"], {"done": 0, "total": 2})
		self.run_job()
		self.assertIsNone(_sheet.progress(row.name))

	def test_the_slim_poll_reads_the_state_without_the_records(self):
		conv, row = self.sheet_of(_supplier(), _item("zz-fbs I1"))
		full = self.view(row.name)

		def slim(user=OWNER):
			with as_user(user), patch.object(_seal, "unseal_call", side_effect=AssertionError("unsealed")):
				return approvals_api.get_pending_action(row.name, slim=1)

		self.assertEqual(
			slim(),
			{
				"name": row.name,
				"status": "Pending",
				"collecting": 0,
				"progress": None,
				"card_sha256": full["card_sha256"],
				"record_count": 2,
				"counts_line": "1 supplier · 1 item",
			},
		)
		self.assertEqual(slim(SM)["status"], "Pending", "a System Manager reads it too")
		with self.assertRaises(frappe.DoesNotExistError):
			slim(OTHER)
		self.assertTrue(self.apply(row.name)["ok"])
		self.assertEqual((slim()["status"], slim()["progress"]), ("Executing", {"done": 0, "total": 2}))
		self.run_job()
		self.assertEqual((slim()["status"], slim()["progress"]), ("Executed", None))

	def test_both_paths_lock_the_same_record_the_same_way(self):
		sheet_record = held_sheets._record({"op": "create", "name": "", **_supplier()})
		held_item = held_parties.items_of("create_doc", _supplier())[0]
		self.assertEqual(_sheet.lock_keys([sheet_record]), _sheet.lock_keys([held_item]))
		self.assertEqual(
			_sheet.lock_keys([sheet_record]), [f"gstin:Supplier:{TAX}", "name:Supplier:zz fbs acme traders"]
		)
		self.assertLessEqual(max(len(_sheet._lock_name(k)) for k in _sheet.lock_keys([sheet_record])), 64)
		self.assertLessEqual(len(_sheet._apply_lock()), 64)

	@contextmanager
	def held_lock(self, keys, *, then=None, hold_s=2.0, apply_lock=False):
		"""Another session holds ``keys``' record locks, runs ``then`` and lets go."""
		errors, locked = [], threading.Event()

		def hold():
			with _sheet.record_locks(keys, wait=5, apply_lock=apply_lock):
				locked.set()
				time.sleep(hold_s)
				if then:
					then()
					frappe.db.commit()

		t = _in_thread(hold, errors)
		self.assertTrue(locked.wait(timeout=30))
		try:
			yield
		finally:
			t.join(timeout=60)
			self.assertEqual(errors, [])

	def test_a_sheet_waits_for_a_record_another_apply_holds_then_uses_it(self):
		"""The naming-series supplier race (D3): never a second supplier."""
		with _series():
			_c, row = self.sheet_of(_supplier(), _address())
			keys = _sheet.lock_keys([held_sheets._record({"op": "create", "name": "", **_supplier()})])

			def create_it():
				frappe.get_doc({"doctype": "Supplier", "supplier_name": PARTY, "tax_id": TAX}).insert(
					ignore_permissions=True
				)

			self.assertTrue(self.apply(row.name)["ok"])
			with self.held_lock(keys, then=create_it):
				done = self.run_job()
		self.assertEqual(len(self.suppliers()), 1)
		[supplier] = self.suppliers()
		self.assertEqual(
			self.outcome(done)["records"][0],
			{**self.outcome(done)["records"][0], "action": "existing", "name": supplier},
		)
		address = frappe.get_doc("Address", {"address_title": PARTY})
		self.assertEqual(address.links[0].link_name, supplier)

	def test_two_sheets_with_one_new_supplier_create_it_once_in_either_order(self):
		for first_wins in (True, False):
			with self.subTest(first_wins=first_wins), _series():
				_c, a = self.sheet_of(_supplier(), _item("zz-fbs IA"))
				_c, b = self.sheet_of(_supplier(), _item("zz-fbs IB"))
				one, two = (a, b) if first_wins else (b, a)
				self.assertEqual(self.applied(one.name).status, "Executed")
				done = self.applied(two.name)
				self.assertEqual(len(self.suppliers()), 1)
				self.assertEqual(self.outcome(done)["records"][0]["action"], "existing")
				self.wipe()

	def wipe(self):
		from jarvis.tests.test_filebox_sheets import _wipe

		_wipe()
		self.enqueued.clear()

	def test_a_busy_record_lock_bounces_the_apply(self):
		_c, row = self.sheet_of(_supplier())
		keys = _sheet.lock_keys([held_sheets._record({"op": "create", "name": "", **_supplier()})])
		self.assertTrue(self.apply(row.name)["ok"])
		with patch.object(_sheet, "RECORD_LOCK_WAIT_S", 0.5), self.held_lock(keys):
			done = self.run_job()
		self.assertEqual(done.status, "Pending")
		self.assertEqual(json.loads(done.apply_errors), {"sheet": _sheet._BUSY_TEXT})
		self.assertEqual(self.suppliers(), [])

	def test_the_job_holds_the_site_apply_lock_and_waits_for_another(self):
		_c, row = self.sheet_of(_supplier())
		keys = _sheet.lock_keys([held_sheets._record({"op": "create", "name": "", **_supplier()})])
		names, seen, real = [_sheet._apply_lock(), *map(_sheet._lock_name, keys)], [], _sheet._walk

		def probe(*a, **k):
			errors = []

			def look():
				seen.extend(frappe.db.sql("SELECT IS_FREE_LOCK(%s)", (n,))[0][0] for n in names)

			_in_thread(look, errors).join(timeout=30)
			self.assertEqual(errors, [])
			return real(*a, **k)

		self.assertTrue(self.apply(row.name)["ok"])
		with patch.object(_sheet, "_walk", side_effect=probe):
			self.assertEqual(self.run_job().status, "Executed")
		self.assertEqual(seen, [0] * len(names), "taken while the records are written")
		_c, row = self.sheet_of(_item("zz-fbs I1"))
		edit = {"action": "edit", "values": {"description": "zz-fbs edited"}}
		self.assertTrue(self.apply(row.name, records={"0": edit})["ok"])
		with self.held_lock([], apply_lock=True), patch.object(_sheet, "APPLY_LOCK_WAIT_S", 0.5):
			done = self.run_job()
		self.assertEqual(
			(done.status, json.loads(done.apply_errors)), ("Pending", {"sheet": _sheet._BUSY_TEXT})
		)
		self.assertEqual(json.loads(done.apply_request)["records"]["0"], edit, "the edits are kept")
		self.assertLessEqual(_sheet.APPLY_LOCK_WAIT_S, 30, "a busy apply never parks a worker for long")
		self.assertNotIn("Another sheet", _sheet._BUSY_TEXT, "a legacy held record lock is busy too")
		self.assertIn("busy", _sheet._BUSY_TEXT)

	def test_a_stop_racing_the_finish_waits_for_the_conversation_lock(self):
		"""The finish locks the conversation, then the row (Stop's order): a Stop that
		holds the conversation stops it, never deadlocks with it."""
		from jarvis.chat.pending_actions import _lifecycle

		conv, row, question = self.routed()
		with patch.object(filebox_skills, "_eligible_choice", return_value=True):
			self.assertTrue(self.apply(row.name, answers={question: {"text": "zz-skill-a"}})["ok"])
		errors, stopping, real = [], threading.Event(), _sheet._finish

		def stop():
			_store.lock_conversation(conv)
			stopping.set()
			time.sleep(1)
			_lifecycle._drop_waiter(row.name, conv, "cancelled", sheet_to="Discarded")

		def racing(*a, **k):
			t = _in_thread(stop, errors)
			self.assertTrue(stopping.wait(timeout=30))
			try:
				return real(*a, **k)
			finally:
				t.join(timeout=60)

		with (
			patch.object(_sheet, "_finish", side_effect=racing),
			patch.object(filebox_skills, "_eligible_choice", return_value=True),
		):
			done = self.run_job()
		self.assertEqual(errors, [])
		self.assertEqual((done.status, done.stop_requested), ("Executed", 1))
		self.assertEqual(frappe.db.get_value(CONV, conv, "filebox_skill_choice"), "zz-skill-a")
		self.assertEqual(self.resumes(), [])

	def test_the_requests_first_progress_never_overwrites_the_jobs(self):
		_c, row = self.sheet_of(_supplier(), _item("zz-fbs I1"))
		self.addCleanup(_sheet._clear_progress, row.name)
		_sheet._progress(row, None, 1, 2)  # the job got there first
		self.events.clear()
		self.assertTrue(self.apply(row.name)["ok"])
		self.assertEqual(_sheet.progress(row.name), {"done": 1, "total": 2})
		self.assertEqual([p for _u, p in self.events if p.get("kind") == "sheet:progress"], [])

	def test_a_legacy_held_create_takes_the_same_locks(self):
		conv = self.conv()
		with patch.object(held_sheets, "enabled", return_value=False):
			res = self.call("create_doc", _supplier(), conv)
		self.assertEqual(res["data"]["held_for"], "approval_board")
		[held] = frappe.get_all(PA, {"owner_user": OWNER, "kind": "file_box_held"}, pluck="name")
		keys = _sheet.lock_keys(held_parties.items_of("create_doc", _supplier()))
		with patch.object(_sheet, "LEGACY_LOCK_WAIT_S", 0.5), self.held_lock(keys):
			with as_user(OWNER):
				busy = approvals_api.decide_held_action(held, "create")
				edited = approvals_api.edit_and_create_held(held, "{}")
		self.assertEqual((busy["reason_code"], edited["reason_code"]), ("busy", "busy"))
		self.assertEqual((self.row(held).status, self.suppliers()), ("Pending", []))
		with as_user(OWNER):
			self.assertEqual(approvals_api.decide_held_action(held, "create")["reason_code"], "created")
		self.assertEqual(len(self.suppliers()), 1)


# --------------------------------------------------------------------------- #
# An Address is the same one by its party, type, GSTIN and street, never its GSTIN
# alone (India Compliance's Address ``gstin``, a custom field here)
# --------------------------------------------------------------------------- #
GSTIN = "33ZZFBS0000A1Z5"


class TestAddressIdentity(_ApplyBase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.added = not frappe.get_meta("Address").has_field("gstin")
		if cls.added:
			field = {"dt": "Address", "fieldname": "gstin", "label": "GSTIN", "fieldtype": "Data"}
			frappe.get_doc({"doctype": "Custom Field", **field}).insert(ignore_permissions=True)
			frappe.clear_cache(doctype="Address")
			frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		if cls.added:
			frappe.delete_doc("Custom Field", "Address-gstin", ignore_permissions=True, force=True)
			frappe.clear_cache(doctype="Address")
			frappe.db.commit()
		super().tearDownClass()

	def existing(self, **values):
		doc = frappe.get_doc({**_address(**values)["values"], "doctype": "Address"})
		return doc.insert(ignore_permissions=True).name

	def party(self):
		frappe.get_doc({"doctype": "Supplier", "supplier_name": PARTY}).insert(ignore_permissions=True)
		frappe.db.commit()

	def test_a_partys_billing_and_shipping_with_one_gstin_stay_two(self):
		_c, row = self.sheet_of(_supplier(), _address(gstin=GSTIN), _address(kind="Shipping", gstin=GSTIN))
		done = self.applied(row.name)
		self.assertEqual([e["action"] for e in self.outcome(done)["records"]], ["created"] * 3)
		self.assertEqual(
			sorted(frappe.get_all("Address", {"address_title": PARTY}, pluck="address_type")),
			["Billing", "Shipping"],
		)

	def test_a_new_address_elsewhere_is_never_the_old_one(self):
		self.party()
		_c, row = self.sheet_of(_address(line="9 MG Road", city="Bengaluru", pincode="560001"))
		old = self.existing(pincode="600001")
		frappe.db.commit()
		[entry] = self.outcome(self.applied(row.name))["records"]
		self.assertEqual(entry["action"], "created")
		self.assertNotEqual(entry["name"], old)

	def test_only_the_same_street_of_the_party_is_reused(self):
		self.party()
		_c, row = self.sheet_of(_address(line="9 Hill Street", gstin=GSTIN, pincode="600002"))
		self.existing(gstin=GSTIN, pincode="600001")
		same = self.existing(line="9  hill street", gstin=GSTIN, pincode="600 002")
		frappe.db.commit()
		[entry] = self.outcome(self.applied(row.name))["records"]
		self.assertEqual((entry["action"], entry["name"]), ("existing", same))

	def test_a_street_without_a_pincode_is_the_same_only_in_its_city(self):
		self.party()
		_c, row = self.sheet_of(_address())  # 1 Main Road, Chennai, no pincode
		mumbai = self.existing(city="Mumbai")
		self.existing(city="Pune", pincode="411001")
		frappe.db.commit()
		[entry] = self.outcome(self.applied(row.name))["records"]
		self.assertEqual(entry["action"], "created")
		self.assertNotEqual(entry["name"], mumbai)
		values = _address(line="3 Lake View")["values"]
		self.assertIsNone(_sheet._existing_address(values))
		same = self.existing(line="3  lake view", city=" CHENNAI ")
		frappe.db.commit()
		self.assertEqual(_sheet._existing_address(values), same)
		self.assertIsNone(_sheet._existing_address({**values, "pincode": "600001"}))

	def test_the_address_type_matches_in_any_case(self):
		self.party()
		existing = self.existing()
		frappe.db.commit()
		self.assertEqual(_sheet._existing_address(_address(kind=" billing ")["values"]), existing)
		self.assertIsNone(_sheet._existing_address(_address(kind="Shipping")["values"]))

	def test_older_addresses_of_another_type_never_hide_the_match(self):
		self.party()
		old = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-1)
		names = [f"zz-fbs Ship {i:03}" for i in range(201)]  # past the scan's LIMIT 200
		frappe.db.bulk_insert(
			"Address",
			(
				"name",
				"address_title",
				"address_type",
				"address_line1",
				"city",
				"country",
				"creation",
				"modified",
			),
			[(n, PARTY, "Shipping", "1 Main Road", "Chennai", "India", old, old) for n in names],
		)
		frappe.db.bulk_insert(
			"Dynamic Link",
			("name", "parent", "parenttype", "parentfield", "idx", "link_doctype", "link_name"),
			[(frappe.generate_hash(length=12), n, "Address", "links", 1, "Supplier", PARTY) for n in names],
		)
		billing = self.existing()
		frappe.db.commit()
		self.assertEqual(_sheet._existing_address(_address()["values"]), billing)

	def test_another_gstin_is_another_address(self):
		self.party()
		existing = self.existing(gstin="27AAAAA0000A1Z5")
		frappe.db.commit()
		self.assertEqual(_sheet._existing_address(_address(gstin="27AAAAA0000A1Z5")["values"]), existing)
		self.assertIsNone(_sheet._existing_address(_address(gstin="27BBBBB1111B1Z5")["values"]))


# --------------------------------------------------------------------------- #
# Scale: 250 records apply in well under a minute
# --------------------------------------------------------------------------- #
class TestTiming(_ApplyBase):
	def big_sheet(self, n: int, tag: str) -> str:
		conv = self.conv()
		records = [
			held_sheets._record(
				{
					"op": "create",
					"name": "",
					"doctype": "Item",
					"values": {
						"item_code": f"zz-fbs {tag} {i:03d}",
						"item_name": f"zz-fbs {tag} {i:03d}",
						"item_group": GROUP,
						"stock_uom": UOM,
					},
				}
			)
			for i in range(n)
		]
		held_sheets.link_deps(records)
		with as_user(OWNER):
			name = held_sheets._open(conv, OWNER, records)
		_store.seal_sheet(name)
		frappe.db.commit()
		return name

	def test_100_and_250_items_apply_in_under_a_minute(self):
		"""The bench takes 1-4 s for 250; 60 s catches a regression long before a timeout."""
		real, report = _sheet._create, []
		for n in (100, 250):
			name = self.big_sheet(n, f"T{n}")
			took = []

			def timed(*a, **k):
				t0 = time.monotonic()
				try:
					return real(*a, **k)
				finally:
					took.append(time.monotonic() - t0)

			payload = self.decisions(name)
			t0 = time.monotonic()
			self.assertTrue(self.apply(name, payload=payload)["ok"])
			preflight = time.monotonic() - t0
			with patch.object(_sheet, "_create", side_effect=timed):
				t1 = time.monotonic()
				done = self.run_job()
				total = time.monotonic() - t1
			self.assertEqual(done.status, "Executed")
			self.assertEqual(frappe.db.count("Item", {"item_code": ["like", f"zz-fbs T{n} %"]}), n)
			self.assertTrue(
				frappe.db.exists("UOM Conversion Detail", {"parent": f"zz-fbs T{n} 000", "uom": UOM}),
				"each insert ran the Item controller",
			)
			self.assertLess(preflight + total, 60)
			report.append(
				f"n={n} preflight={preflight:.1f}s job={total:.1f}s per-record p50={statistics.median(took) * 1000:.0f}ms"
			)
		print("\nSHEET APPLY TIMING: " + " | ".join(report))
