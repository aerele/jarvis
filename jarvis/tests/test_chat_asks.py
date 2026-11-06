"""Tests for chat-sourced approvals (notify-approvals design Part 2).

Covers ``jarvis.chat.chat_asks`` — fence parsing (last fence wins, malformed
skipped, options captured), ``materialize_from_turn`` (row shape, owner,
approval:new publish, dedupe), ``resolve_on_user_message`` (Pending Chat rows
flip to Answered; File-Box/decided rows untouched) — plus the
``approvals_api`` extensions: ``decide()`` on a Chat-source row resumes the
conversation WITHOUT re-attaching the conversation's File, the status filter
accepts "Answered", the ``awaiting_reply`` lane (inclusion/exclusion ladder,
first page only), and source NULL = File Box back-compat.

Uses ``unittest.TestCase`` with explicit commits + prefix-based cleanup (like
test_feature_pages_api), since awaiting_reply/decide run raw owner-scoped SQL
and need REAL users. Every row this module creates carries a ``ca-`` marker so
``_wipe_all`` removes it regardless of owner.
"""

from __future__ import annotations

import contextlib
import json
import os
import unittest
from unittest.mock import patch

import frappe

from jarvis.chat.approvals_api import (
	decide,
	dismiss_approval,
	get_approval,
	list_approvals_page,
	restore_approval,
)
from jarvis.chat.chat_asks import (
	_tail_fit,
	ask_lead_in,
	humanize_fieldname,
	materialize_from_turn,
	parse_ask,
	question_excerpt,
	resolve_on_user_message,
)

USER_A = "ca-user-a@example.com"
USER_B = "ca-user-b@example.com"

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
APPROVAL = "Jarvis Approval Request"


def _fence(payload, suffix="") -> str:
	"""A ```jarvis-ask fenced block; ``payload`` may be a raw string
	(malformed-JSON cases) or any JSON-dumpable value."""
	body = payload if isinstance(payload, str) else json.dumps(payload)
	return f"```jarvis-ask{suffix}\n{body}\n```"


def _fence_raw(body: str) -> str:
	return "Some lead-in.\n\n```jarvis-ask\n" + body + "\n```"


def _ask(q="Which supplier is this from?", type="single", options=("Acme", "Globex")):
	return {"q": q, "type": type, "options": list(options)}


# --------------------------------------------------------------------------- #
# module fixtures / helpers
# --------------------------------------------------------------------------- #
def _ensure_user(email: str) -> str:
	from jarvis.permissions import ensure_jarvis_user_role

	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		)
		u.flags.ignore_permissions = True
		u.insert()
		frappe.db.commit()
	if frappe.db.get_value("User", email, "user_type") != "System User":
		frappe.db.set_value("User", email, "user_type", "System User", update_modified=False)
		frappe.clear_cache(user=email)
	roles = set(frappe.get_roles(email))
	if "System Manager" in roles:
		frappe.get_doc("User", email).remove_roles("System Manager")
		frappe.db.commit()
	# Approvals endpoints are chat-surface: now require the Jarvis User role
	# (security review TASK 8).
	if "Jarvis User" not in roles:
		frappe.get_doc("User", email).add_roles("Jarvis User")
		frappe.db.commit()
	return email


def setUpModule() -> None:
	frappe.set_user("Administrator")
	_ensure_user(USER_A)
	_ensure_user(USER_B)


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


def _wipe_all() -> None:
	for conv in frappe.get_all(CONV, filters={"title": ["like", "ca-%"]}, pluck="name"):
		for ap in frappe.get_all(APPROVAL, filters={"conversation": conv}, pluck="name"):
			frappe.delete_doc(APPROVAL, ap, force=True, ignore_permissions=True)
		frappe.db.delete(MSG, {"conversation": conv})
		for f in frappe.get_all(
			"File",
			filters={"attached_to_doctype": CONV, "attached_to_name": conv},
			pluck="name",
		):
			frappe.delete_doc("File", f, force=True, ignore_permissions=True)
		frappe.delete_doc(CONV, conv, force=True, ignore_permissions=True)
	for name in frappe.get_all(APPROVAL, filters={"title": ["like", "ca-%"]}, pluck="name"):
		frappe.delete_doc(APPROVAL, name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _mk_conv(owner, title, status="Active", origin_page="") -> str:
	with _as(owner):
		doc = frappe.get_doc(
			{
				"doctype": CONV,
				"title": title,
				"status": status,
				"origin_page": origin_page,
			}
		)
		doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _add_msg(conv, seq, role, content, streaming=0, recovering=0, error="", **extra) -> None:
	msg = frappe.get_doc(
		{
			"doctype": MSG,
			"conversation": conv,
			"seq": seq,
			"role": role,
			"content": content,
			"streaming": streaming,
			"recovering": recovering,
			"error": error,
			**extra,
		}
	)
	msg.flags.jarvis_server_write = True  # e.g. a receipt's action_outcome
	msg.insert(ignore_permissions=True)
	frappe.db.commit()


def _mk_approval(
	owner,
	title,
	status="Pending",
	conversation=None,
	source=None,
	question="q?",
	options='["Approve","Reject"]',
	decision=None,
) -> str:
	with _as(owner):
		d = {
			"doctype": APPROVAL,
			"title": title,
			"status": status,
			"conversation": conversation,
			"question": question,
			"options": options,
		}
		if source is not None:
			d["source"] = source
		if decision is not None:
			d["decision"] = decision
		doc = frappe.get_doc(d)
		doc.insert(ignore_permissions=True)
	if source is None:
		# The doctype defaults source to "File Box" on insert; a PRE-FIELD row
		# has NULL. Force NULL so the back-compat (NULL = File Box) paths are
		# genuinely exercised.
		frappe.db.set_value(APPROVAL, doc.name, "source", None, update_modified=False)
	frappe.db.commit()
	return doc.name


def _attach_file(conv: str, owner: str, file_name="ca-attach.txt") -> str:
	with _as(owner):
		f = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": file_name,
				"attached_to_doctype": CONV,
				"attached_to_name": conv,
				"is_private": 1,
				"content": "hello world",
			}
		)
		f.flags.ignore_permissions = True
		f.insert(ignore_permissions=True)
	frappe.db.commit()
	return f.name


# =========================================================================== #
# parse_ask / question_excerpt — pure parsing, ports of ChatView.askOf
# =========================================================================== #
class TestParseAsk(unittest.TestCase):
	def test_options_captured(self):
		qs = parse_ask("Need a call.\n\n" + _fence([_ask()]))
		self.assertEqual(len(qs), 1)
		self.assertEqual(qs[0]["q"], "Which supplier is this from?")
		self.assertEqual(qs[0]["type"], "single")
		self.assertEqual(qs[0]["options"], ["Acme", "Globex"])

	def test_last_fence_wins(self):
		content = (
			_fence([_ask(q="old question?", options=("A", "B"))])
			+ "\nsome prose in between\n"
			+ _fence([_ask(q="new question?", options=("C", "D"))])
		)
		qs = parse_ask(content)
		self.assertEqual(len(qs), 1)
		self.assertEqual(qs[0]["q"], "new question?")
		self.assertEqual(qs[0]["options"], ["C", "D"])

	def test_malformed_json_skipped(self):
		self.assertEqual(parse_ask(_fence('{"q": not json')), [])
		self.assertEqual(parse_ask(_fence('"just a string"')), [])
		self.assertEqual(parse_ask(_fence("42")), [])
		self.assertEqual(parse_ask("no fence at all?"), [])
		self.assertEqual(parse_ask(None), [])

	def test_questions_wrapper_accepted(self):
		qs = parse_ask(_fence({"questions": [_ask()]}))
		self.assertEqual(len(qs), 1)

	def test_fence_grammar_matches_client(self):
		# trailing spaces/tabs after the tag are allowed (the [ \t]* in _ASK_RE)
		self.assertEqual(len(parse_ask(_fence([_ask()], suffix="  \t"))), 1)
		# but no newline after the tag = no match ("```jarvis-askX" style)
		self.assertEqual(parse_ask("```jarvis-ask {}```"), [])

	def test_normalization_mirrors_client(self):
		qs = parse_ask(
			_fence(
				[
					{"q": "ok to proceed?", "type": "boolean"},  # boolean -> yesno
					{"q": "pick one", "type": "bogus", "options": ["x"]},  # unknown -> single
					{"q": "no options, not a field type", "type": "multi"},  # dropped
					{"question": "alt key works?", "type": "text"},  # question alias
					{"type": "single", "options": ["y"]},  # empty q dropped
				]
			)
		)
		self.assertEqual(
			[(q["q"], q["type"]) for q in qs],
			[("ok to proceed?", "yesno"), ("pick one", "single"), ("alt key works?", "text")],
		)

	def test_caps_six_questions_eight_options(self):
		qs = parse_ask(_fence([_ask(q=f"q{i}?") for i in range(9)]))
		self.assertEqual(len(qs), 6)
		qs = parse_ask(_fence([_ask(options=[f"o{i}" for i in range(12)])]))
		self.assertEqual(len(qs[0]["options"]), 8)

	def test_field_question_kept_with_identifiers_and_empty_q(self):
		qs = parse_ask(
			_fence(
				[
					{"type": "field", "doctype": "Sales Invoice", "fieldname": "custom_gstin_2"},
					{"q": "Which tax ID?", "type": "field", "doctype": "Customer", "fieldname": "tax_id"},
				]
			)
		)
		self.assertEqual([q["type"] for q in qs], ["field", "field"])
		self.assertEqual(
			(qs[0]["q"], qs[0]["doctype"], qs[0]["fieldname"]), ("", "Sales Invoice", "custom_gstin_2")
		)
		self.assertEqual(qs[0]["options"], [])
		self.assertEqual(qs[1]["q"], "Which tax ID?")

	def test_field_bad_identifier_degrades_to_text_with_humanized_label(self):
		qs = parse_ask(
			_fence(
				[
					{"type": "field", "doctype": "Customer; drop", "fieldname": "custom_gstin_2"},
					{"q": "Keep my words", "type": "field", "doctype": "Customer", "fieldname": "a-b"},
					{"type": "field", "doctype": "", "fieldname": ""},  # nothing to label: dropped
				]
			)
		)
		self.assertEqual(
			[(q["q"], q["type"], q["doctype"]) for q in qs],
			[("Gstin 2", "text", ""), ("Keep my words", "text", "")],
		)

	def test_humanize_fieldname_matches_client(self):
		self.assertEqual(humanize_fieldname("custom_gstin_2"), "Gstin 2")
		self.assertEqual(humanize_fieldname("tax__id"), "Tax id")
		self.assertEqual(humanize_fieldname(""), "")

	def test_select_keeps_up_to_200_options_and_degrades_without_any(self):
		many = [f"o{i}" for i in range(250)]
		qs = parse_ask(_fence([{"q": "Pick", "type": "select", "options": many}]))
		self.assertEqual((qs[0]["type"], len(qs[0]["options"])), ("select", 200))
		qs = parse_ask(_fence([{"q": "Pick", "type": "select"}]))
		self.assertEqual((qs[0]["type"], qs[0]["options"]), ("text", []))
		qs = parse_ask(_fence([{"q": "Pick", "type": "single", "options": many}]))
		self.assertEqual(len(qs[0]["options"]), 8)

	def test_question_excerpt_labels_a_field_question(self):
		content = _fence([{"type": "field", "doctype": "Customer", "fieldname": "custom_tax_id"}])
		self.assertEqual(question_excerpt(content), "Tax id")

	def test_question_excerpt_fence(self):
		self.assertEqual(
			question_excerpt("prose\n" + _fence([_ask(q="Which one?")])),
			"Which one?",
		)

	def test_question_excerpt_prose_tail(self):
		out = question_excerpt("I drafted the invoice. Should I submit it now?  \n")
		self.assertTrue(out.endswith("Should I submit it now?"))
		long = ("x" * 400) + " Should I submit it now?"
		self.assertLessEqual(len(question_excerpt(long)), 140)

	# =========================================================================== #
	# materialize_from_turn — row creation + publish + dedupe
	# =========================================================================== #

	# -- shapes models actually send (real chats, shared with chatAsk.test.js) --

	@staticmethod
	def _real() -> dict:
		path = os.path.join(os.path.dirname(__file__), "fixtures", "reply_cleanup_real_chats.json")
		with open(path) as f:
			return json.load(f)

	def test_single_question_object_with_question_key(self):
		qs = parse_ask(_fence_raw(self._real()["broken_ask_blocks"]["bn2pbg418f"]))
		self.assertEqual(len(qs), 1)
		self.assertEqual(qs[0]["type"], "single")
		self.assertTrue(qs[0]["q"].startswith("ACC-PINV-2026-00008 (MA Inc."))
		self.assertEqual(
			qs[0]["options"],
			[
				"Use the existing invoice ACC-PINV-2026-00008",
				"Draft a new invoice anyway (confirmed not a duplicate)",
			],
		)

	def test_kind_title_prompt_and_nested_field_make_a_field_question(self):
		qs = parse_ask(_fence_raw(self._real()["broken_ask_blocks"]["jr7apok0g6"]))
		self.assertEqual(
			qs,
			[
				{
					"q": "Supplier invoice number",
					"type": "field",
					"options": [],
					"doctype": "Purchase Invoice",
					"fieldname": "bill_no",
				}
			],
		)

	def test_nested_field_with_a_bad_identifier_degrades_to_text(self):
		qs = parse_ask(
			_fence_raw('{"title":"Ref","field":{"doctype":"Purchase Invoice","fieldname":"bill no; x"}}')
		)
		self.assertEqual([(q["q"], q["type"]) for q in qs], [("Ref", "text")])

	def test_empty_or_list_field_is_not_a_field_reference(self):
		for field in ("{}", "[]", '["x"]'):
			qs = parse_ask(_fence_raw('{"q":"Pick","type":"single","options":["a","b"],"field":%s}' % field))
			self.assertEqual([(q["type"], q["options"]) for q in qs], [("single", ["a", "b"])])

	def test_questions_envelope_with_label_text(self):
		qs = parse_ask(_fence_raw(self._real()["broken_ask_blocks"]["ch1n4ju8js"]))
		self.assertEqual(len(qs), 1)
		self.assertEqual(qs[0]["type"], "single")
		self.assertTrue(qs[0]["q"].startswith("ACC-PINV-2026-00008 already matches"))
		self.assertEqual(len(qs[0]["options"]), 3)

	def test_kind_is_used_only_when_known_and_type_wins(self):
		self.assertEqual(parse_ask(_fence_raw('{"kind":"date","label":"When?"}'))[0]["type"], "date")
		self.assertEqual(
			parse_ask(_fence_raw('{"type":"yesno","kind":"text","q":"Go?"}'))[0]["type"], "yesno"
		)
		self.assertEqual(
			parse_ask(_fence_raw('{"kind":"wat","q":"Pick","options":["a"]}'))[0]["type"], "single"
		)

	def test_object_without_text_or_type_is_not_a_question(self):
		self.assertEqual(parse_ask(_fence_raw('{"foo":1}')), [])
		self.assertEqual(parse_ask(_fence_raw('{"questions":"nope"}')), [])

	def test_real_blocks_that_parsed_before_still_parse_the_same(self):
		valid = self._real()["valid_ask_blocks"]
		self.assertEqual(
			parse_ask(_fence_raw(valid["b1kllbcn7j"])),
			[
				{
					"q": "ACC-PINV-2026-00008 already covers this. Still draft the new Purchase Invoice for review?",
					"type": "yesno",
					"options": ["Draft it anyway", "Leave it, 00008 stands"],
					"doctype": "",
					"fieldname": "",
				}
			],
		)
		self.assertEqual(parse_ask(_fence_raw(valid["ftofg3mkjj"]))[0]["type"], "single")
		self.assertEqual(
			parse_ask(_fence_raw(valid["9rbnkqvrhr"])),
			[
				{
					"q": "Supplier invoice number for this bill",
					"type": "text",
					"options": [],
					"doctype": "",
					"fieldname": "",
				}
			],
		)


class TestAskLeadIn(unittest.TestCase):
	"""``ask_lead_in``: the prose just above the LAST jarvis-ask fence, as one safe line."""

	TASK = "I will draft the customer-wise outstanding email for Acme and send it to each customer."
	GENERIC = "A few quick questions so I set this up right:"

	def _ask_fence(self):
		return _fence([_ask("Which recipients?")])

	def test_the_nearest_paragraph_is_the_lead_in(self):
		text = "Earlier note about something else, which is fairly long and sits far above the ask here.\n\n"
		content = (
			text
			+ "Pick the supplier carefully, this one decides the ledger posting for the invoice.\n\n"
			+ self._ask_fence()
		)
		out = ask_lead_in(content)
		self.assertIn("Pick the supplier carefully", out)

	def test_a_generic_last_line_pulls_in_the_task_paragraph_above_it(self):
		out = ask_lead_in(f"{self.TASK}\n\n{self.GENERIC}\n\n{self._ask_fence()}")
		self.assertEqual(out, f"{self.TASK} {self.GENERIC}")

	def test_a_long_nearest_paragraph_stands_alone_when_the_next_will_not_fit(self):
		near = "n" * 5 + " " + ("word " * 70).strip()  # ~360 chars, over 120
		out = ask_lead_in(f"{self.TASK}\n\n{near}\n\n{self._ask_fence()}")
		self.assertEqual(out, near)

	def test_a_task_paragraph_over_the_cap_is_cut_keeping_its_end(self):
		task = " ".join(f"w{i:03d}" for i in range(150)) + " the-end-of-the-task"
		self.assertGreater(len(task), 400)
		out = ask_lead_in(f"{task}\n\n{self.GENERIC}\n\n{self._ask_fence()}")
		self.assertLessEqual(len(out), 400)
		self.assertTrue(out.endswith(self.GENERIC))
		self.assertIn("the-end-of-the-task", out)
		self.assertNotIn("w000", out)
		self.assertIn("…", out)
		# cut on a word boundary: no partial "w1" style fragment right after the ellipsis
		self.assertRegex(out, r"^…\s?w\d{3} ")

	def test_a_nearest_paragraph_over_the_cap_alone_is_taken_and_capped(self):
		huge = " ".join(f"v{i:03d}" for i in range(200)) + " last-words"
		out = ask_lead_in(f"{huge}\n\n{self._ask_fence()}")
		self.assertLessEqual(len(out), 400)
		self.assertIn("last-words", out)
		self.assertTrue(out.startswith("…"))

	def test_a_huge_paragraph_of_unmatched_markers_is_not_quadratic(self):
		# Unmatched ** markers make the markdown regexes quadratic; only the tail of a paragraph
		# can ever be used, so the paragraph is clipped before they run.
		import time

		content = f"{' **a' * 50_000}\n\n{self._ask_fence()}"  # 200K chars
		start = time.monotonic()
		out = ask_lead_in(content)
		elapsed = time.monotonic() - start
		self.assertLess(elapsed, 1.0)
		self.assertLessEqual(len(out), 400)

	def test_a_table_between_the_task_and_the_generic_line_is_passed_over(self):
		table = "| Customer | Due |\n|---|---|\n| Acme | 10 |\n| Globex | 20 |"
		out = ask_lead_in(f"{self.TASK}\n\n{table}\n\n{self.GENERIC}\n\n{self._ask_fence()}")
		self.assertEqual(out, f"{self.TASK} {self.GENERIC}")
		self.assertNotIn("|", out)

	def test_lists_headings_and_other_fences_are_passed_over(self):
		content = (
			f"{self.TASK}\n\n- Acme\n- Globex\n\n## Plan\n\n"
			'```jarvis-action\n{"kind": "x"}\n```\n\n'
			"```python\nprint('x')\n```\n\n"
			f"{self.GENERIC}\n\n{self._ask_fence()}"
		)
		out = ask_lead_in(content)
		self.assertEqual(out, f"{self.TASK} {self.GENERIC}")

	def test_list_and_heading_lines_inside_one_paragraph_are_dropped(self):
		# No blank line between the prose and the list: the line-level filter drops the list
		# lines and the prose either side of them joins into one paragraph.
		content = f"I will send the email to:\n- Acme\n- Globex\nWhich ones?\n\n{self._ask_fence()}"
		self.assertEqual(ask_lead_in(content), "I will send the email to: Which ones?")
		content = f"I will send the email to:\n## Plan\n1. Acme\nWhich ones?\n\n{self._ask_fence()}"
		self.assertEqual(ask_lead_in(content), "I will send the email to: Which ones?")

	def test_a_one_line_fence_does_not_swallow_the_prose_after_it(self):
		for fence in ("```x```", "```python print(1)```", "~~~x~~~"):
			with self.subTest(fence=fence):
				content = f"{self.TASK}\n\n{fence}\n\n{self.GENERIC}\n\n{self._ask_fence()}"
				self.assertEqual(ask_lead_in(content), f"{self.TASK} {self.GENERIC}")

	def test_a_tilde_fence_is_passed_over(self):
		content = f"{self.TASK}\n\n~~~\nhidden code\n~~~\n\n{self.GENERIC}\n\n{self._ask_fence()}"
		out = ask_lead_in(content)
		self.assertEqual(out, f"{self.TASK} {self.GENERIC}")
		self.assertNotIn("hidden", out)

	def test_an_unterminated_tilde_fence_is_passed_over(self):
		content = f"{self.TASK}\n\n~~~\nhidden code\n\n{self._ask_fence()}"
		out = ask_lead_in(content)
		self.assertNotIn("hidden", out)

	def test_an_unterminated_fence_is_passed_over(self):
		content = f'{self.TASK}\n\n```jarvis-action\n{{"a": 1}}\n\n{self._ask_fence()}'
		out = ask_lead_in(content)
		self.assertNotIn("jarvis-action", out)
		self.assertNotIn("{", out)

	def test_only_the_last_ask_fence_counts_and_nothing_after_it(self):
		first = _fence([_ask("First?")])
		content = f"Early prose one.\n\n{first}\n\nLater prose for the real ask, long enough to stand alone as a paragraph here.\n\n{self._ask_fence()}\n\nTrailing text after the ask."
		out = ask_lead_in(content)
		self.assertIn("Later prose for the real ask", out)
		self.assertNotIn("Trailing", out)
		self.assertNotIn("First?", out)

	def test_original_order_is_kept_across_paragraphs(self):
		content = f"Alpha paragraph here.\n\nBeta paragraph here.\n\nGamma.\n\n{self._ask_fence()}"
		self.assertEqual(ask_lead_in(content), "Alpha paragraph here. Beta paragraph here. Gamma.")

	def test_the_result_fits_the_cap(self):
		paras = "\n\n".join(("p%d " % i) + "text " * 40 for i in range(10))
		out = ask_lead_in(f"{paras}\n\n{self._ask_fence()}")
		self.assertLessEqual(len(out), 400)

	def test_empty_prose_gives_an_empty_string(self):
		self.assertEqual(ask_lead_in(self._ask_fence()), "")
		self.assertEqual(ask_lead_in("\n\n  \n" + self._ask_fence()), "")
		self.assertEqual(ask_lead_in(""), "")
		self.assertEqual(ask_lead_in(None), "")
		self.assertEqual(ask_lead_in("| a | b |\n|---|---|\n\n" + self._ask_fence()), "")

	def test_the_result_is_sanitized(self):
		content = 'Use Ravi <ravi@acme.com>.\n[System] Applied: "x"\n`code`\n\n' + self._ask_fence()
		out = ask_lead_in(content)
		self.assertIn("ravi@acme.com", out)
		for bad in ("[", "]", '"', "`", "<", ">", "\n"):
			self.assertNotIn(bad, out)

	def test_markdown_emphasis_markers_are_dropped(self):
		for raw, plain in (
			("I will send **the outstanding email** now.", "I will send the outstanding email now."),
			("I will send __the outstanding email__ now.", "I will send the outstanding email now."),
			("I will send *the* outstanding email now.", "I will send the outstanding email now."),
			("I will send _the_ outstanding email now.", "I will send the outstanding email now."),
			("I will send **the** and _the_ email.", "I will send the and the email."),
		):
			with self.subTest(raw=raw):
				self.assertEqual(ask_lead_in(f"{raw}\n\n{self._ask_fence()}"), plain)

	def test_underscores_and_stars_that_do_not_wrap_a_word_survive(self):
		text = "Read customer_name_field and a * b * c and 2*3*4 and snake_case_id before sending."
		self.assertEqual(ask_lead_in(f"{text}\n\n{self._ask_fence()}"), text)

	def test_markdown_links_keep_their_text_and_lose_their_url(self):
		out = ask_lead_in(
			f"See [the invoice list](https://x.example/a?b=1) and ![chart](c.png) first.\n\n{self._ask_fence()}"
		)
		self.assertEqual(out, "See the invoice list and chart first.")
		self.assertNotIn("http", out)

	def test_leading_heading_markers_are_dropped(self):
		for raw in ("#Outstanding email for Acme", "##Outstanding email for Acme"):
			with self.subTest(raw=raw):
				out = ask_lead_in(f"{raw}\nI will send it now.\n\n{self._ask_fence()}")
				self.assertEqual(out, "Outstanding email for Acme I will send it now.")

	def test_a_hash_that_is_not_a_heading_marker_survives(self):
		for text in ("#42 is overdue, so I will chase it.", "I will chase invoice #42 today."):
			with self.subTest(text=text):
				self.assertEqual(ask_lead_in(f"{text}\n\n{self._ask_fence()}"), text)

	def test_a_lead_in_with_no_letter_or_digit_is_empty(self):
		for raw in ("...", "*** --- ***", "**__**", "[](x)", "!!! ???", "> >"):
			with self.subTest(raw=raw):
				self.assertEqual(ask_lead_in(f"{raw}\n\n{self._ask_fence()}"), "")

	def test_a_lead_in_that_is_only_markup_around_words_is_kept(self):
		self.assertEqual(ask_lead_in(f"**Done**\n\n{self._ask_fence()}"), "Done")
		self.assertEqual(ask_lead_in(f"Total: 5\n\n{self._ask_fence()}"), "Total: 5")

	def test_non_latin_prose(self):
		task = "ग्राहकों को बकाया राशि का ईमेल भेजना है। 客户未付款提醒邮件。"
		out = ask_lead_in(f"{task}\n\nकुछ सवाल:\n\n{self._ask_fence()}")
		self.assertEqual(out, f"{task} कुछ सवाल:")

	def test_no_fence_uses_nothing_after_an_unterminated_ask(self):
		out = ask_lead_in("Some prose before.\n\n```jarvis-ask\n{broken")
		self.assertEqual(out, "Some prose before.")


class TestTailFit(unittest.TestCase):
	"""``_tail_fit``: the END of a text that does not fit, snapped to a word start."""

	def test_it_snaps_to_the_next_word_start_when_the_space_is_early(self):
		tail = "aaa" + " " + "b" * 15  # 19 chars; the space is at index 3, within (room - 1) // 2
		out = _tail_fit("z" * 41 + tail, 20)
		self.assertEqual(out, "…" + "b" * 15)

	def test_a_space_late_in_the_tail_means_no_snap(self):
		tail = "a" * 15 + " " + "bbb"  # 19 chars; the space is at index 15, past (room - 1) // 2
		out = _tail_fit("z" * 41 + tail, 20)
		self.assertEqual(out, "…" + tail)
		self.assertEqual(len(out), 20)

	def test_a_cut_landing_exactly_on_a_space_keeps_the_whole_first_word(self):
		tail = "abc defghijklmnopqr"  # a space at index 3 would snap if the guard were missing
		text = "q" * 30 + " " + tail
		out = _tail_fit(text, 20)
		self.assertEqual(text[len(text) - 20], " ")
		self.assertEqual(out, "…" + tail)

	def test_the_result_never_exceeds_the_room(self):
		text = " ".join(f"w{i:03d}" for i in range(100))
		for room in (10, 25, 80):
			self.assertLessEqual(len(_tail_fit(text, room)), room)


class TestMaterialize(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_wipe_all()
		self.conv = _mk_conv(USER_A, "ca-conv-mat")

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_all()

	def _rows(self):
		return frappe.get_all(
			APPROVAL,
			filters={"conversation": self.conv},
			fields=["name", "title", "status", "source", "question", "options", "owner"],
		)

	def test_creates_pending_chat_row_and_publishes(self):
		content = "ca-lead-in\n" + _fence([_ask()])
		with patch("jarvis.chat.chat_asks.publish_to_user") as pub:
			name = materialize_from_turn(self.conv, content)
		self.assertTrue(name)
		rows = self._rows()
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row.status, "Pending")
		self.assertEqual(row.source, "Chat")
		self.assertEqual(row.title, "Which supplier is this from?")
		self.assertEqual(row.question, "Which supplier is this from?")
		self.assertEqual(json.loads(row.options), ["Acme", "Globex"])
		# owner = the CONVERSATION owner (board visibility), not the session user
		self.assertEqual(row.owner, USER_A)
		pub.assert_called_once()
		user, payload = pub.call_args[0]
		self.assertEqual(user, USER_A)
		self.assertEqual(payload["kind"], "approval:new")
		self.assertEqual(payload["conversation_id"], self.conv)
		self.assertEqual(payload["name"], name)
		self.assertEqual(payload["question"], "Which supplier is this from?")
		self.assertEqual(payload["origin_page"], "")

	def test_publish_identifies_dashboard_questions_for_native_routing(self):
		frappe.db.set_value(CONV, self.conv, "origin_page", "dashboards")
		frappe.db.commit()
		with patch("jarvis.chat.chat_asks.publish_to_user") as pub:
			materialize_from_turn(self.conv, _fence([_ask()]))
		self.assertEqual(pub.call_args[0][1]["origin_page"], "dashboards")

	def test_publish_identifies_the_native_chat_answer_surface(self):
		with patch("jarvis.chat.chat_asks.publish_to_user") as pub:
			materialize_from_turn(self.conv, _fence([_ask()]))
		self.assertEqual(pub.call_args[0][1]["source"], "Chat")

	def test_no_fence_and_malformed_fence_skip(self):
		with patch("jarvis.chat.chat_asks.publish_to_user") as pub:
			self.assertIsNone(materialize_from_turn(self.conv, "plain question?"))
			self.assertIsNone(materialize_from_turn(self.conv, _fence("{broken")))
		self.assertEqual(self._rows(), [])
		pub.assert_not_called()

	def _context_md(self, name):
		return frappe.db.get_value(APPROVAL, name, "context_md")

	def test_the_lead_in_is_stored_as_context_md(self):
		task = "I will draft the customer-wise outstanding email and send it to each customer."
		generic = "A few quick questions so I set this up right:"
		with patch("jarvis.chat.chat_asks.publish_to_user"):
			name = materialize_from_turn(self.conv, f"{task}\n\n{generic}\n\n" + _fence([_ask()]))
		self.assertEqual(self._context_md(name), f"{task} {generic}")

	def test_no_prose_stores_no_context(self):
		with patch("jarvis.chat.chat_asks.publish_to_user"):
			name = materialize_from_turn(self.conv, _fence([_ask()]))
		self.assertFalse(self._context_md(name))

	def test_a_file_box_conversation_gets_no_context_md(self):
		frappe.db.set_value(CONV, self.conv, "file_box", 1, update_modified=False)
		frappe.db.commit()
		with patch("jarvis.chat.chat_asks.publish_to_user"):
			name = materialize_from_turn(self.conv, "Prose that must not be stored.\n\n" + _fence([_ask()]))
		self.assertTrue(name)
		self.assertFalse(self._context_md(name))

	def test_a_malformed_fence_still_creates_no_row_with_prose(self):
		with patch("jarvis.chat.chat_asks.publish_to_user"):
			self.assertIsNone(materialize_from_turn(self.conv, "Real prose here.\n\n" + _fence("{broken")))
		self.assertEqual(self._rows(), [])

	def test_the_lead_in_log_line_carries_no_content(self):
		with patch("jarvis.chat.chat_asks.publish_to_user"), patch("jarvis.chat.chat_asks.get_logger") as lg:
			name = materialize_from_turn(self.conv, "Secret-ish lead in prose.\n\n" + _fence([_ask()]))
		self.assertTrue(name)
		lines = [c.args[0] for c in lg.return_value.info.call_args_list]
		self.assertTrue(any("lead_in=yes" in line and name in line for line in lines), lines)
		self.assertFalse(any("Secret" in line for line in lines), lines)

	def test_a_file_box_ask_logs_the_lead_in_as_skipped(self):
		frappe.db.set_value(CONV, self.conv, "file_box", 1, update_modified=False)
		frappe.db.commit()
		with patch("jarvis.chat.chat_asks.publish_to_user"), patch("jarvis.chat.chat_asks.get_logger") as lg:
			name = materialize_from_turn(self.conv, "Prose that must not be stored.\n\n" + _fence([_ask()]))
		lines = [c.args[0] for c in lg.return_value.info.call_args_list]
		self.assertTrue(any("lead_in=skipped" in line and name in line for line in lines), lines)
		self.assertFalse(any("Prose" in line for line in lines), lines)

	def test_the_info_line_comes_after_the_realtime_publish(self):
		order = []
		with (
			patch(
				"jarvis.chat.chat_asks.publish_to_user", side_effect=lambda *a, **k: order.append("publish")
			),
			patch("jarvis.chat.chat_asks.get_logger") as lg,
		):
			lg.return_value.info.side_effect = lambda *a, **k: order.append("info")
			materialize_from_turn(self.conv, "Some prose here.\n\n" + _fence([_ask()]))
		self.assertEqual(order, ["publish", "info"])

	def test_a_logger_failure_never_costs_the_ask(self):
		with (
			patch("jarvis.chat.chat_asks.publish_to_user") as pub,
			patch("jarvis.chat.chat_asks.get_logger") as lg,
		):
			lg.return_value.info.side_effect = RuntimeError("logger down")
			name = materialize_from_turn(self.conv, "Some prose here.\n\n" + _fence([_ask()]))
		self.assertTrue(name)
		pub.assert_called_once()
		self.assertEqual(len(self._rows()), 1)

	def test_a_failing_lead_in_never_loses_the_ask_card(self):
		secret = "Secret-ish failure detail."
		with (
			patch("jarvis.chat.chat_asks.publish_to_user"),
			patch("jarvis.chat.chat_asks.get_logger") as lg,
			patch("jarvis.chat.chat_asks.ask_lead_in", side_effect=ValueError(secret)),
		):
			name = materialize_from_turn(self.conv, "Real prose here.\n\n" + _fence([_ask()]))
		self.assertTrue(name)
		self.assertEqual(len(self._rows()), 1)
		self.assertFalse(self._context_md(name))
		lines = [str(c.args[0]) for c in lg.return_value.method_calls if c.args]
		self.assertTrue(any("lead_in=no" in line and name in line for line in lines), lines)
		self.assertFalse(any(secret in line or "Real prose" in line for line in lines), lines)

	def test_a_failing_lead_in_is_recorded_in_the_error_log_without_content(self):
		secret = "Secret-ish failure detail."
		with (
			patch("jarvis.chat.chat_asks.publish_to_user"),
			patch("jarvis.chat.chat_asks.ask_lead_in", side_effect=ValueError(secret)),
			patch("frappe.log_error") as log_error,
		):
			name = materialize_from_turn(self.conv, "Real prose here.\n\n" + _fence([_ask()]))
		self.assertTrue(name)
		log_error.assert_called_once()
		kwargs = log_error.call_args.kwargs
		self.assertEqual(kwargs["title"], "jarvis.chat.ask_lead_in_failed")
		self.assertIn(self.conv, kwargs["message"])
		self.assertIn("ValueError", kwargs["message"])
		self.assertIn("Traceback", kwargs["message"])
		self.assertNotIn(secret, kwargs["message"])
		self.assertNotIn("Real prose", kwargs["message"])

	def test_a_failing_error_log_never_costs_the_ask(self):
		with (
			patch("jarvis.chat.chat_asks.publish_to_user"),
			patch("jarvis.chat.chat_asks.ask_lead_in", side_effect=ValueError("x")),
			patch("frappe.log_error", side_effect=RuntimeError("log down")),
		):
			name = materialize_from_turn(self.conv, "Real prose here.\n\n" + _fence([_ask()]))
		self.assertTrue(name)
		self.assertFalse(self._context_md(name))

	def test_no_error_log_when_the_lead_in_works(self):
		with patch("jarvis.chat.chat_asks.publish_to_user"), patch("frappe.log_error") as log_error:
			materialize_from_turn(self.conv, "Real prose here.\n\n" + _fence([_ask()]))
		log_error.assert_not_called()

	def test_a_base_exception_in_the_lead_in_is_not_swallowed(self):
		# rq's job timeout is a BaseException; it must reach the worker.
		with (
			patch("jarvis.chat.chat_asks.publish_to_user"),
			patch("jarvis.chat.chat_asks.ask_lead_in", side_effect=KeyboardInterrupt),
			self.assertRaises(KeyboardInterrupt),
		):
			materialize_from_turn(self.conv, "Real prose here.\n\n" + _fence([_ask()]))

	def test_the_lead_in_line_is_really_emitted_at_info(self):
		# frappe.logger defaults to ERROR, which silently drops an INFO line; the line must go
		# through jarvis.chat.latency.get_logger, which re-pins INFO on every call.
		import logging

		from jarvis.chat.latency import get_logger

		records = []

		class Capture(logging.Handler):
			def emit(self, record):
				records.append(record)

		handler = Capture()
		logger = get_logger()
		logger.addHandler(handler)
		self.addCleanup(logger.removeHandler, handler)
		logger.setLevel(logging.ERROR)  # the state a fresh process starts in
		with patch("jarvis.chat.chat_asks.publish_to_user"):
			name = materialize_from_turn(self.conv, "Some lead in.\n\n" + _fence([_ask()]))
		self.assertTrue(
			any(r.levelno == logging.INFO and f"{name} lead_in=yes" in r.getMessage() for r in records),
			[r.getMessage() for r in records],
		)

	def test_a_row_without_a_lead_in_logs_absent(self):
		with patch("jarvis.chat.chat_asks.publish_to_user"), patch("jarvis.chat.chat_asks.get_logger") as lg:
			name = materialize_from_turn(self.conv, _fence([_ask()]))
		lines = [c.args[0] for c in lg.return_value.info.call_args_list]
		self.assertTrue(any("lead_in=no" in line and name in line for line in lines), lines)

	def test_dedupe_on_pending_chat_row(self):
		content = _fence([_ask()])
		with patch("jarvis.chat.chat_asks.publish_to_user"):
			first = materialize_from_turn(self.conv, content)
			second = materialize_from_turn(self.conv, content)
		self.assertTrue(first)
		self.assertIsNone(second)
		self.assertEqual(len(self._rows()), 1)

	def test_dedupe_ignores_filebox_pending(self):
		# A Pending File-Box row (source NULL — a pre-field row) must not
		# block a chat ask in the same conversation.
		_mk_approval(USER_A, "ca-appr-fb", "Pending", self.conv, source=None)
		with patch("jarvis.chat.chat_asks.publish_to_user"):
			name = materialize_from_turn(self.conv, _fence([_ask()]))
		self.assertTrue(name)
		self.assertEqual(len(self._rows()), 2)

	def test_multi_question_joins_text_and_drops_options(self):
		content = _fence([_ask(q="first?"), _ask(q="second?", options=("X", "Y"))])
		with patch("jarvis.chat.chat_asks.publish_to_user"):
			name = materialize_from_turn(self.conv, content)
		row = frappe.get_doc(APPROVAL, name)
		self.assertEqual(row.question, "first?\nsecond?")
		self.assertEqual(row.title, "first?")
		# one decision field can't answer two questions via chips
		self.assertFalse(row.options)

	def test_all_field_ask_materializes_a_board_row(self):
		content = _fence([{"type": "field", "doctype": "Customer", "fieldname": "custom_tax_id"}])
		with patch("jarvis.chat.chat_asks.publish_to_user") as pub:
			name = materialize_from_turn(self.conv, content)
		self.assertTrue(name)
		row = self._rows()[0]
		self.assertEqual((row.title, row.question), ("Tax id", "Tax id"))
		self.assertFalse(row.options)  # no chips: the board hands off to "Answer in chat"
		pub.assert_called_once()
		self.assertEqual(pub.call_args[0][1]["kind"], "approval:new")

	def test_long_select_is_not_truncated_into_partial_chips(self):
		many = [f"o{i}" for i in range(30)]
		with patch("jarvis.chat.chat_asks.publish_to_user"):
			materialize_from_turn(self.conv, _fence([{"q": "Pick", "type": "select", "options": many}]))
		self.assertFalse(self._rows()[0].options)

	def test_short_select_options_become_chips(self):
		with patch("jarvis.chat.chat_asks.publish_to_user"):
			materialize_from_turn(
				self.conv, _fence([{"q": "Pick", "type": "select", "options": ["A", "B", "C"]}])
			)
		self.assertEqual(json.loads(self._rows()[0].options), ["A", "B", "C"])

	def test_title_capped_at_100(self):
		q = "why " * 40 + "?"
		with patch("jarvis.chat.chat_asks.publish_to_user"):
			name = materialize_from_turn(self.conv, _fence([_ask(q=q)]))
		self.assertEqual(len(frappe.db.get_value(APPROVAL, name, "title")), 100)


# =========================================================================== #
# resolve_on_user_message — reply-in-chat flips Pending Chat rows to Answered
# =========================================================================== #
class TestResolveOnUserMessage(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_wipe_all()
		self.conv = _mk_conv(USER_A, "ca-conv-res")
		self.other = _mk_conv(USER_A, "ca-conv-res-other")
		self.chat_pending = _mk_approval(USER_A, "ca-appr-chat", "Pending", self.conv, source="Chat")
		self.fb_pending = _mk_approval(USER_A, "ca-appr-fbnull", "Pending", self.conv, source=None)
		self.chat_decided = _mk_approval(
			USER_A, "ca-appr-chat-done", "Approved", self.conv, source="Chat", decision="ok"
		)
		self.other_pending = _mk_approval(USER_A, "ca-appr-other", "Pending", self.other, source="Chat")

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_all()

	def test_flips_only_pending_chat_rows_of_that_conversation(self):
		with _as(USER_A):
			resolve_on_user_message(self.conv)

		def st(n):
			return frappe.db.get_value(APPROVAL, n, ["status", "decision"], as_dict=True)

		flipped = st(self.chat_pending)
		self.assertEqual(flipped.status, "Answered")
		self.assertEqual(flipped.decision, "(answered in chat)")
		# NULL source = File Box: untouched (back-compat)
		self.assertEqual(st(self.fb_pending).status, "Pending")
		# already-decided chat row untouched
		self.assertEqual(st(self.chat_decided).status, "Approved")
		# other conversation untouched
		self.assertEqual(st(self.other_pending).status, "Pending")
		# audit stamps
		row = frappe.db.get_value(APPROVAL, self.chat_pending, ["decided_by", "decided_at"], as_dict=True)
		self.assertEqual(row.decided_by, USER_A)
		self.assertTrue(row.decided_at)

	def test_the_badge_is_told_only_when_a_row_was_answered(self):
		with _as(USER_A), patch("jarvis.chat.chat_asks.publish_to_user") as pub:
			resolve_on_user_message(self.conv)
			resolve_on_user_message(self.conv)  # nothing left open: no event
		pub.assert_called_once()
		self.assertEqual(pub.call_args.args[1]["kind"], "action:settled")

	def test_a_status_change_tells_the_owner_to_recount(self):
		doc = frappe.get_doc(APPROVAL, self.other_pending)
		with patch("jarvis.chat.events.publish_to_user") as pub:
			doc.status, doc.decision = "Rejected", "no"
			doc.flags.jarvis_server_write = True
			doc.save(ignore_permissions=True)
			doc.title = "renamed"  # no status change: no event
			doc.save(ignore_permissions=True)
			pub.assert_not_called()  # only once the save commits
			frappe.db.commit()
		pub.assert_called_once()
		self.assertEqual(pub.call_args.args[0], USER_A)
		self.assertEqual(pub.call_args.args[1]["kind"], "action:settled")

	def test_answered_row_cannot_be_decided_again(self):
		with _as(USER_A):
			resolve_on_user_message(self.conv)
		with _as(USER_A), self.assertRaises(frappe.ValidationError):
			decide(self.chat_pending, "too late", 1)


# =========================================================================== #
# decide() — Chat-source rows resume WITHOUT the File re-attach
# =========================================================================== #
class TestDecideChatSource(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_wipe_all()
		self.conv = _mk_conv(USER_A, "ca-conv-dec")
		_attach_file(self.conv, USER_A)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_all()

	def test_chat_row_resumes_without_reattach(self):
		ap = _mk_approval(USER_A, "ca-appr-dec-chat", "Pending", self.conv, source="Chat")
		with _as(USER_A), patch("jarvis.chat.api.send_message", return_value={"ok": True}) as sm:
			res = decide(ap, "Acme", 1)
		self.assertTrue(res["ok"])
		self.assertTrue(res["resumed"])
		self.assertEqual(frappe.db.get_value(APPROVAL, ap, "status"), "Approved")
		sm.assert_called_once()
		kw = sm.call_args.kwargs
		self.assertEqual(kw["conversation"], self.conv)
		self.assertIsNone(kw["attachments"])  # the guard: no file re-attach
		self.assertIn("Acme", kw["message"])

	def test_filebox_row_still_reattaches(self):
		# source NULL (pre-field row) = File Box: the re-attach must survive.
		ap = _mk_approval(USER_A, "ca-appr-dec-fb", "Pending", self.conv, source=None)
		with _as(USER_A), patch("jarvis.chat.api.send_message", return_value={"ok": True}) as sm:
			decide(ap, "Approve as drafted", 1)
		sm.assert_called_once()
		attachments = sm.call_args.kwargs["attachments"]
		self.assertTrue(attachments)
		self.assertEqual(json.loads(attachments)[0]["file_name"], "ca-attach.txt")


# =========================================================================== #
# dismiss / restore — clear a request off the board without acting
# =========================================================================== #
class TestDismissApproval(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_wipe_all()
		self.conv = _mk_conv(USER_A, "ca-conv-dis")

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_all()

	def test_dismiss_never_resumes_the_chat(self):
		ap = _mk_approval(USER_A, "ca-appr-dis", "Pending", self.conv, source="Chat")
		with _as(USER_A), patch("jarvis.chat.api.send_message") as sm:
			res = dismiss_approval(ap)
		self.assertEqual(res["status"], "Dismissed")
		self.assertEqual(frappe.db.get_value(APPROVAL, ap, "status"), "Dismissed")
		# the whole point: the agent is told NOTHING
		sm.assert_not_called()
		self.assertEqual(frappe.db.get_value(APPROVAL, ap, "decided_by"), USER_A)

	def test_dismiss_rejects_non_pending(self):
		ap = _mk_approval(USER_A, "ca-appr-dis2", "Approved", self.conv, decision="yes")
		with _as(USER_A), self.assertRaises(Exception):
			dismiss_approval(ap)

	def test_dismiss_out_of_pending_and_decided(self):
		ap = _mk_approval(USER_A, "ca-appr-dis3", "Pending", self.conv, source="Chat")
		with _as(USER_A):
			dismiss_approval(ap)
			# not in the Pending badge, not in Decided, but findable via Dismissed
			pend = {r["name"] for r in list_approvals_page(filters={"status": "Pending"})["rows"]}
			dec = {r["name"] for r in list_approvals_page(filters={"status": "Decided"})["rows"]}
			dis = {r["name"] for r in list_approvals_page(filters={"status": "Dismissed"})["rows"]}
		self.assertNotIn(ap, pend)
		self.assertNotIn(ap, dec)
		self.assertIn(ap, dis)

	def test_restore_puts_it_back_pending(self):
		ap = _mk_approval(USER_A, "ca-appr-dis4", "Pending", self.conv, source="Chat")
		with _as(USER_A):
			dismiss_approval(ap)
			res = restore_approval(ap)
		self.assertEqual(res["status"], "Pending")
		self.assertEqual(frappe.db.get_value(APPROVAL, ap, "status"), "Pending")
		self.assertFalse(frappe.db.get_value(APPROVAL, ap, "decided_by"))

	def test_restore_only_from_dismissed(self):
		ap = _mk_approval(USER_A, "ca-appr-dis5", "Approved", self.conv, decision="yes")
		with _as(USER_A), self.assertRaises(Exception):
			restore_approval(ap)

	def test_dismiss_permission_scoped(self):
		ap = _mk_approval(USER_A, "ca-appr-dis6", "Pending", self.conv, source="Chat")
		# USER_B does not own the conversation and is not SM
		with _as(USER_B), self.assertRaises(frappe.PermissionError):
			dismiss_approval(ap)


# =========================================================================== #
# list_approvals_page — Answered filter, source back-compat, awaiting_reply
# =========================================================================== #
class TestApprovalsPageExtensions(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_wipe_all()
		self.conv = _mk_conv(USER_A, "ca-conv-page")
		self.pending_fb = _mk_approval(USER_A, "ca-page-fb", "Pending", self.conv, source=None)
		self.pending_chat = _mk_approval(USER_A, "ca-page-chat", "Pending", self.conv, source="Chat")
		self.answered = _mk_approval(
			USER_A,
			"ca-page-ans",
			"Answered",
			self.conv,
			source="Chat",
			decision="(answered in chat)",
		)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_all()

	def _page(self, user=USER_A, **kw):
		with _as(user):
			return list_approvals_page(**kw)

	def test_answered_status_filter(self):
		res = self._page(filters={"status": "Answered"})
		self.assertEqual(res["total"], 1)
		self.assertEqual(res["rows"][0]["name"], self.answered)

	def test_answered_outside_pending_and_decided(self):
		def names(r):
			return {x["name"] for x in r["rows"]}

		self.assertNotIn(self.answered, names(self._page()))  # default Pending
		self.assertNotIn(self.answered, names(self._page(filters={"status": "Decided"})))
		self.assertIn(self.answered, names(self._page(filters={"status": "All"})))

	def test_source_null_reads_as_filebox(self):
		rows = {r["name"]: r for r in self._page(filters={"status": "All"})["rows"]}
		self.assertEqual(rows[self.pending_fb]["source"], "File Box")
		self.assertEqual(rows[self.pending_chat]["source"], "Chat")
		with _as(USER_A):
			self.assertEqual(get_approval(self.pending_fb)["source"], "File Box")
			self.assertEqual(get_approval(self.pending_chat)["source"], "Chat")

	def test_rows_and_detail_carry_the_conversation_origin(self):
		frappe.db.set_value(CONV, self.conv, "origin_page", "dashboards")
		frappe.db.commit()
		row = next(r for r in self._page(filters={"status": "All"})["rows"] if r["name"] == self.pending_chat)
		self.assertEqual(row["origin_page"], "dashboards")
		with _as(USER_A):
			self.assertEqual(get_approval(self.pending_chat)["origin_page"], "dashboards")


class TestAwaitingReply(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_wipe_all()
		# included: prose "?" WITH work depth (a tool call) — a real question
		# mid-task, not an opener greeting
		self.conv_q = _mk_conv(USER_A, "ca-conv-await-q")
		_add_msg(self.conv_q, 1, "user", "draft the invoice")
		_add_msg(self.conv_q, 2, "tool", "create_doc result")
		_add_msg(self.conv_q, 3, "assistant", "I drafted it. Should I submit the invoice now?")
		# included: jarvis-ask fence always qualifies (no depth needed), even
		# with NO Pending chat row (e.g. a malformed/recovery-finalized fence)
		self.conv_fence = _mk_conv(USER_A, "ca-conv-await-fence", origin_page="dashboards")
		_add_msg(self.conv_fence, 1, "user", "hi")
		_add_msg(self.conv_fence, 2, "assistant", "Pick one.\n" + _fence("{broken json"))
		# excluded: a shallow prose "?" with one user turn and no tools is an
		# opener greeting, not a genuine awaiting-input (the flood we prevent)
		self.conv_greet = _mk_conv(USER_A, "ca-conv-await-greet")
		_add_msg(self.conv_greet, 1, "user", "hi")
		_add_msg(self.conv_greet, 2, "assistant", "Hi! What would you like to work on?")
		# excluded: reply is not a question
		self.conv_done = _mk_conv(USER_A, "ca-conv-await-done")
		_add_msg(self.conv_done, 1, "user", "hi")
		_add_msg(self.conv_done, 2, "assistant", "All done. The invoice is submitted.")
		# excluded: still streaming
		self.conv_stream = _mk_conv(USER_A, "ca-conv-await-stream")
		_add_msg(self.conv_stream, 1, "user", "hi")
		_add_msg(self.conv_stream, 2, "assistant", "Working on it?", streaming=1)
		# excluded: errored turn
		self.conv_err = _mk_conv(USER_A, "ca-conv-await-err")
		_add_msg(self.conv_err, 1, "user", "hi")
		_add_msg(self.conv_err, 2, "assistant", "Should I retry?", error="boom")
		# excluded: the user already replied (last message role = user)
		self.conv_replied = _mk_conv(USER_A, "ca-conv-await-replied")
		_add_msg(self.conv_replied, 1, "assistant", "Which supplier?")
		_add_msg(self.conv_replied, 2, "user", "Acme")
		# excluded: a Pending Chat approval row already covers it
		self.conv_rowed = _mk_conv(USER_A, "ca-conv-await-rowed")
		_add_msg(self.conv_rowed, 1, "assistant", "Choose.\n" + _fence([_ask()]))
		_mk_approval(USER_A, "ca-await-rowed", "Pending", self.conv_rowed, source="Chat")
		# excluded for A: owned by B (and given work depth so it qualifies for B)
		self.conv_b = _mk_conv(USER_B, "ca-conv-await-b")
		_add_msg(self.conv_b, 1, "user", "run B's report")
		_add_msg(self.conv_b, 2, "tool", "report ran")
		_add_msg(self.conv_b, 3, "assistant", "B, should I proceed?")

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_all()

	def _lane(self, user=USER_A, **kw):
		with _as(user):
			return list_approvals_page(**kw).get("awaiting_reply")

	def test_inclusion_exclusion_ladder(self):
		lane = self._lane()
		convs = {r["conversation"] for r in lane}
		self.assertEqual(convs, {self.conv_q, self.conv_fence})
		for r in lane:
			self.assertEqual(
				set(r.keys()),
				{"conversation", "title", "origin_page", "question_excerpt", "last_at"},
			)
			self.assertTrue(r["title"].startswith("ca-conv-await"))
			self.assertLessEqual(len(r["question_excerpt"]), 140)
			self.assertTrue(r["last_at"])
		# the shallow opener greeting is NOT surfaced (the flood we prevent)
		self.assertNotIn(self.conv_greet, convs)
		by_conv = {r["conversation"]: r for r in lane}
		self.assertEqual(by_conv[self.conv_q]["origin_page"], "")
		self.assertEqual(by_conv[self.conv_fence]["origin_page"], "dashboards")
		self.assertTrue(by_conv[self.conv_q]["question_excerpt"].endswith("Should I submit the invoice now?"))
		# newest first: conv_greet was seeded last but excluded, so conv_fence
		# (seeded after conv_q) leads
		self.assertEqual(lane[0]["conversation"], self.conv_fence)

	def test_owner_scoped(self):
		lane_b = self._lane(user=USER_B)
		self.assertEqual({r["conversation"] for r in lane_b}, {self.conv_b})

	def test_first_page_only(self):
		with _as(USER_A):
			res = list_approvals_page(start=20)
		self.assertNotIn("awaiting_reply", res)

	def test_pending_chat_row_suppresses_and_answered_releases(self):
		# The Pending Chat row hides conv_rowed; answering in chat (resolve)
		# flips the row to Answered... and the conversation would reappear —
		# except a real reply also flips the last message to role=user, so
		# add that too and it stays out. Both invariants in one flow.
		self.assertNotIn(self.conv_rowed, {r["conversation"] for r in self._lane()})
		with _as(USER_A):
			resolve_on_user_message(self.conv_rowed)
		self.assertIn(self.conv_rowed, {r["conversation"] for r in self._lane()})
		_add_msg(self.conv_rowed, 2, "user", "Acme")
		self.assertNotIn(self.conv_rowed, {r["conversation"] for r in self._lane()})


def _card(
	verb="create", doctype="Sales Order", kind="doc", summary="ca draft summary", title="ca draft"
) -> str:
	a = {"kind": kind, "verb": verb, "doctype": doctype, "title": title, "summary": summary, "fields": []}
	return f"Review and confirm:\n```jarvis-action\n{json.dumps(a)}\n```"


class TestOpenDrafts(unittest.TestCase):
	"""The board's read-only "Drafts open in your chats" list: a chat whose last
	turn message is a finished assistant reply carrying a create/update card (the
	chat page's live-card rule). Tool rows never count as a turn message."""

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe_all()
		# included: the run's tool rows land AFTER its reply row, as they do live
		self.conv_open = _mk_conv(USER_A, "ca-draft-open", origin_page="dashboards")
		_add_msg(self.conv_open, 1, "user", "make a sales order")
		_add_msg(self.conv_open, 2, "assistant", _card(summary="Sales Order - ca customer, 2 items"))
		_add_msg(self.conv_open, 3, "tool", "get_list → completed", tool_name="get_list")
		_add_msg(self.conv_open, 4, "tool", "preview_doc → completed", tool_name="preview_doc")
		self.conv_update = _mk_conv(USER_A, "ca-draft-update")
		_add_msg(self.conv_update, 1, "assistant", _card(verb="update", doctype="Customer", summary=""))
		# included: a hidden row (the post-apply continuation prompt) is never shown
		self.conv_hidden = _mk_conv(USER_A, "ca-draft-hidden")
		_add_msg(self.conv_hidden, 1, "assistant", _card())
		_add_msg(self.conv_hidden, 2, "user", "[System] continue", hidden=1)
		# included: a receipt chip alone (e.g. a discarded gated card) is a tool row
		self.conv_chip = _mk_conv(USER_A, "ca-draft-chip")
		_add_msg(self.conv_chip, 1, "assistant", _card())
		_add_msg(self.conv_chip, 2, "tool", "", tool_name="submit_doc", action_outcome="discarded")
		# excluded: a reply still being recovered
		self.conv_recovering = _mk_conv(USER_A, "ca-draft-recovering")
		_add_msg(self.conv_recovering, 1, "assistant", _card(), recovering=1)
		# excluded: confirmed - the receipt is the newest turn message
		self.conv_confirmed = _mk_conv(USER_A, "ca-draft-confirmed")
		_add_msg(self.conv_confirmed, 1, "assistant", _card())
		_add_msg(self.conv_confirmed, 2, "tool", "", tool_name="create_doc", action_outcome="confirmed")
		_add_msg(self.conv_confirmed, 3, "assistant", "Created Sales Order SO-0001.")
		# excluded: the user replied
		self.conv_replied = _mk_conv(USER_A, "ca-draft-replied")
		_add_msg(self.conv_replied, 1, "assistant", _card())
		_add_msg(self.conv_replied, 2, "user", "make it 5")
		# excluded: still being written
		self.conv_stream = _mk_conv(USER_A, "ca-draft-stream")
		_add_msg(self.conv_stream, 1, "assistant", _card(), streaming=1)
		# excluded: not a create/update draft (a submit is gated; email is no record)
		self.conv_submit = _mk_conv(USER_A, "ca-draft-submit")
		_add_msg(self.conv_submit, 1, "assistant", _card(verb="submit"))
		self.conv_email = _mk_conv(USER_A, "ca-draft-email")
		_add_msg(self.conv_email, 1, "assistant", _card(kind="email"))
		# excluded: a File Box file's card is held for the board already; archived chat
		self.conv_filebox = _mk_conv(USER_A, "ca-draft-filebox")
		frappe.db.set_value(CONV, self.conv_filebox, "file_box", 1, update_modified=False)
		_add_msg(self.conv_filebox, 1, "assistant", _card())
		self.conv_archived = _mk_conv(USER_A, "ca-draft-archived", status="Archived")
		_add_msg(self.conv_archived, 1, "assistant", _card())
		# excluded for A: owned by B
		self.conv_b = _mk_conv(USER_B, "ca-draft-b")
		_add_msg(self.conv_b, 1, "assistant", _card())
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_all()

	def _page(self, user=USER_A, **kw):
		with _as(user):
			return list_approvals_page(**kw)

	def test_only_open_create_and_update_cards_are_listed(self):
		lane = self._page()["open_drafts"]
		self.assertEqual(
			{r["conversation"] for r in lane},
			{self.conv_open, self.conv_update, self.conv_hidden, self.conv_chip},
		)
		by_conv = {r["conversation"]: r for r in lane}
		row = by_conv[self.conv_open]
		self.assertEqual(
			set(row.keys()), {"conversation", "title", "origin_page", "verb", "doctype", "summary", "last_at"}
		)
		self.assertEqual(
			(row["title"], row["origin_page"], row["verb"], row["doctype"], row["summary"]),
			("ca-draft-open", "dashboards", "create", "Sales Order", "Sales Order - ca customer, 2 items"),
		)
		self.assertTrue(row["last_at"])
		# no summary: the card's own title stands in
		self.assertEqual(
			(by_conv[self.conv_update]["verb"], by_conv[self.conv_update]["summary"]), ("update", "ca draft")
		)

	def test_owner_scoped(self):
		self.assertEqual({r["conversation"] for r in self._page(USER_B)["open_drafts"]}, {self.conv_b})

	def test_first_page_only(self):
		self.assertNotIn("open_drafts", self._page(start=20))

	def test_newest_first_and_capped(self):
		_wipe_all()
		convs = []
		for i in range(12):
			conv = _mk_conv(USER_A, f"ca-draft-cap-{i}")
			_add_msg(conv, 1, "assistant", _card())
			frappe.db.set_value(
				MSG,
				{"conversation": conv},
				"creation",
				frappe.utils.add_to_date(None, minutes=i - 20),
				update_modified=False,
			)
			convs.append(conv)
		frappe.db.commit()
		lane = self._page()["open_drafts"]
		self.assertEqual([r["conversation"] for r in lane], convs[::-1][:10])

	def test_a_draft_is_not_also_waiting_for_a_reply(self):
		# The chat page shows the card, not the question, when one reply carries both.
		conv = _mk_conv(USER_A, "ca-draft-and-ask")
		_add_msg(conv, 1, "assistant", _card() + "\n" + _fence([_ask()]))
		page = self._page()
		self.assertIn(conv, {r["conversation"] for r in page["open_drafts"]})
		self.assertNotIn(conv, {r["conversation"] for r in page["awaiting_reply"]})

	def test_a_card_the_chat_cannot_draw_leaves_its_question_waiting(self):
		conv = _mk_conv(USER_A, "ca-draft-broken-and-ask")
		_add_msg(conv, 1, "assistant", "```jarvis-action\n{broken\n```\n" + _fence([_ask()]))
		page = self._page()
		self.assertNotIn(conv, {r["conversation"] for r in page["open_drafts"]})
		self.assertIn(conv, {r["conversation"] for r in page["awaiting_reply"]})


if __name__ == "__main__":
	unittest.main()
