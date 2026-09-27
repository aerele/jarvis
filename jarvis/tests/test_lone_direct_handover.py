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
	_enqueued_handover_via_admin,
	_enqueued_sync_via_admin,
	_finish_switch_run,
	_finish_switch_run_after_followup,
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

	def setUp(self):
		super().setUp()
		# CI finding (2026 review, fourth pass): _enqueue_handover now ALWAYS
		# routes through llm_switch.begin() (only frappe.enqueue is mocked
		# below), which - under FrappeTestCase's forced in_test=True - runs
		# begin() inline and writes a REAL, applied switch record into redis.
		# That record is not rolled back by the SQL transaction FrappeTestCase
		# undoes, so it would otherwise leak into every later test in the
		# same process.
		from jarvis.chat import llm_switch

		llm_switch._reset_for_tests()
		self.addCleanup(llm_switch._reset_for_tests)

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
	branch, then restores them and drains the WHOLE after_commit queue so
	nothing leaks into a later test's commit.

	CI finding (2026 review, fourth pass): flipping frappe.flags.in_test off
	is a global flag, not an llm_switch-scoped one - writing to Jarvis
	Settings while it is off can make OTHER, unrelated Frappe/Jarvis
	machinery (e.g. the triggers engine) ALSO take its own deferred path and
	register a second, unrelated after_commit callback. The double-wrap
	invariant this test actually cares about is "running the ONE callback
	llm_switch registered calls _begin_now exactly once, not llm_switch.begin
	again" - checked directly below - not an exact queue-length count, which
	that unrelated registration made a false positive."""

	def test_handover_switch_defers_to_after_commit_via_begin_with_no_double_wrap(self):
		from jarvis.chat import llm_switch

		settings = _handover_ready_settings()
		original_in_test = frappe.flags.in_test
		original_inline = frappe.flags.get("run_admin_sync_inline")
		frappe.flags.in_test = False
		frappe.flags.run_admin_sync_inline = False
		try:
			with patch.object(llm_switch, "_begin_now") as mock_begin_now:
				before_count = len(frappe.db.after_commit._functions)
				JarvisSettings._enqueue_handover(settings)

				self.assertGreaterEqual(
					len(frappe.db.after_commit._functions),
					before_count + 1,
					"_enqueue_handover must defer via frappe.db.after_commit, not run inline",
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
			# Drain the WHOLE queue, not just the one item this test added -
			# any OTHER callback that also registered while in_test was
			# flipped off must never linger to fire unexpectedly at some
			# LATER test's real commit.
			frappe.db.after_commit.reset()


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
	"""_enqueued_handover_via_admin finishes the run _enqueue_handover began
	(via llm_switch.finish()) on every terminal outcome (ok, a bounded
	pending-retry, or a genuine failure) - but ONLY the run it was actually
	given (review fix wave: llm_switch_run_id) - and defers finishing (never
	touches the switch here) on the pool/resync fallback - spec Part C: "A
	retry of a failed handover (attempt N) is a new switch"."""

	def _run_with_follow_up(
		self, settings, follow_up, *, last_sync_status, run_id="test-run-1", switch_status=None
	):
		"""frappe.get_single -> settings (patched); _handover_via_admin patched
		to report follow_up, with last_sync_status set to whatever the real
		function would have written for that outcome - _finish_switch_run
		reads the CURRENT status straight from the DB, not from this bare
		fixture, so frappe.db.get_value is patched to match.

		``switch_status``: what llm_switch.status() reports to
		llm_switch.finish() - defaults to an APPLIED record under the SAME
		run_id (this attempt's own switch, released and ready to end), the
		shape every "terminal outcome ends the switch" test below needs."""
		if switch_status is None:
			switch_status = {"applied": True, "run_id": run_id, "job": "x", "next": None}
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
			_enqueued_handover_via_admin(llm_switch_run_id=run_id)

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

	def test_a_terminal_outcome_with_a_stale_run_id_never_ends_the_switch(self):
		"""Review fix wave: the active record now belongs to a DIFFERENT run
		(e.g. a retarget moved it on while this attempt was executing) - this
		attempt's own terminal outcome must not end or hand off a switch it
		no longer owns."""
		settings = _handover_ready_settings()
		with patch("jarvis.chat.llm_switch.end") as mock_end:
			self._run_with_follow_up(
				settings,
				None,
				last_sync_status="ok (moved to direct via admin)",
				switch_status={"applied": True, "run_id": "some-other-run", "job": "x", "next": None},
			)

		mock_end.assert_not_called()

	def test_a_terminal_outcome_with_no_run_id_never_touches_the_switch(self):
		"""This worker was never released by any switch
		(llm_switch_run_id=None - e.g. old data, or a plain apply) - must
		never call llm_switch.finish at all."""
		settings = _handover_ready_settings()
		with patch("jarvis.chat.llm_switch.finish") as mock_finish:
			self._run_with_follow_up(
				settings, None, last_sync_status="ok (moved to direct via admin)", run_id=None
			)

		mock_finish.assert_not_called()

	def test_pool_fallback_delegates_to_enqueue_pool_sync_then_finishes_this_run(self):
		"""The retarget-vs-plain-enqueue decision lives entirely inside
		_switch_or_enqueue (review round 1: it OR's in
		llm_switch.is_active()), so this worker just calls
		settings._enqueue_pool_sync() unconditionally, exactly as before Task
		5 - the retarget itself is covered by
		TestSwitchOrEnqueueJoinsAnActiveSwitch below. Live e2e finding (third
		pass, Bug 2): THIS run must still finish itself afterwards - the
		follow-up's own begin() call parks under THIS run (still applied,
		still executing), and nothing else would ever release it."""
		settings = _handover_ready_settings()
		settings._enqueue_pool_sync = MagicMock()
		with patch("jarvis.chat.llm_switch.finish") as mock_finish:
			self._run_with_follow_up(settings, "pool", last_sync_status="")

		settings._enqueue_pool_sync.assert_called_once_with()
		mock_finish.assert_called_once_with("test-run-1", "")

	def test_resync_fallback_delegates_to_request_resync_then_finishes_this_run(self):
		"""Same fix as the pool branch above - request_resync's own calls
		flow through _switch_or_enqueue too, so its follow-up job also parks
		under THIS run and needs the same after-followup finish."""
		settings = _handover_ready_settings()
		with (
			patch("jarvis.jarvis.doctype.jarvis_settings.jarvis_settings.request_resync") as mock_resync,
			patch("jarvis.chat.llm_switch.finish") as mock_finish,
		):
			self._run_with_follow_up(settings, "resync", last_sync_status="")

		mock_resync.assert_called_once_with(settings)
		mock_finish.assert_called_once_with("test-run-1", "")


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


class TestFinishSwitchRun(FrappeTestCase):
	"""Review fix wave: _finish_switch_run(run_id, *, crashed) is the shared
	per-worker ``finally`` hook - a no-op when run_id is falsy (this job was
	never released by any switch), enforces CR-0 ONLY on a genuine crash
	(llm_switch.finish() must never see a non-terminal status left behind by
	an uncaught exception), and otherwise hands whatever status the worker
	itself wrote straight through to llm_switch.finish(run_id, status).

	Live e2e finding (third pass, Bug 1): a worker's own DELIBERATE
	non-terminal-looking exit (the handover's bounded "pending: handover to
	direct (attempt N)" pending-retry, or a "skipped: ...") is a NORMAL end
	of THIS run, not a crash - crashed=False must never rewrite it, or
	reconcile_pending_llm_sync's retry breaks."""

	def test_no_run_id_is_a_no_op(self):
		with patch("jarvis.chat.llm_switch.finish") as mock_finish:
			_finish_switch_run(None, crashed=False)

		mock_finish.assert_not_called()

	def test_terminal_status_is_passed_through_unchanged(self):
		with (
			patch("frappe.db.get_value", return_value="ok (restart via admin)"),
			patch("jarvis.chat.llm_switch.finish") as mock_finish,
		):
			_finish_switch_run("run-1", crashed=False)

		mock_finish.assert_called_once_with("run-1", "ok (restart via admin)")

	def test_pending_applying_on_a_normal_exit_awaits_admin_instead_of_finishing(self):
		"""jarvis#1425 review, fourth pass ("the switch ends when the apply is
		CONFIRMED, not when the worker stops waiting"): admin ACCEPTED the
		push and this worker's own in-band convergence wait simply gave up -
		the apply is still in progress on admin's side. finish() must NOT be
		called (that would end the switch and clear the banner/send-hold
		before the container actually finishes); await_admin() takes over."""
		with (
			patch("frappe.db.get_value", return_value=_PENDING_APPLYING_STATUS),
			patch("jarvis.chat.llm_switch.finish") as mock_finish,
			patch("jarvis.chat.llm_switch.await_admin") as mock_await,
		):
			_finish_switch_run("run-1", crashed=False)

		mock_finish.assert_not_called()
		mock_await.assert_called_once_with("run-1")

	def test_pending_applying_on_a_crash_still_finishes_not_awaits(self):
		"""A CRASH at this exact status is not the deliberate "admin accepted,
		still converging" exit await_admin exists for - it is the CR-0
		backstop's own territory (rewrite to the generic failure, then
		finish), unchanged."""
		with (
			patch("frappe.db.get_value", return_value=_PENDING_APPLYING_STATUS),
			patch(
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._write_settings_fields"
			) as mock_write,
			patch("jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._commit_terminal_sync_status"),
			patch("jarvis.chat.llm_switch.finish") as mock_finish,
			patch("jarvis.chat.llm_switch.await_admin") as mock_await,
		):
			_finish_switch_run("run-1", crashed=True)

		mock_await.assert_not_called()
		mock_write.assert_called_once()
		mock_finish.assert_called_once_with("run-1", "failed: unexpected error; see Error Log")

	def test_a_pending_status_on_a_normal_exit_is_never_rewritten(self):
		"""The core of Bug 1: a bounded pending-retry is this run's OWN
		deliberate, normal outcome - crashed=False must pass it straight
		through untouched, never rewriting it to the generic CR-0 failure."""
		with (
			patch(
				"frappe.db.get_value",
				return_value=f"{_PENDING_HANDOVER_STATUS} (attempt 1)",
			),
			patch(
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._write_settings_fields"
			) as mock_write,
			patch("jarvis.chat.llm_switch.finish") as mock_finish,
		):
			_finish_switch_run("run-1", crashed=False)

		mock_write.assert_not_called()
		mock_finish.assert_called_once_with("run-1", f"{_PENDING_HANDOVER_STATUS} (attempt 1)")

	def test_a_pending_status_on_a_crash_is_rewritten_to_the_generic_failure(self):
		"""CR-0: llm_switch.finish() must never see a non-terminal status left
		behind by a genuine crash - if the worker unwound via an uncaught
		exception without its own terminal write ever landing, stamp the
		generic failure here first, guarded exactly like every worker's own
		backstop (_write_settings_fields + _commit_terminal_sync_status)."""
		with (
			patch("frappe.db.get_value", return_value="pending: provisioning container"),
			patch(
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._write_settings_fields"
			) as mock_write,
			patch(
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._commit_terminal_sync_status"
			) as mock_commit,
			patch("jarvis.chat.llm_switch.finish") as mock_finish,
		):
			_finish_switch_run("run-1", crashed=True)

		mock_write.assert_called_once()
		self.assertEqual(
			mock_write.call_args.args[1],
			{"last_sync_status": "failed: unexpected error; see Error Log"},
		)
		mock_commit.assert_called_once()
		mock_finish.assert_called_once_with("run-1", "failed: unexpected error; see Error Log")

	def test_an_ok_or_failed_prefixed_status_is_never_rewritten_even_on_a_crash(self):
		"""Sanity check: an already-terminal status (either prefix) must pass
		through untouched regardless of crashed - a crash AFTER the real
		terminal write landed must not clobber it."""
		for status in ("ok (restart via admin)", "failed: auth: token expired"):
			with self.subTest(status=status):
				with (
					patch("frappe.db.get_value", return_value=status),
					patch(
						"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._write_settings_fields"
					) as mock_write,
					patch("jarvis.chat.llm_switch.finish") as mock_finish,
				):
					_finish_switch_run("run-1", crashed=True)

				mock_write.assert_not_called()
				mock_finish.assert_called_once_with("run-1", status)

	def test_a_failure_reading_status_is_logged_not_raised(self):
		"""CR-2: guard the status read so a DB hiccup here can never mask the
		worker's own real exception propagating through its finally."""
		with (
			patch("frappe.db.get_value", side_effect=RuntimeError("db down")),
			patch("frappe.log_error") as mock_log,
			patch("jarvis.chat.llm_switch.finish") as mock_finish,
		):
			_finish_switch_run("run-1", crashed=True)  # must not raise

		mock_log.assert_called_once()
		mock_finish.assert_not_called()


class TestFinishSwitchRunAfterFollowup(FrappeTestCase):
	"""Live e2e finding (third pass, Bug 2):
	_finish_switch_run_after_followup(run_id) finishes a switch-released
	worker's OWN run after handing off to a follow-up sync (the handover's
	"pool"/"resync" branches) - the follow-up's own begin() call parks itself
	under THIS run (still applied, still executing), and nothing else would
	ever release that park."""

	def test_no_run_id_is_a_no_op(self):
		with patch("jarvis.chat.llm_switch.finish") as mock_finish:
			_finish_switch_run_after_followup(None)

		mock_finish.assert_not_called()

	def test_inline_finishes_immediately(self):
		"""frappe.flags.in_test is True for every test - the follow-up's own
		begin() call already ran synchronously by the time it returns, so
		finishing immediately is safe."""
		with (
			patch("frappe.db.get_value", return_value="pending: provisioning container (pool)"),
			patch("jarvis.chat.llm_switch.finish") as mock_finish,
		):
			_finish_switch_run_after_followup("run-1")

		mock_finish.assert_called_once_with("run-1", "pending: provisioning container (pool)")

	def test_deferred_registers_after_the_followups_own_callback(self):
		"""Outside inline mode, begin() defers its own park to
		frappe.db.after_commit - this must register a SECOND callback so
		Frappe's registration-ordered CallbackManager runs it after the park,
		not before (there would be nothing in rec["next"] yet)."""
		original_in_test = frappe.flags.in_test
		original_inline = frappe.flags.get("run_admin_sync_inline")
		frappe.flags.in_test = False
		frappe.flags.run_admin_sync_inline = False
		try:
			with patch("jarvis.chat.llm_switch.finish") as mock_finish:
				before_count = len(frappe.db.after_commit._functions)
				_finish_switch_run_after_followup("run-1")

				self.assertGreaterEqual(len(frappe.db.after_commit._functions), before_count + 1)
				mock_finish.assert_not_called()

				queued = list(frappe.db.after_commit._functions)[-1]
				with patch("frappe.db.get_value", return_value="ok (pool_update via admin)"):
					queued()

				mock_finish.assert_called_once_with("run-1", "ok (pool_update via admin)")
		finally:
			frappe.flags.in_test = original_in_test
			frappe.flags.run_admin_sync_inline = original_inline
			# Drain the WHOLE queue, not just the one item this test added -
			# see the matching comment in TestSwitchBeginDeferredToAfterCommit.
			frappe.db.after_commit.reset()


class TestHandoverWorkerCrashWritesFailedAndEndsTheSwitch(FrappeTestCase):
	"""Bug 1, crash path: an uncaught exception from _handover_via_admin -
	simulated here as escaping BEFORE any status write, the worst case - must
	still write the generic CR-0 failure (crashed=True) and end the switch,
	re-raising the original exception afterwards so rq still sees the job
	fail."""

	def test_a_raising_handover_ends_the_switch_with_the_generic_failure(self):
		settings = _handover_ready_settings(last_sync_status="")
		with (
			patch("frappe.get_single", return_value=settings),
			patch(
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._handover_via_admin",
				side_effect=RuntimeError("boom"),
			),
			patch("frappe.db.get_value", return_value=""),
			patch(
				"jarvis.chat.llm_switch.status",
				return_value={"applied": True, "run_id": "run-1", "job": "x", "next": None},
			),
			patch("jarvis.chat.llm_switch.end") as mock_end,
		):
			with self.assertRaises(RuntimeError):
				_enqueued_handover_via_admin(llm_switch_run_id="run-1")

		mock_end.assert_called_once_with("failed: unexpected error; see Error Log")


class TestHandoverFollowupReleasesFromParkInsteadOfStrandingForever(FrappeTestCase):
	"""Live e2e finding (third pass, Bug 2), end to end through the REAL
	llm_switch mechanics (no mocking of begin/finish/status): a handover
	whose follow-up is "pool" (or "resync" re-driving the pool leg) must not
	leave that follow-up parked forever - its own run finishes itself right
	after, releasing the park as a fresh run rather than sitting until the
	20-minute TTL."""

	def setUp(self):
		from jarvis.chat import llm_switch

		self.llm_switch = llm_switch
		frappe.cache().delete_value(llm_switch.KEY)
		self._orig_status = frappe.db.get_value("Jarvis Settings", "Jarvis Settings", "last_sync_status")

	def tearDown(self):
		frappe.cache().delete_value(self.llm_switch.KEY)
		frappe.db.set_single_value(
			"Jarvis Settings", {"last_sync_status": self._orig_status or ""}, update_modified=False
		)

	def _begin_a_real_handover_switch(self, settings):
		frappe.db.set_single_value(
			"Jarvis Settings",
			{"last_sync_status": f"{_PENDING_HANDOVER_STATUS} (attempt 1)"},
			update_modified=False,
		)
		with (
			patch.object(self.llm_switch, "_inflight", return_value=0),
			patch("frappe.enqueue"),
			patch("frappe.publish_realtime"),
		):
			JarvisSettings._enqueue_handover(settings)
		run_id = self.llm_switch.status()["run_id"]
		self.assertIsNotNone(run_id)
		return run_id

	def _fake_pool_park(self, idempotency_key=None):
		"""Mirrors _enqueue_pool_sync's two switch-relevant effects: stamp its
		own pending status, then reach llm_switch.begin() for real -
		is_active() is True (we are still inside the handover's own
		still-applied run), so it parks."""
		frappe.db.set_single_value(
			"Jarvis Settings",
			{"last_sync_status": "pending: provisioning container (pool)"},
			update_modified=False,
		)
		self.llm_switch.begin(
			"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_sync_via_admin_pool",
			job_id="jarvis_settings_sync:pool",
			deduplicate=True,
		)

	def test_pool_followup_is_released_under_a_new_run_id_not_parked_forever(self):
		settings = _handover_ready_settings()
		run_id = self._begin_a_real_handover_switch(settings)
		settings._enqueue_pool_sync = MagicMock(side_effect=self._fake_pool_park)

		with (
			patch("frappe.get_single", return_value=settings),
			patch(
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._handover_via_admin",
				return_value="pool",
			),
			patch("frappe.enqueue") as mock_enqueue,
			patch("frappe.publish_realtime") as mock_pub,
		):
			_enqueued_handover_via_admin(llm_switch_run_id=run_id)

		# The pool job was RELEASED for real (enqueued under a new run_id),
		# not left parked - and no second "switching"/"done" banner fired.
		mock_pub.assert_not_called()
		mock_enqueue.assert_called_once()
		self.assertEqual(
			mock_enqueue.call_args.args[0],
			"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_sync_via_admin_pool",
		)
		rec = self.llm_switch.status()
		self.assertIsNotNone(rec)
		self.assertTrue(rec["applied"])
		self.assertNotEqual(rec["run_id"], run_id)
		self.assertIsNone(rec["next"])

		# That released job's own finish() then ends the switch for real.
		frappe.db.set_single_value(
			"Jarvis Settings", {"last_sync_status": "ok (pool_update via admin)"}, update_modified=False
		)
		with patch("frappe.publish_realtime") as mock_pub2:
			self.llm_switch.finish(rec["run_id"], "ok (pool_update via admin)")

		mock_pub2.assert_called_once()
		self.assertIsNone(self.llm_switch.status())

	def test_resync_followup_is_released_under_a_new_run_id_not_parked_forever(self):
		"""Same mechanism, via request_resync's own follow-up instead of the
		handover's direct pool fallback."""
		settings = _handover_ready_settings()
		run_id = self._begin_a_real_handover_switch(settings)

		def _fake_request_resync(s):
			self._fake_pool_park()
			return "pool"

		with (
			patch("frappe.get_single", return_value=settings),
			patch(
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._handover_via_admin",
				return_value="resync",
			),
			patch(
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings.request_resync",
				side_effect=_fake_request_resync,
			),
			patch("frappe.enqueue") as mock_enqueue,
			patch("frappe.publish_realtime") as mock_pub,
		):
			_enqueued_handover_via_admin(llm_switch_run_id=run_id)

		mock_pub.assert_not_called()
		mock_enqueue.assert_called_once()
		rec = self.llm_switch.status()
		self.assertIsNotNone(rec)
		self.assertTrue(rec["applied"])
		self.assertNotEqual(rec["run_id"], run_id)
		self.assertIsNone(rec["next"])


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
	finishes the run it was given in its finally, exactly like the pool and
	handover jobs - and never touches a switch it was not released by
	(llm_switch_run_id=None)."""

	def test_ends_an_applied_switch_after_the_sync_runs(self):
		settings = _handover_ready_settings()
		settings._sync_via_admin = MagicMock()
		with (
			patch("frappe.get_single", return_value=settings),
			patch("frappe.db.get_value", return_value="ok (restart via admin)"),
			patch("jarvis.chat.llm_switch.finish") as mock_finish,
		):
			_enqueued_sync_via_admin("restart", llm_switch_run_id="run-1")

		settings._sync_via_admin.assert_called_once_with("restart")
		mock_finish.assert_called_once_with("run-1", "ok (restart via admin)")

	def test_no_run_id_never_touches_the_switch(self):
		settings = _handover_ready_settings()
		settings._sync_via_admin = MagicMock()
		with (
			patch("frappe.get_single", return_value=settings),
			patch("frappe.db.get_value", return_value="ok (restart via admin)"),
			patch("jarvis.chat.llm_switch.finish") as mock_finish,
		):
			_enqueued_sync_via_admin("restart")

		mock_finish.assert_not_called()
