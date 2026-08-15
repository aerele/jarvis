"""Tests for jarvis.tools.run_method - whitelist-only dispatch, optional
config allowlist, and input/permission errors. Runs against real Frappe
(the whitelist gate is the security boundary; exercising it for real is
more meaningful than mocking it)."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools.run_method import run_method


class TestRunMethod(FrappeTestCase):
	def test_calls_whitelisted_method(self):
		self.assertEqual(run_method("frappe.ping"), "pong")

	def test_blocks_resolvable_but_non_whitelisted_method(self):
		# frappe.get_all / frappe.delete_doc resolve but are NOT @whitelist'd.
		with self.assertRaises(PermissionDeniedError):
			run_method("frappe.get_all", {"doctype": "ToDo"})
		with self.assertRaises(PermissionDeniedError):
			run_method("frappe.delete_doc", {"doctype": "ToDo", "name": "x"})

	def test_unknown_method_raises(self):
		with self.assertRaises(InvalidArgumentError):
			run_method("nope.not.a.real.method")

	def test_blank_method_raises(self):
		with self.assertRaises(InvalidArgumentError):
			run_method("")

	def test_non_dict_args_raises(self):
		with self.assertRaises(InvalidArgumentError):
			run_method("frappe.ping", args="not-a-dict")

	def test_allowlist_blocks_unmatched_method(self):
		with patch.dict(frappe.local.conf, {"jarvis_run_method_allowlist": ["erpnext.*"]}):
			with self.assertRaises(PermissionDeniedError):
				run_method("frappe.ping")  # whitelisted, but not in the allowlist

	def test_allowlist_permits_matched_method(self):
		with patch.dict(frappe.local.conf, {"jarvis_run_method_allowlist": ["frappe.*"]}):
			self.assertEqual(run_method("frappe.ping"), "pong")

	def test_empty_allowlist_blocks_everything(self):
		# [] is "block all", not "no restriction" (the falsy-empty pitfall).
		with patch.dict(frappe.local.conf, {"jarvis_run_method_allowlist": []}):
			with self.assertRaises(PermissionDeniedError):
				run_method("frappe.ping")

	def test_unknown_arg_is_rejected(self):
		# A mistyped/extra arg must error, not be silently dropped by frappe.call.
		with self.assertRaises(InvalidArgumentError):
			run_method("frappe.ping", {"bogus_arg": 1})
