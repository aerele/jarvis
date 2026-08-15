"""admin_set_user_model_limit + per-model rows in the settings/admin payloads
(fleet usage spec §7). Admin-gated; mirrors admin_set_user_limit."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import usage, user_settings_api
from jarvis.permissions import (
	JARVIS_ADMIN_ROLE,
	JARVIS_USER_ROLE,
	ensure_jarvis_admin_role,
	ensure_jarvis_user_role,
)

USETT = "Jarvis User Settings"
MODEL_USAGE = "Jarvis User Model Usage"
USER_A = "jarvis-umlapi-a@example.test"
USER_ADMIN = "jarvis-umlapi-admin@example.test"
USER_PLAIN = "jarvis-umlapi-plain@example.test"
_ALL = (USER_A, USER_ADMIN, USER_PLAIN)


def _ensure_user(email, roles=()):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Jarvis",
				"last_name": "UmlApi",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	if roles:
		frappe.get_doc("User", email).add_roles(*roles)


def _strip_admin(email):
	doc = frappe.get_doc("User", email)
	drop = {r.role for r in doc.get("roles", [])} & {"System Manager", JARVIS_ADMIN_ROLE}
	if drop:
		doc.remove_roles(*drop)


def _cleanup():
	for email in _ALL:
		for n in frappe.get_all(MODEL_USAGE, filters={"parent": email}, pluck="name"):
			frappe.delete_doc(MODEL_USAGE, n, ignore_permissions=True, force=True)
		for n in frappe.get_all(USETT, filters={"user": email}, pluck="name"):
			frappe.delete_doc(USETT, n, ignore_permissions=True, force=True)


class _Base(FrappeTestCase):
	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user("Administrator")
		ensure_jarvis_user_role()
		ensure_jarvis_admin_role()
		_ensure_user(USER_A, (JARVIS_USER_ROLE,))
		_ensure_user(USER_ADMIN, (JARVIS_ADMIN_ROLE,))
		_ensure_user(USER_PLAIN, (JARVIS_USER_ROLE,))
		_strip_admin(USER_A)
		_strip_admin(USER_PLAIN)
		_cleanup()
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		_cleanup()
		frappe.db.commit()
		frappe.set_user(self._orig)


class TestAdminSetModelLimit(_Base):
	def test_creates_row_and_sets_cap(self):
		out = user_settings_api.admin_set_user_model_limit(
			user=USER_A, model="gpt-4o", monthly_token_limit=500
		)
		self.assertTrue(out["ok"])
		self.assertEqual(out["data"]["model"], "gpt-4o")
		self.assertEqual(out["data"]["monthly_token_limit"], 500)
		self.assertEqual(
			frappe.db.get_value(
				MODEL_USAGE,
				{"parent": USER_A, "model": "gpt-4o", "month_key": usage.current_month_key()},
				"monthly_token_limit",
			),
			500,
		)

	def test_unknown_user_refused(self):
		out = user_settings_api.admin_set_user_model_limit(
			user="nobody@example.invalid", model="gpt-4o", monthly_token_limit=1
		)
		self.assertFalse(out["ok"])
		self.assertEqual(out["reason"], "unknown_user")

	def test_blank_model_refused(self):
		out = user_settings_api.admin_set_user_model_limit(user=USER_A, model="  ", monthly_token_limit=1)
		self.assertFalse(out["ok"])
		self.assertEqual(out["reason"], "invalid_model")

	def test_plain_user_refused(self):
		frappe.set_user(USER_PLAIN)
		with self.assertRaises(frappe.PermissionError):
			user_settings_api.admin_set_user_model_limit(user=USER_A, model="gpt-4o", monthly_token_limit=1)

	def test_admin_list_includes_per_model(self):
		_make = usage.get_or_create_user_settings(USER_A)
		usage.set_model_limit(USER_A, "gpt-4o", 500)
		frappe.db.set_value(
			MODEL_USAGE,
			{"parent": USER_A, "model": "gpt-4o", "month_key": usage.current_month_key()},
			{"month_input_tokens": 30, "month_output_tokens": 10},
			update_modified=False,
		)
		frappe.db.commit()
		out = user_settings_api.admin_list_user_usage()
		row = {r["user"]: r for r in out["data"]}[USER_A]
		self.assertIn("per_model", row)
		pm = {m["model"]: m for m in row["per_model"]}
		self.assertIn("gpt-4o", pm)
		self.assertEqual(pm["gpt-4o"]["month_tokens"], 40)
		self.assertEqual(pm["gpt-4o"]["monthly_token_limit"], 500)

	def test_get_my_settings_includes_per_model(self):
		usage.set_model_limit(USER_A, "gpt-4o", 500)
		frappe.set_user(USER_A)
		out = user_settings_api.get_my_settings()
		self.assertIn("per_model", out["data"])
		self.assertTrue(any(m["model"] == "gpt-4o" for m in out["data"]["per_model"]))


# --------------------------------------------------------------------------- #
# admin_list_user_usage per-model batching (N+1 fix): a single query for all
# listed users' current-month child rows, bucketed in Python, instead of one
# _per_model_rows(user) call per row.
# --------------------------------------------------------------------------- #
BATCH_USER_1 = "jarvis-umlapi-batch1@example.test"
BATCH_USER_2 = "jarvis-umlapi-batch2@example.test"
BATCH_USER_3 = "jarvis-umlapi-batch3@example.test"
_BATCH_USERS = (BATCH_USER_1, BATCH_USER_2, BATCH_USER_3)


class TestAdminListPerModelBatched(FrappeTestCase):
	def setUp(self):
		self._orig = frappe.session.user
		frappe.set_user("Administrator")
		ensure_jarvis_user_role()
		for email in _BATCH_USERS:
			_ensure_user(email, (JARVIS_USER_ROLE,))
		self._cleanup()
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._cleanup()
		frappe.db.commit()
		frappe.set_user(self._orig)

	def _cleanup(self):
		for email in _BATCH_USERS:
			for n in frappe.get_all(MODEL_USAGE, filters={"parent": email}, pluck="name"):
				frappe.delete_doc(MODEL_USAGE, n, ignore_permissions=True, force=True)
			for n in frappe.get_all(USETT, filters={"user": email}, pluck="name"):
				frappe.delete_doc(USETT, n, ignore_permissions=True, force=True)

	def _seed(self):
		# 3 users, overlapping + unique models, so bucketing-by-parent must
		# not cross-contaminate between users.
		usage.get_or_create_user_settings(BATCH_USER_1)
		usage.set_model_limit(BATCH_USER_1, "gpt-4o", 1000)
		usage.set_model_limit(BATCH_USER_1, "claude-sonnet", 0)
		usage.get_or_create_user_settings(BATCH_USER_2)
		usage.set_model_limit(BATCH_USER_2, "gpt-4o", 0)
		usage.get_or_create_user_settings(BATCH_USER_3)
		# BATCH_USER_3 has no per-model rows at all.
		month = usage.current_month_key()
		frappe.db.set_value(
			MODEL_USAGE,
			{"parent": BATCH_USER_1, "model": "gpt-4o", "month_key": month},
			{"month_input_tokens": 30, "month_output_tokens": 10},
			update_modified=False,
		)
		frappe.db.set_value(
			MODEL_USAGE,
			{"parent": BATCH_USER_1, "model": "claude-sonnet", "month_key": month},
			{"month_input_tokens": 5, "month_output_tokens": 5},
			update_modified=False,
		)
		frappe.db.set_value(
			MODEL_USAGE,
			{"parent": BATCH_USER_2, "model": "gpt-4o", "month_key": month},
			{"month_input_tokens": 100, "month_output_tokens": 50},
			update_modified=False,
		)
		frappe.db.commit()

	def test_admin_list_per_model_matches_per_user_reference_without_per_user_calls(self):
		self._seed()
		# Reference: what the OLD per-user path would have produced, computed
		# independently of the code under test.
		expected = {email: user_settings_api._per_model_rows(email) for email in _BATCH_USERS}
		with patch.object(
			user_settings_api, "_per_model_rows", wraps=user_settings_api._per_model_rows
		) as spy:
			out = user_settings_api.admin_list_user_usage()
		# The admin listing must not fall back to the per-user helper at all -
		# that's the N+1 this fix removes. (The single-user path, exercised
		# by get_my_settings, is untouched and keeps using it.)
		spy.assert_not_called()
		self.assertTrue(out["ok"])
		rows = {r["user"]: r for r in out["data"]}
		for email in _BATCH_USERS:
			self.assertIn(email, rows)
			got = sorted(rows[email]["per_model"], key=lambda m: m["model"])
			want = sorted(expected[email], key=lambda m: m["model"])
			self.assertEqual(got, want, f"per_model mismatch for {email}")
