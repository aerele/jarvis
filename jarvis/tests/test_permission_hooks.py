"""``effective_hooks`` drops only ERPNext's audited ``"*"`` hooks, only while inert."""

import unittest
from unittest.mock import MagicMock, patch

import frappe

from jarvis.permission_hooks import AUDITED_WILDCARD_HOOKS, effective_hooks

QUERY = "permission_query_conditions"
DOC = "has_permission"
AUDITED_QUERY = sorted(AUDITED_WILDCARD_HOOKS[QUERY])
AUDITED_DOC = sorted(AUDITED_WILDCARD_HOOKS[DOC])
UNKNOWN = "other_app.hooks.slice"


def _hooks(registered):
	def get_hooks(key=None, default=None, **kwargs):
		return registered.get(key, default)

	return get_hooks


def _effective(registered, hook_name, user_permissions, configured=True):
	with (
		patch.object(frappe, "get_hooks", side_effect=_hooks(registered)),
		patch("jarvis.permission_hooks.get_user_permissions", return_value=user_permissions),
		patch("jarvis.permission_hooks._restriction_configured", return_value=configured),
	):
		return effective_hooks(hook_name, "someone@example.com")


class TestEffectiveHooks(unittest.TestCase):
	def test_audited_wildcard_is_dropped_when_the_user_has_no_company_permission(self):
		for name, paths in ((QUERY, AUDITED_QUERY), (DOC, AUDITED_DOC)):
			with self.subTest(name):
				registered = {name: {"*": paths, "Item": ["erpnext.item_hook"]}}
				self.assertEqual(
					_effective(registered, name, {"Territory": []}), {"Item": ["erpnext.item_hook"]}
				)

	def test_audited_wildcard_stays_for_a_user_with_a_company_permission(self):
		registered = {QUERY: {"*": AUDITED_QUERY}}
		self.assertEqual(_effective(registered, QUERY, {"Company": []}), {"*": AUDITED_QUERY})

	def test_audited_wildcard_is_dropped_while_no_master_is_restricted(self):
		registered = {DOC: {"*": AUDITED_DOC}}
		self.assertEqual(_effective(registered, DOC, {"Company": []}, configured=False), {})

	def test_restriction_probe_reports_configured_unless_every_probe_is_empty(self):
		from jarvis.permission_hooks import _restriction_configured

		def probe(**kwargs):
			with patch("jarvis.permission_hooks.frappe.db", MagicMock(**kwargs), create=True):
				return _restriction_configured()

		self.assertFalse(probe(**{"exists.return_value": None}))
		self.assertTrue(probe(**{"exists.side_effect": [None, None, 1]}))
		self.assertTrue(probe(**{"exists.return_value": "CR-1"}))
		self.assertTrue(probe(**{"exists.side_effect": RuntimeError("no table")}))

	def test_an_unknown_wildcard_is_never_dropped(self):
		for paths in ([UNKNOWN], [*AUDITED_QUERY, UNKNOWN]):
			with self.subTest(paths=paths):
				registered = {QUERY: {"*": paths}}
				self.assertEqual(_effective(registered, QUERY, {}), {"*": paths})

	def test_an_audited_path_of_the_other_hook_kind_is_unknown(self):
		registered = {QUERY: {"*": AUDITED_DOC}}
		self.assertEqual(_effective(registered, QUERY, {}), {"*": AUDITED_DOC})

	def test_doctype_specific_hooks_and_odd_shapes_pass_through(self):
		registered = {QUERY: {"Item": AUDITED_QUERY}}
		self.assertEqual(_effective(registered, QUERY, {}), {"Item": AUDITED_QUERY})
		for odd in ({"*": []}, {"*": None}, {"*": 7}):
			with self.subTest(odd=odd):
				self.assertEqual(_effective({QUERY: odd}, QUERY, {}), odd)

	def test_no_wildcard_at_all_is_the_framework_answer(self):
		self.assertEqual(_effective({QUERY: {}}, QUERY, {}), {})
