"""What a confirmation card says (round 2, J1-cards; replaces J3).

- submit / cancel / amend / delete park a DESCRIBED card (R2-2): nothing runs at
  park, no trial in the sandbox, and the card names the consequence;
- a create / update trial that found background jobs or a rolled-back hook puts
  ONE warning on the card, with the real job count (R2-6);
- sensitive configuration (R2-8 / R2-12) is shown in full: no clip, no row cap,
  passwords as "set" / "changed", invisible characters made visible, a line diff
  on update, and a card too large to show is refused, never clipped.

Real documents through ``api._run_tool``; a mock only where a test proves a
negative (the sandbox is never entered).
"""

import json
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import pending_confirm
from jarvis.chat.confirm_card import build_card
from jarvis.tests._conv_helpers import CONV, _make_conv
from jarvis.tests.test_chat_api import TEST_USER, _ensure_test_user

PENDING = "Jarvis Pending Action"
MSG = "Jarvis Chat Message"
PREFIX = "jarvis-cr-"
THIS = "jarvis.tests.test_card_risk"
PING = "frappe.ping"
SENSITIVE = {"risk": "sensitive", "risk_line": "This runs code for every user."}


def _hook_enqueues_500(doc, method=None):
	for _ in range(500):
		frappe.enqueue(PING)


@contextmanager
def _doc_event(doctype, event, handler):
	hooks = frappe.get_doc_hooks()
	with patch.dict(hooks, {doctype: {**hooks.get(doctype, {}), event: [f"{THIS}.{handler}"]}}):
		yield


def _script(lines: int = 60) -> str:
	return "\n".join(f"// line {i} " + "x" * 30 for i in range(lines))


def _client_script(name: str, script: str) -> dict:
	return {"name": name, "dt": "ToDo", "view": "Form", "enabled": 0, "script": script}


def _drop_client_scripts() -> None:
	for name in frappe.get_all("Client Script", {"name": ["like", PREFIX + "%"]}, pluck="name"):
		frappe.delete_doc("Client Script", name, force=True, ignore_permissions=True)


def _todo(description: str):
	return frappe.get_doc({"doctype": "ToDo", "description": description}).insert(ignore_permissions=True)


class _Base(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_test_user()

	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user(TEST_USER)
		self.minted = []

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		_drop_client_scripts()
		for conv in frappe.get_all(CONV, filters={"owner": TEST_USER, "title": "conv test"}, pluck="name"):
			pending_confirm.clear_for_conversation(TEST_USER, conv)
			frappe.db.delete(PENDING, {"conversation": conv})
			frappe.db.delete(MSG, {"conversation": conv})
			frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		frappe.db.delete("ToDo", {"description": ["like", PREFIX + "%"]})
		frappe.db.commit()
		frappe.set_user(self._orig)

	def _conv(self, **flags) -> str:
		conv = _make_conv(TEST_USER)
		if flags:
			frappe.db.set_value(CONV, conv, flags, update_modified=False)
			frappe.db.commit()
		return conv

	def _park(self, tool, args, conv=None) -> dict:
		"""Park through the gate; return the model-facing result and keep the stored
		preview (with its card) the mint received."""
		real = pending_confirm.mint

		def spy(**kwargs):
			self.minted.append(kwargs)
			return real(**kwargs)

		with (
			patch("jarvis.chat.pending_confirm.mint", side_effect=spy),
			patch("jarvis.chat.events.publish_to_user"),
		):
			return api._run_tool(tool, args, conversation=conv)

	def _card(self) -> dict:
		return self.minted[-1]["preview"]["card"]


# --------------------------------------------------------------------------- #
# R2-2: verbs are described, never trial-run
# --------------------------------------------------------------------------- #
class TestVerbsDescribed(_Base):
	CONSEQUENCE = {
		"submit_doc": "Submitting posts its accounting and stock entries, if any, and locks the document.",
		"cancel_doc": "Cancelling reverses its entries.",
		"delete_doc": "Deleting removes it permanently.",
		"amend_doc": "Amending creates a new draft from the cancelled document.",
	}

	def test_each_verb_parks_described_and_never_enters_the_sandbox(self):
		todo = _todo(PREFIX + "verb")
		for tool, line in self.CONSEQUENCE.items():
			with (
				patch(
					"jarvis.tools._preview_sandbox.preview_sandbox",
					side_effect=AssertionError("the sandbox ran at park"),
				),
				patch("jarvis.api.dispatch", side_effect=AssertionError("dispatched at park")),
			):
				r = self._park(tool, {"doctype": "ToDo", "name": todo.name})
			self.assertEqual(r["data"]["status"], "pending_confirmation", (tool, r))
			preview = self.minted[-1]["preview"]
			self.assertIs(preview["described"], True, tool)
			self.assertFalse(preview["preview"], tool)
			self.assertNotIn("would", preview, tool)
			self.assertIn("Nothing has run yet", preview["note"], tool)
			card = self._card()
			self.assertEqual((card["kind"], card["consequence"]), ("verb", line), tool)
		self.assertTrue(frappe.db.exists("ToDo", todo.name), "the parked delete removed nothing")

	def test_a_bulk_verb_speaks_of_each_document(self):
		a, b = _todo(PREFIX + "a"), _todo(PREFIX + "b")
		with patch("jarvis.tools._preview_sandbox.preview_sandbox", side_effect=AssertionError("sandbox")):
			self._park("cancel_doc", {"doctype": "ToDo", "names": [a.name, b.name]})
		self.assertEqual(self._card()["consequence"], "Cancelling reverses each document's entries.")
		with patch("jarvis.tools._preview_sandbox.preview_sandbox", side_effect=AssertionError("sandbox")):
			self._park("delete_doc", {"doctype": "ToDo", "names": [a.name, b.name]})
		self.assertEqual(self._card()["consequence"], "Deleting removes them permanently.")

	def test_a_workflow_action_has_no_consequence_line(self):
		card = build_card("apply_workflow_action", {"doctype": "ToDo", "name": "x", "action": "Approve"}, {})
		self.assertNotIn("consequence", card)

	def test_a_parked_delete_confirms_and_runs_once(self):
		from jarvis.chat.actions_api import confirm_tool

		todo = _todo(PREFIX + "gone")
		conv = self._conv()
		published = []
		with patch("jarvis.chat.events.publish_to_user", side_effect=lambda u, p: published.append(p)):
			r = api._run_tool("delete_doc", {"doctype": "ToDo", "name": todo.name}, conversation=conv)
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		self.assertTrue(frappe.db.exists("ToDo", todo.name))
		with patch("jarvis.chat.api._dispatch_turn"):
			res = confirm_tool(published[-1]["token"], conversation=conv)
		self.assertTrue(res["ok"], res)
		self.assertFalse(frappe.db.exists("ToDo", todo.name))


# --------------------------------------------------------------------------- #
# R2-6: one warning line, only when the trial found something
# --------------------------------------------------------------------------- #
class TestCardWarning(_Base):
	def test_the_real_job_count_rides_the_card(self):
		todo = _todo(PREFIX + "jobs")
		args = {"doctype": "ToDo", "name": todo.name, "changes": {"description": PREFIX + "jobs-2"}}
		with _doc_event("ToDo", "on_update", "_hook_enqueues_500"):
			r = self._park("update_doc", args)
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		preview = self.minted[-1]["preview"]
		self.assertEqual(preview["will_queue_jobs"], 500)
		self.assertEqual(len(preview["will_queue"]), 1, "names are deduplicated; the count is not")
		self.assertEqual(self._card()["warning"], {"jobs": 500, "rolled_back": False})

	def test_a_clean_trial_puts_no_warning_on_the_card(self):
		todo = _todo(PREFIX + "clean")
		self._park("update_doc", {"doctype": "ToDo", "name": todo.name, "changes": {"priority": "High"}})
		self.assertNotIn("will_queue_jobs", self.minted[-1]["preview"])
		self.assertNotIn("warning", self._card())

	def test_build_card_reads_the_trial_keys(self):
		args = {"doctype": "ToDo", "values": {"description": "x"}}
		would = {"name": "T-1", "description": "x"}
		cases = [
			({"will_queue_jobs": 3}, {"jobs": 3, "rolled_back": False}),
			({"hook_rolled_back": True}, {"jobs": 0, "rolled_back": True}),
			({"will_queue_jobs": 2, "hook_rolled_back": True}, {"jobs": 2, "rolled_back": True}),
			({}, None),
			({"will_queue_jobs": 0, "hook_rolled_back": False}, None),
			({"will_queue_jobs": "many"}, None),
			({"will_queue_jobs": True}, None),
		]
		for extra, expected in cases:
			card = build_card("create_doc", args, {"would": would, **extra})
			self.assertEqual(card.get("warning"), expected, extra)

	def test_every_trial_kind_carries_it(self):
		trial = {"will_queue_jobs": 4}
		batch = build_card(
			"create_doc",
			{"docs": [{"doctype": "ToDo", "values": {"description": "a"}}]},
			{"would": {"created": [{"doctype": "ToDo", "name": "T-1"}]}, **trial},
		)
		todo = _todo(PREFIX + "bulk")
		bulk = build_card(
			"update_doc",
			{"doctype": "ToDo", "updates": [{"name": todo.name, "changes": {"priority": "High"}}]},
			trial,
		)
		update = build_card(
			"update_doc", {"doctype": "ToDo", "name": todo.name, "changes": {"priority": "High"}}, trial
		)
		for card in (batch, bulk, update):
			self.assertEqual(card["warning"], {"jobs": 4, "rolled_back": False}, card["kind"])


# --------------------------------------------------------------------------- #
# R2-8: sensitive configuration in full
# --------------------------------------------------------------------------- #
class TestSensitiveFullCard(_Base):
	def _row(self, card, label):
		return next(r for r in card["rows"] if r["label"] == label)

	def test_a_parked_client_script_shows_the_whole_script_and_its_risk(self):
		conv = self._conv(auto_mode=1)
		script = _script(80)
		self.assertGreater(len(script), 2000)
		r = self._park(
			"create_doc",
			{"doctype": "Client Script", "values": _client_script(PREFIX + "full", script)},
			conv,
		)
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		card = json.loads(frappe.db.get_value(PENDING, {"conversation": conv}, "card"))
		self.assertEqual(card["risk"], "sensitive")
		self.assertEqual(card["risk_line"], "This runs code for every user.")
		row = self._row(card, "Script")
		self.assertEqual(row["value"], script, "no clip")
		self.assertIs(row["multiline"], True)

	def test_every_field_shows_with_no_row_cap(self):
		values = {f"field_{i:02d}": f"value {i}" for i in range(30)}
		card = build_card(
			"create_doc", {"doctype": "Webhook", "values": values}, {"would": values, **SENSITIVE}
		)
		self.assertEqual(len(card["rows"]), 30)
		ordinary = build_card("create_doc", {"doctype": "Webhook", "values": values}, {"would": values})
		self.assertEqual(len(ordinary["rows"]), 20, "an ordinary card keeps its cap")

	def test_a_password_shows_as_set_and_never_its_value(self):
		values = {"email_id": "cr@example.com", "password": "hunter2-secret"}
		card = build_card(
			"create_doc", {"doctype": "Email Account", "values": values}, {"would": values, **SENSITIVE}
		)
		self.assertEqual(self._row(card, "Password")["value"], "set")
		self.assertNotIn("hunter2", json.dumps(card))

	def test_a_changed_password_shows_as_changed(self):
		frappe.set_user("Administrator")
		app = frappe.get_doc(
			{"doctype": "Connected App", "provider_name": PREFIX + "app", "client_secret": "old-secret-1"}
		).insert()
		self.addCleanup(frappe.delete_doc, "Connected App", app.name, force=True, ignore_permissions=True)
		changes = {"client_secret": "new-secret-2"}
		card = build_card(
			"update_doc",
			{"doctype": "Connected App", "name": app.name, "changes": changes},
			{"would": changes, **SENSITIVE},
		)
		[row] = card["diff"]
		self.assertEqual((row["from"], row["to"]), ("set", "changed"))
		self.assertNotIn("secret-", json.dumps(card))

	def test_a_same_length_password_change_still_shows_through_the_gate(self):
		"""The trial's resolved doc holds the masked column (one "*" per character),
		so an equal-length new password must not compare equal to the stored one."""
		frappe.set_user("Administrator")
		app = frappe.get_doc(
			{"doctype": "Connected App", "provider_name": PREFIX + "gate", "client_secret": "old-secret-1"}
		).insert()
		frappe.db.commit()
		self.addCleanup(frappe.db.commit)
		self.addCleanup(frappe.delete_doc, "Connected App", app.name, force=True, ignore_permissions=True)
		conv = self._conv()
		args = {"doctype": "Connected App", "name": app.name, "changes": {"client_secret": "new-secret-2"}}
		r = self._park("update_doc", args, conv)
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		card = json.loads(frappe.db.get_value(PENDING, {"conversation": conv}, "card"))
		self.assertEqual(card["risk_line"], "This changes how people sign in.")
		self.assertEqual([(d["from"], d["to"]) for d in card["diff"]], [("set", "changed")])
		self.assertNotIn("secret-", json.dumps(card))

	def test_invisible_and_direction_characters_are_shown(self):
		values = {"script": "alert(1)\u202e//\u200b ok\ufeff"}
		card = build_card(
			"create_doc", {"doctype": "Client Script", "values": values}, {"would": values, **SENSITIVE}
		)
		self.assertEqual(
			self._row(card, "Script")["value"],
			"alert(1)\u27e6U+202E\u27e7//\u27e6U+200B\u27e7 ok\u27e6U+FEFF\u27e7",
		)

	def test_a_name_that_hides_characters_is_shown_visibly(self):
		"""The card's header and targets too, not only the values: a record name with
		a direction override reads as something else."""
		name = "evil\u202etpircs\u200b"
		marked = "evil\u27e6U+202E\u27e7tpircs\u27e6U+200B\u27e7"
		values = {"name": name, "script": "x"}
		card = build_card(
			"create_doc", {"doctype": "Client Script", "values": values}, {"would": values, **SENSITIVE}
		)
		self.assertEqual(card["name"], marked)
		verb = build_card("delete_doc", {"doctype": "Client Script", "name": name}, SENSITIVE)
		self.assertEqual(verb["targets"], [marked])
		self.assertEqual(verb["records"][0]["name"], marked)
		ordinary = build_card("delete_doc", {"doctype": "ToDo", "name": name}, {})
		self.assertEqual(ordinary["targets"], [name], "an ordinary card is unchanged")

	def test_an_update_shows_a_line_diff_and_the_full_text(self):
		frappe.set_user("Administrator")
		old = _script(40)
		cs = frappe.get_doc({"doctype": "Client Script", **_client_script(PREFIX + "diff", old)}).insert()
		frappe.db.commit()
		new = old.replace("// line 20 ", "// changed 20 ")
		conv = self._conv()
		r = self._park(
			"update_doc", {"doctype": "Client Script", "name": cs.name, "changes": {"script": new}}, conv
		)
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		card = json.loads(frappe.db.get_value(PENDING, {"conversation": conv}, "card"))
		[row] = card["diff"]
		self.assertEqual((row["from"], row["to"]), (old, new), "both sides in full")
		removed = [x["text"] for x in row["lines"] if x["op"] == "-"]
		added = [x["text"] for x in row["lines"] if x["op"] == "+"]
		self.assertEqual(len(removed), 1)
		self.assertEqual(len(added), 1)
		self.assertTrue(added[0].startswith("// changed 20 "))
		self.assertLess(len(row["lines"]), 10, "context around the change, not the whole text")

	def test_a_card_too_large_to_show_is_refused_not_clipped(self):
		conv = self._conv()
		script = "// " + "x" * (300 * 1024)
		r = self._park(
			"create_doc", {"doctype": "Client Script", "values": _client_script(PREFIX + "big", script)}, conv
		)
		self.assertFalse(r["ok"], r)
		self.assertEqual(r["error"]["code"], "sensitive_refused")
		self.assertIn("too large to show in full", r["error"]["message"])
		self.assertIn("Desk", r["error"]["hint"])
		self.assertFalse(frappe.db.exists(PENDING, {"conversation": conv}))
		self.assertFalse(frappe.db.exists("Client Script", PREFIX + "big"))
		self.assertTrue(
			frappe.db.exists(
				"Jarvis Agent Write",
				{"tool": "create_doc", "ref_doctype": "Client Script", "outcome": "refused"},
			)
		)

	def test_a_huge_sensitive_update_is_refused_before_any_card_is_built(self):
		"""The size check runs on the call first: a line diff of megabytes of repeated
		lines is never computed for a card that would be refused anyway."""
		frappe.set_user("Administrator")
		cs = frappe.get_doc({"doctype": "Client Script", **_client_script(PREFIX + "huge", "a")}).insert()
		frappe.db.commit()
		conv = self._conv()
		big = "}\n" * (600 * 1024)
		with patch("jarvis.chat.confirm_card.build_card", side_effect=AssertionError("card built")):
			r = self._park(
				"update_doc", {"doctype": "Client Script", "name": cs.name, "changes": {"script": big}}, conv
			)
		self.assertEqual(r["error"]["code"], "sensitive_refused", r)
		self.assertFalse(frappe.db.exists(PENDING, {"conversation": conv}))

	def test_the_line_diff_is_bounded(self):
		from jarvis.chat._record_summary import line_diff

		big = "}\n" * (200 * 1024)
		self.assertIsNone(line_diff(big, big + "x"))

	def test_a_sensitive_write_with_no_card_is_refused(self):
		"""A card that could not be built would fall back to the raw view with no risk
		banner: refused instead of parked."""
		conv = self._conv()
		with patch("jarvis.chat.confirm_card.build_card", return_value=None):
			r = self._park(
				"create_doc",
				{"doctype": "Client Script", "values": _client_script(PREFIX + "nocard", "a")},
				conv,
			)
		self.assertEqual(r["error"]["code"], "sensitive_refused", r)
		self.assertIn("cannot be shown in full", r["error"]["message"])
		self.assertFalse(frappe.db.exists(PENDING, {"conversation": conv}))

	def test_the_draft_panel_gets_plain_words_for_a_card_too_large(self):
		from jarvis.chat.actions_api import apply_action

		conv = self._conv()
		with patch("jarvis.chat.events.publish_to_user"):
			r = apply_action(
				{
					"verb": "create",
					"doctype": "Client Script",
					"name": PREFIX + "panelbig",
					"values": _client_script(PREFIX + "panelbig", "// " + "x" * (300 * 1024)),
					"conversation": conv,
				}
			)
		self.assertFalse(r["ok"], r)
		self.assertEqual(
			r["error"]["message"],
			"This change is too large to show in full on a confirmation card, so it cannot be made from chat. "
			"Make it in Desk.",
		)
		self.assertNotIn("tool", r["error"]["message"])

	def test_a_child_table_change_is_shown_as_tables(self):
		frappe.set_user("Administrator")
		wh = frappe.get_doc(
			{
				"doctype": "Webhook",
				"name": PREFIX + "hook",
				"webhook_doctype": "ToDo",
				"webhook_docevent": "on_update",
				"request_url": "https://example.com/hook",
				"request_method": "POST",
				"enabled": 0,
				"webhook_headers": [{"key": "X-One", "value": "1"}],
			}
		).insert()
		self.addCleanup(frappe.delete_doc, "Webhook", wh.name, force=True, ignore_permissions=True)
		headers = [{"key": "X-One", "value": "1"}, {"key": "Authorization", "value": "Bearer abc"}]
		card = build_card(
			"update_doc",
			{"doctype": "Webhook", "name": wh.name, "changes": {"webhook_headers": headers}},
			{"would": {"webhook_headers": headers}, **SENSITIVE},
		)
		[row] = card["diff"]
		self.assertEqual(
			[r["cells"] for r in row["to_table"]["rows"]], [["X-One", "1"], ["Authorization", "Bearer abc"]]
		)
		self.assertEqual([r["cells"] for r in row["from_table"]["rows"]], [["X-One", "1"]])
		self.assertEqual(row["from_table"]["unknown_columns"], 0, "stored row metadata is not counted")

	def test_a_json_value_is_shown_not_dotted(self):
		values = {"data": {"a": 1, "b": [1, 2]}}
		card = build_card(
			"create_doc", {"doctype": "Webhook", "values": values}, {"would": values, **SENSITIVE}
		)
		self.assertIn('"b"', card["rows"][0]["value"])

	def test_a_crlf_only_update_names_the_line_endings(self):
		frappe.set_user("Administrator")
		cs = frappe.get_doc({"doctype": "Client Script", **_client_script(PREFIX + "crlf", "a\nb")}).insert()
		self.addCleanup(frappe.delete_doc, "Client Script", cs.name, force=True, ignore_permissions=True)
		card = build_card(
			"update_doc",
			{"doctype": "Client Script", "name": cs.name, "changes": {"script": "a\r\nb"}},
			{"would": {"script": "a\r\nb"}, **SENSITIVE},
		)
		[row] = card["diff"]
		self.assertIs(row["multiline"], True)
		self.assertEqual(row["to"], "a\r\nb", "a CRLF line ending is not marked")
		self.assertNotIn("lines", row)
		self.assertEqual(row["note"], "Only the line endings change.")
		one_line = "x" * 500
		card = build_card(
			"update_doc",
			{"doctype": "Client Script", "name": cs.name, "changes": {"script": one_line}},
			{"would": {"script": one_line}, **SENSITIVE},
		)
		self.assertIs(card["diff"][0]["multiline"], True)

	def test_a_crlf_script_shows_no_markers_and_a_lone_cr_still_does(self):
		def row_of(script):
			values = {"script": script}
			card = build_card(
				"create_doc", {"doctype": "Client Script", "values": values}, {"would": values, **SENSITIVE}
			)
			return card["rows"][0]

		crlf = row_of("line 1\r\nline 2\r\n")
		self.assertEqual(crlf["value"], "line 1\r\nline 2\r\n")
		self.assertNotIn("note", crlf)
		lone = row_of("x = 1\rrun()")
		self.assertEqual(lone["value"], "x = 1" + MARK.format(0x0D) + "run()")
		self.assertIn("note", lone)
		mixed = row_of("a\r\nb\rc")
		self.assertEqual(mixed["value"], "a\r\nb" + MARK.format(0x0D) + "c", "only the lone one is marked")
		self.assertIn("note", mixed)
		self.assertEqual(row_of("end\r")["value"], "end" + MARK.format(0x0D), "a trailing CR is lone")

	def test_a_crlf_line_diff_keeps_lines_unmarked(self):
		frappe.set_user("Administrator")
		cs = frappe.get_doc(
			{"doctype": "Client Script", **_client_script(PREFIX + "crlfdiff", "a\r\nb\r\nc")}
		).insert()
		self.addCleanup(frappe.delete_doc, "Client Script", cs.name, force=True, ignore_permissions=True)
		new = "a\r\nB\r\nc"
		card = build_card(
			"update_doc",
			{"doctype": "Client Script", "name": cs.name, "changes": {"script": new}},
			{"would": {"script": new}, **SENSITIVE},
		)
		[row] = card["diff"]
		self.assertEqual(
			[x for x in row["lines"] if x["op"] != " "], [{"op": "-", "text": "b"}, {"op": "+", "text": "B"}]
		)
		self.assertNotIn("note", row)

	def test_non_ascii_text_is_measured_by_its_real_size(self):
		from jarvis.chat.confirm_card import MAX_FULL_CARD_BYTES, too_large

		text = "\u0bb5" * (MAX_FULL_CARD_BYTES // 4)  # 3 bytes each in UTF-8: about 192 KB
		self.assertFalse(too_large({"v": text}, None))

	def test_an_ordinary_card_is_still_clipped(self):
		values = {"description": "y" * 500}
		card = build_card("create_doc", {"doctype": "ToDo", "values": values}, {"would": values})
		self.assertEqual(len(card["rows"][0]["value"]), 200)
		self.assertNotIn("multiline", card["rows"][0])
		self.assertNotIn("risk", card)

	def test_a_sensitive_delete_names_the_risk_and_the_consequence(self):
		frappe.set_user("Administrator")
		cs = frappe.get_doc({"doctype": "Client Script", **_client_script(PREFIX + "del", "a")}).insert()
		frappe.db.commit()
		frappe.set_user(TEST_USER)
		conv = self._conv(auto_mode=1)
		r = self._park("delete_doc", {"doctype": "Client Script", "name": cs.name}, conv)
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		card = json.loads(frappe.db.get_value(PENDING, {"conversation": conv}, "card"))
		self.assertEqual(card["risk_line"], "This runs code for every user.")
		self.assertEqual(card["consequence"], "Deleting removes it permanently.")
		self.assertTrue(frappe.db.exists("Client Script", cs.name))

	def test_the_draft_panel_card_is_shown_in_full(self):
		from jarvis.chat.actions_api import apply_action

		conv = self._conv()
		script = _script(80)
		with patch("jarvis.chat.events.publish_to_user"):
			r = apply_action(
				{
					"verb": "create",
					"doctype": "Client Script",
					"name": PREFIX + "panel",
					"values": _client_script(PREFIX + "panel", script),
					"conversation": conv,
				}
			)
		self.assertTrue(r.get("parked"), r)
		card = json.loads(frappe.db.get_value(PENDING, {"conversation": conv}, "card"))
		self.assertEqual(self._row(card, "Script")["value"], script)
		self.assertEqual(card["risk_line"], "This runs code for every user.")


# --------------------------------------------------------------------------- #
# Review cycle 1: hidden characters, held / imported / deleted sensitive records
# --------------------------------------------------------------------------- #
MARK = "\u27e6U+{:04X}\u27e7"

# The spec, written independently of the implementation: what a full card marks.
_EXTRA_HIDING = {
	*range(0xFE00, 0xFE10),  # variation selectors
	*range(0xE0100, 0xE01F0),
	0x034F,  # combining grapheme joiner
	*range(0x180B, 0x1810),  # Mongolian free variation selectors
	0x3164,
	0x115F,
	0x1160,
	0xFFA0,  # Hangul fillers
	0x2800,  # braille blank
	0x17B4,
	0x17B5,  # Khmer inherent vowels
	0x1D159,  # musical null notehead
	0x27E6,  # the marker's own bracket, so a typed marker cannot pass for a real one
}


def _hides(cp: int) -> bool:
	import unicodedata

	ch = chr(cp)
	if ch in "\n\t":
		return False
	cat = unicodedata.category(ch)
	if cat in ("Cf", "Cc", "Zl", "Zp", "Cn", "Co"):
		return True
	if cat == "Zs" and ch != " ":
		return True
	return cp in _EXTRA_HIDING


class TestHiddenCharacters(_Base):
	def test_every_code_point_is_marked_by_category(self):
		from jarvis.chat._record_summary import visible

		wrong = []
		for cp in range(0x110000):
			if 0xD800 <= cp <= 0xDFFF:
				continue  # surrogates never reach a card (not valid in JSON text)
			want = MARK.format(cp) if _hides(cp) else chr(cp)
			if visible(chr(cp)) != want:
				wrong.append(hex(cp))
		self.assertEqual(wrong[:20], [], f"{len(wrong)} code points marked wrongly")

	def test_the_reported_hiding_characters_are_all_marked(self):
		from jarvis.chat._record_summary import visible

		for ch in "\r\u2028\u2029\x85\x0b\x0c\U000e0041\ufe0f\U000e0100\u034f\u180b\u3164\u115f\u1160\uffa0\u2800\u2065\u206a\u206f\x01\x1b\x7f\x9b\xa0\u2000\u200a\u3000\u202f":
			self.assertEqual(visible(ch), MARK.format(ord(ch)), hex(ord(ch)))
		self.assertEqual(visible("a\tb\nc d"), "a\tb\nc d", "tab, newline and space stay")

	def test_a_typed_marker_cannot_pass_for_a_real_one(self):
		values = {"script": "\u27e6U+200B\u27e7 and \u200b"}
		card = build_card(
			"create_doc", {"doctype": "Client Script", "values": values}, {"would": values, **SENSITIVE}
		)
		shown = card["rows"][0]["value"]
		self.assertEqual(shown, MARK.format(0x27E6) + "U+200B\u27e7 and " + MARK.format(0x200B))
		self.assertEqual(shown.count(MARK.format(0x200B)), 1, "only the real one reads as a marker")

	def test_a_javascript_line_terminator_forces_a_block_and_a_note(self):
		for script in ("// harmless\u2028alert(1)", "x = 1  # note\rfrappe.log_error('PWNED')"):
			values = {"script": script}
			card = build_card(
				"create_doc", {"doctype": "Client Script", "values": values}, {"would": values, **SENSITIVE}
			)
			row = card["rows"][0]
			self.assertIs(row["multiline"], True, script)
			self.assertIn("\u27e6U+", row["value"])
			self.assertEqual(
				row["note"],
				"Contains a line break the card shows as a marker: the code may run it as a new line.",
			)

	def test_a_short_update_to_a_lone_carriage_return_shows_the_change(self):
		frappe.set_user("Administrator")
		cs = frappe.get_doc({"doctype": "Client Script", **_client_script(PREFIX + "cr", "x")}).insert()
		self.addCleanup(frappe.delete_doc, "Client Script", cs.name, force=True, ignore_permissions=True)
		new = "x\rfrappe.log_error(1)"
		card = build_card(
			"update_doc",
			{"doctype": "Client Script", "name": cs.name, "changes": {"script": new}},
			{"would": {"script": new}, **SENSITIVE},
		)
		[row] = card["diff"]
		self.assertIs(row["multiline"], True)
		self.assertIn("note", row)
		self.assertEqual(
			[x for x in row["lines"] if x["op"] != " "],
			[{"op": "-", "text": "x"}, {"op": "+", "text": "x" + MARK.format(0x0D) + "frappe.log_error(1)"}],
		)

	def test_the_line_diff_splits_on_newline_only(self):
		from jarvis.chat._record_summary import line_diff

		lines = line_diff("a\u2028b\nc", "a\u2028b\nd")
		self.assertIn({"op": " ", "text": "a\u2028b"}, lines)

	def test_the_line_diff_gap_counts_the_lines_left_out(self):
		from jarvis.chat._record_summary import line_diff

		old = "\n".join(f"line {i}" for i in range(40))
		lines = line_diff(old, old.replace("line 20", "line XX"))
		gaps = [x for x in lines if x["op"] == "gap"]
		shown = [x for x in lines if x["op"] != "gap"]
		self.assertEqual(sum(g["count"] for g in gaps) + sum(1 for x in shown if x["op"] != "+"), 40)

	def test_a_large_line_diff_takes_the_linear_path(self):
		from jarvis.chat import _record_summary

		old = "\n".join("}" for _ in range(999))
		new = "\n".join("}" if i % 2 else "{" for i in range(999))
		with patch.object(
			_record_summary.difflib, "SequenceMatcher", side_effect=AssertionError("quadratic")
		):
			lines = _record_summary.line_diff(old, new)
		self.assertTrue(any(x["op"] == "+" for x in lines))
		small = _record_summary.line_diff("a\nb", "a\nc")
		self.assertIn({"op": "+", "text": "c"}, small)


class TestSensitiveRoutes(_Base):
	def test_a_sensitive_import_is_refused_with_plain_words(self):
		for conv in (self._conv(), self._conv(auto_mode=1)):
			r = self._park("run_import", {"doctype": "Client Script", "file_url": "/x.csv"}, conv)
			self.assertEqual(r["error"]["code"], "sensitive_refused", r)
			self.assertIn("/app/data-import/new", r["error"]["hint"])
			self.assertNotIn("tool", r["error"]["person_message"])
			self.assertFalse(frappe.db.exists(PENDING, {"conversation": conv}))

	def test_a_sensitive_delete_shows_the_whole_record(self):
		frappe.set_user("Administrator")
		script = _script(30)
		cs = frappe.get_doc(
			{"doctype": "Client Script", **_client_script(PREFIX + "delall", script)}
		).insert()
		self.addCleanup(frappe.delete_doc, "Client Script", cs.name, force=True, ignore_permissions=True)
		card = build_card("delete_doc", {"doctype": "Client Script", "name": cs.name}, SENSITIVE)
		rows = {r["label"]: r for r in card["records"][0]["rows"]}
		self.assertEqual(rows["Script"]["value"], script)
		self.assertIs(rows["Script"]["multiline"], True)
		self.assertIn("DocType", rows)
		ordinary = build_card("delete_doc", {"doctype": "Client Script", "name": cs.name}, {})
		self.assertNotIn("multiline", json.dumps(ordinary))

	def test_a_batch_names_every_risk(self):
		conv = self._conv()
		args = {
			"docs": [
				{"doctype": "Client Script", "values": _client_script(PREFIX + "batch", "a")},
				{"doctype": "Role", "values": {"role_name": PREFIX + "role"}},
			]
		}
		r = self._park("create_doc", args, conv)
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		card = json.loads(frappe.db.get_value(PENDING, {"conversation": conv}, "card"))
		self.assertEqual(
			card["risk_line"], "This runs code for every user. This changes who can see or edit."
		)


class TestFullModeEveryKind(_Base):
	def test_bulk_update_shows_every_record_and_value(self):
		todos = [_todo(PREFIX + f"bu{i}") for i in range(21)]
		long = "y" * 500
		args = {
			"doctype": "ToDo",
			"updates": [{"name": t.name, "changes": {"description": long}} for t in todos],
		}
		card = build_card("update_doc", args, SENSITIVE)
		self.assertEqual(len(card["records"]), 21)
		self.assertEqual(card["records"][0]["diff"][0]["to"], long)
		self.assertEqual(len(build_card("update_doc", args, {})["records"]), 20, "ordinary keeps its cap")

	def test_bulk_update_masks_a_secret(self):
		frappe.set_user("Administrator")
		app = frappe.get_doc(
			{"doctype": "Connected App", "provider_name": PREFIX + "bulk", "client_secret": "old-secret-1"}
		).insert()
		self.addCleanup(frappe.delete_doc, "Connected App", app.name, force=True, ignore_permissions=True)
		args = {
			"doctype": "Connected App",
			"updates": [{"name": app.name, "changes": {"client_secret": "new-secret-22"}}],
		}
		card = build_card("update_doc", args, SENSITIVE)
		self.assertEqual(
			card["records"][0]["diff"], [{"label": "Client Secret", "from": "set", "to": "changed"}]
		)
		self.assertNotIn("secret-", json.dumps(card))

	def test_an_echoed_dummy_password_is_unchanged(self):
		frappe.set_user("Administrator")
		app = frappe.get_doc(
			{"doctype": "Connected App", "provider_name": PREFIX + "dummy", "client_secret": "old-secret-1"}
		).insert()
		self.addCleanup(frappe.delete_doc, "Connected App", app.name, force=True, ignore_permissions=True)
		changes = {"client_secret": "*****"}
		card = build_card(
			"update_doc",
			{"doctype": "Connected App", "name": app.name, "changes": changes},
			{"would": changes, **SENSITIVE},
		)
		self.assertEqual(card["diff"], [{"label": "Client Secret", "from": "set", "to": "unchanged"}])

	def test_batch_create_shows_every_value_whole(self):
		long = "z" * 500
		args = {"docs": [{"doctype": "ToDo", "values": {"description": long}}]}
		would = {"would": {"created": [{"doctype": "ToDo", "name": "T-1"}]}}
		card = build_card("create_doc", args, {**would, **SENSITIVE})
		self.assertEqual(card["records"][0]["rows"][0]["value"], long)
		self.assertEqual(len(build_card("create_doc", args, would)["records"][0]["rows"][0]["value"]), 200)

	def test_verb_records_show_values_whole(self):
		todo = _todo(PREFIX + "v" + "w" * 500)
		card = build_card("delete_doc", {"doctype": "ToDo", "name": todo.name}, SENSITIVE)
		values = [r["value"] for r in card["records"][0]["rows"]]
		self.assertIn(PREFIX + "v" + "w" * 500, values)

	def test_a_secret_in_a_child_row_is_masked(self):
		values = {
			"email": "cr-child@example.com",
			"user_emails": [{"email_account": "acc", "awaiting_password": 1}],
		}
		card = build_card("create_doc", {"doctype": "User", "values": values}, {"would": values, **SENSITIVE})
		[table] = card["tables"]
		col = table["fieldnames"].index("awaiting_password")
		self.assertEqual(table["rows"][0]["cells"][col], "set")

	def test_the_size_limit_boundary(self):
		from jarvis.chat.confirm_card import MAX_FULL_CARD_BYTES, too_large

		overhead = len(json.dumps({"v": ""}).encode())
		at = {"v": "x" * (MAX_FULL_CARD_BYTES - overhead)}
		self.assertFalse(too_large(at, None), "exactly the limit is shown")
		self.assertTrue(too_large({"v": at["v"] + "x"}, None), "one byte over is refused")


class TestCycleTwoGateCases(_Base):
	def test_a_password_created_through_the_gate_shows_as_set(self):
		"""The trial's resolved doc holds the masked column: the card judges the call."""
		conv = self._conv()
		args = {
			"doctype": "Connected App",
			"values": {"provider_name": PREFIX + "new", "client_secret": "abc12"},
		}
		r = self._park("create_doc", args, conv)
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		card = json.loads(frappe.db.get_value(PENDING, {"conversation": conv}, "card"))
		self.assertEqual({x["label"]: x["value"] for x in card["rows"]}["Client Secret"], "set")
		self.assertNotIn("abc12", json.dumps(card))

	def test_an_emptied_child_table_shows_the_current_rows_and_none(self):
		frappe.set_user("Administrator")
		wh = frappe.get_doc(
			{
				"doctype": "Webhook",
				"name": PREFIX + "hook-empty",
				"webhook_doctype": "ToDo",
				"webhook_docevent": "on_update",
				"request_url": "https://example.com/hook",
				"request_method": "POST",
				"enabled": 0,
				"webhook_headers": [{"key": "Authorization", "value": "Bearer abc"}],
			}
		).insert()
		self.addCleanup(frappe.delete_doc, "Webhook", wh.name, force=True, ignore_permissions=True)
		card = build_card(
			"update_doc",
			{"doctype": "Webhook", "name": wh.name, "changes": {"webhook_headers": []}},
			{"would": {"webhook_headers": []}, **SENSITIVE},
		)
		[row] = card["diff"]
		self.assertEqual((row["from"], row["to"]), ("1 row", "0 rows"))
		self.assertEqual([r["cells"] for r in row["from_table"]["rows"]], [["Authorization", "Bearer abc"]])
		self.assertNotIn("to_table", row)


# --------------------------------------------------------------------------- #
# Delta review: imports refused only when they would write something sensitive
# --------------------------------------------------------------------------- #
def _csv(content: str) -> str:
	f = frappe.get_doc(
		{"doctype": "File", "file_name": PREFIX + "import.csv", "content": content, "is_private": 1}
	).insert(ignore_permissions=True)
	return f.file_url


def _portal_header() -> str:
	return "User ({})".format(frappe.get_meta("Customer").get_field("portal_users").label)


class TestImportNarrowed(_Base):
	"""As Administrator: importing Customer / Employee needs a role the shared test user
	does not have, and granting it would leak into other tests."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def _conv(self, **flags) -> str:
		conv = _make_conv("Administrator")
		if flags:
			frappe.db.set_value(CONV, conv, flags, update_modified=False)
			frappe.db.commit()
		return conv

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		for conv in frappe.get_all(
			CONV, filters={"owner": "Administrator", "title": "conv test"}, pluck="name"
		):
			pending_confirm.clear_for_conversation("Administrator", conv)
			frappe.db.delete(PENDING, {"conversation": conv})
			frappe.db.delete(MSG, {"conversation": conv})
			frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
		for name in frappe.get_all("File", {"file_name": PREFIX + "import.csv"}, pluck="name"):
			frappe.delete_doc("File", name, force=True, ignore_permissions=True)
		frappe.db.delete(
			"Data Import", {"reference_doctype": "Customer", "import_file": ["like", "%" + PREFIX + "%"]}
		)
		frappe.db.commit()
		super().tearDown()

	def test_an_ordinary_customer_import_parks_its_card(self):
		url = _csv("Customer Name\n" + PREFIX + "cust\n")
		conv = self._conv()
		r = self._park("run_import", {"doctype": "Customer", "file_url": url}, conv)
		self.assertEqual(r["data"]["status"], "pending_confirmation", r)
		card = json.loads(frappe.db.get_value(PENDING, {"conversation": conv}, "card"))
		self.assertEqual(card["kind"], "import")
		self.assertNotIn("risk", card)

	def test_a_customer_import_with_portal_users_is_refused(self):
		url = _csv(f"Customer Name,{_portal_header()}\n{PREFIX}cust,{TEST_USER}\n")
		conv = self._conv()
		r = self._park("run_import", {"doctype": "Customer", "file_url": url}, conv)
		self.assertEqual(r["error"]["code"], "sensitive_refused", r)
		self.assertFalse(frappe.db.exists(PENDING, {"conversation": conv}))

	def test_an_empty_portal_column_does_not_count(self):
		from jarvis.tools import _write_risk as wr

		url = _csv(f"Customer Name,{_portal_header()}\n{PREFIX}cust,\n")
		self.assertIsNone(wr.risk_of("run_import", {"doctype": "Customer", "file_url": url}))

	def test_an_employee_import_counts_only_its_access_columns(self):
		from jarvis.tools import _write_risk as wr

		plain = _csv("First Name,Gender\nAsha,Female\n")
		with_user = _csv(f"First Name,User ID\nAsha,{TEST_USER}\n")
		status_only = _csv("ID,Status\nHR-EMP-00001,Left\n")
		self.assertIsNone(wr.risk_of("run_import", {"doctype": "Employee", "file_url": plain}))
		self.assertEqual(
			wr.risk_of("run_import", {"doctype": "Employee", "file_url": with_user}), "sensitive"
		)
		self.assertIsNone(
			wr.risk_of("run_import", {"doctype": "Employee", "file_url": status_only}),
			"a status on new records turns no login on or off",
		)
		self.assertEqual(
			wr.risk_of(
				"run_import",
				{"doctype": "Employee", "file_url": status_only, "import_type": "Update Existing Records"},
			),
			"sensitive",
			"a status change on existing employees can turn their logins on or off",
		)
		r = self._park("run_import", {"doctype": "Employee", "file_url": with_user}, self._conv())
		self.assertEqual(r["error"]["code"], "sensitive_refused", r)

	def test_a_sensitive_doctype_import_is_refused(self):
		r = self._park(
			"run_import", {"doctype": "Server Script", "file_url": "/private/files/x.csv"}, self._conv()
		)
		self.assertEqual(r["error"]["code"], "sensitive_refused", r)

	def test_a_missing_file_gets_the_imports_own_error(self):
		"""A mistyped file is a fixable mistake, not a sensitive write: the import's own
		validation answers (retryable), never "use Data Import in Desk"."""
		for args in (
			{"doctype": "Customer", "file_url": "/private/files/no-such-file.csv"},
			{"doctype": "Customer", "filename": "no-such-file.csv"},
		):
			conv = self._conv()
			r = self._park("run_import", args, conv)
			self.assertFalse(r["ok"], r)
			self.assertEqual(r["error"]["code"], "InvalidArgumentError", r)
			self.assertNotIn("person_message", r["error"])
			self.assertNotIn("Data Import in Desk", r["error"]["message"])
			self.assertFalse(frappe.db.exists(PENDING, {"conversation": conv}))

	def test_a_header_only_file_gets_the_imports_own_error(self):
		url = _csv("Customer Name\n")
		r = self._park("run_import", {"doctype": "Customer", "file_url": url}, self._conv())
		self.assertEqual(r["error"]["code"], "InvalidArgumentError", r)
		self.assertNotIn("person_message", r["error"])

	def test_a_bad_mapping_key_gets_the_imports_own_error(self):
		url = _csv("Customer Name\n" + PREFIX + "cust\n")
		args = {"doctype": "Customer", "file_url": url, "mapping": {"No Such Column": "customer_name"}}
		r = self._park("run_import", args, self._conv())
		self.assertEqual(r["error"]["code"], "InvalidArgumentError", r)
		self.assertIn("No Such Column", r["error"]["message"])

	def test_a_file_read_but_not_classifiable_is_refused(self):
		"""Fail closed once the file was read: an error classifying its columns refuses."""
		url = _csv(f"Customer Name,{_portal_header()}\n{PREFIX}cust,{TEST_USER}\n")
		conv = self._conv()
		with patch(
			"frappe.model.meta.Meta.get_table_fields", side_effect=RuntimeError("classification failed")
		):
			r = self._park("run_import", {"doctype": "Customer", "file_url": url}, conv)
		self.assertEqual(r["error"]["code"], "sensitive_refused", r)
		self.assertIn("Data Import", r["error"]["person_message"])
		self.assertFalse(frappe.db.exists(PENDING, {"conversation": conv}))

	def _confirm_legacy(self, args):
		from jarvis.chat.actions_api import confirm_tool

		token = pending_confirm.mint(
			conversation="",
			owner="Administrator",
			tool="run_import",
			args=args,
			run_id="",
			preview={"preview": False, "described": True, "note": "parked before the narrowed rule"},
		)
		return confirm_tool(token)

	def test_confirm_refuses_a_parked_sensitive_import(self):
		"""A row parked during the J1-guard window showed a clipped card: Confirm refuses it."""
		url = _csv(f"Customer Name,{_portal_header()}\n{PREFIX}cust,{TEST_USER}\n")
		before = frappe.db.count("Data Import")
		with patch("frappe.enqueue") as enqueue:
			res = self._confirm_legacy({"doctype": "Customer", "file_url": url})
		self.assertFalse(res["ok"], res)
		self.assertEqual(res["error"]["code"], "sensitive_refused", res)
		self.assertEqual(frappe.db.count("Data Import"), before, "nothing staged")
		self.assertFalse(enqueue.called)

	def test_confirm_still_runs_an_ordinary_import(self):
		url = _csv("Customer Name\n" + PREFIX + "cust\n")
		before = frappe.db.count("Data Import")
		with patch("frappe.enqueue"):
			res = self._confirm_legacy({"doctype": "Customer", "file_url": url})
		self.assertTrue(res["ok"], res)
		self.assertEqual(frappe.db.count("Data Import"), before + 1)


class TestDeltaMinors(_Base):
	def test_khmer_and_musical_blanks_are_marked(self):
		from jarvis.chat._record_summary import visible

		for cp in (0x17B4, 0x17B5, 0x1D159, 0x1D173, 0x1D17A):
			self.assertEqual(visible(chr(cp)), MARK.format(cp), hex(cp))

	def test_a_cleared_secret_shows_cleared(self):
		frappe.set_user("Administrator")
		app = frappe.get_doc(
			{"doctype": "Connected App", "provider_name": PREFIX + "clear", "client_secret": "old-secret-1"}
		).insert()
		self.addCleanup(frappe.delete_doc, "Connected App", app.name, force=True, ignore_permissions=True)
		changes = {"client_secret": ""}
		card = build_card(
			"update_doc",
			{"doctype": "Connected App", "name": app.name, "changes": changes},
			{"would": changes, **SENSITIVE},
		)
		self.assertEqual(card["diff"], [{"label": "Client Secret", "from": "set", "to": "cleared"}])

	def test_a_delete_card_shows_child_tables_as_marked_tables(self):
		frappe.set_user("Administrator")
		wh = frappe.get_doc(
			{
				"doctype": "Webhook",
				"name": PREFIX + "hook-del",
				"webhook_doctype": "ToDo",
				"webhook_docevent": "on_update",
				"request_url": "https://example.com/hook",
				"request_method": "POST",
				"enabled": 0,
				"webhook_headers": [{"key": "X-Run", "value": "a\rb"}],
			}
		).insert()
		self.addCleanup(frappe.delete_doc, "Webhook", wh.name, force=True, ignore_permissions=True)
		card = build_card("delete_doc", {"doctype": "Webhook", "name": wh.name}, SENSITIVE)
		record = card["records"][0]
		[table] = record["tables"]
		self.assertEqual(table["rows"][0]["cells"], ["X-Run", "a" + MARK.format(0x0D) + "b"])
		self.assertEqual(
			table["note"],
			"Contains a line break the card shows as a marker: the code may run it as a new line.",
		)
		self.assertNotIn("Headers", [r["label"] for r in record["rows"]], "not as escaped JSON")

	def test_a_value_of_plain_spaces_is_shown_visibly(self):
		"""Spaces alone would draw as an empty cell: on a sensitive card every character
		of a whitespace-only value is marked (row values, diff sides, table cells), not
		the indentation of a line-diff line; an ordinary card is unchanged."""
		values = {"script": "   ", "dt": "ToDo"}
		card = build_card(
			"create_doc", {"doctype": "Client Script", "values": values}, {"would": values, **SENSITIVE}
		)
		rows = {r["label"]: r["value"] for r in card["rows"]}
		self.assertEqual(rows["Script"], MARK.format(0x20) * 3)
		self.assertEqual(rows["DocType"], "ToDo")
		ordinary = build_card("create_doc", {"doctype": "Client Script", "values": values}, {"would": values})
		self.assertNotIn("Script", [r["label"] for r in ordinary["rows"]])
		frappe.set_user("Administrator")
		cs = frappe.get_doc(
			{"doctype": "Client Script", **_client_script(PREFIX + "ws", "a\n    \nb")}
		).insert()
		self.addCleanup(frappe.delete_doc, "Client Script", cs.name, force=True, ignore_permissions=True)
		new = "a\n    \nc"
		diff = build_card(
			"update_doc",
			{"doctype": "Client Script", "name": cs.name, "changes": {"script": new}},
			{"would": {"script": new}, **SENSITIVE},
		)["diff"][0]
		self.assertIn({"op": " ", "text": "    "}, diff["lines"], "a line's indentation stays as typed")

	def test_a_whitespace_only_value_is_kept_and_marked(self):
		values = {"script": "\u3000", "dt": "ToDo"}
		card = build_card(
			"create_doc", {"doctype": "Client Script", "values": values}, {"would": values, **SENSITIVE}
		)
		rows = {r["label"]: r["value"] for r in card["rows"]}
		self.assertEqual(rows["Script"], MARK.format(0x3000))
