"""File Box draft scope: an update is auto-applied only to a draft this same run
created; an update of any other record is held for a human."""

from __future__ import annotations

from unittest.mock import patch

from jarvis.chat import held_writes
from jarvis.tests import test_filebox_held as fbh
from jarvis.tests._pending_action_helpers import draft_doctype


class TestFileBoxDraftScope(fbh._Base):
	def setUp(self):
		super().setUp()
		self.dt = draft_doctype() or self.skipTest("no submittable doctype installed")
		self.applied = []

		def _dispatch(tool, args, **kw):
			self.applied.append((tool, args.get("doctype"), args.get("name")))
			return {"ok": True, "data": {"doctype": args.get("doctype"), "name": f"D-{len(self.applied)}"}}

		dc = patch("jarvis.api.dispatch_confirmed", side_effect=_dispatch)
		dc.start()
		self.addCleanup(dc.stop)

	def update(self, name, **changes):
		return {"doctype": self.dt, "name": name, "changes": changes or {"remarks": "x"}}

	def test_update_of_a_draft_the_run_created_is_applied(self):
		conv = self.conv()
		made = self.call("create_doc", {"doctype": self.dt, "values": {"remarks": "x"}}, conv)
		self.assertTrue(made["ok"], made)
		name = made["data"]["name"]
		with patch.object(held_writes, "_hold", side_effect=AssertionError("held")):
			res = self.call("update_doc", self.update(name), conv)
		self.assertTrue(res["ok"], res)
		self.assertEqual(self.applied[-1], ("update_doc", self.dt, name))

	def test_update_of_a_draft_the_run_did_not_create_is_held(self):
		conv = self.conv()
		held = {"ok": True, "data": {"status": "pending_confirmation"}}
		with patch.object(held_writes, "_hold", return_value=held) as hold:
			res = self.call("update_doc", self.update("OTHER-DRAFT-1"), conv)
		self.assertIs(res, held)
		self.assertEqual(hold.call_count, 1)
		self.assertEqual(self.applied, [])

	def test_a_draft_created_in_another_run_is_not_updatable_here(self):
		first, second = self.conv(), self.conv()
		made = self.call("create_doc", {"doctype": self.dt, "values": {"remarks": "x"}}, first)
		self.assertTrue(made["ok"], made)
		with patch.object(held_writes, "_hold", return_value={"ok": True, "data": {}}) as hold:
			self.call("update_doc", self.update(made["data"]["name"]), second)
		self.assertEqual(hold.call_count, 1)
		self.assertEqual(self.applied, [("create_doc", self.dt, None)])
