"""History paging for jarvis.chat.api.get_conversation (#715 F13).

Without ``limit`` the endpoint still returns the whole visible thread, as every
caller had it. With ``limit`` it returns one page, newest first by ``seq``, snapped
back to the user row that opens its oldest turn, with a ``before`` cursor, the
pending cards older than the page, a refresh of named older rows, and large tool
results sent compact (``get_tool_detail`` returns them whole, same owner gate).

Nothing here commits: every row lives in the test transaction and rolls back.
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import api as chat_api

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
OWNER = "jarvis-history-paging@example.com"
OTHER = "jarvis-history-paging-other@example.com"


def _ensure_user(user: str) -> None:
	if frappe.db.exists("User", user):
		return
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": user,
			"first_name": "Jarvis",
			"last_name": "Paging",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
		}
	)
	doc.insert(ignore_permissions=True)
	doc.add_roles("System Manager")


def _big_result(**data) -> str:
	"""A tool result well past the inline limit, with record rows to link."""
	rows = [{"name": f"SINV-{i:05d}", "doctype": "Sales Invoice", "note": "x" * 200} for i in range(20)]
	return json.dumps({"ok": True, "data": {"rows": rows, **data}})


class TestHistoryPaging(FrappeTestCase):
	def setUp(self):
		_ensure_user(OWNER)
		_ensure_user(OTHER)
		self._orig_user = frappe.session.user
		frappe.set_user(OWNER)
		self.conv = (
			frappe.get_doc({"doctype": CONV, "title": "Paging test"}).insert(ignore_permissions=True).name
		)
		self.seq = 0

	def tearDown(self):
		frappe.set_user(self._orig_user)

	def _msg(self, role: str, conversation: str | None = None, **kw) -> str:
		self.seq += 1
		row = {"doctype": MSG, "conversation": conversation or self.conv, "seq": self.seq, "role": role}
		row.update(kw)
		doc = frappe.get_doc(row)
		doc.flags.jarvis_server_write = True  # tool rows set server-owned fields
		return doc.insert(ignore_permissions=True).name

	def _turn(self, tools: int = 2) -> list:
		names = [self._msg("user", content="question"), self._msg("assistant", content="answer")]
		names += [
			self._msg("tool", tool_name="get_list", tool_status="completed", tool_result='{"ok": true}')
			for _ in range(tools)
		]
		return names

	def _page(self, **kw) -> dict:
		return chat_api.get_conversation(self.conv, **kw)

	def test_no_limit_keeps_the_whole_thread_unpaged(self):
		for _ in range(5):
			self._turn()
		big = self._msg("tool", tool_name="get_doc", tool_status="completed", tool_result=_big_result())
		out = self._page()
		self.assertEqual(len(out["messages"]), 21)
		self.assertNotIn("paging", out)
		self.assertNotIn("older_pending", out)
		row = next(m for m in out["messages"] if m.name == big)
		self.assertNotIn("tool_result_compact", row)
		self.assertEqual(json.loads(row.tool_result)["data"]["rows"][0]["note"], "x" * 200)

	def test_newest_page_snaps_to_a_turn_start_and_walks_back_without_gaps(self):
		every = []
		for tools in (1, 3, 0, 4, 2, 5, 1, 2):
			every += self._turn(tools)
		pages, before = [], 0
		while True:
			out = self._page(limit=6, before=before) if before else self._page(limit=6)
			rows = out["messages"]
			pages.append([m.name for m in rows])
			self.assertEqual([m.seq for m in rows], sorted(m.seq for m in rows))
			if out["paging"]["has_more"]:
				# Every page but the first of the thread opens on a user row.
				self.assertEqual(rows[0].role, "user")
				self.assertEqual(out["paging"]["before"], rows[0].seq)
				before = out["paging"]["before"]
			else:
				self.assertIsNone(out["paging"]["before"])
				break
		walked = [n for page in reversed(pages) for n in page]
		self.assertEqual(walked, every, "pages cover every row once, in order")
		self.assertGreater(len(pages), 2)

	def test_a_turn_longer_than_the_widening_is_cut_at_the_count(self):
		self._turn(1)
		self._turn(12)
		with patch.object(chat_api, "HISTORY_TURN_WIDEN_MAX", 5):
			out = self._page(limit=4)
		self.assertEqual(len(out["messages"]), 4)
		self.assertTrue(out["paging"]["has_more"])
		self.assertEqual(out["messages"][0].role, "tool")
		# Within the widening, the same long turn loads whole with its reply row.
		whole = self._page(limit=4)
		self.assertEqual(len(whole["messages"]), 14)
		self.assertEqual(whole["messages"][0].role, "user")

	def test_hidden_rows_never_ride_a_page(self):
		self._turn(1)
		hidden = self._msg("user", content="continue", hidden=1)
		self._turn(1)
		out = self._page(limit=50)
		self.assertNotIn(hidden, [m.name for m in out["messages"]])

	def test_an_old_pending_card_rides_beside_the_newest_page_only(self):
		self._turn(0)
		pending = self._msg(
			"tool",
			tool_name="submit_doc",
			tool_status="pending",
			tool_call_id="paging-pending-tok",
			pending_card=json.dumps({"title": "Submit SINV-1"}),
		)
		for _ in range(6):
			self._turn(2)
		out = self._page(limit=4)
		self.assertNotIn(pending, [m.name for m in out["messages"]])
		[row] = out["older_pending"]
		self.assertEqual(row.name, pending)
		self.assertEqual(row.pending_card, {"title": "Submit SINV-1"})
		self.assertIn("recent", row)
		older = self._page(limit=4, before=out["paging"]["before"])
		self.assertEqual(older["older_pending"], [])

	def test_older_pending_skips_expired_cards_and_keeps_only_the_newest(self):
		self._turn(0)
		past = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-1)
		future = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=1)
		expired = self._msg("tool", tool_name="submit_doc", tool_status="pending", expires_at=past)
		live = [
			self._msg("tool", tool_name="submit_doc", tool_status="pending", expires_at=future)
			for _ in range(4)
		]
		no_expiry = self._msg("tool", tool_name="submit_doc", tool_status="pending")
		for _ in range(6):
			self._turn(2)
		names = [m.name for m in self._page(limit=4)["older_pending"]]
		self.assertNotIn(expired, names)
		self.assertEqual(names, [*live, no_expiry], "oldest first, a card without expiry kept")
		with patch.object(chat_api, "HISTORY_OLDER_PENDING_MAX", 3):
			capped = [m.name for m in self._page(limit=4)["older_pending"]]
		self.assertEqual(capped, [live[2], live[3], no_expiry], "the newest ones, oldest first")

	def test_refresh_returns_current_state_and_drops_hidden_or_foreign_rows(self):
		self._turn(0)
		card = self._msg("tool", tool_name="submit_doc", tool_status="pending", tool_call_id="paging-tok-2")
		gone = self._msg("assistant", content="later hidden")
		for _ in range(6):
			self._turn(2)
		frappe.db.set_value(MSG, card, {"tool_status": "completed", "action_outcome": "confirmed"})
		frappe.db.set_value(MSG, gone, "hidden", 1)
		foreign_conv = (
			frappe.get_doc({"doctype": CONV, "title": "Other"}).insert(ignore_permissions=True).name
		)
		foreign = self._msg("tool", conversation=foreign_conv, tool_name="get_list", tool_status="completed")
		out = self._page(limit=4, refresh=json.dumps([card, gone, foreign]))
		self.assertEqual([m.name for m in out["refreshed"]], [card])
		self.assertEqual(out["refreshed"][0].action_outcome, "confirmed")

	def test_large_tool_results_ride_compact_with_what_the_ui_reads(self):
		self._msg("user", content="run the report")
		self._msg("assistant", content="here it is")
		scope = {"version": 1, "report_name": "AP Summary", "company": "Acme", "report_date": "2026-10-12"}
		report = self._msg(
			"tool",
			tool_name="run_report",
			tool_status="completed",
			tool_result=_big_result(result=[{"pad": "y" * 3000}], report_scope=scope, status="ready"),
		)
		failed = self._msg(
			"tool",
			tool_name="get_doc",
			tool_status="error",
			tool_result=json.dumps({"ok": False, "error": {"message": "Not permitted", "hint": "z" * 3000}}),
		)
		receipt = self._msg(
			"tool",
			tool_name="submit_doc",
			tool_status="completed",
			action_outcome="confirmed",
			tool_result=_big_result(),
		)
		small = self._msg(
			"tool", tool_name="query", tool_status="completed", tool_result='{"ok": true, "data": [1]}'
		)
		rows = {m.name: m for m in self._page(limit=20)["messages"]}

		compact = json.loads(rows[report].tool_result)
		self.assertEqual(rows[report].tool_result_compact, 1)
		self.assertTrue(compact["ok"])
		self.assertEqual(compact["data"]["report_scope"], scope)
		self.assertEqual(compact["data"]["status"], "ready")
		self.assertEqual(compact["data"]["result"], [])
		self.assertEqual(compact["data"]["rows"][0], {"name": "SINV-00000", "doctype": "Sales Invoice"})
		self.assertEqual(json.loads(rows[failed].tool_result)["error"]["message"], "Not permitted")
		self.assertNotIn("tool_result_compact", rows[receipt])
		self.assertNotIn("tool_result_compact", rows[small])

		detail = chat_api.get_tool_detail(report)
		self.assertEqual(json.loads(detail["tool_result"])["data"]["result"], [{"pad": "y" * 3000}])

	def test_tool_detail_and_pages_use_the_owner_gate(self):
		self._turn(1)
		tool = self._msg("tool", tool_name="get_doc", tool_status="completed", tool_result=_big_result())
		hidden = self._msg("tool", tool_name="get_doc", tool_status="completed", hidden=1)
		user_row = self._msg("user", content="hi")
		with self.assertRaises(frappe.DoesNotExistError):
			chat_api.get_tool_detail(hidden)
		with self.assertRaises(frappe.DoesNotExistError):
			chat_api.get_tool_detail(user_row)
		frappe.set_user(OTHER)
		with self.assertRaises(frappe.PermissionError):
			chat_api.get_tool_detail(tool)
		with self.assertRaises(frappe.PermissionError):
			chat_api.get_conversation(self.conv, limit=10)
