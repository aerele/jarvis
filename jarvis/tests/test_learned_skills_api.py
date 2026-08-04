"""Tests for the learned-skills push chain (jarvis/chat/learned_skills_api.py)
namespace-cutover chaining: the ``chain_custom_reconcile`` flag rides the
enqueue kwargs; the worker enqueues the GRACEFUL custom reconcile (the
strict=False ``_enqueued_push_custom_skills`` worker - never the strict
interactive ``apply_custom_skills``) ONLY after a confirmed-ok push, stamping
the custom pair pending first, and never on a failure path (the stale
``custom-learned-*`` dirs keep the old guidance live until a later reconcile).

End-to-end cutover coverage (real apply -> chained inline workers, over-cap
bench decoupling, fresh-tenant gate) lives in
jarvis/tests/learning/test_compiler.py.
"""

from __future__ import annotations

import unittest
from unittest import mock

import frappe

from jarvis.chat import learned_skills_api

SETTINGS = "Jarvis Settings"


class TestLearnedSkillsCutoverChain(unittest.TestCase):
	def setUp(self):
		self._sync = (
			frappe.db.get_value(
				SETTINGS,
				SETTINGS,
				[
					"learned_skills_synced_at",
					"learned_skills_sync_status",
					"custom_skills_synced_at",
					"custom_skills_sync_status",
				],
				as_dict=True,
			)
			or frappe._dict()
		)

	def tearDown(self):
		frappe.db.set_value(
			SETTINGS,
			SETTINGS,
			{
				"learned_skills_synced_at": self._sync.get("learned_skills_synced_at"),
				"learned_skills_sync_status": self._sync.get("learned_skills_sync_status"),
				"custom_skills_synced_at": self._sync.get("custom_skills_synced_at"),
				"custom_skills_sync_status": self._sync.get("custom_skills_sync_status"),
			},
			update_modified=False,
		)
		frappe.db.commit()

	def _custom_status(self) -> str:
		return frappe.db.get_value(SETTINGS, SETTINGS, "custom_skills_sync_status") or ""

	def _learned_status(self) -> str:
		return frappe.db.get_value(SETTINGS, SETTINGS, "learned_skills_sync_status") or ""

	# ------------------------------------------------------------------ #
	# enqueue contract: the chain flag rides the job kwargs
	# ------------------------------------------------------------------ #
	def test_enqueue_passes_chain_flag(self):
		with mock.patch("frappe.enqueue") as enq:
			learned_skills_api.enqueue_learned_skills_push(chain_custom_reconcile=True)
		self.assertIs(enq.call_args.kwargs["chain_custom_reconcile"], True)

		with mock.patch("frappe.enqueue") as enq:
			learned_skills_api.enqueue_learned_skills_push()
		self.assertIs(enq.call_args.kwargs["chain_custom_reconcile"], False)

	# ------------------------------------------------------------------ #
	# worker: chain fires ONLY on a confirmed-ok push
	# ------------------------------------------------------------------ #
	def test_worker_success_chains_graceful_custom_worker(self):
		with (
			mock.patch("jarvis.admin_client.post_push_learned_skills", return_value={}),
			mock.patch("frappe.enqueue") as enq,
		):
			learned_skills_api._enqueued_push_learned_skills(chain_custom_reconcile=True)
		self.assertTrue(self._learned_status().startswith("ok ("))
		self.assertIn("installed via admin", self._learned_status())
		# the custom pair is stamped pending BEFORE the reconcile is enqueued
		# (the same stamps the SPA custom apply uses - the board polls them).
		self.assertEqual(self._custom_status(), "pending: applying skills")
		enq.assert_called_once()
		self.assertEqual(
			enq.call_args.args[0],
			"jarvis.chat.custom_skills_api._enqueued_push_custom_skills",
		)
		kwargs = enq.call_args.kwargs
		self.assertEqual(kwargs["job_id"], "jarvis_custom_skills_push")
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["queue"], "long")

	def test_worker_failure_does_not_chain(self):
		from jarvis.exceptions import AdminUnreachableError

		before = self._custom_status()
		with (
			mock.patch(
				"jarvis.admin_client.post_push_learned_skills",
				side_effect=AdminUnreachableError("boom"),
			),
			mock.patch("frappe.enqueue") as enq,
		):
			learned_skills_api._enqueued_push_learned_skills(chain_custom_reconcile=True)
		self.assertTrue(self._learned_status().startswith("failed: admin unreachable"))
		# never stamped pending, never enqueued: the old dirs stay live.
		self.assertEqual(self._custom_status(), before)
		enq.assert_not_called()

	def test_worker_default_does_not_chain(self):
		before = self._custom_status()
		with (
			mock.patch("jarvis.admin_client.post_push_learned_skills", return_value={}),
			mock.patch("frappe.enqueue") as enq,
		):
			learned_skills_api._enqueued_push_learned_skills()
		self.assertTrue(self._learned_status().startswith("ok ("))
		self.assertEqual(self._custom_status(), before)
		enq.assert_not_called()

	# ------------------------------------------------------------------ #
	# reviewer-facing terminal wording (#479)
	# ------------------------------------------------------------------ #
	def test_ok_status_names_the_role_restricted_count(self):
		# Every compiled managed row carries allowed_roles, so "applied 1" after a
		# four-domain Apply was the normal reading of a healthy bench and reviewers
		# would file it as a bug. The status now separates what reached the
		# container from what is held bench-side and fetched on demand.
		self.assertEqual(
			learned_skills_api._ok_status(1, 3),
			"ok (1 installed via admin, 3 role-restricted and fetched on demand)",
		)
		self.assertEqual(
			learned_skills_api._ok_status(0, 4),
			"ok (0 installed via admin, 4 role-restricted and fetched on demand)",
		)
		# nothing held back -> no clause about it, so the common case stays terse.
		self.assertEqual(learned_skills_api._ok_status(2, 0), "ok (2 installed via admin)")


if __name__ == "__main__":
	unittest.main()
