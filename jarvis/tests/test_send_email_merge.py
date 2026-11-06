"""send_email recipients_from: the bench resolves a mail-merge's recipients from a query,
so the model never types an address (6 Nov 2026: 3 of 171 typed addresses were wrong).
Never queues real email: email.make, sendmail and enqueue are patched."""

from unittest import TestCase
from unittest.mock import MagicMock, patch

from jarvis.chat.confirm_card import build_card
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import _email_merge
from jarvis.tools.send_email import send_email

SPEC = {
	"from": "Customer",
	"alias": "cu",
	"joins": [
		{
			"type": "inner",
			"doctype": "Contact",
			"alias": "co",
			"on": {"co.name": "cu.customer_primary_contact"},
		}
	],
	"select": ["cu.name", "co.full_name", "co.email_id"],
}
SRC = {"spec": SPEC, "email_field": "email_id"}


def _rows(n, **over):
	rows = [
		{"name": f"CUST-{i:03d}", "full_name": f"Person {i}", "email_id": f"p{i}@example.invalid"}
		for i in range(n)
	]
	for i, row in over.items():
		rows[int(i[1:])].update(row)
	return rows


class _Base(TestCase):
	def setUp(self):
		def start(target, **kwargs):
			p = patch(target, **kwargs)
			self.addCleanup(p.stop)
			return p.start()

		self.rows = _rows(3)
		self.masked = {}
		self.limited = False
		self.query_rows = start(
			"jarvis.tools.query.query_rows",
			side_effect=lambda spec, max_rows: ([dict(r) for r in self.rows], self.masked, self.limited),
		)
		self.db = start("frappe.db", new=MagicMock())
		self.db.exists.return_value = True
		start("frappe.has_permission", return_value=True)
		self.readable = None  # None: every row's name is a readable record
		start(
			"frappe.get_list",
			side_effect=lambda dt, filters, pluck, limit: [
				n for n in filters["name"][1] if self.readable is None or n in self.readable
			],
		)
		self.make = start("frappe.core.doctype.communication.email.make", return_value={"name": "MOCK-COMM"})
		start("frappe.sendmail", side_effect=AssertionError("real email forbidden"))
		start("frappe.enqueue", side_effect=AssertionError("queueing forbidden"))

	def call(self, **over):
		return {
			"recipients_from": SRC,
			"subject": "Diwali wishes, {{ full_name }}",
			"content": "Dear {{ full_name }},\nHappy Diwali.",
			**over,
		}


class TestBuildMessages(_Base):
	def test_one_message_per_row_filled_and_referenced(self):
		stored, note = _email_merge.expand(self.call())
		self.assertEqual(len(stored["messages"]), 3)
		first = stored["messages"][0]
		self.assertEqual(first["recipients"], "p0@example.invalid")
		self.assertEqual(first["doctype"], "Customer")
		self.assertEqual(first["name"], "CUST-000")
		self.assertEqual(first["subject"], "Diwali wishes, Person 0")
		self.assertEqual(first["content"], "Dear Person 0,\nHappy Diwali.")
		self.assertEqual(note, {"rows": 3, "no_email": 0, "duplicate": 0, "limited": False})

	def test_the_callers_dict_is_left_as_it_was(self):
		call = self.call()
		_email_merge.expand(call)
		self.assertNotIn("messages", call)

	def test_invalid_blank_and_repeated_addresses_are_left_out_and_counted(self):
		self.rows = _rows(
			5, r1={"email_id": ""}, r2={"email_id": "not an address"}, r4={"email_id": "P0@example.invalid"}
		)
		stored, note = _email_merge.expand(self.call())
		self.assertEqual([m["name"] for m in stored["messages"]], ["CUST-000", "CUST-003"])
		self.assertEqual(note["no_email"], 2)
		self.assertEqual(note["duplicate"], 1)

	def test_a_result_id_resolves_to_its_saved_spec(self):
		with patch("jarvis.tools._export.saved_queries.resolve", return_value=SPEC) as resolve:
			stored, _ = _email_merge.expand(
				self.call(recipients_from={"result_id": "q_abc", "email_field": "email_id"})
			)
		resolve.assert_called_once_with("q_abc")
		self.assertEqual(len(stored["messages"]), 3)

	def test_refusals(self):
		cases = {
			"exactly one": {"recipients_from": {"email_field": "email_id"}},
			"exactly one of result_id or spec": {"recipients_from": {**SRC, "result_id": "q_x"}},
			"email_field is required": {"recipients_from": {"spec": SPEC}},
			"does not take": {"recipients_from": {**SRC, "to": "x"}},
			"does not select 'phone'": {"recipients_from": {**SRC, "email_field": "phone"}},
			"not columns of the query": {"content": "Dear {{ first_name }}"},
			"recipients or recipients_from": {"recipients": "a@example.invalid"},
			"messages or recipients_from": {"messages": [{"recipients": "typed@example.invalid"}]},
		}
		for words, over in cases.items():
			with self.subTest(words), self.assertRaisesRegex(InvalidArgumentError, words):
				_email_merge.expand(self.call(**over))

	def test_a_masked_email_column_is_refused(self):
		self.masked = {"co": {"email_id": object()}}
		with self.assertRaisesRegex(InvalidArgumentError, "masked"):
			_email_merge.expand(self.call())

	def test_a_blank_placeholder_value_is_refused_not_sent_as_dear_comma(self):
		self.rows = _rows(3, r1={"full_name": ""})
		with self.assertRaisesRegex(InvalidArgumentError, "1 recipient.*no full_name"):
			_email_merge.expand(self.call())

	def test_a_name_that_is_not_the_reference_record_is_refused_at_the_card(self):
		# SELECT cu.name, co.name keeps the Contact's name: caught before any card, not after Confirm
		self.readable = {"CUST-000", "CUST-002"}
		with self.assertRaisesRegex(InvalidArgumentError, "1 of 3 'name' values are not Customer records"):
			_email_merge.expand(self.call())

	def test_no_valid_address_at_all(self):
		self.rows = _rows(2, r0={"email_id": ""}, r1={"email_id": None})
		with self.assertRaisesRegex(InvalidArgumentError, "no row has a valid email"):
			_email_merge.expand(self.call())


class TestSend(_Base):
	def test_the_card_list_is_sent_past_the_typed_batch_cap(self):
		self.rows = _rows(25)
		stored, _ = _email_merge.expand(self.call())
		result = send_email(**{k: v for k, v in stored.items()})
		self.assertEqual(result["count"], 25)
		self.assertEqual(self.make.call_count, 25)
		self.assertEqual(self.make.call_args_list[7].kwargs["recipients"], "p7@example.invalid")

	def test_a_changed_list_since_the_card_sends_nothing(self):
		stored, _ = _email_merge.expand(self.call())
		self.rows = _rows(3, r2={"email_id": "someone-else@example.invalid"})
		with self.assertRaisesRegex(InvalidArgumentError, "changed since the confirmation card"):
			send_email(**stored)
		self.make.assert_not_called()

	def test_hand_made_messages_cannot_ride_recipients_from(self):
		stored, _ = _email_merge.expand(self.call())
		stored["messages"][0]["recipients"] = "typo@example.invalid"
		with self.assertRaisesRegex(InvalidArgumentError, "changed since"):
			send_email(**stored)
		self.make.assert_not_called()

	def test_an_uncarded_run_sends_what_the_query_gives(self):
		result = send_email(**self.call())
		self.assertEqual(result["count"], 3)

	def test_an_uncarded_run_keeps_the_typed_cap_of_20(self):
		self.rows = _rows(21)
		with self.assertRaisesRegex(InvalidArgumentError, "without a confirmation card at most 20"):
			send_email(**self.call())
		self.make.assert_not_called()

	def test_typed_messages_keep_the_cap_of_20(self):
		typed = [
			{
				"doctype": "Customer",
				"name": f"C{i}",
				"recipients": f"t{i}@example.invalid",
				"subject": "s",
				"content": "c",
			}
			for i in range(21)
		]
		with self.assertRaisesRegex(InvalidArgumentError, "max 20"):
			send_email(messages=typed)
		self.make.assert_not_called()


class TestCard(_Base):
	def test_the_expanded_call_shows_as_a_bulk_email_card(self):
		self.rows = _rows(171)
		stored, _ = _email_merge.expand(self.call())
		card = build_card("send_email", stored, {"described": True})
		self.assertEqual(card["kind"], "bulk_email")
		self.assertEqual(card["count"], 171)
		self.assertEqual(len(card["messages"]), 20)
		self.assertEqual(card["extra"], 151)
		self.assertEqual(card["messages"][0]["recipients"], "p0@example.invalid")
		self.assertIn("Dear Person 0", card["messages"][0]["body"])
		# every address Confirm will email, not only the 20 shown in full
		self.assertEqual(len(card["all_recipients"]), 171)
		self.assertEqual(card["all_recipients"][170], "p170@example.invalid")


class TestQueryRows(TestCase):
	def _run(self, spec, n, max_rows):
		q = MagicMock()
		q.limit.return_value.run.return_value = [{"name": str(i)} for i in range(n)]
		with (
			patch("jarvis.tools.query.compile_spec", return_value=q),
			patch("jarvis.tools.query._masking_for_rows", return_value=([], {})),
		):
			from jarvis.tools.query import query_rows

			return query_rows(spec, max_rows=max_rows), q

	def test_more_rows_than_the_cap_is_refused_never_cut(self):
		with self.assertRaisesRegex(InvalidArgumentError, "more than 5 rows"):
			self._run({"from": "Customer"}, 6, 5)

	def test_the_specs_own_limit_applies_and_says_it_cut(self):
		(rows, _masked, limited), q = self._run({"from": "Customer", "limit": 3}, 4, 500)
		q.limit.assert_called_once_with(4)
		self.assertEqual(len(rows), 3)
		self.assertTrue(limited)

	def test_under_the_cap_returns_every_row(self):
		(rows, _masked, limited), q = self._run({"from": "Customer"}, 171, 500)
		q.limit.assert_called_once_with(501)
		self.assertEqual(len(rows), 171)
		self.assertFalse(limited)
