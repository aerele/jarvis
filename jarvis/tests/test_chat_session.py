"""Tests for the Jarvis Chat Session DocType.

Sanity checks: DocType exists, fields exist, unique constraint on session_key,
and the model-level behaviour (before_insert sets created_at).
"""

import frappe
from frappe.tests.utils import FrappeTestCase

DOCTYPE = "Jarvis Chat Session"


def _insert(session_key: str, user: str = "Administrator") -> str:
	"""Insert a Jarvis Chat Session row and return its name (hash)."""
	doc = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"session_key": session_key,
			"user": user,
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _cleanup(session_key: str) -> None:
	"""Delete any Jarvis Chat Session rows with the given session_key."""
	names = frappe.get_all(DOCTYPE, filters={"session_key": session_key}, pluck="name")
	for name in names:
		frappe.delete_doc(DOCTYPE, name, ignore_permissions=True, force=True)
	frappe.db.commit()


class TestJarvisChatSessionDocType(FrappeTestCase):
	"""Basic sanity tests for the Jarvis Chat Session DocType."""

	def test_doctype_exists(self):
		"""The DocType must be installed/migrated."""
		self.assertTrue(frappe.db.exists("DocType", DOCTYPE))

	def test_required_fields_exist(self):
		"""session_key, user, created_at fields must be defined."""
		meta = frappe.get_meta(DOCTYPE)
		field_names = {f.fieldname for f in meta.fields}
		self.assertIn("session_key", field_names)
		self.assertIn("user", field_names)
		self.assertIn("created_at", field_names)
		self.assertIn("last_seen_at", field_names)

	def test_session_key_is_unique_and_mandatory(self):
		"""session_key field must be unique and required."""
		meta = frappe.get_meta(DOCTYPE)
		sk_field = next(f for f in meta.fields if f.fieldname == "session_key")
		self.assertTrue(sk_field.reqd)
		self.assertTrue(sk_field.unique)

	def test_user_is_link_to_user(self):
		"""user field must be a Link to the User DocType."""
		meta = frappe.get_meta(DOCTYPE)
		user_field = next(f for f in meta.fields if f.fieldname == "user")
		self.assertEqual(user_field.fieldtype, "Link")
		self.assertEqual(user_field.options, "User")

	def test_insert_and_retrieve(self):
		"""Inserting a session row and reading it back works correctly."""
		key = "test-session-doctype-insert-1"
		_cleanup(key)
		try:
			name = _insert(key, "Administrator")
			self.assertTrue(name)
			doc = frappe.get_doc(DOCTYPE, name)
			self.assertEqual(doc.session_key, key)
			self.assertEqual(doc.user, "Administrator")
		finally:
			_cleanup(key)

	def test_created_at_is_set_on_insert(self):
		"""created_at must be populated automatically on insert."""
		key = "test-session-doctype-insert-2"
		_cleanup(key)
		try:
			name = _insert(key, "Administrator")
			doc = frappe.get_doc(DOCTYPE, name)
			self.assertIsNotNone(doc.created_at)
		finally:
			_cleanup(key)

	def test_unique_constraint_on_session_key(self):
		"""Inserting two rows with the same session_key must raise an error."""
		key = "test-session-unique-constraint-1"
		_cleanup(key)
		try:
			_insert(key, "Administrator")
			with self.assertRaises(Exception):
				# Should raise frappe.exceptions.DuplicateEntryError or similar
				_insert(key, "Administrator")
		finally:
			_cleanup(key)

	def test_lookup_by_session_key(self):
		"""frappe.db.get_value can look up a user by session_key."""
		key = "test-session-lookup-1"
		_cleanup(key)
		try:
			_insert(key, "Administrator")
			user = frappe.db.get_value(DOCTYPE, {"session_key": key}, "user")
			self.assertEqual(user, "Administrator")
		finally:
			_cleanup(key)

	def test_missing_session_key_not_found(self):
		"""Looking up a non-existent session_key returns None."""
		user = frappe.db.get_value(
			DOCTYPE,
			{"session_key": "definitely-not-a-real-session-key-xyz"},
			"user",
		)
		self.assertIsNone(user)
