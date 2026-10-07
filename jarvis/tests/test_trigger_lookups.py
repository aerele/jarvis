"""Unit tests for jarvis.triggers.lookups - the read-only lookup loop LLM
triggers can use (jarvis-admin-v2#608, step 1).

No DB, no network: the DocType meta, the User check, the session user and the
chat's own list/get tools are stubbed, so these run without a site. The
end-to-end run through ``run_llm_action`` lives in test_triggers_engine.py.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

import frappe

from jarvis.exceptions import PermissionDeniedError
from jarvis.triggers import lookups

OWNER = "owner@example.com"
GET_LIST = "jarvis.tools.get_list.get_list"
GET_DOC = "jarvis.tools.get_doc.get_doc"


def _df(fieldname, fieldtype="Data", permlevel=0, in_list_view=0, options=None):
	return frappe._dict(
		fieldname=fieldname,
		fieldtype=fieldtype,
		permlevel=permlevel,
		in_list_view=in_list_view,
		options=options,
	)


class _Meta:
	def __init__(self, name, fields, istable=0, issingle=0, module="Accounts", title_field=None):
		self.name = name
		self.fields = fields
		self.istable = istable
		self.issingle = issingle
		self.module = module
		self.title_field = title_field
		self.is_virtual = 0

	def get_field(self, fieldname):
		return next((df for df in self.fields if df.fieldname == fieldname), None)


def _invoice_meta():
	return _Meta(
		"Sales Invoice",
		[
			_df("customer", "Link", in_list_view=1, options="Customer"),
			_df("status", "Select", in_list_view=1),
			_df("grand_total", "Currency", in_list_view=1),
			_df("due_date", "Date"),
			_df("internal_cost", "Currency", permlevel=2),
			_df("api_key", "Data"),
			_df("items", "Table", options="Sales Invoice Item"),
		],
		title_field="customer",
	)


class _LookupCase(unittest.TestCase):
	def setUp(self):
		self.metas = {
			"Sales Invoice": _invoice_meta(),
			"Sales Invoice Item": _Meta("Sales Invoice Item", [_df("item_code")], istable=1),
			"Stock Settings": _Meta("Stock Settings", [_df("default_warehouse")], issingle=1),
			"Vault Entry": _Meta("Vault Entry", [_df("note"), _df("secret", "Password")]),
			"Jarvis Thing": _Meta("Jarvis Thing", [_df("title")], module="Jarvis"),
			"User": _Meta("User", [_df("email")]),
			"Server Script": _Meta("Server Script", [_df("script", "Code")]),
		}
		self.user = frappe._dict(user="Administrator")
		self.user_calls = []

		def set_user(user):
			self.user_calls.append(user)
			self.user.user = user

		self.enabled = 1
		db = MagicMock()
		db.exists.side_effect = (
			lambda dt, name=None: True if dt == "DocType" and name in self.metas else False
		)
		db.get_value.side_effect = lambda *a, **k: self.enabled
		for p in (
			patch("frappe.db", db),
			patch("frappe.get_meta", side_effect=lambda dt: self.metas[dt]),
			patch("frappe.session", self.user),
			patch("frappe.set_user", side_effect=set_user),
			patch("frappe.log_error"),
		):
			p.start()
			self.addCleanup(p.stop)

	def lookup(self, tool, **args):
		return lookups.execute_lookup(OWNER, {"tool": tool, "args": args})


class TestParseReply(unittest.TestCase):
	def test_finding_object_and_text_forms(self):
		self.assertEqual(lookups.parse_reply({"finding": " ok "})[:2], ("finding", "ok"))
		self.assertEqual(lookups.parse_reply('{"finding": "ok"}')[:2], ("finding", "ok"))
		self.assertEqual(lookups.parse_reply("plain prose")[:2], ("finding", "plain prose"))

	def test_lookup_object_and_json_text(self):
		req = {"tool": "list", "args": {"doctype": "ToDo"}}
		self.assertEqual(lookups.parse_reply({"lookup": req})[:2], ("lookup", req))
		self.assertEqual(lookups.parse_reply(json.dumps({"lookup": req}))[:2], ("lookup", req))

	def test_everything_else_is_the_finding(self):
		for reply in (
			{"finding": "a", "lookup": {"tool": "list", "args": {}}},
			{"lookup": {"tool": "report", "args": {}}},
			{"lookup": {"tool": "list", "args": "x"}},
			{"lookup": "list"},
			{"finding": 3},
			'{"lookup": {"tool": "list", "args": {}}',
			"{not json",
		):
			with self.subTest(reply=reply):
				kind, payload, text = lookups.parse_reply(reply)
				self.assertEqual(kind, "finding")
				self.assertEqual(payload, text)


class TestExecuteList(_LookupCase):
	def test_runs_as_owner_with_defaults_and_restores_user(self):
		seen = {}

		def fake_get_list(doctype, **kwargs):
			seen["user"] = frappe.session.user
			seen["call"] = (doctype, kwargs)
			return [{"name": "SINV-1", "customer": "C1"}]

		with patch(GET_LIST, side_effect=fake_get_list):
			result, line = self.lookup(
				"list",
				doctype="Sales Invoice",
				filters={"customer": "C1", "status": ["in", ["Overdue", "Unpaid"]]},
				order_by="due_date desc",
				limit=500,
			)
		self.assertEqual(result["count"], 1)
		self.assertEqual(seen["user"], OWNER)
		self.assertEqual(self.user_calls, [OWNER, "Administrator"])
		self.assertEqual(self.user.user, "Administrator")
		doctype, kwargs = seen["call"]
		self.assertEqual(doctype, "Sales Invoice")
		self.assertEqual(kwargs["limit"], lookups.MAX_ROWS)
		self.assertEqual(kwargs["order_by"], "due_date desc")
		self.assertEqual(
			kwargs["filters"],
			[["customer", "=", "C1"], ["status", "in", ["Overdue", "Unpaid"]]],
		)
		# name + title field + list-view fields; never a permlevel>0 or secret-named field.
		self.assertEqual(kwargs["fields"], ["name", "customer", "status", "grand_total"])
		self.assertEqual(line, "list Sales Invoice: 1 row")
		self.assertNotIn("SINV-1", line)

	def test_user_restored_after_exception_inside_lookup(self):
		with patch(GET_LIST, side_effect=RuntimeError("boom")):
			result, line = self.lookup("list", doctype="Sales Invoice")
		self.assertEqual(result, {"error": "lookup failed"})
		self.assertIn("refused: lookup failed", line)
		self.assertEqual(self.user_calls, [OWNER, "Administrator"])
		self.assertEqual(self.user.user, "Administrator")

	def test_permission_error_is_a_result_not_an_exception(self):
		with patch(GET_LIST, side_effect=PermissionDeniedError("no read permission on Sales Invoice")):
			result, line = self.lookup("list", doctype="Sales Invoice")
		self.assertEqual(result, {"error": "no read permission on Sales Invoice"})
		self.assertEqual(self.user.user, "Administrator")

	def test_rows_are_capped_by_size_with_a_marker(self):
		rows = [{"name": f"SINV-{i}", "customer": "x" * 1000} for i in range(20)]
		with patch(GET_LIST, return_value=rows):
			result, _line = self.lookup("list", doctype="Sales Invoice")
		self.assertTrue(result["truncated"])
		self.assertLess(len(result["rows"]), 20)
		self.assertLessEqual(len(json.dumps(result["rows"])), lookups.MAX_RESULT_CHARS)

	def test_refusals_never_reach_the_read(self):
		cases = {
			"denylisted doctype": dict(doctype="User"),
			"server script": dict(doctype="Server Script"),
			"jarvis module doctype": dict(doctype="Jarvis Thing"),
			"password field doctype": dict(doctype="Vault Entry"),
			"child table": dict(doctype="Sales Invoice Item"),
			"single": dict(doctype="Stock Settings"),
			"unknown doctype": dict(doctype="Nope"),
			"permlevel 2 field": dict(doctype="Sales Invoice", fields=["internal_cost"]),
			"secret-named field": dict(doctype="Sales Invoice", fields=["api_key"]),
			"table field": dict(doctype="Sales Invoice", fields=["items"]),
			"sql in fields": dict(doctype="Sales Invoice", fields=["name, (select 1)"]),
			"sql as filter field": dict(doctype="Sales Invoice", filters={"1=1) or (1": "x"}),
			"permlevel filter field": dict(doctype="Sales Invoice", filters={"internal_cost": 1}),
			"bad operator": dict(doctype="Sales Invoice", filters=[["status", "regexp", "x"]]),
			"sql operator": dict(doctype="Sales Invoice", filters=[["status", "= 1 or 1=1 --", "x"]]),
			"subquery value": dict(doctype="Sales Invoice", filters={"status": {"$sub": "select 1"}}),
			"four part filter": dict(
				doctype="Sales Invoice", filters=[["Sales Invoice", "status", "=", "x"]]
			),
			"raw sql order": dict(doctype="Sales Invoice", order_by="name; select sleep(5)"),
			"permlevel order": dict(doctype="Sales Invoice", order_by="internal_cost desc"),
		}
		for label, args in cases.items():
			with self.subTest(label), patch(GET_LIST) as read:
				result, line = self.lookup("list", **args)
				self.assertIn("error", result)
				self.assertIn("refused", line)
				read.assert_not_called()
		self.assertEqual(self.user.user, "Administrator")

	def test_other_tools_are_refused(self):
		for tool in ("report", "query", "run_method", "delete_doc", None):
			with self.subTest(tool), patch(GET_LIST) as read, patch(GET_DOC) as read_doc:
				result, _line = self.lookup(tool, doctype="Sales Invoice")
				self.assertEqual(result, {"error": "lookup tool not allowed"})
				read.assert_not_called()
				read_doc.assert_not_called()

	def test_missing_disabled_or_guest_owner_is_refused(self):
		with patch(GET_LIST) as read:
			self.enabled = None
			self.assertIn("error", self.lookup("list", doctype="Sales Invoice")[0])
			self.enabled = 0
			self.assertIn("error", self.lookup("list", doctype="Sales Invoice")[0])
			self.enabled = 1
			result, _line = lookups.execute_lookup(
				"Guest", {"tool": "list", "args": {"doctype": "Sales Invoice"}}
			)
			self.assertIn("error", result)
			result, _line = lookups.execute_lookup("", {"tool": "list", "args": {"doctype": "Sales Invoice"}})
			self.assertIn("error", result)
			read.assert_not_called()
		self.assertEqual(self.user_calls, [])


class TestExecuteGet(_LookupCase):
	def test_returns_permlevel_0_fields_only_without_secrets(self):
		doc = {
			"name": "SINV-1",
			"customer": "C1",
			"internal_cost": 99,
			"api_key": "k",
			"_user_tags": ",x",
			"items": [{"item_code": "A", "idx": 1}],
		}
		with patch(GET_DOC, return_value=doc) as read:
			result, line = self.lookup("get", doctype="Sales Invoice", name="SINV-1")
		read.assert_called_once_with("Sales Invoice", name="SINV-1")
		self.assertEqual(result["doc"]["customer"], "C1")
		for hidden in ("internal_cost", "api_key", "_user_tags"):
			self.assertNotIn(hidden, result["doc"])
		self.assertEqual(line, "get Sales Invoice: 1 record")

	def test_missing_and_unreadable_look_the_same(self):
		for exc in (frappe.DoesNotExistError("x"), PermissionDeniedError("no read permission")):
			with self.subTest(exc=type(exc).__name__), patch(GET_DOC, side_effect=exc):
				result, _line = self.lookup("get", doctype="Sales Invoice", name="SINV-9")
				self.assertEqual(result, {"error": "no such record, or no read permission"})

	def test_get_refuses_denylisted_doctype_and_bad_name(self):
		with patch(GET_DOC) as read:
			self.assertIn("error", self.lookup("get", doctype="User", name="Administrator")[0])
			self.assertIn("error", self.lookup("get", doctype="Sales Invoice")[0])
			self.assertIn("error", self.lookup("get", doctype="Sales Invoice", name="x" * 200)[0])
			read.assert_not_called()


class TestRunLoop(unittest.TestCase):
	"""The bounded loop, with a scripted model and a stubbed lookup."""

	def run_loop(self, replies, clock=None, results=None):
		calls = []
		queue = list(replies)

		def complete(prompt, input_text, messages, timeout):
			calls.append(frappe._dict(prompt=prompt, input=input_text, messages=messages, timeout=timeout))
			reply = queue.pop(0)
			return (None, str(reply)) if isinstance(reply, Exception) else (reply, None)

		executed = []

		def execute(owner, request, budget=0):
			executed.append((owner, request))
			return {"rows": [{"name": "SINV-1"}], "count": 1}, "list Sales Invoice: 1 row"

		kwargs = {"clock": clock} if clock else {}
		with patch.object(lookups, "execute_lookup", side_effect=execute):
			out = lookups.run_loop(
				owner=OWNER,
				system_prompt="SYS",
				instruction="check it",
				doc_label="Sales Invoice X, event: on_submit",
				snapshot_fenced="<untrusted-data>snap</untrusted-data>",
				build_prompt=lambda contract: f"PROMPT {contract}",
				complete=complete,
				fence=lambda text, source: f"<fence {source}>{text}</fence>",
				**kwargs,
			)
		return out, calls, executed

	LOOKUP = {"lookup": {"tool": "list", "args": {"doctype": "Sales Invoice"}}}

	def test_lookup_then_finding(self):
		(finding, error, log, rounds), calls, executed = self.run_loop(
			[self.LOOKUP, {"finding": "Two overdue."}]
		)
		self.assertEqual((finding, error, rounds), ("Two overdue.", None, 2))
		self.assertEqual(log, ["1. list Sales Invoice: 1 row"])
		self.assertEqual(executed, [(OWNER, self.LOOKUP["lookup"])])
		self.assertNotIn("lookup 1 result", calls[0].input)
		self.assertIn("<fence lookup 1 result>", calls[1].input)
		self.assertIn("SINV-1", calls[1].input)
		self.assertIn("<untrusted-data>snap", calls[1].input)
		self.assertEqual(calls[0].timeout, lookups.ROUND_TIMEOUT_S)
		self.assertIn("at most 3 more", calls[0].messages[0]["content"])

	def test_fourth_request_is_the_finding_and_forced_final_prompt_goes_out(self):
		replies = [self.LOOKUP, self.LOOKUP, self.LOOKUP, self.LOOKUP]
		(finding, error, log, rounds), calls, executed = self.run_loop(replies)
		self.assertIsNone(error)
		self.assertEqual(len(executed), 3)
		self.assertEqual(rounds, 4)
		self.assertEqual(len(log), 3)
		self.assertIn("No more lookups are allowed", calls[3].prompt)
		self.assertNotIn("No more lookups", calls[2].prompt)
		self.assertIn('"lookup"', finding)

	def test_malformed_reply_is_the_finding(self):
		for reply in ("Looks fine to me.", {"finding": "a", "lookup": self.LOOKUP["lookup"]}):
			with self.subTest(reply=reply):
				(finding, error, _log, rounds), _calls, executed = self.run_loop([reply])
				self.assertIsNone(error)
				self.assertEqual(rounds, 1)
				self.assertEqual(executed, [])
				self.assertTrue(finding)

	def test_transport_error_ends_the_run_with_the_error(self):
		(finding, error, log, _rounds), _calls, _executed = self.run_loop([self.LOOKUP, RuntimeError("down")])
		self.assertIsNone(finding)
		self.assertEqual(error, "down")
		self.assertEqual(len(log), 1)

	def test_out_of_time_is_an_error_not_an_exception(self):
		ticks = iter([0, 0, 200])
		(finding, error, log, rounds), calls, _executed = self.run_loop(
			[self.LOOKUP, {"finding": "never asked"}], clock=lambda: next(ticks)
		)
		self.assertIsNone(finding)
		self.assertEqual(error, lookups.TIMEOUT_MESSAGE)
		self.assertEqual((len(log), rounds, len(calls)), (1, 1, 1))

	def test_under_20_seconds_left_forces_the_final_round_with_a_short_timeout(self):
		ticks = iter([0, 0, 135])
		(finding, error, _log, _rounds), calls, executed = self.run_loop(
			[self.LOOKUP, self.LOOKUP], clock=lambda: next(ticks)
		)
		self.assertIsNone(error)
		self.assertEqual(len(executed), 1)
		self.assertEqual(calls[1].timeout, 15)
		self.assertIn("No more lookups are allowed", calls[1].prompt)
		self.assertIn('"lookup"', finding)
