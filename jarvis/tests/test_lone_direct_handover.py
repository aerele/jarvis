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
	_HANDOVER_MAX_ATTEMPTS,
	_PENDING_APPLYING_STATUS,
	_PENDING_HANDOVER_STATUS,
	JarvisSettings,
	_end_switch_if_released,
	_enqueued_handover_via_admin,
	_enqueued_sync_via_admin,
	_handover_attempt,
	_handover_via_admin,
	_switch_or_enqueue,
	reconcile_pending_llm_sync,
	request_resync,
)
from jarvis.tests.test_settings_on_update import _reset_settings
from jarvis.tests.test_unified_llm_config import (
	_account,
	_add_model_row,
	_api_key_model,
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
	directly against a bare settings fixture.

	CR-3 (2026-09-25 review): the return value is now the follow-up action the
	caller must run AFTER releasing the admin-sync lock - None, "pool", or
	"resync" - not a bool. Tests that only care "nothing further to do here"
	assert assertIsNone; the fallback/resync cases are exercised through
	_enqueued_handover_via_admin below, where the follow-up actually runs."""

	def test_confirmed_handover_swaps_the_stamps_in_one_write(self):
		from jarvis.jarvis.pool_serialize import compute_pool_mode, compute_proxy_active

		settings = _handover_ready_settings()
		with patch.object(
			admin_client, "post_subscription_handover", return_value={"result": "ok", "status": "applied"}
		) as mock_post:
			follow_up = _handover_via_admin(settings)

		self.assertIsNone(follow_up)
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
					follow_up = _handover_via_admin(settings)

				self.assertIsNone(follow_up)
				# 2026-09-25 review: the pending write now always carries the
				# attempt suffix; the fixture starts with no pending-handover
				# status at all (last_sync_status=""), so _handover_attempt reads
				# 0 there and max(1, 0) is attempt 1.
				self.assertEqual(settings.last_sync_status, f"{_PENDING_HANDOVER_STATUS} (attempt 1)")
				self.assertEqual(settings.llm_pool_synced_at, "2026-08-01 00:00:00")
				self.assertIsNone(settings.llm_direct_synced_at)
				self.assertEqual(int(settings.proxy_active or 0), 1)

	def test_unreachable_admin_records_pending_handover(self):
		settings = _handover_ready_settings()
		with patch.object(
			admin_client, "post_subscription_handover", side_effect=AdminUnreachableError("timeout")
		):
			follow_up = _handover_via_admin(settings)

		self.assertIsNone(follow_up)
		self.assertEqual(settings.last_sync_status, f"{_PENDING_HANDOVER_STATUS} (attempt 1)")
		self.assertIsNone(settings.llm_direct_synced_at)
		self.assertEqual(settings.llm_pool_synced_at, "2026-08-01 00:00:00")

	def test_unreachable_outcome_keeps_the_current_attempt_number(self):
		"""2026-09-25 review: a pending write must re-record the CURRENT attempt,
		never reset it - only _enqueue_handover ever advances/resets the count."""
		settings = _handover_ready_settings(last_sync_status=f"{_PENDING_HANDOVER_STATUS} (attempt 2)")
		with patch.object(
			admin_client, "post_subscription_handover", side_effect=AdminUnreachableError("timeout")
		):
			follow_up = _handover_via_admin(settings)

		self.assertIsNone(follow_up)
		self.assertEqual(settings.last_sync_status, f"{_PENDING_HANDOVER_STATUS} (attempt 2)")

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
			follow_up = _handover_via_admin(settings)

		mock_post.assert_not_called()
		self.assertIsNone(follow_up)

	def test_never_paid_auth_records_not_paid_without_raising(self):
		"""CR-5: mirrors _sync_via_admin's exemption - a never-paid 403 is an
		ordinary billing state, not a genuine auth defect."""
		settings = _handover_ready_settings()
		auth_err = admin_client.AdminAuthError("forbidden", status_code=403, code="CUSTOMER_NOT_PAID")
		with patch.object(admin_client, "post_subscription_handover", side_effect=auth_err):
			follow_up = _handover_via_admin(settings)  # must not raise

		self.assertIsNone(follow_up)
		self.assertEqual(settings.last_sync_status, "failed: not paid (CUSTOMER_NOT_PAID)")

	def test_other_auth_error_raises(self):
		"""CR-5: any OTHER auth failure keeps today's behavior - terminal status,
		then re-raise so the rq job surfaces it."""
		settings = _handover_ready_settings()
		auth_err = admin_client.AdminAuthError("token expired", status_code=401)
		with patch.object(admin_client, "post_subscription_handover", side_effect=auth_err):
			with self.assertRaises(admin_client.AdminAuthError):
				_handover_via_admin(settings)

		self.assertEqual(settings.last_sync_status, f"failed: auth: {auth_err}")


class TestEnqueuedHandoverViaAdmin(FrappeTestCase):
	"""_enqueued_handover_via_admin - the redis-locked worker, where the two
	follow-up actions _handover_via_admin can report - a pool push
	(settings._enqueue_pool_sync()) or a resync (request_resync) - actually
	run, and only AFTER the admin-sync redis lock is released (CR-3)."""

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

	def test_rejected_falls_back_to_the_pool_push(self):
		"""CR-2: a TERMINAL rejection (admin reached, permanently refused) must
		not strand the workspace on "failed:" - it falls back exactly like the
		unsupported case, and the pool push (not this function) owns the status
		afterwards, so nothing here is written."""
		settings = _handover_ready_settings()
		settings._enqueue_pool_sync = MagicMock()
		rejection = AdminRejectedError(
			"admin returned a 502 error: bad spec", code="FleetConfigError", detail="bad spec"
		)
		with (
			patch("frappe.get_single", return_value=settings),
			patch.object(admin_client, "post_subscription_handover", side_effect=rejection) as mock_post,
		):
			_enqueued_handover_via_admin()

		mock_post.assert_called_once()
		settings._enqueue_pool_sync.assert_called_once()
		self.assertEqual(settings.llm_pool_synced_at, "2026-08-01 00:00:00")
		self.assertIsNone(settings.llm_direct_synced_at)
		self.assertEqual(settings.last_sync_status, "", "stamps untouched - the pool push owns the status")

	def test_non_unsupported_validation_rejection_falls_back_to_the_pool_push(self):
		"""CR-2's other half: a business AdminValidationError (not the
		missing-endpoint/unsupported shape) is just as terminal, so it takes the
		same fallback."""
		settings = _handover_ready_settings()
		settings._enqueue_pool_sync = MagicMock()
		rejection = admin_client.AdminValidationError("unusable oauth grant")
		with (
			patch("frappe.get_single", return_value=settings),
			patch.object(admin_client, "post_subscription_handover", side_effect=rejection) as mock_post,
		):
			_enqueued_handover_via_admin()

		mock_post.assert_called_once()
		settings._enqueue_pool_sync.assert_called_once()
		self.assertEqual(settings.last_sync_status, "", "stamps untouched - the pool push owns the status")

	def test_not_due_with_pending_status_resyncs_after_the_lock(self):
		"""CR-3: the shape moved on (no longer due) while a handover was left
		pending - request_resync must run, and only AFTER the redis lock this
		function held is released (it acquires the SAME lock itself)."""
		settings = _handover_ready_settings(
			last_sync_status=_PENDING_HANDOVER_STATUS,
			models=[
				_subscription_model(
					order=0, model="gpt-5.5", accounts=[_account(upstream="openai", oauth_blob=_OAUTH_BLOB)]
				),
				_subscription_model(
					order=1,
					model="gpt-5.6",
					accounts=[_account(upstream="openai", account_ref="ACC_002", oauth_blob=_OAUTH_BLOB)],
				),
			],
		)
		with (
			patch("frappe.get_single", return_value=settings),
			patch("jarvis.jarvis.doctype.jarvis_settings.jarvis_settings.request_resync") as mock_resync,
			patch.object(admin_client, "post_subscription_handover") as mock_post,
		):
			_enqueued_handover_via_admin()

		mock_post.assert_not_called()
		mock_resync.assert_called_once_with(settings)


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


class TestHandoverAttempt(FrappeTestCase):
	"""_handover_attempt(status) - the parser reconcile's retry budget (below)
	is built on. Pure function, no fixture needed."""

	def test_no_suffix_reads_as_attempt_one(self):
		"""An old row written before this counter existed (or _enqueue_handover's
		very first write) has no suffix at all - reads as attempt 1, not 0."""
		self.assertEqual(_handover_attempt(_PENDING_HANDOVER_STATUS), 1)

	def test_parses_the_attempt_suffix(self):
		self.assertEqual(_handover_attempt(f"{_PENDING_HANDOVER_STATUS} (attempt 2)"), 2)

	def test_an_unrelated_status_reads_as_zero(self):
		"""Not a pending-handover status at all - the caller's is-this-a-handover
		check and the parse gate are the same test."""
		self.assertEqual(_handover_attempt("ok (moved to direct via admin)"), 0)
		self.assertEqual(_handover_attempt(_PENDING_APPLYING_STATUS), 0)
		self.assertEqual(_handover_attempt(""), 0)


class TestEnqueueHandoverAttempt(FrappeTestCase):
	"""JarvisSettings._enqueue_handover - the write side of the attempt counter."""

	def test_a_user_initiated_apply_resets_the_attempt_budget(self):
		"""A workspace already stuck at attempt 3 (reconcile has been retrying a
		failing fleet) gets a FRESH budget the moment a user acts (on_update,
		save_llm_pool, request_resync all call this with the default) - a user
		action is not a continuation of a stuck automatic retry."""
		settings = _handover_ready_settings(last_sync_status=f"{_PENDING_HANDOVER_STATUS} (attempt 3)")
		with patch("frappe.enqueue"):
			JarvisSettings._enqueue_handover(settings)

		self.assertEqual(settings.last_sync_status, f"{_PENDING_HANDOVER_STATUS} (attempt 1)")


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
			f"{_PENDING_HANDOVER_STATUS} (attempt 1)",
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


class TestReconcileHandoverAttemptBudget(FrappeTestCase):
	"""2026-09-25 review (production hazard): reconcile's re-drive of a due
	handover must be BOUNDED. Each attempt stops and restarts the customer's
	container, so a persistently failing fleet endpoint (e.g. doctor always
	fails) must not be re-driven every */5 tick forever - that bounces chat
	indefinitely. Skip a tick whose in-band job may still be running; past
	that, count attempts and give up onto the pool push after
	_HANDOVER_MAX_ATTEMPTS, leaving the workspace on the proxy (working)."""

	def _reconcile_ready_settings(self, **overrides):
		settings = _handover_ready_settings(**overrides)
		settings.get_password = MagicMock(return_value="test-admin-key")
		settings._enqueue_handover = MagicMock()
		settings._enqueue_pool_sync = MagicMock()
		return settings

	def test_a_fresh_request_does_nothing_the_job_may_still_be_running(self):
		settings = self._reconcile_ready_settings(
			last_sync_status=f"{_PENDING_HANDOVER_STATUS} (attempt 1)",
			last_sync_requested_at=frappe.utils.now(),
		)
		with patch("frappe.get_single", return_value=settings):
			reconcile_pending_llm_sync()

		settings._enqueue_handover.assert_not_called()
		settings._enqueue_pool_sync.assert_not_called()

	def test_a_stale_attempt_one_bumps_to_attempt_two(self):
		settings = self._reconcile_ready_settings(
			last_sync_status=f"{_PENDING_HANDOVER_STATUS} (attempt 1)",
			last_sync_requested_at="2020-01-01 00:00:00",
		)
		with patch("frappe.get_single", return_value=settings):
			reconcile_pending_llm_sync()

		settings._enqueue_handover.assert_called_once_with(attempt=2)
		settings._enqueue_pool_sync.assert_not_called()

	def test_a_stale_attempt_at_the_max_gives_up_and_pushes_the_pool(self):
		settings = self._reconcile_ready_settings(
			last_sync_status=f"{_PENDING_HANDOVER_STATUS} (attempt {_HANDOVER_MAX_ATTEMPTS})",
			last_sync_requested_at="2020-01-01 00:00:00",
		)
		with (
			patch("frappe.get_single", return_value=settings),
			patch("frappe.log_error") as mock_log_error,
		):
			reconcile_pending_llm_sync()

		settings._enqueue_pool_sync.assert_called_once()
		settings._enqueue_handover.assert_not_called()
		mock_log_error.assert_called_once()


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


# ---------------------------------------------------------------------------
# Task 5 (jarvis#1425 follow-up, seamless proxy <-> direct switch, spec Part
# C): every proxy <-> direct switch routes through llm_switch.begin(), ends
# on every outcome, and the pollers reconcile it.
# ---------------------------------------------------------------------------


class TestEnqueueRoutesThroughSwitch(FrappeTestCase):
	"""_enqueue_handover ALWAYS begins a switch (spec Part C: "the handover job
	(_enqueue_handover)"), so it must route through llm_switch.begin() instead
	of a direct frappe.enqueue."""

	def test_handover_is_routed_through_the_switch(self):
		settings = _handover_ready_settings()
		with (
			patch("jarvis.chat.llm_switch.begin") as mock_begin,
			patch("frappe.enqueue") as mock_enqueue,
		):
			JarvisSettings._enqueue_handover(settings)

		mock_begin.assert_called_once_with(
			"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_handover_via_admin",
			job_id="jarvis_settings_sync:handover",
			deduplicate=True,
		)
		mock_enqueue.assert_not_called()
		self.assertEqual(settings.last_sync_status, f"{_PENDING_HANDOVER_STATUS} (attempt 1)")


class TestSwitchBeginDeferredToAfterCommit(FrappeTestCase):
	"""Review Focus 1 (updated for review round 1 ruling #3): _switch_or_enqueue
	calls llm_switch.begin() DIRECTLY and lets begin() own its entire
	inline-vs-after-commit decision - it must NOT re-implement that check and
	wrap begin() in a second frappe.db.after_commit.add() (harmless but
	pointless double indirection the review flagged). This test exercises the
	REAL routing path (JarvisSettings._enqueue_handover, not llm_switch.begin
	called directly) so a reintroduced double-wrap would be caught: it would
	still queue exactly one after-commit callback, but running it would call
	llm_switch.begin() again (itself re-queuing) instead of _begin_now
	directly, so `mock_begin_now.assert_called_once_with(...)` below would
	fail. FrappeTestCase forces frappe.flags.in_test True for every test,
	which makes begin() take the immediate branch instead - this test flips
	both inline flags off around the call to exercise the real deferred
	branch, then restores them and drains the one callback it queued so
	nothing leaks into a later test's commit."""

	def test_handover_switch_defers_to_after_commit_via_begin_with_no_double_wrap(self):
		from jarvis.chat import llm_switch

		settings = _handover_ready_settings()
		original_in_test = frappe.flags.in_test
		original_inline = frappe.flags.get("run_admin_sync_inline")
		frappe.flags.in_test = False
		frappe.flags.run_admin_sync_inline = False
		queued = None
		try:
			with patch.object(llm_switch, "_begin_now") as mock_begin_now:
				before_count = len(frappe.db.after_commit._functions)
				JarvisSettings._enqueue_handover(settings)

				self.assertEqual(
					len(frappe.db.after_commit._functions),
					before_count + 1,
					"_enqueue_handover must route through begin()'s OWN single "
					"after-commit deferral, not double-wrap it",
				)
				mock_begin_now.assert_not_called()

				queued = list(frappe.db.after_commit._functions)[-1]
				queued()

				mock_begin_now.assert_called_once_with(
					"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_handover_via_admin",
					{"job_id": "jarvis_settings_sync:handover", "deduplicate": True},
				)
		finally:
			frappe.flags.in_test = original_in_test
			frappe.flags.run_admin_sync_inline = original_inline
			if queued is not None:
				try:
					frappe.db.after_commit._functions.remove(queued)
				except ValueError:
					pass


class TestPoolSyncSwitchDetection(_RT3SettingsTestCase):
	"""JarvisSettings._enqueue_pool_sync routes through llm_switch.begin() only
	when THIS push flips proxy_active (direct -> proxy, or a converge-teardown
	that turns it off) - every other pool push keeps today's direct enqueue
	(spec Part C, "What counts as a switch")."""

	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()

	def _save_two_api_key_pool(self):
		"""Baseline: a 2-model, all-api-key pool - pool_mode True (>=2 enabled
		models), proxy_active False (no subscription model at all)."""
		settings = frappe.get_single("Jarvis Settings")
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o",
			order=0,
			api_key="sk-1",
			base_url="https://api.openai.com",
		)
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o-mini",
			order=1,
			api_key="sk-2",
			base_url="https://api.openai.com",
		)
		with patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}):
			settings.save()
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(
			int(settings.proxy_active or 0),
			0,
			"fixture: expected an all-api-key pool to be proxy-inactive",
		)
		return settings

	def test_direct_to_proxy_pool_sync_is_a_switch(self):
		settings = self._save_two_api_key_pool()
		# Replace the second (api-key) row with a chat subscription - still a
		# 2-model pool (pool_mode stays True), but proxy_active now flips 0 -> 1
		# (a non-Claude, non-lone-direct subscription needs the sidecar).
		settings.set("models", [m for m in settings.models if m.credential_type == "api_key"])
		_add_openai_subscription_row(settings, order=1, account_ref="ACC_SWITCH_1")

		with (
			patch("jarvis.chat.llm_switch.begin") as mock_begin,
			patch("frappe.enqueue") as mock_enqueue,
		):
			settings.save()

		mock_begin.assert_called_once()
		self.assertEqual(
			mock_begin.call_args.args[0],
			"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_sync_via_admin_pool",
		)
		self.assertEqual(mock_begin.call_args.kwargs.get("job_id"), "jarvis_settings_sync:pool")
		self.assertFalse(mock_begin.call_args.kwargs.get("converge_teardown"))
		mock_enqueue.assert_not_called()

	def test_pool_sync_that_leaves_proxy_active_alone_enqueues_directly(self):
		settings = self._save_two_api_key_pool()
		# An unrelated pool-relevant change (still all api-key) - not redundant
		# (the snapshot differs), but proxy_active stays 0 throughout.
		settings.models[0].model = "gpt-4o-2"

		with (
			patch("jarvis.chat.llm_switch.begin") as mock_begin,
			patch("frappe.enqueue") as mock_enqueue,
		):
			settings.save()

		mock_enqueue.assert_called_once()
		self.assertEqual(
			mock_enqueue.call_args.args[0],
			"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_sync_via_admin_pool",
		)
		mock_begin.assert_not_called()

	def test_converge_teardown_that_flips_proxy_active_off_is_also_a_switch(self):
		settings = frappe.get_single("Jarvis Settings")
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o",
			order=0,
			api_key="sk-1",
			base_url="https://api.openai.com",
		)
		_add_openai_subscription_row(settings, order=1, account_ref="ACC_TEARDOWN_1")
		with patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}):
			settings.save()
		settings = frappe.get_single("Jarvis Settings")
		self.assertEqual(
			int(settings.proxy_active or 0), 1, "fixture: expected the 2-model pool to be proxy-active"
		)

		# Narrow back to the lone api-key row: pool_mode drops to False (< 2
		# models, no subscription), which is_leaving_pool_mode routes through
		# _enqueue_pool_sync(converge_teardown=True) - and proxy_active flips
		# 1 -> 0 in the same save.
		settings.set("models", [m for m in settings.models if m.credential_type == "api_key"])

		with (
			patch("jarvis.chat.llm_switch.begin") as mock_begin,
			patch("frappe.enqueue") as mock_enqueue,
		):
			settings.save()

		mock_begin.assert_called_once()
		self.assertEqual(mock_begin.call_args.kwargs.get("job_id"), "jarvis_settings_sync:pool:teardown")
		self.assertTrue(mock_begin.call_args.kwargs.get("converge_teardown"))
		mock_enqueue.assert_not_called()


class TestHandoverEndsTheSwitch(FrappeTestCase):
	"""_enqueued_handover_via_admin ends the switch _enqueue_handover began on
	every terminal outcome (ok, a bounded pending-retry, or a genuine failure)
	- but ONLY if that switch actually released this run (review round 1,
	Critical: _end_switch_if_released) - and defers ending (never ends it
	here) on the pool/resync fallback - spec Part C: "A retry of a failed
	handover (attempt N) is a new switch"."""

	def _run_with_follow_up(self, settings, follow_up, *, last_sync_status, switch_status=None):
		"""frappe.get_single -> settings (patched); _handover_via_admin patched
		to report follow_up, with last_sync_status set to whatever the real
		function would have written for that outcome - _enqueued_handover_via_admin
		reads the CURRENT status straight from the DB to end() with, not from
		this bare fixture, so frappe.db.get_value is patched to match.

		``switch_status``: what llm_switch.status() reports to
		_end_switch_if_released - defaults to an APPLIED record (this
		attempt's own switch, released and ready to end), the shape every
		"terminal outcome ends the switch" test below needs; pass an
		unapplied record (or None) to exercise the release guard itself."""
		if switch_status is None:
			switch_status = {"applied": True, "job": "x"}
		settings.last_sync_status = last_sync_status
		with (
			patch("frappe.get_single", return_value=settings),
			patch(
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._handover_via_admin",
				return_value=follow_up,
			),
			patch("frappe.db.get_value", return_value=last_sync_status),
			patch("jarvis.chat.llm_switch.status", return_value=switch_status),
		):
			_enqueued_handover_via_admin()

	def test_confirmed_move_ends_the_switch(self):
		settings = _handover_ready_settings()
		with patch("jarvis.chat.llm_switch.end") as mock_end:
			self._run_with_follow_up(settings, None, last_sync_status="ok (moved to direct via admin)")

		mock_end.assert_called_once_with("ok (moved to direct via admin)")

	def test_pending_retry_ends_the_switch(self):
		settings = _handover_ready_settings()
		with patch("jarvis.chat.llm_switch.end") as mock_end:
			self._run_with_follow_up(
				settings, None, last_sync_status=f"{_PENDING_HANDOVER_STATUS} (attempt 1)"
			)

		mock_end.assert_called_once_with(f"{_PENDING_HANDOVER_STATUS} (attempt 1)")

	def test_failed_ends_the_switch(self):
		settings = _handover_ready_settings()
		with patch("jarvis.chat.llm_switch.end") as mock_end:
			self._run_with_follow_up(settings, None, last_sync_status="failed: auth: token expired")

		mock_end.assert_called_once_with("failed: auth: token expired")

	def test_a_terminal_outcome_never_ends_a_switch_it_did_not_release(self):
		"""Review round 1 (Critical): if the active record is still HELD (not
		applied - e.g. it belongs to a DIFFERENT, still-waiting switch), this
		outcome must not end it, even though its own status write is
		terminal."""
		settings = _handover_ready_settings()
		with patch("jarvis.chat.llm_switch.end") as mock_end:
			self._run_with_follow_up(
				settings,
				None,
				last_sync_status="ok (moved to direct via admin)",
				switch_status={"applied": False, "job": "x"},
			)

		mock_end.assert_not_called()

	def test_pool_fallback_delegates_to_enqueue_pool_sync_and_never_ends_here(self):
		"""The retarget-vs-plain-enqueue decision now lives entirely inside
		_switch_or_enqueue (review round 1: it OR's in
		llm_switch.is_active()), so this worker just calls
		settings._enqueue_pool_sync() unconditionally, exactly as before Task
		5, and lets the switch continue - the retarget itself is covered by
		TestSwitchOrEnqueueJoinsAnActiveSwitch below."""
		settings = _handover_ready_settings()
		settings._enqueue_pool_sync = MagicMock()
		with patch("jarvis.chat.llm_switch.end") as mock_end:
			self._run_with_follow_up(settings, "pool", last_sync_status="")

		settings._enqueue_pool_sync.assert_called_once_with()
		mock_end.assert_not_called()


class TestPollersCallSwitchReconcile(FrappeTestCase):
	"""onboarding.get_llm_sync_status (the SPA polls it while an apply is
	pending) and the scheduled reconcile_pending_llm_sync both drive
	llm_switch forward on every call."""

	def test_reconcile_pending_llm_sync_calls_switch_reconcile_before_the_early_return(self):
		"""llm_switch.reconcile() must fire even on a tick that is otherwise a
		no-op for this function's own logic (no admin credentials yet)."""
		settings = frappe._dict(get_password=MagicMock(return_value=""))
		with (
			patch("frappe.get_single", return_value=settings),
			patch("jarvis.chat.llm_switch.reconcile") as mock_reconcile,
		):
			reconcile_pending_llm_sync()

		mock_reconcile.assert_called_once()

	def test_get_llm_sync_status_calls_switch_reconcile(self):
		from jarvis import onboarding

		with patch("jarvis.chat.llm_switch.reconcile") as mock_reconcile:
			onboarding.get_llm_sync_status()

		mock_reconcile.assert_called_once()


# ---------------------------------------------------------------------------
# Task 5, Fix round 1 (jarvis#1425 review): a held-but-unapplied switch must
# never be bypassed by an unrelated apply, nor wrongly cleared by one that
# was.
# ---------------------------------------------------------------------------


class TestSwitchOrEnqueueJoinsAnActiveSwitch(FrappeTestCase):
	"""Critical fix: _switch_or_enqueue must route EVERY apply through
	llm_switch.begin() while a switch is active, even when the caller's OWN
	is_switch computation says False - an unrelated background save (pattern
	learning, chat-device writes) reaching _enqueue_pool_sync while a switch
	is HELD (not yet applied) would otherwise compare against a before-doc
	whose proxy_active is ALREADY the switch's target value, read
	is_switch=False, and fall through to a plain frappe.enqueue that runs at
	once - recreating the container mid-reply and letting its own terminal
	write wrongly end the switch it never went through."""

	def test_background_save_during_a_held_switch_retargets_instead_of_enqueuing(self):
		with (
			patch("jarvis.chat.llm_switch.is_active", return_value=True),
			patch("jarvis.chat.llm_switch.begin") as mock_begin,
			patch("jarvis.chat.llm_switch.end") as mock_end,
			patch("frappe.enqueue") as mock_enqueue,
		):
			_switch_or_enqueue(
				frappe._dict(),
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_sync_via_admin_pool",
				is_switch=False,
				job_id="jarvis_settings_sync:pool",
				deduplicate=True,
			)

		mock_begin.assert_called_once_with(
			"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_sync_via_admin_pool",
			job_id="jarvis_settings_sync:pool",
			deduplicate=True,
		)
		mock_enqueue.assert_not_called()
		# _switch_or_enqueue never ends anything itself - the held record (for
		# whatever switch is active) is left exactly as begin()'s own
		# idempotent retarget leaves it.
		mock_end.assert_not_called()

	def test_is_switch_true_still_routes_through_begin_when_no_switch_is_active(self):
		"""Sanity check the OR the other way: a genuine switch-worthy apply
		with no OTHER switch active still goes through begin(), unchanged
		from before this fix."""
		with (
			patch("jarvis.chat.llm_switch.is_active", return_value=False),
			patch("jarvis.chat.llm_switch.begin") as mock_begin,
			patch("frappe.enqueue") as mock_enqueue,
		):
			_switch_or_enqueue(
				frappe._dict(),
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_handover_via_admin",
				is_switch=True,
				job_id="jarvis_settings_sync:handover",
				deduplicate=True,
			)

		mock_begin.assert_called_once()
		mock_enqueue.assert_not_called()

	def test_neither_switch_nor_active_enqueues_directly(self):
		with (
			patch("jarvis.chat.llm_switch.is_active", return_value=False),
			patch("jarvis.chat.llm_switch.begin") as mock_begin,
			patch("frappe.enqueue") as mock_enqueue,
		):
			_switch_or_enqueue(
				frappe._dict(),
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_sync_via_admin_pool",
				is_switch=False,
				job_id="jarvis_settings_sync:pool",
				deduplicate=True,
			)

		mock_enqueue.assert_called_once()
		mock_begin.assert_not_called()


class TestEndSwitchIfReleased(FrappeTestCase):
	"""Critical fix: a job finishing while an active switch record exists but
	is still HELD (not applied - it was never released to run) must not end
	it - that record belongs to a switch still waiting on an in-flight reply
	or the cap, and ending it here would clear the banner and promote queued
	turns while nothing has actually moved."""

	def test_does_not_end_a_held_unapplied_switch(self):
		with (
			patch("jarvis.chat.llm_switch.status", return_value={"applied": False, "job": "x"}),
			patch("jarvis.chat.llm_switch.end") as mock_end,
		):
			_end_switch_if_released("ok (something)")

		mock_end.assert_not_called()

	def test_ends_an_applied_switch(self):
		with (
			patch("jarvis.chat.llm_switch.status", return_value={"applied": True, "job": "x"}),
			patch("jarvis.chat.llm_switch.end") as mock_end,
		):
			_end_switch_if_released("ok (something)")

		mock_end.assert_called_once_with("ok (something)")

	def test_no_active_record_is_a_no_op(self):
		with (
			patch("jarvis.chat.llm_switch.status", return_value=None),
			patch("jarvis.chat.llm_switch.end") as mock_end,
		):
			_end_switch_if_released("ok (something)")

		mock_end.assert_not_called()


class TestResyncJoinsAnActiveSwitch(_RT3SettingsTestCase):
	"""Important fix #2: request_resync's pool AND direct ("restart") branches
	must join an ALREADY-active switch instead of racing it with a plain
	enqueue - request_resync runs on a fresh doc (no before-doc), so neither
	branch's own is_switch notion (the pool leg's _is_pool_switch, or the
	direct leg's total absence of one) can see a switch that started
	elsewhere."""

	def setUp(self):
		super().setUp()
		self._clear_models()
		_reset_settings()

	def test_resync_pool_branch_routes_through_begin_while_a_switch_is_active(self):
		settings = frappe.get_single("Jarvis Settings")
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o",
			order=0,
			api_key="sk-1",
			base_url="https://api.openai.com",
		)
		_add_model_row(
			settings,
			provider="openai_compat",
			model="gpt-4o-mini",
			order=1,
			api_key="sk-2",
			base_url="https://api.openai.com",
		)
		with patch("jarvis.admin_client.post_update_llm_pool", return_value={"action": "pool_update"}):
			settings.save()
		settings = frappe.get_single("Jarvis Settings")

		with (
			patch("jarvis.chat.llm_switch.is_active", return_value=True),
			patch("jarvis.chat.llm_switch.begin") as mock_begin,
			patch("frappe.enqueue") as mock_enqueue,
		):
			leg = request_resync(settings)

		self.assertEqual(leg, "pool")
		mock_begin.assert_called_once()
		self.assertEqual(
			mock_begin.call_args.args[0],
			"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_sync_via_admin_pool",
		)
		mock_enqueue.assert_not_called()

	def test_resync_direct_branch_routes_through_begin_while_a_switch_is_active(self):
		# A single api-key model - not due for handover (no llm_pool_synced_at),
		# not pool_mode (<2 enabled, no subscription) - falls into the direct
		# "restart" leg, which has no proxy_active notion of its own at all.
		settings = _make_settings_with_models([_api_key_model(order=0)])

		with (
			patch("jarvis.chat.llm_switch.is_active", return_value=True),
			patch("jarvis.chat.llm_switch.begin") as mock_begin,
			patch("frappe.enqueue") as mock_enqueue,
		):
			leg = request_resync(settings)

		self.assertEqual(leg, "direct")
		mock_begin.assert_called_once_with(
			"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_sync_via_admin",
			job_id="jarvis_settings_sync:restart",
			deduplicate=True,
			action="restart",
		)
		mock_enqueue.assert_not_called()

	def test_resync_direct_branch_enqueues_directly_with_no_switch_active(self):
		"""Regression guard: an ordinary Resync with nothing else in flight
		keeps today's plain enqueue."""
		settings = _make_settings_with_models([_api_key_model(order=0)])

		with (
			patch("jarvis.chat.llm_switch.is_active", return_value=False),
			patch("jarvis.chat.llm_switch.begin") as mock_begin,
			patch("frappe.enqueue") as mock_enqueue,
		):
			leg = request_resync(settings)

		self.assertEqual(leg, "direct")
		mock_enqueue.assert_called_once()
		self.assertEqual(
			mock_enqueue.call_args.args[0],
			"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_sync_via_admin",
		)
		mock_begin.assert_not_called()


class TestDirectLegJobEndsAnAppliedSwitch(FrappeTestCase):
	"""Important fix #2: _enqueued_sync_via_admin (the direct-leg worker
	request_resync's "restart" branch can now reach through an active switch)
	ends an applied switch in its finally, exactly like the pool and handover
	jobs - and, symmetrically, never ends one it did not release."""

	def test_ends_an_applied_switch_after_the_sync_runs(self):
		settings = _handover_ready_settings()
		settings._sync_via_admin = MagicMock()
		with (
			patch("frappe.get_single", return_value=settings),
			patch("jarvis.chat.llm_switch.status", return_value={"applied": True, "job": "x"}),
			patch("jarvis.chat.llm_switch.end") as mock_end,
			patch("frappe.db.get_value", return_value="ok (restart via admin)"),
		):
			_enqueued_sync_via_admin("restart")

		settings._sync_via_admin.assert_called_once_with("restart")
		mock_end.assert_called_once_with("ok (restart via admin)")

	def test_does_not_end_a_held_unapplied_switch(self):
		settings = _handover_ready_settings()
		settings._sync_via_admin = MagicMock()
		with (
			patch("frappe.get_single", return_value=settings),
			patch("jarvis.chat.llm_switch.status", return_value={"applied": False, "job": "x"}),
			patch("jarvis.chat.llm_switch.end") as mock_end,
			patch("frappe.db.get_value", return_value="ok (restart via admin)"),
		):
			_enqueued_sync_via_admin("restart")

		mock_end.assert_not_called()
