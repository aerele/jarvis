"""Direct tests for the zero-arg jarvis__get_engagement_config tool.

The tool resolves the run's installation from the trusted session_key (never a model
id) and returns ONLY the installation's `config` JSON. These exercise its four raise
paths, the parse/return-shape guards, and the secret-minimisation claim (only the
`config` field is ever read), with the two DB reads + session lookup mocked so the
suite stays deterministic and DB-free (backport-safe unittest.TestCase).
"""

import unittest
from unittest import mock

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools.get_engagement_config import get_engagement_config

RUN = "Jarvis Agent Run"
INSTALLATION = "Jarvis Agent Installation"


class TestGetEngagementConfig(unittest.TestCase):
	def _call(self, *, session_key, run_row, config):
		"""Run the tool with get_session_key + both frappe.db.get_value reads mocked.

		Returns (result, get_value_mock). `run_row` is the RUN lookup's return (dict or
		None); `config` is the INSTALLATION `config` column's raw value (str or None).
		"""

		def fake_get_value(doctype, name, fieldname=None, **kw):
			if doctype == RUN:
				return run_row
			if doctype == INSTALLATION:
				return config
			raise AssertionError(f"unexpected get_value doctype: {doctype}")

		with (
			mock.patch("jarvis.tools._agent_run_ctx.get_session_key", return_value=session_key),
			mock.patch.object(frappe.db, "get_value", side_effect=fake_get_value) as gv,
			mock.patch.object(frappe, "log_error"),  # malformed-config path logs; no DB in tests
		):
			return get_engagement_config(), gv

	def test_no_session_key_raises(self):
		# valid run_row + config so ONLY the missing session_key can raise: if guard 1 were
		# removed the tool would return the config instead of raising, failing this test.
		with self.assertRaises(InvalidArgumentError):
			self._call(
				session_key=None,
				run_row=frappe._dict(name="R1", installation="INST-1"),
				config=frappe.as_json({"x": 1}),
			)

	def test_no_run_bound_to_session_raises(self):
		with self.assertRaises(InvalidArgumentError):
			self._call(session_key="agent:agent-x:R1", run_row=None, config=None)

	def test_run_without_installation_raises(self):
		with self.assertRaises(InvalidArgumentError):
			self._call(
				session_key="agent:agent-x:R1",
				run_row=frappe._dict(name="R1", installation=None),
				config=None,
			)

	def test_happy_path_returns_parsed_config_dict(self):
		cfg = {"materiality": {"percentage": 5}, "s269st_limit": 200000}
		result, gv = self._call(
			session_key="agent:agent-x:R1",
			run_row=frappe._dict(name="R1", installation="INST-1"),
			config=frappe.as_json(cfg),
		)
		self.assertEqual(dict(result), cfg)
		# secret minimisation: the installation read asks for the `config` field ONLY -
		# never the whole row (which carries agent_url / device tokens / run-as identity).
		inst_calls = [c for c in gv.call_args_list if c.args and c.args[0] == INSTALLATION]
		self.assertEqual(len(inst_calls), 1)
		self.assertEqual(inst_calls[0].args[2], "config")

	def test_empty_config_returns_empty_dict(self):
		result, _ = self._call(
			session_key="agent:agent-x:R1",
			run_row=frappe._dict(name="R1", installation="INST-1"),
			config=None,
		)
		self.assertEqual(result, {})

	def test_malformed_json_config_returns_empty_dict(self):
		result, _ = self._call(
			session_key="agent:agent-x:R1",
			run_row=frappe._dict(name="R1", installation="INST-1"),
			config="{not valid json",
		)
		self.assertEqual(result, {})

	def test_non_dict_json_config_returns_empty_dict(self):
		result, _ = self._call(
			session_key="agent:agent-x:R1",
			run_row=frappe._dict(name="R1", installation="INST-1"),
			config="[1, 2, 3]",
		)
		self.assertEqual(result, {})
