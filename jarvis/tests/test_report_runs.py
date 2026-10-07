"""The chat's background-report card and the Approval Board's "Reports ready in
your chats" list (jarvis.chat.report_runs), read from a chat's run_report rows."""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, formatdate, now

from jarvis.chat.approvals_api import list_approvals_page
from jarvis.chat.report_runs import _filters_label, _open_runs, chat_report_runs, ready_reports
from jarvis.tests.test_chat_asks import _ensure_user
from jarvis.tests.test_run_report import _ENQUEUE, PREP_REPORT, _ensure_report

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
OTHER = "rr-other@example.com"
BOARD = "rr-board@example.com"


def _conv(owner="Administrator", title="rr chat", status="Active") -> str:
	doc = frappe.get_doc({"doctype": CONV, "title": title, "status": status}).insert(ignore_permissions=True)
	frappe.db.set_value(CONV, doc.name, "owner", owner, update_modified=False)
	return doc.name


def _pr(status, owner="Administrator", ended=None) -> str:
	with patch(_ENQUEUE):
		pr = frappe.get_doc(
			{"doctype": "Prepared Report", "report_name": PREP_REPORT, "filters": "{}"}
		).insert(ignore_permissions=True)
	values = {"status": status, "owner": owner}
	if status == "Completed":
		values["report_end_time"] = ended or now()
	frappe.db.set_value("Prepared Report", pr.name, values, update_modified=False)
	return pr.name


def _tool(conv, seq, status, run, filters=None, created=None) -> None:
	msg = frappe.get_doc(
		{
			"doctype": MSG,
			"conversation": conv,
			"seq": seq,
			"role": "tool",
			"tool_name": "run_report",
			"tool_status": "completed",
			"tool_args": json.dumps({"report_name": PREP_REPORT, "filters": filters or {"company": "Acme"}}),
			"tool_result": json.dumps(
				{
					"ok": True,
					"data": {
						"prepared_report": True,
						"status": status,
						"report_name": PREP_REPORT,
						"run": run,
					},
				}
			),
		}
	)
	msg.flags.jarvis_server_write = True
	msg.insert(ignore_permissions=True)
	if created:
		frappe.db.set_value(MSG, msg.name, "creation", created, update_modified=False)


def _states(conv):
	return [(i["run"], i["status"]) for i in chat_report_runs(conv)["items"]]


class TestChatReportCard(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_user(OTHER)  # commits: before any fixture of this class

	def setUp(self):
		frappe.set_user("Administrator")
		_ensure_report(PREP_REPORT, prepared=1)
		frappe.db.delete("Prepared Report", {"report_name": PREP_REPORT})
		self.conv = _conv()

	def test_a_started_run_is_preparing_then_ready(self):
		run = _pr("Started")
		_tool(self.conv, 1, "started", run)
		[item] = chat_report_runs(self.conv)["items"]
		self.assertEqual(
			(item["run"], item["status"], item["report_name"], item["filters"], item["ready_at"]),
			(run, "preparing", PREP_REPORT, "Acme", ""),
		)
		frappe.db.set_value("Prepared Report", run, {"status": "Completed", "report_end_time": now()})
		[item] = chat_report_runs(self.conv)["items"]
		self.assertEqual(item["status"], "ready")
		self.assertTrue(item["ready_at"])

	def test_a_failed_run_says_so(self):
		run = _pr("Error")
		_tool(self.conv, 1, "started", run)
		self.assertEqual(_states(self.conv), [(run, "failed")])

	def test_a_desk_run_found_still_generating_gets_a_card(self):
		run = _pr("Queued")
		_tool(self.conv, 1, "generating", run)
		self.assertEqual(_states(self.conv), [(run, "preparing")])

	def test_showing_the_results_closes_the_card(self):
		run = _pr("Completed")
		_tool(self.conv, 1, "started", run)
		_tool(self.conv, 2, "ready", run)
		self.assertEqual(_states(self.conv), [])

	def test_results_of_an_equal_newer_run_close_it_too(self):
		run, newer = _pr("Completed"), _pr("Completed")
		_tool(self.conv, 1, "started", run, filters={"company": "Acme", "skip": 0})
		_tool(self.conv, 2, "ready", newer, filters={"company": "Acme"})
		self.assertEqual(_states(self.conv), [])

	def test_results_for_other_filters_leave_it_open(self):
		run, other = _pr("Completed"), _pr("Completed")
		_tool(self.conv, 1, "started", run)
		_tool(self.conv, 2, "ready", other, filters={"company": "Other"})
		self.assertEqual(_states(self.conv), [(run, "ready")])

	def test_cancelled_deleted_or_foreign_runs_are_dropped(self):
		cancelled, foreign = _pr("Cancelled"), _pr("Completed", owner=OTHER)
		_tool(self.conv, 1, "started", cancelled)
		_tool(self.conv, 2, "started", "no-such-run")
		_tool(self.conv, 3, "started", foreign, filters={"company": "Elsewhere"})
		self.assertEqual(_states(self.conv), [])

	def test_an_unexpected_result_is_skipped(self):
		run = _pr("Started")
		_tool(self.conv, 1, "started", run)
		frappe.db.set_value(
			MSG, {"conversation": self.conv, "seq": 1}, "tool_result", '["not", "an", "object"]'
		)
		self.assertEqual(_states(self.conv), [])

	def test_only_the_owner_reads_a_chats_cards(self):
		frappe.set_user(OTHER)
		try:
			with self.assertRaises(frappe.PermissionError):
				chat_report_runs(self.conv)
		finally:
			frappe.set_user("Administrator")

	def test_results_close_only_their_own_chats_card(self):
		def row(conv, status, run):
			return frappe._dict(
				conversation=conv,
				tool_args=json.dumps({"report_name": PREP_REPORT, "filters": {"company": "Acme"}}),
				tool_result=json.dumps({"data": {"status": status, "run": run, "report_name": PREP_REPORT}}),
			)

		runs = _open_runs([row("chat-a", "started", "PR-X"), row("chat-b", "ready", "PR-X")])
		self.assertEqual([(r["conversation"], r["run"]) for r in runs], [("chat-a", "PR-X")])

	def test_the_filters_read_as_values(self):
		self.assertEqual(
			_filters_label({"company": "Acme", "from_date": "2026-09-01", "item_code": [], "flag": 0}),
			f"Acme · {formatdate('2026-09-01')}",
		)
		self.assertEqual(_filters_label({"item_code": ["A", "B"]}), "A, B")
		self.assertEqual(_filters_label("not a dict"), "")


class TestReadyReportsOnTheBoard(FrappeTestCase):
	"""As BOARD, a user with no other chats, so the list holds only these fixtures."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_user(BOARD)
		_ensure_user(OTHER)

	def setUp(self):
		frappe.set_user("Administrator")
		_ensure_report(PREP_REPORT, prepared=1)
		frappe.db.delete("Prepared Report", {"report_name": PREP_REPORT})

	def test_lists_ready_runs_not_yet_shown_newest_first(self):
		a, b = _conv(BOARD, "rr first"), _conv(BOARD, "rr second")
		older = _pr("Completed", BOARD, ended=add_days(now(), -1))
		newer = _pr("Completed", BOARD)
		preparing, shown = _pr("Started", BOARD), _pr("Completed", BOARD)
		_tool(a, 1, "started", older)
		_tool(b, 1, "started", newer, filters={"company": "Newer"})
		_tool(b, 2, "started", preparing, filters={"company": "Still"})
		_tool(a, 2, "started", shown, filters={"company": "Shown"})
		_tool(a, 3, "ready", shown, filters={"company": "Shown"})
		rows = ready_reports(BOARD)
		self.assertEqual([(r["run"], r["conversation"]) for r in rows], [(newer, b), (older, a)])
		self.assertEqual(
			set(rows[0]),
			{"conversation", "title", "origin_page", "run", "report_name", "filters", "status", "ready_at"},
		)
		self.assertEqual(rows[0]["title"], "rr second")

	def test_results_shown_in_one_chat_leave_anothers_card(self):
		a, b = _conv(BOARD), _conv(BOARD)
		run = _pr("Completed", BOARD)
		_tool(a, 1, "started", run)
		_tool(b, 1, "generating", run)
		_tool(b, 2, "ready", run)
		self.assertEqual([(r["run"], r["conversation"]) for r in ready_reports(BOARD)], [(run, a)])

	def test_skips_old_archived_and_other_owners_chats(self):
		old, archived, theirs = _conv(BOARD), _conv(BOARD, status="Archived"), _conv(OTHER)
		_tool(old, 1, "started", _pr("Completed", BOARD), created=add_days(now(), -30))
		_tool(archived, 1, "started", _pr("Completed", BOARD), filters={"company": "Archived"})
		_tool(theirs, 1, "started", _pr("Completed", owner=OTHER), filters={"company": "Theirs"})
		self.assertEqual(ready_reports(BOARD), [])

	def test_capped(self):
		conv = _conv(BOARD)
		for i in range(12):
			_tool(conv, i + 1, "started", _pr("Completed", BOARD), filters={"company": f"Co {i}"})
		self.assertEqual(len(ready_reports(BOARD)), 10)

	def test_on_the_first_page_of_the_board_only(self):
		conv = _conv(BOARD)
		run = _pr("Completed", BOARD)
		_tool(conv, 1, "started", run)
		frappe.set_user(BOARD)
		try:
			self.assertEqual([r["run"] for r in list_approvals_page()["ready_reports"]], [run])
			self.assertNotIn("ready_reports", list_approvals_page(start=20))
		finally:
			frappe.set_user("Administrator")
