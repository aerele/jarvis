"""update_doc on a child table must never silently wipe what it was not told
about (CR-3, whole-product review 2026-09-26).

A table payload used to be applied with ``doc.set``: every existing row was
deleted and re-inserted from the payload, so any field the payload did not
carry (custom fields, ``so_detail`` links, project, cost centre, ``is_primary``
here) was lost while the receipt said "Updated". The rule now:

* a row with ``name`` updates that existing row, only the keys it carries;
* a row without ``name`` is a new row;
* existing rows the payload does not name are removed (the payload is the
  complete final set);
* a payload with no row names on a table that already has rows is refused,
  never guessed at.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools.update_doc import update_doc


def _contact(*emails):
	return frappe.get_doc(
		{
			"doctype": "Contact",
			"first_name": "Child Rows Test",
			"email_ids": [{"email_id": e, "is_primary": 1 if i == 0 else 0} for i, e in enumerate(emails)],
		}
	).insert()


def _emails(name):
	doc = frappe.get_doc("Contact", name)
	return [(r.name, r.email_id, r.is_primary) for r in doc.email_ids]


class TestUpdateDocChildRows(FrappeTestCase):
	def test_named_row_changes_only_the_fields_sent(self):
		c = _contact("a@example.com")
		row = c.email_ids[0].name
		update_doc("Contact", c.name, {"email_ids": [{"name": row, "email_id": "b@example.com"}]})
		self.assertEqual(_emails(c.name), [(row, "b@example.com", 1)])  # is_primary kept

	def test_nameless_row_is_added_next_to_named_rows(self):
		c = _contact("a@example.com")
		row = c.email_ids[0].name
		update_doc(
			"Contact",
			c.name,
			{"email_ids": [{"name": row}, {"email_id": "new@example.com"}]},
		)
		emails = _emails(c.name)
		self.assertEqual(emails[0], (row, "a@example.com", 1))
		self.assertEqual(emails[1][1:], ("new@example.com", 0))

	def test_rows_not_named_are_removed(self):
		c = _contact("a@example.com", "b@example.com")
		keep = c.email_ids[1].name
		update_doc("Contact", c.name, {"email_ids": [{"name": keep}]})
		self.assertEqual([r[0] for r in _emails(c.name)], [keep])

	def test_nameless_table_on_existing_rows_is_refused_and_nothing_changes(self):
		c = _contact("a@example.com")
		before = _emails(c.name)
		with self.assertRaises(InvalidArgumentError) as err:
			update_doc("Contact", c.name, {"email_ids": [{"email_id": "x@example.com"}]})
		self.assertIn("name", str(err.exception))
		self.assertEqual(_emails(c.name), before)

	def test_empty_list_clears_the_table(self):
		c = _contact("a@example.com")
		update_doc("Contact", c.name, {"email_ids": []})
		self.assertEqual(_emails(c.name), [])

	def test_nameless_rows_fill_an_empty_table(self):
		c = frappe.get_doc({"doctype": "Contact", "first_name": "Child Rows Empty"}).insert()
		update_doc("Contact", c.name, {"email_ids": [{"email_id": "a@example.com"}]})
		self.assertEqual([r[1] for r in _emails(c.name)], ["a@example.com"])

	def test_unknown_row_name_is_refused(self):
		c = _contact("a@example.com")
		with self.assertRaises(InvalidArgumentError):
			update_doc("Contact", c.name, {"email_ids": [{"name": "not-a-row", "email_id": "x@example.com"}]})

	def test_row_system_keys_cannot_move_a_row(self):
		c = _contact("a@example.com")
		row = c.email_ids[0].name
		update_doc(
			"Contact",
			c.name,
			{"email_ids": [{"name": row, "parent": "other", "idx": 9, "email_id": "b@example.com"}]},
		)
		doc = frappe.get_doc("Contact", c.name)
		self.assertEqual((doc.email_ids[0].parent, doc.email_ids[0].idx), (c.name, 1))

	def test_rows_are_renumbered_after_a_delete_and_an_add(self):
		c = _contact("a@example.com", "b@example.com", "c@example.com")
		b, cc = c.email_ids[1].name, c.email_ids[2].name
		update_doc(
			"Contact",
			c.name,
			{"email_ids": [{"name": b}, {"name": cc}, {"email_id": "d@example.com"}]},
		)
		doc = frappe.get_doc("Contact", c.name)
		self.assertEqual([r.idx for r in doc.email_ids], [1, 2, 3])
		self.assertEqual(
			[r.email_id for r in doc.email_ids], ["b@example.com", "c@example.com", "d@example.com"]
		)

	def test_a_row_named_twice_is_refused(self):
		# The same row twice would be counted twice in totals but saved once.
		c = _contact("a@example.com")
		row = c.email_ids[0].name
		with self.assertRaises(InvalidArgumentError):
			update_doc("Contact", c.name, {"email_ids": [{"name": row}, {"name": row}]})

	def test_dunder_keys_on_a_row_are_ignored(self):
		c = _contact("a@example.com")
		row = c.email_ids[0].name
		update_doc(
			"Contact", c.name, {"email_ids": [{"name": row, "__islocal": 1, "email_id": "b@example.com"}]}
		)
		self.assertEqual(_emails(c.name), [(row, "b@example.com", 1)])

	def test_refusal_names_the_table_by_label_in_plain_words(self):
		c = _contact("a@example.com")
		with self.assertRaises(InvalidArgumentError) as err:
			update_doc("Contact", c.name, {"email_ids": [{"email_id": "x@example.com"}]})
		label = frappe.get_meta("Contact").get_field("email_ids").label
		self.assertIn(label, str(err.exception))
		self.assertIn("Nothing was changed", str(err.exception))

	def test_null_clears_the_table(self):
		c = _contact("a@example.com")
		update_doc("Contact", c.name, {"email_ids": None})
		self.assertEqual(_emails(c.name), [])

	def test_a_row_name_that_is_not_text_is_refused_cleanly(self):
		c = _contact("a@example.com")
		for bad in (["x"], {"x": 1}, 7):
			with self.assertRaises(InvalidArgumentError):
				update_doc("Contact", c.name, {"email_ids": [{"name": bad}, {"name": bad}]})
		self.assertEqual(len(_emails(c.name)), 1)
