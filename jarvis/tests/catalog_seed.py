"""before_tests hook: give the test site a catalog without an admin.

CI has no admin, and the app ships no model or preset lists, so the per-site
snapshot is seeded from the test fixtures. The Redis copies are dropped so every
read goes through the snapshot the tests control.
"""

import json

import frappe

from jarvis.catalog_store import MODELS, PRESETS, SNAPSHOT_DT


def seed_catalog_snapshot() -> None:
	from jarvis.tests.fixtures.model_catalog import MODEL_CATALOG
	from jarvis.tests.fixtures.preset_catalog import PRESET_CATALOG

	try:
		frappe.db.set_single_value(
			SNAPSHOT_DT,
			{"model_catalog": json.dumps(MODEL_CATALOG), "preset_catalog": json.dumps(PRESET_CATALOG)},
		)
		# Test classes roll back their own transactions and no runner commits
		# after before_tests, so the seed must be committed here to be visible.
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		raise
	for store in (MODELS, PRESETS):
		frappe.cache().delete_value(store.cache_key)
