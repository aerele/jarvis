"""One approval per proposed create (admin-v2 #777 / #778, ``jarvis.chat.one_approval``).

The agent can stage one new record twice: as a draft card (a ``jarvis-action`` block,
saved through ``apply_action``) and as a gated ``create_doc`` confirmation card. These
tests drive both routes for real (the gate parks the card, ``confirm_tool`` runs it,
``apply_action`` saves the draft) as a plain Jarvis user, and two of them race real
second connections.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import pending_confirm
from jarvis.chat.actions_api import apply_action, confirm_tool, dismiss_tool
from jarvis.chat.api import _next_seq
from jarvis.chat.one_approval import create_key
from jarvis.tests._pending_action_helpers import OWNER, PendingActionTestMixin, as_user
from jarvis.tests.test_pending_confirm import _purge_legacy

PA = "Jarvis Pending Action"
MSG = "Jarvis Chat Message"
_TRIAL = {"preview": True, "would": {"ok": True}, "note": ""}


class TestOneApprovalPerCreate(PendingActionTestMixin, FrappeTestCase):
	def setUp(self):
		super().setUp()
		# The follow-up turns (a confirmed card's, a failed save's) are not under test:
		# never reach the pump.
		for target in (
			"jarvis.chat.actions_api.enqueue_continuation",
			"jarvis.chat.api.enqueue_continuation",
		):
			cont = patch(target, return_value=None)
			cont.start()
			self.addCleanup(cont.stop)
		_purge_legacy(OWNER)
		self.addCleanup(_purge_legacy, OWNER)
		self.tag = "pa-test one-approval " + frappe.generate_hash(length=8)
		self.conv = self.make_conv()

	# -- the two routes ---------------------------------------------------------------

	def values(self, date="2026-10-20"):
		# Allocated to the owner: Frappe's ToDo permission hook lets a non-manager
		# create only their own.
		return {"description": self.tag, "date": date, "allocated_to": OWNER}

	def draft(self, values=None, *, doctype="ToDo", conv=None, role="assistant", verb="create"):
		"""The assistant reply carrying the draft card; its name is the draft key."""
		values = values or self.values()
		block = {
			"kind": "doc",
			"verb": verb,
			"doctype": doctype,
			"fields": [{"label": k, "value": v} for k, v in values.items()],
		}
		doc = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": conv or self.conv,
				"seq": _next_seq(conv or self.conv),
				"role": role,
				"content": f"Here it is.\n\n```jarvis-action\n{frappe.as_json(block, indent=None)}\n```",
				"streaming": 0,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		time.sleep(0.01)  # the reply's creation strictly precedes what follows
		return doc.name

	def human(self):
		"""The person speaks: what follows is another turn."""
		row = frappe.get_doc(
			{
				"doctype": MSG,
				"conversation": self.conv,
				"seq": _next_seq(self.conv),
				"role": "user",
				"origin": "human",
				"content": "one more thing",
			}
		)
		row.flags.jarvis_server_write = True
		row.insert(ignore_permissions=True)
		frappe.db.commit()

	def card(self, values=None, *, doctype="ToDo"):
		"""A create confirmation card, parked by the real gate as the owner (its sandboxed
		trial run stubbed, as in test_card_supersede: the card, its stamp and the park are real)."""
		with patch("jarvis.api._run_preview", return_value=_TRIAL), as_user(OWNER):
			res = api._run_tool(
				"create_doc", {"doctype": doctype, "values": values or self.values()}, conversation=self.conv
			)
		frappe.db.commit()
		self.assertEqual(res["data"]["status"], "pending_confirmation", res)
		return frappe.get_all(
			PA,
			filters={"conversation": self.conv, "status": "Pending"},
			order_by="creation desc",
			pluck="name",
		)[0]

	def confirm(self, token):
		with as_user(OWNER):
			res = confirm_tool(token, conversation=self.conv)
		frappe.db.commit()
		return res

	def save(self, message, date="2026-10-29", *, values=None, doctype="ToDo"):
		with as_user(OWNER):
			res = apply_action(
				frappe.as_json(
					{
						"verb": "create",
						"doctype": doctype,
						"values": values or self.values(date),
						"conversation": self.conv,
						"message": message,
					}
				)
			)
		frappe.db.commit()
		return res

	def dates(self):
		return sorted(str(d) for d in frappe.get_all("ToDo", filters={"description": self.tag}, pluck="date"))

	def status(self, token):
		return frappe.db.get_value(PA, token, "status")

	# -- both approval orders, the person's edits ---------------------------------------

	def test_draft_saved_first_retires_the_same_proposals_card_and_keeps_the_edit(self):
		token = self.card()
		draft = self.draft()
		self.assertTrue(self.save(draft)["ok"])
		self.assertEqual(self.status(token), "Superseded")
		again = self.confirm(token)
		self.assertFalse(again.get("ok"))
		self.assertEqual(again.get("outcome"), "superseded")
		self.assertEqual(self.dates(), ["2026-10-29"])  # the edited date, once

	def test_card_confirmed_first_refuses_the_same_proposals_draft(self):
		draft = self.draft()
		token = self.card()
		self.assertTrue(self.confirm(token)["ok"])
		created = frappe.db.get_value(PA, token, "result_name")
		refused = self.save(draft)
		self.assertFalse(refused["ok"])
		self.assertNotIn("closed", refused)  # the panel stays open with the person's edits
		self.assertIn(
			f"{created} was already created from its confirmation card", refused["error"]["message"]
		)
		self.assertEqual(self.dates(), ["2026-10-20"])

	# -- same doctype: matched on the turn AND the original call, never on the doctype -----

	def test_a_different_card_from_the_same_turn_is_never_retired(self):
		# Same turn, other values: maybe the same record restaged, maybe not. Ask.
		token = self.card(self.values("2026-11-05"))
		draft = self.draft()
		refused = self.save(draft)
		self.assertFalse(refused["ok"])
		self.assertIn("still waiting", refused["error"]["message"])
		self.assertEqual(self.status(token), "Pending")
		self.assertEqual(self.dates(), [])
		with as_user(OWNER):
			dismiss_tool(token, conversation=self.conv)
		frappe.db.commit()
		self.assertTrue(self.save(draft)["ok"])  # the person decided: the draft saves
		self.assertEqual(self.dates(), ["2026-10-29"])

	def test_equal_values_from_another_turn_are_never_merged(self):
		# Two open proposals with the same values, neither saved: maybe one record asked
		# twice, maybe two. The card is never retired on equal values alone.
		token = self.card()
		self.human()
		draft = self.draft()
		refused = self.save(draft)
		self.assertIn("may be this same record", refused["error"]["message"])
		self.assertEqual(self.status(token), "Pending")
		self.assertEqual(self.dates(), [])

	def test_a_different_card_from_another_turn_is_ambiguous_and_waits(self):
		# Other turn, other values proves nothing: it may be the record the person
		# just changed (below). The card stays and the person decides.
		token = self.card(self.values("2026-11-05"))
		self.human()
		refused = self.save(self.draft())
		self.assertIn("still waiting", refused["error"]["message"])
		self.assertEqual(self.status(token), "Pending")
		self.assertEqual(self.dates(), [])

	def test_an_edited_restage_never_leaves_the_old_card_to_create_the_old_date(self):
		# #778: a card with the 18th, then "make it the 30th" and a draft with the 30th.
		token = self.card(self.values("2026-10-18"))
		self.human()
		draft = self.draft(self.values("2026-10-30"))
		refused = self.save(draft, date="2026-10-30")
		self.assertIn("still waiting", refused["error"]["message"])
		with as_user(OWNER):
			dismiss_tool(token, conversation=self.conv)
		frappe.db.commit()
		self.assertTrue(self.save(draft, date="2026-10-30")["ok"])
		self.assertFalse(self.confirm(token).get("ok"))
		self.assertEqual(self.dates(), ["2026-10-30"])

	def test_a_different_card_from_the_same_turn_confirmed_after_the_draft_is_not_taken_for_it(self):
		draft = self.draft()
		token = self.card(self.values("2026-11-05"))
		self.assertTrue(self.confirm(token)["ok"])
		created = frappe.db.get_value(PA, token, "result_name")
		refused = self.save(draft)
		self.assertFalse(refused["ok"])
		self.assertIn(
			f"ToDo {created} was created from a confirmation card after this draft",
			refused["error"]["message"],
		)
		self.assertNotIn("already created", refused["error"]["message"])
		self.assertEqual(self.dates(), ["2026-11-05"])

	def test_equal_values_confirmed_from_another_turn_after_the_draft_are_not_taken_for_it(self):
		draft = self.draft()
		self.human()
		token = self.card()
		self.assertTrue(self.confirm(token)["ok"])
		refused = self.save(draft)
		self.assertIn("may be this same record", refused["error"]["message"])
		self.assertNotIn("already created", refused["error"]["message"])
		self.assertEqual(self.dates(), ["2026-10-20"])

	def test_a_different_card_from_another_turn_confirmed_after_the_draft_refuses_it(self):
		draft = self.draft()
		self.human()
		token = self.card(self.values("2026-11-05"))
		self.assertTrue(self.confirm(token)["ok"])
		refused = self.save(draft)
		self.assertIn("may be this same record", refused["error"]["message"])
		self.assertEqual(self.dates(), ["2026-11-05"])

	def test_a_card_for_another_doctype_stays(self):
		token = self.card({"title": self.tag, "public": 0}, doctype="Note")
		self.assertTrue(self.save(self.draft())["ok"])
		self.assertEqual(self.status(token), "Pending")

	# -- new proposals after a save -------------------------------------------------------

	def test_a_card_confirmed_before_the_draft_was_proposed_leaves_the_draft_alone(self):
		token = self.card()
		self.assertTrue(self.confirm(token)["ok"])
		self.assertTrue(self.save(self.draft())["ok"])
		self.assertEqual(self.dates(), ["2026-10-20", "2026-10-29"])

	def test_a_card_proposed_after_the_save_is_a_new_proposal_even_with_equal_values(self):
		self.assertTrue(self.save(self.draft())["ok"])
		token = self.card()
		self.assertTrue(self.confirm(token)["ok"])
		self.assertEqual(self.dates(), ["2026-10-20", "2026-10-29"])

	def test_a_new_draft_with_equal_values_after_a_save_is_a_new_proposal(self):
		self.assertTrue(self.save(self.draft())["ok"])
		self.assertTrue(self.save(self.draft())["ok"])
		self.assertEqual(self.dates(), ["2026-10-29", "2026-10-29"])

	# -- one save per draft ---------------------------------------------------------------

	def test_the_same_draft_saved_twice_creates_one_record(self):
		draft = self.draft()
		self.assertTrue(self.save(draft)["ok"])
		again = self.save(draft)
		self.assertFalse(again["ok"])
		self.assertIn("already saved", again["error"]["message"])
		self.assertEqual(self.dates(), ["2026-10-29"])

	def test_a_saved_draft_cannot_be_reopened_from_the_client(self):
		draft = self.draft()
		self.assertTrue(self.save(draft)["ok"])
		with as_user(OWNER):
			row = frappe.get_doc(MSG, draft)
			row.draft_saved_as = ""
			self.assertRaises(frappe.PermissionError, row.save, ignore_permissions=True)
		frappe.db.rollback()
		self.assertFalse(self.save(draft)["ok"])
		self.assertEqual(self.dates(), ["2026-10-29"])

	def test_two_saves_of_the_same_draft_at_once_create_one_record(self):
		draft = self.draft()
		results = self._race(lambda: self._save_in_thread(draft), lambda: self._save_in_thread(draft))
		self.assertEqual(sorted(bool(r.get("ok")) for r in results), [False, True], results)
		self.assertEqual(self.dates(), ["2026-10-29"])

	def test_a_draft_save_racing_its_cards_confirm_creates_one_record(self):
		token = self.card()
		draft = self.draft()
		results = self._race(lambda: self._save_in_thread(draft), lambda: self._confirm_in_thread(token))
		self.assertEqual(len(self.dates()), 1, results)
		self.assertEqual(sum(1 for r in results if r.get("ok")), 1, results)

	def test_a_receipt_failure_still_uses_the_draft_once(self):
		draft = self.draft()
		with patch("jarvis.chat.actions_api._append_receipt", side_effect=RuntimeError("receipt down")):
			self.assertTrue(self.save(draft)["ok"])
		self.assertFalse(self.save(draft)["ok"])
		self.assertEqual(self.dates(), ["2026-10-29"])

	def test_a_settle_failure_never_turns_a_saved_draft_into_a_failure(self):
		token = self.card()
		draft = self.draft()
		with patch("jarvis.chat.pending_actions.settle", side_effect=RuntimeError("settle down")):
			self.assertTrue(self.save(draft)["ok"])
		self.assertEqual(self.status(token), "Superseded")
		self.assertFalse(self.save(draft)["ok"])
		self.assertFalse(self.confirm(token).get("ok"))
		self.assertEqual(self.dates(), ["2026-10-29"])

	def test_a_failed_save_keeps_the_card_and_the_draft(self):
		token = self.card()
		draft = self.draft()
		self.assertFalse(self.save(draft, date="not-a-date")["ok"])
		self.assertEqual(self.status(token), "Pending")
		self.assertFalse(frappe.db.get_value(MSG, draft, "draft_saved_as"))
		self.assertTrue(self.save(draft)["ok"])
		self.assertEqual(self.status(token), "Superseded")
		self.assertEqual(self.dates(), ["2026-10-29"])

	def test_a_chat_changed_after_this_requests_read_view_is_a_try_again(self):
		# Snapshot isolation on (MariaDB 11.6+ default): the lock meets a row another
		# connection changed after this request first read. Refused, nothing claimed.
		from jarvis.tests.race_harness import open_read_view, other_connection, snapshot_isolation_on

		draft = self.draft()
		with snapshot_isolation_on():
			open_read_view()
			with other_connection() as db:
				db.sql(
					"UPDATE `tabJarvis Conversation` SET last_active_at=NOW() WHERE name=%(c)s",
					{"c": self.conv},
				)
				db.commit()
			refused = self.save(draft)
		self.assertIn("Try again", refused["error"]["message"])
		self.assertFalse(frappe.db.get_value(MSG, draft, "draft_saved_as"))
		self.assertEqual(self.dates(), [])
		self.assertTrue(self.save(draft)["ok"])

	def test_a_failed_submit_rolls_back_the_claim_and_the_retirement(self):
		token = self.card()
		draft = self.draft()
		with (
			patch("jarvis.tools.submit_doc.submit_doc", side_effect=frappe.ValidationError("cannot submit")),
			as_user(OWNER),
		):
			res = apply_action(
				frappe.as_json(
					{
						"verb": "create",
						"doctype": "ToDo",
						"values": self.values("2026-10-29"),
						"conversation": self.conv,
						"message": draft,
						"submit": 1,
					}
				)
			)
		frappe.db.commit()
		self.assertFalse(res["ok"])
		self.assertEqual(self.status(token), "Pending")
		self.assertFalse(frappe.db.get_value(MSG, draft, "draft_saved_as"))
		self.assertEqual(self.dates(), [])

	# -- cards it cannot tell apart: wait, never guess ------------------------------------

	def test_a_card_being_confirmed_right_now_refuses_the_draft(self):
		token = self.card()
		draft = self.draft()
		with self._row_held(token):
			refused = self.save(draft)
		self.assertFalse(refused["ok"])
		self.assertIn("Try again", refused["error"]["message"])
		self.assertEqual(self.status(token), "Pending")
		self.assertEqual(self.dates(), [])

	def test_a_running_card_refuses_the_draft(self):
		token = self.card()
		draft = self.draft()
		frappe.db.sql(f"UPDATE `tab{PA}` SET status='Executing' WHERE name=%(n)s", {"n": token})
		frappe.db.commit()
		refused = self.save(draft)
		self.assertIn("right now", refused["error"]["message"])
		self.assertEqual(self.dates(), [])

	def test_a_card_without_its_card_refuses_the_draft(self):
		with as_user(OWNER):
			token = pending_confirm.mint(
				conversation=self.conv,
				owner=OWNER,
				tool="create_doc",
				args={"doctype": "ToDo", "values": self.values()},
				run_id="",
				preview=None,
			)
		frappe.db.commit()
		refused = self.save(self.draft())
		self.assertIn("still waiting", refused["error"]["message"])
		self.assertEqual(self.status(token), "Pending")

	def test_a_card_parked_before_proposal_keys_is_never_matched(self):
		token = self.card()
		preview = frappe.parse_json(frappe.db.get_value(PA, token, "preview"))
		preview["card"].pop("proposal_key")
		frappe.db.set_value(PA, token, "preview", frappe.as_json(preview), update_modified=False)
		frappe.db.commit()
		refused = self.save(self.draft())
		self.assertIn("still waiting", refused["error"]["message"])
		self.assertEqual(self.status(token), "Pending")

	def test_a_waiting_legacy_card_refuses_the_draft(self):
		with (
			patch.dict(frappe.local.conf, {"jarvis_pa_chat_cards": 0}),
			patch("jarvis.api._run_preview", return_value=_TRIAL),
			as_user(OWNER),
		):
			res = api._run_tool(
				"create_doc", {"doctype": "ToDo", "values": self.values()}, conversation=self.conv
			)
		frappe.db.commit()
		self.assertEqual(res["data"]["status"], "pending_confirmation", res)
		self.assertFalse(frappe.db.exists(PA, {"conversation": self.conv}))
		refused = self.save(self.draft())
		self.assertIn("still waiting", refused["error"]["message"])
		self.assertEqual(self.dates(), [])

	# -- the source reply -----------------------------------------------------------------

	def test_a_named_draft_must_be_a_create_reply_in_this_chat(self):
		other = self.make_conv()
		for message in (
			"not-a-message",
			self.draft(conv=other),
			self.draft(role="user"),
			self.draft({"title": self.tag}, doctype="Note"),
			self.draft(verb="update"),
		):
			refused = self.save(message)
			self.assertFalse(refused["ok"], message)
			self.assertIn("no longer available", refused["error"]["message"])
		self.assertEqual(self.dates(), [])

	def test_a_save_naming_no_draft_is_refused_while_a_create_card_of_the_doctype_exists(self):
		self.assertTrue(self.save("")["ok"])  # no card: a plain create, as before
		token = self.card()
		self.assertIn("still waiting", self.save("")["error"]["message"])
		self.assertTrue(self.confirm(token)["ok"])
		self.assertIn("may be this same record", self.save("")["error"]["message"])
		self.assertEqual(self.dates(), ["2026-10-20", "2026-10-29"])

	def test_the_gate_stamps_the_original_proposal(self):
		token = self.card()
		card = frappe.parse_json(frappe.db.get_value(PA, token, "preview"))["card"]
		self.assertEqual(card["proposal_key"], create_key("ToDo", self.values()))
		# Blanks are not proposals, and "2546" is 2546.
		self.assertEqual(create_key("ToDo", {"a": "2546", "b": ""}), create_key("ToDo", {"a": 2546}))
		self.assertNotEqual(create_key("ToDo", {"a": 1}), create_key("Note", {"a": 1}))

	# -- real second connections ----------------------------------------------------------

	def _race(self, *calls):
		"""Run ``calls`` on their own connections at once; each create is slowed so the
		two really overlap."""
		from jarvis.tools import create_doc as create_mod

		real = create_mod.create_doc

		def slow(*a, **kw):
			time.sleep(0.5)
			return real(*a, **kw)

		self.site = frappe.local.site
		self.gate = threading.Barrier(len(calls))
		results: list = [None] * len(calls)

		def run(i, fn):
			results[i] = fn()

		with patch.object(create_mod, "create_doc", side_effect=slow):
			threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(calls)]
			for t in threads:
				t.start()
			for t in threads:
				t.join(timeout=60)
		frappe.db.commit()
		return results

	def _in_thread(self, fn):
		frappe.init(site=self.site)
		frappe.connect()
		try:
			# As MariaDB 10.x runs by default (CI's 10.11 too): the engine does not refuse a
			# stale read view, so only the code under test can stop the second save.
			frappe.db.sql("SET SESSION innodb_snapshot_isolation=OFF")
			frappe.set_user(OWNER)
			self.gate.wait(timeout=20)
			res = fn()
			frappe.db.commit()
			return res if isinstance(res, dict) else {"ok": False, "result": res}
		except Exception as e:
			frappe.db.rollback()
			return {"ok": False, "raised": repr(e)}
		finally:
			frappe.destroy()

	def _save_in_thread(self, message):
		payload = frappe.as_json(
			{
				"verb": "create",
				"doctype": "ToDo",
				"values": self.values("2026-10-29"),
				"conversation": self.conv,
				"message": message,
			}
		)
		return self._in_thread(lambda: apply_action(payload))

	def _confirm_in_thread(self, token):
		return self._in_thread(lambda: confirm_tool(token, conversation=self.conv))

	def _row_held(self, token):
		"""Hold ``token``'s row lock on a second connection, as a Confirm's claim does."""
		test = self
		site = frappe.local.site

		class _Held:
			def __enter__(self):
				self.locked, self.release = threading.Event(), threading.Event()

				def holder():
					frappe.init(site=site)
					frappe.connect()
					try:
						frappe.db.sql(f"SELECT name FROM `tab{PA}` WHERE name=%(n)s FOR UPDATE", {"n": token})
						self.locked.set()
						self.release.wait(timeout=30)
						frappe.db.rollback()
					finally:
						frappe.destroy()

				self.thread = threading.Thread(target=holder)
				self.thread.start()
				test.assertTrue(self.locked.wait(timeout=20))
				return self

			def __exit__(self, *exc):
				self.release.set()
				self.thread.join(timeout=30)

		return _Held()
