"""Tests for the lone-direct handover job (jarvis#1425 Task 10).

A pooled workspace narrowed to one ChatGPT account asks admin to move it off
the proxy onto the direct leg. Stamps swap in ONE write only on a confirmed
move; every other outcome is a distinct, safe-to-retry pending status; an old
admin/fleet falls back to today's pool push.

Fixtures follow the bare frappe._dict pattern TestLoneDirectHandoverDue (Task
8, in test_unified_llm_config.py) already uses for `settings` - no DB save, no
commit. `_write_settings_fields` still writes the REAL Jarvis Settings single
row underneath (it always does, doctype writes are not object-shaped), but
that write lands inside this test's own transaction and is rolled back by
FrappeTestCase like every other write here; nothing in this file commits.
"""

import json
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client
from jarvis.admin_client import AdminContractError, AdminRejectedError, AdminUnreachableError
from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
	_PENDING_HANDOVER_STATUS,
	_enqueued_handover_via_admin,
	_handover_via_admin,
)
from jarvis.tests.test_unified_llm_config import _account, _make_settings_with_models, _subscription_model

# A well-formed direct-leg oauth blob: _direct_subscription_blob requires a
# "provider" key (it is what the fleet auth-profile write keys on) or it
# raises "missing or malformed oauth_blob" before admin is ever called.
_OAUTH_BLOB = json.dumps({"provider": "openai", "access": "AT-1", "refresh": "RT-1"})


def _handover_ready_settings(**overrides):
	"""A lone-ChatGPT-account settings fixture already synced through the pool
	leg, i.e. lone_direct_handover_due(settings) is True - the shape Task 8's
	TestLoneDirectHandoverDue.test_due_for_a_synced_lone_chatgpt_account also
	builds. proxy_active starts at 1 and llm_direct_synced_at at None so a
	swap (or the lack of one) is observable."""
	settings = _make_settings_with_models(
		[
			_subscription_model(
				order=0, model="gpt-5.5", accounts=[_account(upstream="openai", oauth_blob=_OAUTH_BLOB)]
			)
		]
	)
	settings.llm_pool_synced_at = "2026-08-01 00:00:00"
	settings.llm_provider = "OpenAI"
	settings.llm_model = "gpt-5.5"
	settings.llm_base_url = ""
	settings.last_sync_status = ""
	settings.proxy_active = 1
	settings.llm_direct_synced_at = None
	settings.update(overrides)
	return settings


class TestHandoverViaAdmin(FrappeTestCase):
	"""_handover_via_admin(settings) - the per-call decision, exercised directly
	(it never re-reads Jarvis Settings itself; only its _enqueued_* wrapper
	does), same style as Task 8's tests calling lone_direct_handover_due
	directly against a bare settings fixture."""

	def test_confirmed_handover_swaps_the_stamps_in_one_write(self):
		from jarvis.jarvis.pool_serialize import compute_pool_mode, compute_proxy_active

		settings = _handover_ready_settings()
		with patch.object(
			admin_client, "post_subscription_handover", return_value={"result": "ok", "status": "applied"}
		) as mock_post:
			fall_back = _handover_via_admin(settings)

		self.assertFalse(fall_back)
		mock_post.assert_called_once()
		self.assertEqual(mock_post.call_args.args[0], "openai")
		self.assertTrue(settings.llm_direct_synced_at)
		self.assertFalse(settings.llm_pool_synced_at)
		self.assertEqual(int(settings.proxy_active or 0), 0)
		self.assertTrue((settings.last_sync_status or "").startswith("ok"))
		# The swap makes the workspace lone-direct-capable again (the
		# non-retroactivity stamp is gone), so both derived flags flip too.
		self.assertFalse(compute_pool_mode(settings))
		self.assertFalse(compute_proxy_active(settings))

	def test_applying_or_superseded_keeps_the_pool_and_stays_pending(self):
		for result_value in ("applying", "superseded"):
			with self.subTest(result=result_value):
				settings = _handover_ready_settings()
				with patch.object(
					admin_client, "post_subscription_handover", return_value={"result": result_value}
				):
					fall_back = _handover_via_admin(settings)

				self.assertFalse(fall_back)
				self.assertEqual(settings.last_sync_status, _PENDING_HANDOVER_STATUS)
				self.assertEqual(settings.llm_pool_synced_at, "2026-08-01 00:00:00")
				self.assertIsNone(settings.llm_direct_synced_at)
				self.assertEqual(int(settings.proxy_active or 0), 1)

	def test_unreachable_admin_records_pending_handover(self):
		settings = _handover_ready_settings()
		with patch.object(
			admin_client, "post_subscription_handover", side_effect=AdminUnreachableError("timeout")
		):
			fall_back = _handover_via_admin(settings)

		self.assertFalse(fall_back)
		self.assertEqual(settings.last_sync_status, _PENDING_HANDOVER_STATUS)
		self.assertIsNone(settings.llm_direct_synced_at)
		self.assertEqual(settings.llm_pool_synced_at, "2026-08-01 00:00:00")

	def test_rejected_records_failed_with_reason(self):
		settings = _handover_ready_settings()
		rejection = AdminRejectedError(
			"admin returned a 502 error: bad spec", code="FleetConfigError", detail="bad spec"
		)
		with patch.object(admin_client, "post_subscription_handover", side_effect=rejection):
			fall_back = _handover_via_admin(settings)

		self.assertFalse(fall_back)
		self.assertTrue((settings.last_sync_status or "").startswith("failed:"))
		self.assertIn("bad spec", settings.last_sync_status)
		self.assertIsNone(settings.llm_direct_synced_at)

	def test_job_is_a_no_op_when_the_shape_moved_on(self):
		"""A second model row appeared before the job ran (a race with a fresh
		save) - the shape no longer matches, so the admin call must never fire."""
		settings = _handover_ready_settings(
			models=[
				_subscription_model(
					order=0, model="gpt-5.5", accounts=[_account(upstream="openai", oauth_blob=_OAUTH_BLOB)]
				),
				_subscription_model(
					order=1,
					model="gpt-5.6",
					accounts=[_account(upstream="openai", account_ref="ACC_002", oauth_blob=_OAUTH_BLOB)],
				),
			]
		)
		with patch.object(admin_client, "post_subscription_handover") as mock_post:
			fall_back = _handover_via_admin(settings)

		mock_post.assert_not_called()
		self.assertFalse(fall_back)


class TestEnqueuedHandoverViaAdmin(FrappeTestCase):
	"""_enqueued_handover_via_admin - the redis-locked worker, where the pool
	fallback (settings._enqueue_pool_sync()) actually lives."""

	def test_unsupported_handover_falls_back_to_the_pool_push(self):
		settings = _handover_ready_settings()
		settings._enqueue_pool_sync = MagicMock()
		rejection = AdminContractError("handover unsupported", code="HandoverUnsupported")
		with (
			patch("frappe.get_single", return_value=settings),
			patch.object(admin_client, "post_subscription_handover", side_effect=rejection) as mock_post,
		):
			_enqueued_handover_via_admin()

		mock_post.assert_called_once()
		settings._enqueue_pool_sync.assert_called_once()
		# The unsupported branch returns before any status write - untouched.
		self.assertEqual(settings.llm_pool_synced_at, "2026-08-01 00:00:00")
		self.assertIsNone(settings.llm_direct_synced_at)
