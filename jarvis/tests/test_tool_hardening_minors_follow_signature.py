"""follow_document / unfollow_document call Frappe's real function with a
signature it accepts (no mock that swallows any arguments)."""

import inspect
from unittest.mock import patch

import frappe
from frappe.desk.form import document_follow
from frappe.tests.utils import FrappeTestCase

from jarvis.tools.follow_document import _follow_document_one
from jarvis.tools.unfollow_document import _unfollow_document_one


def _target(fn: str) -> str:
	public = getattr(document_follow, fn)
	return fn if "user" in inspect.signature(public).parameters else f"_{fn}"


class TestFollowSignature(FrappeTestCase):
	def _assert_call_binds(self, fn_name, runner):
		target = _target(fn_name)
		real = getattr(document_follow, target)
		seen = {}

		def spy(*args, **kwargs):
			# bind() raises TypeError if the call does not fit the real signature.
			inspect.signature(real).bind(*args, **kwargs)
			seen["args"] = args
			return True

		# autospec keeps the real signature, which the tool reads to pick the function.
		with patch(f"frappe.desk.form.document_follow.{target}", autospec=True, side_effect=spy):
			runner()
		self.assertEqual(seen["args"], ("User", "Administrator", "someone@example.com"))

	def test_follow_call_fits_real_signature(self):
		with patch("frappe.has_permission", return_value=True):
			self._assert_call_binds(
				"follow_document",
				lambda: _follow_document_one("User", "Administrator", "someone@example.com"),
			)

	def test_unfollow_call_fits_real_signature(self):
		with patch("frappe.has_permission", return_value=True):
			self._assert_call_binds(
				"unfollow_document",
				lambda: _unfollow_document_one("User", "Administrator", "someone@example.com"),
			)

	def test_follow_runs_against_real_function(self):
		# Unpatched: Administrator cannot follow, so the real function returns
		# falsy; the point is that it runs without a TypeError.
		frappe.set_user("Administrator")
		self.assertFalse(_follow_document_one("User", "Administrator", "Administrator"))
