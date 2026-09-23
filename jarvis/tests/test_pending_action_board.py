"""Approval Board lane for held File Box writes (PR-2b): list, detail, decide, badge.

Held rows are sealed ``Jarvis Pending Action`` rows (kind ``file_box_held``); the
board reads them only through these endpoints, as the owner or a System Manager."""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import approvals_api as board
from jarvis.chat import held_parties
from jarvis.tests._pending_action_helpers import (
	OTHER,
	OWNER,
	PA,
	SM_USER,
	PendingActionTestMixin,
	as_user,
	ensure_user,
	fake_dispatch,
)

HELD = "file_box_held"
PARTY = "zz-pab Acme Pvt. Ltd."
TAX = "29ABCDE1234F1Z5"
SECRET = "PAB-SECRET-7719"
BUYER = "pab-buyer@example.com"  # an exec_user who can read Suppliers


def _create_args(name=PARTY, tax=None):
	values = {"supplier_name": name, "supplier_details": SECRET}
	if tax:
		values["tax_id"] = tax
	return {"doctype": "Supplier", "values": values}


def _batch_args():
	return {
		"docs": [
			{"doctype": "Supplier", "values": {"supplier_name": PARTY}},
			{"doctype": "Supplier", "values": {"supplier_name": PARTY + " Two"}},
		]
	}


class _Base(PendingActionTestMixin, FrappeTestCase):
	def setUp(self):
		super().setUp()
		ensure_user(BUYER, ("Jarvis User", "Purchase Manager"))
		self.addCleanup(self._wipe)

	def _wipe(self):
		frappe.db.delete("Supplier", {"supplier_name": ["like", "zz-pab%"]})
		frappe.db.delete("Jarvis Agent Write", {"actor": BUYER})
		frappe.db.commit()

	def held(self, owner=OWNER, exec_user=None, args=None, conv=None, **kw):
		conv = conv or self.make_conv(owner)
		name = self.park(
			conv,
			kind=HELD,
			owner=owner,
			exec_user=exec_user or owner,
			tool="create_doc",
			args=args or _create_args(),
			summary=kw.pop("summary", f"New supplier: {PARTY}"),
			**kw,
		)
		return name, conv

	def supplier(self, name=PARTY, tax=None):
		doc = frappe.get_doc({"doctype": "Supplier", "supplier_name": name, "tax_id": tax})
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name

	def decide(self, user, name, action, use_existing=None):
		with as_user(user):
			return board.decide_held_action(name, action, use_existing)


class TestHeldParties(FrappeTestCase):
	def test_items_and_keys(self):
		single = held_parties.items_of("create_doc", _create_args(tax="29abcde 1234f1z5"))
		self.assertEqual(len(single), 1)
		self.assertEqual(held_parties.item_key(single[0]), f"gstin:Supplier:{TAX}")
		named = held_parties.items_of("create_doc", _create_args(name="  ACME   pvt. ltd "))[0]
		self.assertEqual(held_parties.item_key(named), "name:Supplier:acme pvt ltd")
		bare = held_parties.items_of("create_doc", {"doctype": "Address", "values": {"city": "X"}})[0]
		self.assertTrue(held_parties.item_key(bare).startswith("args:"))
		self.assertEqual(len(held_parties.items_of("create_docs", _batch_args())), 2)
		upd = held_parties.items_of("update_doc", {"doctype": "Supplier", "name": "S-1", "changes": {"x": 1}})
		key = held_parties.item_key(upd[0])
		self.assertTrue(key.startswith("doc:Supplier:S-1:"), key)  # + a hash of the changes
		same = held_parties.items_of(
			"update_doc", {"doctype": "Supplier", "updates": [{"name": "S-1", "changes": {"x": 1}}]}
		)
		other = held_parties.items_of(
			"update_doc", {"doctype": "Supplier", "name": "S-1", "changes": {"x": 2}}
		)
		self.assertEqual(held_parties.item_key(same[0]), key)
		self.assertNotEqual(held_parties.item_key(other[0]), key)
		self.assertEqual(held_parties.items_of("submit_doc", {"doctype": "Supplier"}), [])


class TestLane(_Base):
	def test_scoped_to_owner_or_system_manager_and_never_sealed(self):
		mine, _ = self.held()
		theirs, _ = self.held(owner=OTHER)
		self.park(self.make_conv(), kind="chat")  # chat rows are PR-3c's lane
		done, _ = self.held()
		self.set_col(done, status="Discarded")
		with as_user(OWNER):
			res = board.list_pending_actions_lane()
		self.assertEqual([r["name"] for r in res["rows"]], [mine])
		row = res["rows"][0]
		self.assertEqual(row["waiters_count"], 1)
		self.assertEqual(row["for_user"], "")
		self.assertEqual(
			set(row), {"name", "status", "summary", "waiters_count", "created_at", "age", "for_user"}
		)
		self.assertNotIn(SECRET, json.dumps(res))
		with as_user(SM_USER):
			names = {r["name"] for r in board.list_pending_actions_lane()["rows"]}
		self.assertTrue({mine, theirs} <= names)
		self.assertNotIn(done, names)

	def test_pending_count_adds_held_rows(self):
		with as_user(OWNER):
			before = board.pending_count()
		self.held()
		self.held(owner=OTHER)
		with as_user(OWNER):
			self.assertEqual(board.pending_count(), before + 1)


class TestDetail(_Base):
	def test_owner_reads_card_and_never_sealed_columns(self):
		name, _ = self.held()
		with as_user(OWNER):
			res = board.get_pending_action(name)
		self.assertEqual(res["kind"], HELD)
		self.assertEqual(res["card"], self.row(name)["card"] and json.loads(self.row(name)["card"]))
		self.assertEqual((res["can_act"], res["can_use_existing"], res["doctype"]), (1, 1, "Supplier"))
		self.assertEqual(res["waiters_count"], 1)
		# AC-U4 carve-out (PR-2d): the owner sees the proposed values, never the seal.
		self.assertEqual(res["docs"][0]["values"]["supplier_details"], SECRET)
		dump = json.dumps(res)
		for leak in ("sealed", "open_key", self.row(name)["sealed_call"][:24]):
			self.assertNotIn(leak, dump)

	def test_unauthorized_or_wrong_kind_is_not_found(self):
		name, _ = self.held()
		chat = self.park(self.make_conv())
		for user, target in ((OTHER, name), (OWNER, chat), (OWNER, "zz-missing")):
			with self.subTest(user=user, target=target), as_user(user):
				with self.assertRaises(frappe.DoesNotExistError):
					board.get_pending_action(target)

	def test_candidates_are_read_as_exec_user(self):
		existing = self.supplier(tax=TAX)
		visible, _ = self.held(owner=SM_USER, exec_user=BUYER, args=_create_args(tax=TAX))
		hidden, _ = self.held(owner=SM_USER, args=_create_args(tax=TAX))
		with as_user(SM_USER):
			seen = board.get_pending_action(visible)["candidates"]
			# Same approver; only BUYER (the first row's exec_user) can read Suppliers.
			self.assertEqual(board.get_pending_action(hidden)["candidates"], [])
		self.assertEqual(seen, [{"name": existing, "title": PARTY, "gstin": TAX}])

	def test_batch_offers_no_use_existing(self):
		name, _ = self.held(args=_batch_args())
		with as_user(OWNER):
			res = board.get_pending_action(name)
		self.assertEqual((res["can_use_existing"], res["candidates"], res["doctype"]), (0, [], ""))

	def test_terminal_unsettled_row_is_settled_on_read(self):
		name, _ = self.held()
		self.set_col(name, status="Discarded", reason_code="discarded")
		with as_user(OWNER):
			res = board.get_pending_action(name)
		self.assertEqual((res["status"], res["can_act"]), ("Discarded", 0))
		self.assertEqual(self.row(name)["settled"], 1)


class TestDecide(_Base):
	def test_skip_needs_no_identity_preflight_but_create_does(self):
		name, _ = self.held(owner=SM_USER, exec_user=OWNER)
		with (
			patch("jarvis.chat.pending_actions._execute.is_valid_unattended_owner", return_value=False),
			fake_dispatch() as calls,
		):
			refused = self.decide(SM_USER, name, "create")
			skipped = self.decide(SM_USER, name, "skip")
		self.assertEqual(refused["reason_code"], "identity_refused")
		self.assertEqual((skipped["reason_code"], skipped["waiters_count"]), ("discarded", 1))
		self.assertEqual((self.row(name)["status"], calls), ("Discarded", []))

	def test_create_runs_once_as_exec_user(self):
		name, _ = self.held()
		seen = []

		def _run(tool, args):
			seen.append(frappe.session.user)
			return {"name": "SUP-1"}

		with fake_dispatch(_run) as calls:
			res = self.decide(OWNER, name, "create")
			again = self.decide(OWNER, name, "create")
		self.assertEqual((res["ok"], res["reason_code"], res["pa_status"]), (True, "created", "Executed"))
		self.assertEqual(again["reason_code"], "already_handled")
		self.assertEqual(len(calls), 1)
		self.assertEqual(seen, [OWNER])

	def test_system_manager_creates_for_the_dropper(self):
		name, _ = self.held()
		seen = []
		with fake_dispatch(lambda t, a: seen.append(frappe.session.user) or {"name": "SUP-1"}):
			res = self.decide(SM_USER, name, "create")
		self.assertEqual(res["pa_status"], "Executed")
		self.assertEqual(seen, [OWNER])
		self.assertEqual(self.row(name)["decided_by"], SM_USER)

	def test_create_refuses_when_the_party_exists_now(self):
		name, _ = self.held(owner=SM_USER, exec_user=BUYER, args=_create_args(tax=TAX))
		existing = self.supplier(name="zz-pab Other Name", tax=TAX)
		with fake_dispatch() as calls:
			res = self.decide(SM_USER, name, "create")
		self.assertEqual(res["reason_code"], "exists")
		self.assertIn("Use existing", res["error"]["message"])
		self.assertEqual(res["candidates"][0]["name"], existing)
		self.assertEqual(calls, [])
		self.assertEqual(self.row(name)["status"], "Pending")

	def test_a_batch_whose_party_exists_now_says_skip_not_rerun(self):
		name, _ = self.held(owner=SM_USER, exec_user=BUYER, args=_batch_args())
		self.supplier()
		with fake_dispatch() as calls:
			res = self.decide(SM_USER, name, "create")
		self.assertEqual(res["reason_code"], "exists")
		self.assertIn("Skip this approval", res["error"]["message"])
		self.assertIn("drop the file again", res["error"]["message"])
		self.assertNotIn("re-run", res["error"]["message"].lower())
		self.assertEqual((calls, self.row(name)["status"]), ([], "Pending"))

	def test_use_existing_resolves_without_creating(self):
		existing = self.supplier()
		name, _ = self.held(owner=SM_USER, exec_user=BUYER)
		before = frappe.db.count("Supplier")
		with fake_dispatch() as calls:
			res = self.decide(SM_USER, name, "use_existing", existing)
		self.assertEqual(
			(res["reason_code"], res["pa_status"], res["outcome"]), ("use_existing", "Executed", "confirmed")
		)
		again = self.decide(SM_USER, name, "use_existing", existing)
		self.assertEqual(
			(again["reason_code"], again["pa_status"], again["outcome"]),
			("already_handled", "Executed", "confirmed"),
		)
		row = self.row(name)
		self.assertEqual(
			(row.result_doctype, row.result_name, row.reason_code), ("Supplier", existing, "use_existing")
		)
		self.assertEqual((calls, frappe.db.count("Supplier")), ([], before))

	def test_use_existing_refusals_leave_the_row_pending(self):
		existing = self.supplier()
		name, _ = self.held()  # exec_user OWNER cannot read Suppliers
		batch, _ = self.held(owner=SM_USER, exec_user=BUYER, args=_batch_args())
		cases = (
			(OWNER, name, "", "record_required"),
			(OWNER, name, "zz-pab missing", "record_not_found"),
			(OWNER, name, existing, "record_not_found"),
			(SM_USER, batch, existing, "use_existing_unavailable"),
		)
		cases += (("Administrator", name, existing, "record_not_found"),)  # readable by the approver only
		for user, target, link, code in cases:
			with self.subTest(code=code, link=link):
				res = self.decide(user, target, "use_existing", link)
				self.assertEqual((res["ok"], res["reason_code"]), (False, code))
				self.assertEqual(self.row(target)["status"], "Pending")

	def test_use_existing_needs_a_valid_exec_user(self):
		existing = self.supplier()
		name, _ = self.held(owner=SM_USER, exec_user=OWNER)
		with patch("jarvis.permissions.is_valid_unattended_owner", return_value=False):
			res = self.decide(SM_USER, name, "use_existing", existing)
		self.assertEqual(res["reason_code"], "identity_refused")
		self.assertEqual(self.row(name)["status"], "Pending")

	def test_a_use_existing_crash_logs_and_raises_no_arg_values(self):
		# AC-U4: what the request handler would log (locals included) must hold no arg.
		from jarvis.chat.pending_actions import ExecuteCrashed

		existing = self.supplier()
		name, _ = self.held(owner=SM_USER, exec_user=BUYER)
		seen = {}
		with patch("jarvis.chat.pending_actions._store._terminal_update", side_effect=RuntimeError("boom")):
			try:
				self.decide(SM_USER, name, "use_existing", existing)
			except ExecuteCrashed as e:
				seen.update(
					msg=str(e), tb=frappe.get_traceback(with_context=True), chained=not e.__suppress_context__
				)
		self.assertEqual(set(seen), {"msg", "tb", "chained"}, "a value-free ExecuteCrashed")
		self.assertFalse(seen["chained"])
		[log] = self.logged("use_existing_crashed")
		self.assertIn("boom", log)
		for text in (seen["msg"], seen["tb"], log):
			self.assertNotIn(SECRET, text)
		self.assertEqual(self.row(name)["status"], "Pending")

	def test_non_owner_learns_nothing_and_writes_nothing(self):
		name, _ = self.held()
		with fake_dispatch() as calls:
			for action in ("create", "use_existing", "skip"):
				with self.subTest(action=action):
					res = self.decide(OTHER, name, action, "x")
					self.assertEqual(res["reason_code"], "not_found")
		self.assertEqual(calls, [])
		self.assertEqual(self.row(name)["status"], "Pending")

	def test_unknown_action_is_refused(self):
		name, _ = self.held()
		with self.assertRaises(frappe.ValidationError):
			self.decide(OWNER, name, "approve")
		self.assertEqual(frappe.db.get_value(PA, name, "status"), "Pending")
