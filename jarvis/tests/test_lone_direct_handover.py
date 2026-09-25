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
FrappeTestCase like every other write here.

Task 11 (S4)'s routing tests below (`TestOnUpdateRoutesToHandover`,
`TestResyncSkipsReadyDuringHandover`) are the exception: they exercise real
`settings.save()` / `on_update` round trips against the actual Jarvis LLM Pool
Model child table (the pool-relevant snapshot / redundancy gate they cover
only reacts to genuinely persisted rows), following the `_RT3SettingsTestCase`
convention from test_unified_llm_config.py - including its commits and
`tearDownClass` model-row cleanup.
"""

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client
from jarvis.admin_client import AdminContractError, AdminRejectedError, AdminUnreachableError
from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
	_PENDING_HANDOVER_STATUS,
	JarvisSettings,
	_enqueued_handover_via_admin,
	_handover_via_admin,
	reconcile_pending_llm_sync,
)
from jarvis.tests.test_settings_on_update import _reset_settings
from jarvis.tests.test_unified_llm_config import (
	_account,
	_add_model_row,
	_make_settings_with_models,
	_RT3SettingsTestCase,
	_subscription_model,
)

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


def _add_openai_subscription_row(settings, *, order=0, account_ref="ACC_1", oauth_blob=None):
	"""Append a REAL model row for one ChatGPT (openai) subscription account.

	Uses the actual encrypted `subscription_accounts` Password field (not the
	in-memory `.accounts` fallback `_model_accounts` also honors for the bare
	frappe._dict fixtures above), so this row survives a `frappe.get_single`
	reload - required for Task 11's routing tests, which save the real
	singleton twice and need the second save's `get_doc_before_save()` to see
	a genuine prior state."""
	row = frappe.new_doc("Jarvis LLM Pool Model")
	row.provider = "openai"
	row.model = "gpt-5.5"
	row.tier = "strong"
	row.order = order
	row.enabled = 1
	row.credential_type = "subscription"
	row.rotation = "sticky"
	row.subscription_accounts = json.dumps(
		[
			{
				"upstream": "openai",
				"account_ref": account_ref,
				"label": "chatgpt@example.com",
				"oauth_blob": oauth_blob or _OAUTH_BLOB,
			}
		]
	)
	settings.append("models", row)
	return row


class TestOnUpdateRoutesToHandover(_RT3SettingsTestCase):
	"""Task 11 (S4): `_on_update_unified_llm`'s pool branch must route a due
	handover ahead of both the redundancy gate and the ordinary pool enqueue -
	no save may push the pool spec while a handover is due (jarvis#1425)."""

	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()
		settings = frappe.get_single("Jarvis Settings")
		settings.db_set("proxy_recommended", 0, update_modified=False)
		settings.db_set("llm_pool_synced_at", None, update_modified=False)
		frappe.db.commit()

	def _seed_two_model_synced_pool(self):
		"""A 2-model pool (one lone-ChatGPT-shaped subscription row + one BYO
		api-key row) that has already synced through the pool leg once - the
		same "establish a synced baseline" shape as
		TestPoolSyncChangeDetection._save_two_model_pool, but with an openai
		subscription as models[0] so narrowing down to it later is due."""
		settings = frappe.get_single("Jarvis Settings")
		_add_openai_subscription_row(settings, order=0)
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o-mini",
			tier="cheap",
			order=1,
			api_key="sk-second-model",
			base_url="https://api.openai.com",
		)
		with patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}):
			settings.save()
		settings = frappe.get_single("Jarvis Settings")
		assert (settings.last_sync_status or "").startswith("ok"), (
			f"fixture: expected the seed 2-model pool to sync ok, got {settings.last_sync_status!r}"
		)
		assert settings.llm_pool_synced_at, "fixture: expected llm_pool_synced_at to be stamped"
		return settings

	def test_settings_save_that_narrows_to_chatgpt_enqueues_the_handover(self):
		"""A save that drops the second model, leaving the lone
		already-pool-synced ChatGPT subscription account, must enqueue the
		handover - never another pool push."""
		settings = self._seed_two_model_synced_pool()
		settings.set("models", [m for m in settings.models if m.credential_type == "subscription"])

		with (
			patch.object(JarvisSettings, "_enqueue_pool_sync") as mock_pool,
			patch.object(JarvisSettings, "_enqueue_handover") as mock_handover,
		):
			settings.save()

		mock_handover.assert_called_once()
		mock_pool.assert_not_called()

	def test_background_save_while_handover_due_never_pushes_the_pool(self):
		"""Review Focus 3: once the shape is due and a handover is already
		pending (`_PENDING_HANDOVER_STATUS`), an UNRELATED background save
		(pattern-learning windows, chat-device writes, ...) must never push
		the pool spec, even though pool_mode is still structurally True."""
		settings = self._seed_two_model_synced_pool()
		settings.set("models", [m for m in settings.models if m.credential_type == "subscription"])
		with patch.object(admin_client, "post_subscription_handover", return_value={"result": "applying"}):
			settings.save()
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(
			settings.last_sync_status,
			_PENDING_HANDOVER_STATUS,
			"fixture: expected the narrowing save to leave the handover pending",
		)

		# An unrelated background save while the handover is still pending
		# (e.g. a pattern-learning window write) - nothing pool-relevant
		# changes, so this stands in for any write of this Single that lands
		# in _on_update_unified_llm without touching models/preset/routing_mode.
		with (
			patch.object(JarvisSettings, "_enqueue_pool_sync") as mock_pool,
			patch.object(admin_client, "post_subscription_handover", return_value={"result": "applying"}),
		):
			settings.save()

		mock_pool.assert_not_called()

	def test_unchanged_background_save_with_ok_status_does_nothing(self):
		"""The redundancy gate (`_pool_sync_is_redundant`) still applies to a
		due shape: an unchanged save with an "ok" status must enqueue
		neither the handover nor a pool sync."""
		settings = self._seed_two_model_synced_pool()
		settings.set("models", [m for m in settings.models if m.credential_type == "subscription"])
		with patch.object(JarvisSettings, "_enqueue_handover"):
			# Narrow, without letting the handover's own status write happen -
			# last_sync_status stays "ok ..." from the 2-model seed above, so
			# this save's own snapshot becomes the "ok" baseline the next,
			# genuinely unchanged save is compared against.
			settings.save()
		settings = frappe.get_single("Jarvis Settings")
		self.assertTrue((settings.last_sync_status or "").startswith("ok"))

		with (
			patch.object(JarvisSettings, "_enqueue_pool_sync") as mock_pool,
			patch.object(JarvisSettings, "_enqueue_handover") as mock_handover,
		):
			settings.save()  # no pool-relevant change

		mock_pool.assert_not_called()
		mock_handover.assert_not_called()


class TestReconcileRedrivesHandover(FrappeTestCase):
	"""Task 11 (S4): the */5 reconcile must re-drive a due handover instead of
	stamping - admin reporting the still-pooled tenant Ready must never close
	a move that never ran (jarvis#1425)."""

	def test_reconcile_redrives_a_pending_handover_instead_of_stamping(self):
		settings = _handover_ready_settings(last_sync_status=_PENDING_HANDOVER_STATUS)
		settings.get_password = MagicMock(return_value="test-admin-key")
		settings._enqueue_handover = MagicMock()
		with (
			patch("frappe.get_single", return_value=settings),
			patch(
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._admin_chat_readiness",
				return_value=("Ready", ""),
			) as mock_readiness,
			patch("jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._stamp_converged_ok") as mock_stamp,
		):
			reconcile_pending_llm_sync()

		settings._enqueue_handover.assert_called_once()
		mock_stamp.assert_not_called()
		# The whole point: this returns BEFORE the probe, so a due handover is
		# re-driven without ever asking admin.
		mock_readiness.assert_not_called()


class TestResyncSkipsReadyDuringHandover(_RT3SettingsTestCase):
	"""Task 11 (S4): resync_llm's Ready shortcut must not stamp a due handover
	as converged - the still-pooled tenant reporting Ready is exactly the case
	the handover exists to move off of (jarvis#1425)."""

	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()

	def test_resync_while_handover_due_skips_the_ready_shortcut(self):
		from jarvis import onboarding

		settings = frappe.get_single("Jarvis Settings")
		_add_openai_subscription_row(settings, order=0)
		settings.db_set("llm_pool_synced_at", "2026-08-01 00:00:00", update_modified=False)
		with (
			patch.object(JarvisSettings, "_enqueue_pool_sync"),
			patch.object(JarvisSettings, "_enqueue_handover"),
		):
			# Establish the due shape without letting either enqueue fire for
			# real - this save's own routing isn't what's under test here.
			settings.save()

		frappe.cache().delete_value(onboarding._RESYNC_COOLDOWN_KEY)

		with (
			patch(
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._admin_chat_readiness",
				return_value=("Ready", ""),
			) as mock_readiness,
			patch("jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._stamp_converged_ok") as mock_stamp,
			patch.object(JarvisSettings, "_enqueue_handover") as mock_handover,
		):
			out = onboarding.resync_llm()

		mock_readiness.assert_called_once()
		mock_stamp.assert_not_called()
		mock_handover.assert_called_once()
		self.assertEqual(out["outcome"], "queued")
		self.assertEqual(out["leg"], "direct")
