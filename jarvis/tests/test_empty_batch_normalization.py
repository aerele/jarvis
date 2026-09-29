"""P0d (decision 16): empty batch arrays are treated as absent.

The model often sends a batch key (``docs``/``names``/``updates``/``messages``)
alongside single-record args, e.g. ``create_doc({doctype, values, docs: []})``.
~20 tools route ANY non-``None`` batch key straight to batch mode and fail on
the empty list (the h400jb1fdk File Box incident: ``get_doc`` seq 16 and
``create_doc`` x3 seq 25/27/28).

``api._parse_args`` now drops a batch key whose value is ``[]``/``""``, but
only for a "dual-form" tool - one whose registry-accepted params carry BOTH a
batch key and ``doctype`` (the single-record form). That derivation is what
naturally excludes ``create_docs`` (batch-only, no ``doctype`` param - keeps
its own clear "docs must be a non-empty list" error) and ``get_my_access``
(``docs`` is a query list of {doctype, name} pairs to check access on, not a
batch of writes - also no ``doctype`` param).
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import pending_confirm
from jarvis.chat.actions_api import confirm_tool
from jarvis.exceptions import InvalidArgumentError
from jarvis.tests._pending_action_helpers import draft_doctype
from jarvis.tests._transport_helpers import provision_legacy_site
from jarvis.tools.create_docs import create_docs

CONV = "Jarvis Conversation"


class _Base(FrappeTestCase):
	def setUp(self):
		super().setUp()
		provision_legacy_site(self)

	def _conv(self, **flags) -> str:
		"""A conversation owned by the test user, with ``file_box`` set
		directly (bypassing the controller's admin-gate, exactly as
		drop_file does - mirrors test_confirm_gate.py's helper)."""
		doc = frappe.get_doc({"doctype": CONV, "title": "p0d empty-batch test"})
		doc.insert(ignore_permissions=True)
		if flags:
			frappe.db.set_value(CONV, doc.name, flags, update_modified=False)
		self.addCleanup(lambda: frappe.delete_doc(CONV, doc.name, force=True, ignore_permissions=True))
		return doc.name


class TestDualFormToolsContract(FrappeTestCase):
	"""The derived set + the two explicit exemptions, pinned so a registry
	change (a tool gaining/losing a batch key or a ``doctype`` param) is
	caught instead of silently drifting."""

	def test_every_batch_key_bearing_tool_is_either_dual_form_or_a_named_exemption(self):
		batch_key_tools = {
			name for name, params in api._ACCEPTED_PARAMS.items() if params.intersection(api._BATCH_KEYS)
		}
		exempt = batch_key_tools - api._DUAL_FORM_TOOLS
		self.assertEqual(exempt, {"create_docs", "get_my_access"})

	def test_the_nineteen_known_dual_form_tools(self):
		# Locks the derived set itself - a tool silently dropping out (e.g. a
		# rename) would otherwise leave that tool's empty-batch bug unfixed
		# with no test noticing.
		self.assertEqual(
			api._DUAL_FORM_TOOLS,
			frozenset(
				{
					"get_doc",
					"update_doc",
					"create_doc",
					"submit_doc",
					"cancel_doc",
					"delete_doc",
					"amend_doc",
					"get_workflow_transitions",
					"apply_workflow_action",
					"send_email",
					"add_comment",
					"share_doc",
					"unshare_doc",
					"assign_to",
					"unassign_from",
					"add_tag",
					"remove_tag",
					"follow_document",
					"unfollow_document",
				}
			),
		)


class TestParseArgsNormalisation(FrappeTestCase):
	"""Direct, registry-derived, table-driven coverage of ``_parse_args`` -
	every dual-form tool, both empty forms (``[]`` and ``""``), and that a
	REAL (non-empty) batch is left untouched."""

	def test_every_dual_form_tool_drops_its_own_empty_batch_key(self):
		for tool in sorted(api._DUAL_FORM_TOOLS):
			batch_key = next(k for k in api._BATCH_KEYS if k in api._ACCEPTED_PARAMS[tool])
			with self.subTest(tool=tool, batch_key=batch_key):
				out = api._parse_args({"doctype": "ToDo", batch_key: []}, tool)
				self.assertNotIn(batch_key, out)

	def test_an_empty_string_batch_value_is_also_dropped(self):
		out = api._parse_args({"doctype": "ToDo", "name": "x", "names": ""}, "get_doc")
		self.assertNotIn("names", out)

	def test_a_real_non_empty_batch_is_left_untouched(self):
		out = api._parse_args({"doctype": "ToDo", "names": ["a", "b"]}, "get_doc")
		self.assertEqual(out["names"], ["a", "b"])

	def test_only_drops_the_batch_key_the_tool_itself_declares(self):
		# create_doc doesn't accept "names" at all; an empty names value must
		# be left alone here (dispatch's own accepted-params filter is what
		# handles a key a tool never declared, not this normaliser).
		out = api._parse_args({"doctype": "ToDo", "values": {}, "docs": [], "names": []}, "create_doc")
		self.assertNotIn("docs", out)
		self.assertIn("names", out)

	def test_no_tool_means_no_normalisation(self):
		# The delegate-denial call site parses args for an audit row before
		# `tool` gating even runs (jarvis.api call_tool) - must not crash and
		# must not guess.
		out = api._parse_args({"doctype": "ToDo", "docs": []}, None)
		self.assertIn("docs", out)

	def test_a_non_dual_form_tool_is_never_touched(self):
		out = api._parse_args({"method": "frappe.ping", "names": []}, "run_method")
		self.assertIn("names", out)

	def test_create_docs_docs_is_never_dropped_batch_only_exemption(self):
		out = api._parse_args({"docs": []}, "create_docs")
		self.assertIn("docs", out)

	def test_get_my_access_docs_is_never_dropped_query_list_exemption(self):
		out = api._parse_args({"docs": []}, "get_my_access")
		self.assertIn("docs", out)

	def test_mutates_the_dict_in_place_so_every_downstream_reader_agrees(self):
		# The persisted tool-row / gate / preview / seal / dispatch all read
		# the SAME reference _run_tool holds after _parse_args - a rebuild
		# (rather than a pop) would desync them.
		args = {"doctype": "ToDo", "name": "x", "names": []}
		out = api._parse_args(args, "get_doc")
		self.assertIs(out, args)
		self.assertNotIn("names", args)


class TestExemptToolsUnchanged(FrappeTestCase):
	"""create_docs(docs=[]) and get_my_access(docs=[]) behave exactly as
	before P0d - both are query/batch-only shapes with no single form to
	normalise toward."""

	def test_create_docs_with_empty_docs_keeps_create_docs_own_clear_error(self):
		with self.assertRaises(InvalidArgumentError) as ctx:
			create_docs(docs=[])
		self.assertIn("docs must be a non-empty list", str(ctx.exception))

	def test_create_docs_via_run_tool_bounces_at_park_with_the_same_clear_error(self):
		r = api._run_tool("create_docs", {"docs": []})
		self.assertFalse(r["ok"])
		self.assertIn("docs must be a non-empty list", r["error"]["message"])

	def test_get_my_access_with_empty_docs_runs_normally_via_run_tool(self):
		# docs=[] is a benign "nothing to check" query, not an error - and is
		# never normalised away (get_my_access is exempt), so can_doc reports
		# empty rather than the field vanishing or the call failing.
		r = api._run_tool("get_my_access", {"docs": []})
		self.assertTrue(r["ok"], r)
		self.assertEqual(r["data"]["can_doc"], [])


class TestIncidentReplay(_Base):
	"""The h400jb1fdk shapes, replayed through the real _run_tool path, in
	both a normal and a file_box=1 conversation."""

	def tearDown(self):
		frappe.db.delete("ToDo", {"description": ["like", "p0d-%"]})
		super().tearDown()

	def test_get_doc_with_empty_names_replays_as_a_single_read(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "p0d-get-doc-normal"})
		todo.insert(ignore_permissions=True)
		r = api._run_tool("get_doc", {"doctype": "ToDo", "name": todo.name, "names": []})
		self.assertTrue(r["ok"], r)
		self.assertEqual(r["data"]["name"], todo.name)
		# The single shape (a flat doc dict), never the batch envelope.
		self.assertNotIn("docs", r["data"])
		self.assertNotIn("count", r["data"])

	def test_get_doc_with_empty_names_replays_as_a_single_read_in_a_file_box_conversation(self):
		conv = self._conv(file_box=1)
		todo = frappe.get_doc({"doctype": "ToDo", "description": "p0d-get-doc-filebox"})
		todo.insert(ignore_permissions=True)
		r = api._run_tool("get_doc", {"doctype": "ToDo", "name": todo.name, "names": []}, conversation=conv)
		self.assertTrue(r["ok"], r)
		self.assertEqual(r["data"]["name"], todo.name)

	def test_create_doc_with_empty_docs_auto_applies_in_a_file_box_conversation(self):
		# h400jb1fdk: the draft create with a stray docs: [] is a SINGLE draft create to
		# the File Box policy (P0d normalises first), so it auto-applies - never a batch.
		dt = draft_doctype() or self.skipTest("no submittable doctype installed")
		conv = self._conv(file_box=1)
		with patch("jarvis.api.dispatch_confirmed", return_value={"ok": True, "data": {"name": "D-1"}}) as dc:
			r = api._run_tool(
				"create_doc", {"doctype": dt, "values": {"remark": "p0d"}, "docs": []}, conversation=conv
			)
		self.assertTrue(r["ok"], r)
		self.assertNotEqual((r.get("data") or {}).get("status"), "pending_confirmation")
		dc.assert_called_once()
		self.assertNotIn("docs", dc.call_args.args[1])

	def test_create_doc_with_empty_docs_parks_as_a_single_card_in_a_normal_conversation(self):
		conv = self._conv()
		desc = "p0d-create-doc-normal"
		r = api._run_tool(
			"create_doc", {"doctype": "ToDo", "values": {"description": desc}, "docs": []}, conversation=conv
		)
		self.assertTrue(r["ok"], r)
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		# The persisted/parked args equal what will execute: docs was dropped
		# BEFORE park, so the SAME (already-normalised) dict is what confirm
		# later dispatches - never re-normalised at confirm time (a legacy
		# pre-P0d token stays exactly as sealed, per S4).
		parked = pending_confirm.list_for_owner(frappe.session.user, conversation=conv)
		self.assertEqual(len(parked), 1)
		token = parked[0]["token"]
		# A pending action's list never unseals: read the sealed call itself.
		from jarvis.chat.pending_actions import _seal
		from jarvis.chat.pending_actions._store import get_row

		self.assertNotIn("docs", _seal.unseal_call(get_row(token))["args"])
		with patch("jarvis.chat.api._dispatch_turn"):
			res = confirm_tool(token, conversation=conv)
		self.assertTrue(res["ok"], res)
		self.assertTrue(frappe.db.exists("ToDo", {"description": desc}))
