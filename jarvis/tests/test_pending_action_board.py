"""Approval Board lane (PR-2b held File Box writes, PR-3c chat cards): list, detail,
decide, badge.

Both are sealed ``Jarvis Pending Action`` rows; the board reads them only through
these endpoints. A held row: its owner or a System Manager. A chat card: its owner
only (D1), listed at once, badged after ``BOARD_BADGE_DELAY_S`` (decision 4)."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import actions_api, held_parties
from jarvis.chat import approvals_api as board
from jarvis.tests._pending_action_helpers import (
	DEFAULT_ARGS,
	DEFAULT_TOOL,
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
		done, _ = self.held()
		self.set_col(done, status="Discarded")
		with as_user(OWNER):
			res = board.list_pending_actions_lane()
		self.assertEqual([r["name"] for r in res["rows"]], [mine])
		row = res["rows"][0]
		self.assertEqual(row["waiters_count"], 1)
		self.assertEqual(row["for_user"], "")
		self.assertEqual(
			set(row),
			{
				"name",
				"kind",
				"status",
				"summary",
				"document_type",
				"waiters_count",
				"created_at",
				"age",
				"for_user",
			},
		)
		self.assertNotIn(SECRET, json.dumps(res))
		with as_user(SM_USER):
			names = {r["name"] for r in board.list_pending_actions_lane()["rows"]}
		self.assertTrue({mine, theirs} <= names)
		self.assertNotIn(done, names)

	def test_rows_carry_the_cards_doctype_never_the_card(self):
		"""The board's type filter reads ``document_type`` off the stored card; the card
		itself (model-derived values) never rides the lane."""
		card = {"kind": "create", "doctype": "Supplier", "rows": [{"label": "Note", "value": SECRET}]}
		typed, _ = self.held(card=card)
		bare, _ = self.held()
		odd, _ = self.held()
		self.set_col(odd, card='["not", "a", "card"]')
		with as_user(OWNER):
			res = board.list_pending_actions_lane()
		rows = {r["name"]: r for r in res["rows"]}
		self.assertEqual(rows[typed]["document_type"], "Supplier")
		self.assertEqual((rows[bare]["document_type"], rows[odd]["document_type"]), ("", ""))
		self.assertNotIn(SECRET, json.dumps(res))

	def test_card_doctype_tolerates_a_missing_or_unparseable_card(self):
		for raw in (None, "", "{not json", "[1]", '"Supplier"', '{"doctype": 7}', '{"doctype": null}'):
			self.assertEqual(board._card_doctype(raw), "", raw)
		self.assertEqual(board._card_doctype('{"doctype": "Item"}'), "Item")

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
		for user, target in ((OTHER, name), (SM_USER, chat), (OTHER, chat), (OWNER, "zz-missing")):
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


class TestChatLane(_Base):
	"""PR-3c: chat cards on the lane (owner-only, even for an SM) and in the badge once
	they have waited ``BOARD_BADGE_DELAY_S``."""

	def chat(self, owner=OWNER, **kw):
		conv = self.make_conv(owner)
		frappe.db.set_value("Jarvis Conversation", conv, "title", kw.pop("title", "Quarterly <b>close</b>"))
		frappe.db.commit()
		return self.park(conv, owner=owner, **kw), conv

	def count(self, user) -> int:
		with as_user(user):
			return board.pending_count()

	def test_chat_cards_are_listed_at_once_for_their_owner_only(self):
		mine, conv = self.chat()
		held, _ = self.held()
		theirs, _ = self.chat(owner=OTHER)
		sm_own, _ = self.chat(owner=SM_USER)
		with as_user(OWNER):
			res = board.list_pending_actions_lane()
		self.assertEqual([r["name"] for r in res["rows"]], [mine, held], "oldest first, both kinds")
		row = res["rows"][0]
		self.assertEqual(
			set(row),
			{
				"name",
				"kind",
				"status",
				"summary",
				"document_type",
				"created_at",
				"age",
				"conversation",
				"conversation_title",
				"origin_page",
			},
		)
		self.assertEqual(
			(row["kind"], row["conversation"], row["conversation_title"]),
			("chat", conv, "Quarterly <b>close</b>"),
		)
		self.assertNotIn(DEFAULT_ARGS["content"], json.dumps(res))
		with as_user(SM_USER):
			names = {r["name"] for r in board.list_pending_actions_lane()["rows"]}
		self.assertIn(held, names, "an SM still sees every held row")
		self.assertIn(sm_own, names)
		self.assertFalse({mine, theirs} & names, "D1: never another user's chat card")

	def test_held_rows_never_crowd_out_a_system_managers_own_cards(self):
		"""E1: each kind is capped on its own, so a backlog of other users' held rows
		can't push an SM's own chat card past the limit."""
		for _ in range(3):
			self.held()
		mine, _ = self.chat(owner=SM_USER)
		with patch.object(board, "_HELD_LANE_LIMIT", 2), as_user(SM_USER):
			rows = board.list_pending_actions_lane()["rows"]
		self.assertIn(mine, [r["name"] for r in rows])
		self.assertEqual(len([r for r in rows if r["kind"] == HELD]), 2, "held rows capped on their own")
		self.assertEqual([r["created_at"] for r in rows], sorted(r["created_at"] for r in rows))

	def test_a_running_card_stays_and_a_decided_one_leaves(self):
		running, _ = self.chat()
		done, _ = self.chat()
		self.set_col(running, status="Executing")
		self.set_col(done, status="Discarded")
		with as_user(OWNER):
			rows = {r["name"]: r["status"] for r in board.list_pending_actions_lane()["rows"]}
		self.assertEqual(rows.get(running), "Executing")
		self.assertNotIn(done, rows)

	def test_detail_is_the_owners_card_and_never_the_seal(self):
		name, conv = self.chat()
		with as_user(OWNER):
			res = board.get_pending_action(name)
		self.assertEqual((res["kind"], res["status"], res["can_act"]), ("chat", "Pending", 1))
		self.assertEqual(res["card"], json.loads(self.row(name)["card"]))
		self.assertEqual((res["conversation"], res["conversation_title"]), (conv, "Quarterly <b>close</b>"))
		dump = json.dumps(res)
		for leak in ("sealed", "open_key", DEFAULT_ARGS["content"], self.row(name)["sealed_call"][:24]):
			self.assertNotIn(leak, dump)
		for user in (SM_USER, OTHER):
			with self.subTest(user=user), as_user(user), self.assertRaises(frappe.DoesNotExistError):
				board.get_pending_action(name)
		with as_user(SM_USER):
			self.assertEqual(board.decide_held_action(name, "skip")["reason_code"], "not_found")

	def test_the_board_decides_through_the_chat_endpoints(self):
		ran, conv = self.chat()
		dropped, other = self.chat()
		with patch("jarvis.chat.api._dispatch_turn"), fake_dispatch() as calls:
			with as_user(SM_USER):
				refused = actions_api.confirm_tool(ran, conversation=conv)
			with as_user(OWNER):
				done = actions_api.confirm_tool(ran, conversation=conv)
				discarded = actions_api.dismiss_tool(dropped, conversation=other)
		self.assertFalse(refused["ok"])
		self.assertEqual((done["ok"], done["pa_status"]), (True, "Executed"))
		self.assertEqual(discarded["reason_code"], "discarded")
		self.assertEqual((len(calls), self.row(dropped)["status"]), (1, "Discarded"))

	def test_the_badge_counts_own_chat_cards_only_after_600_s(self):
		"""AC-U9: at 599 s the card is only on the lane; at 601 s it badges for its
		owner, and never for a System Manager."""
		name, _ = self.chat()
		minted = frappe.utils.get_datetime(self.row(name)["creation"])
		with patch("frappe.utils.now_datetime", return_value=minted + timedelta(seconds=599)):
			owner_before, sm_before = self.count(OWNER), self.count(SM_USER)
			with as_user(OWNER):
				self.assertEqual(board.pending_badge(), {"count": owner_before, "next_in": 1})
		with patch("frappe.utils.now_datetime", return_value=minted + timedelta(seconds=601)):
			self.assertEqual(self.count(OWNER), owner_before + 1)
			self.assertEqual(self.count(SM_USER), sm_before)
			with as_user(OWNER):
				self.assertIsNone(board.pending_badge()["next_in"])
		self.set_col(name, status="Discarded")
		with patch("frappe.utils.now_datetime", return_value=minted + timedelta(seconds=601)):
			self.assertEqual(self.count(OWNER), owner_before, "a decided card leaves the badge")

	def test_the_next_refresh_is_when_the_oldest_waiting_card_crosses(self):
		now = frappe.utils.now_datetime()
		young, _ = self.chat()
		younger, _ = self.chat()
		old, _ = self.chat()
		self.set_col(young, creation=now - timedelta(seconds=100))
		self.set_col(younger, creation=now - timedelta(seconds=40))
		self.set_col(old, creation=now - timedelta(seconds=900))
		with patch("frappe.utils.now_datetime", return_value=now), as_user(OWNER):
			res = board.pending_badge()
			self.assertEqual(res["next_in"], 500)
			self.assertEqual(res["count"], board.pending_count())

	def test_a_reload_carries_the_cards_mint_time(self):
		"""Epoch seconds from the server: the clients' age label needs no timezone guess."""
		from jarvis.chat import pending_confirm
		from jarvis.chat.api import get_conversation

		conv = self.make_conv()
		name = pending_confirm.mint(
			conversation=conv, owner=OWNER, tool=DEFAULT_TOOL, args=dict(DEFAULT_ARGS), run_id=""
		)
		self.set_col(name, creation=self.row(name)["creation"] - timedelta(hours=3))
		with as_user(OWNER):
			[row] = [m for m in get_conversation(conv)["messages"] if m.get("tool_call_id") == name]
		self.assertAlmostEqual(row["created_at"], time.time() - 3 * 3600, delta=60)

	def test_a_legacy_card_reload_carries_its_rows_time_as_epoch(self):
		"""E3: a Redis-era card has no pending action; its row's own creation is
		stamped server-side, so no client ever parses a site-timezone string."""
		from jarvis import api
		from jarvis.chat.api import get_conversation

		conv = self.make_conv()
		api.persist_pending_action(conv, "delete_doc", {"card": {"verb": "delete"}}, "pab-legacy-tok", 9e9)
		frappe.db.sql(
			"UPDATE `tabJarvis Chat Message` SET creation=%(c)s WHERE tool_call_id='pab-legacy-tok'",
			{"c": frappe.utils.now_datetime() - timedelta(hours=3)},
		)
		frappe.db.commit()
		with as_user(OWNER):
			[row] = [
				m for m in get_conversation(conv)["messages"] if m.get("tool_call_id") == "pab-legacy-tok"
			]
		self.assertIsInstance(row["created_at"], int)
		self.assertAlmostEqual(row["created_at"], time.time() - 3 * 3600, delta=60)
