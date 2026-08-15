"""Tests for jarvis.installed_apps_sync - the migrate-time resync that keeps
admin's Jarvis Tenant.installed_apps (the fleet's skill/tool-gating signal)
in step with the bench's actual app set."""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import installed_apps_sync as ias


def _set_snapshot(value) -> None:
	frappe.db.set_single_value(
		ias.SETTINGS,
		ias.FIELD,
		json.dumps(value) if isinstance(value, list) else (value or ""),
		update_modified=False,
	)


def _clear_snapshot() -> None:
	frappe.db.delete("Singles", {"doctype": ias.SETTINGS, "field": ias.FIELD})
	frappe.clear_document_cache(ias.SETTINGS, ias.SETTINGS)


class TestInstalledAppsSync(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_clear_snapshot()
		# Deterministic environment: managed bench, admin configured,
		# single-model mode (pool tests override _pool_active locally).
		self._patches = [
			patch.object(ias, "_admin_configured", return_value=True),
			patch.object(ias, "_pool_active", return_value=False),
		]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in self._patches:
			p.stop()
		_clear_snapshot()
		frappe.set_user("Administrator")

	def test_first_sight_seeds_baseline_without_resync(self):
		"""Feature-deploy migrate must NOT fire a fleet-wide restart wave:
		an empty snapshot seeds the current list silently."""
		with patch("frappe.enqueue") as enq:
			ias.after_migrate()
		enq.assert_not_called()
		self.assertEqual(ias._synced_apps(), sorted(frappe.get_installed_apps()))

	def test_unchanged_set_is_a_noop(self):
		_set_snapshot(sorted(frappe.get_installed_apps()))
		with patch("frappe.enqueue") as enq:
			ias.after_migrate()
		enq.assert_not_called()

	def test_changed_set_enqueues_restart_sync(self):
		stale = sorted(frappe.get_installed_apps())[:-1]  # one app "missing"
		_set_snapshot(stale)
		with patch("frappe.enqueue") as enq:
			ias.after_migrate()
		enq.assert_called_once()
		kwargs = enq.call_args.kwargs
		self.assertEqual(kwargs["action"], "restart")
		self.assertEqual(kwargs["job_id"], "jarvis_settings_sync:restart")
		self.assertTrue(kwargs["deduplicate"])
		self.assertIn("_enqueued_sync_via_admin", enq.call_args.args[0])
		self.assertNotIn("_pool", enq.call_args.args[0])
		# Snapshot must stay STALE until the sync actually succeeds - a
		# premature stamp would silence the gap forever on a failed send.
		self.assertEqual(ias._synced_apps(), stale)

	def test_pool_active_is_derived_from_models_not_the_proxy_flag(self):
		"""A BYO api-key pool has proxy_active=0 (no sidecar - agent drives its
		own failover) yet MUST still resync through the pool leg. Reading the flag
		would send it down the single-model render and cut it to one credential."""
		for p in self._patches:
			p.stop()  # this test exercises the REAL _pool_active
		try:
			settings = frappe._dict(
				preset="",
				models=[
					frappe._dict(enabled=1, credential_type="api_key", model="gpt-4o", order=0),
					frappe._dict(enabled=1, credential_type="api_key", model="gpt-4-turbo", order=1),
				],
			)
			with patch("frappe.get_single", return_value=settings):
				self.assertTrue(ias._pool_active())
		finally:
			for p in self._patches:
				p.start()

	def test_unreadable_pool_flag_defers(self):
		"""Neither leg is safe to guess when pool mode can't be read -
		defer (stale snapshot retries next migrate) instead of risking the
		single-model render on a pool tenant."""
		stale = sorted(frappe.get_installed_apps())[:-1]
		_set_snapshot(stale)
		with patch.object(ias, "_pool_active", return_value=None), patch("frappe.enqueue") as enq:
			ias.after_migrate()
		enq.assert_not_called()
		self.assertEqual(ias._synced_apps(), stale)

	def test_pool_tenant_takes_pool_leg(self):
		"""Pool tenants MUST resync through the pool path - the
		single-model restart would re-render openclaw.json in direct mode
		and knock the container off Bifrost pool routing."""
		stale = sorted(frappe.get_installed_apps())[:-1]
		_set_snapshot(stale)
		with patch.object(ias, "_pool_active", return_value=True), patch("frappe.enqueue") as enq:
			ias.after_migrate()
		enq.assert_called_once()
		self.assertIn("_enqueued_sync_via_admin_pool", enq.call_args.args[0])
		kwargs = enq.call_args.kwargs
		self.assertEqual(kwargs["job_id"], "jarvis_settings_sync:pool")
		self.assertNotIn("action", kwargs)
		self.assertEqual(ias._synced_apps(), stale)  # stale until success

	def test_pool_push_stamps_snapshot_only_on_admin_marker(self):
		"""The pool payload carries installed_apps, but only a NEW admin
		persists it (echoing installed_apps_persisted). Stamping against an
		old admin would silence the resync while the signal is still stale."""
		from jarvis.jarvis.doctype.jarvis_settings import jarvis_settings as js

		with (
			patch(
				"jarvis.admin_client.post_update_llm_pool",
				return_value={"result": "ok", "installed_apps_persisted": True},
			),
			patch("jarvis.installed_apps_sync.record_synced_snapshot") as stamp,
		):
			js._post_pool_with_retry({}, {}, {})
		stamp.assert_called_once()

		with (
			patch("jarvis.admin_client.post_update_llm_pool", return_value={"result": "ok"}),
			patch("jarvis.installed_apps_sync.record_synced_snapshot") as stamp,
		):
			js._post_pool_with_retry({}, {}, {})
		stamp.assert_not_called()

	def test_unconfigured_admin_skips(self):
		_set_snapshot(["frappe"])
		with patch.object(ias, "_admin_configured", return_value=False), patch("frappe.enqueue") as enq:
			ias.after_migrate()
		enq.assert_not_called()

	def test_garbage_snapshot_reseeds(self):
		_set_snapshot("not json at all")
		with patch("frappe.enqueue") as enq:
			ias.after_migrate()
		enq.assert_not_called()
		self.assertEqual(ias._synced_apps(), sorted(frappe.get_installed_apps()))

	def test_never_blocks_migrate(self):
		with (
			patch.object(ias, "_current_apps", side_effect=RuntimeError("boom")),
			patch("frappe.log_error") as logged,
		):
			ias.after_migrate()  # must not raise
		logged.assert_called_once()

	def test_record_snapshot_roundtrip(self):
		ias.record_synced_snapshot()
		self.assertEqual(ias._synced_apps(), sorted(frappe.get_installed_apps()))

	def test_sync_via_admin_stamps_snapshot_on_send(self):
		"""_sync_via_admin('restart') must stamp the snapshot right after
		post_update_llm_creds returns (admin persisted the list desired-first),
		regardless of the convergence handling that follows."""
		settings = frappe.get_doc("Jarvis Settings")
		with (
			patch.object(type(settings), "_resolve_llm_secret_for_push", return_value="sk-test"),
			patch("jarvis.admin_client.post_update_llm_creds", return_value={"action": "restart"}) as post,
			patch(
				"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._is_applying_result",
				return_value=False,
			),
			patch("jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._commit_terminal_sync_status"),
			patch.object(type(settings), "_resync_custom_skills_after_restart"),
			patch.object(type(settings), "_resync_learned_skills_after_restart"),
			patch("jarvis.installed_apps_sync.record_synced_snapshot") as stamp,
		):
			settings._sync_via_admin("restart")
		post.assert_called_once()
		stamp.assert_called_once()

	def test_failed_send_does_not_stamp(self):
		from jarvis import admin_client

		settings = frappe.get_doc("Jarvis Settings")
		with (
			patch.object(type(settings), "_resolve_llm_secret_for_push", return_value="sk-test"),
			patch(
				"jarvis.admin_client.post_update_llm_creds", side_effect=admin_client.AdminAuthError("nope")
			),
			patch("jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._commit_terminal_sync_status"),
			patch("frappe.log_error"),
			patch("jarvis.installed_apps_sync.record_synced_snapshot") as stamp,
		):
			settings._sync_via_admin("restart")
		stamp.assert_not_called()
