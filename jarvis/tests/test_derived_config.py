from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_scheduler, derived_config

_KEYS = ["open_workable_status_set", "fingerprint_salt"]


class _Inst(frappe._dict):
	pass


def _inst(name="INST-1", config=None):
	return _Inst(name=name, config=config)


class TestDerivedConfig(FrappeTestCase):
	def test_salt_stable_distinct_and_hex(self):
		a = derived_config._fingerprint_salt(_inst("A"))
		self.assertEqual(a, derived_config._fingerprint_salt(_inst("A")))
		self.assertNotEqual(a, derived_config._fingerprint_salt(_inst("B")))
		self.assertRegex(a, r"^[0-9a-f]{32}$")

	def test_salt_independent_of_installation_config(self):
		base = derived_config.derive({"fingerprint_salt"}, _inst("A"), {})
		typed = derived_config.derive(
			{"fingerprint_salt"},
			_inst("A", '{"fingerprint_salt": "mine"}'),
			{"fingerprint_salt": "mine"},
		)
		self.assertEqual(base, typed)
		self.assertNotEqual(typed["fingerprint_salt"], "mine")

	def test_open_set_from_lead_status_types(self):
		with (
			patch.object(derived_config.frappe.db, "exists", return_value=True),
			patch.object(derived_config.frappe, "get_all", return_value=["Open", "Contacted"]) as ga,
		):
			out = derived_config.derive({"open_workable_status_set"}, _inst(), {})
		self.assertEqual(out, {"open_workable_status_set": ["Contacted", "Open"]})
		self.assertEqual(ga.call_args.kwargs["filters"], {"type": ["in", ["Open", "Ongoing"]]})

	def test_open_set_omitted_without_crm(self):
		with patch.object(derived_config.frappe.db, "exists", return_value=False):
			out = derived_config.derive({"open_workable_status_set"}, _inst(), {})
		self.assertEqual(out, {})

	def test_open_set_installation_value_wins(self):
		with (
			patch.object(derived_config.frappe.db, "exists", return_value=True),
			patch.object(derived_config.frappe, "get_all", return_value=["Open"]),
		):
			out = derived_config.derive(
				{"open_workable_status_set"}, _inst(), {"open_workable_status_set": ["X"]}
			)
		self.assertEqual(out, {})

	def test_explicit_config_merges_derived_when_declared(self):
		with (
			patch.object(derived_config.frappe.db, "exists", return_value=True),
			patch.object(derived_config.frappe, "get_all", return_value=["Open"]),
		):
			out = agent_scheduler._explicit_config({"config_keys": _KEYS}, _inst())
		self.assertEqual(set(out), set(_KEYS))
		self.assertEqual(out["open_workable_status_set"], ["Open"])

	def test_explicit_config_no_leak_to_other_agents(self):
		out = agent_scheduler._explicit_config({"config_keys": ["ageing.stale_floor_days"]}, _inst())
		self.assertEqual(out, {})
		self.assertEqual(agent_scheduler._explicit_config({}, _inst()), {})

	def test_explicit_config_declared_keys_pass_through(self):
		inst = _inst(config='{"ageing": {"stale_floor_days": 9}, "other": 1}')
		out = agent_scheduler._explicit_config({"config_keys": ["ageing.stale_floor_days"]}, inst)
		self.assertEqual(out, {"ageing": {"stale_floor_days": 9}})

	def test_salt_never_logged(self):
		with (
			patch.object(frappe, "log_error") as le,
			patch.object(frappe.logger(), "info") as li,
		):
			out = agent_scheduler._explicit_config({"config_keys": ["fingerprint_salt"]}, _inst())
		salt = out["fingerprint_salt"]
		for m in (le, li):
			self.assertNotIn(salt, str(m.call_args_list))
