"""Tests for the "Enable Business Wiki" ON -> OFF scrub trigger (#731).

Disabling the wiki must enqueue a one-shot scrub that removes the tenant's
already-mirrored page files from the container. The trigger lives on the Jarvis
Settings controller: validate() records a genuine 1 -> 0 change of the toggle via
has_value_changed, and on_update() fires ``wiki_mirror.enqueue_scrub`` on it.

The pivotal hazard this file guards against: a loaded Check field always coerces
to 0/1 (never None - see frappe base_document._fix_numeric_types), so a pre-v2
tenant whose ``wiki_enabled`` row is NULL surfaces as 0 in memory. A state-based
guard would therefore read "was ON (raw), now 0 (coerced)" on EVERY unrelated
settings save and wipe the mirror. Change-detection (has_value_changed compares
the SUBMITTED value against the stored one) does not, because an untouched field
compares equal to itself. The v2_14 backfill patch then makes a real disable of a
pre-v2 tenant a visible 1 -> 0 change so the kill switch works for them too.

``Jarvis Settings`` is a Single (one row shared by the whole DB), and
``wiki_enabled`` may be absent from tabSingles entirely (NULL = ON). setUp
snapshots the RAW value - including "row absent" - and tearDown restores it. The
LLM dispatch methods are patched out in the save-path tests: saving Jarvis
Settings runs the LLM sync INLINE under ``frappe.flags.in_test``, orthogonal to
this trigger.
"""

from __future__ import annotations

from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import wiki_mirror
from jarvis.patches import v2_14_backfill_wiki_enabled

SETTINGS = "Jarvis Settings"


def _raw_wiki_enabled():
	"""The stored ``wiki_enabled`` exactly as ``wiki_enabled()`` reads it: the
	tabSingles value, or None when the row is absent (NULL = ON)."""
	rows = frappe.db.sql(
		"select value from `tabSingles` where doctype=%s and field=%s",
		(SETTINGS, "wiki_enabled"),
	)
	return rows[0][0] if rows else None


def _set_raw_wiki_enabled(value):
	"""Force the stored toggle to ``value`` (0/1) or, for None, remove the row so
	the NULL=ON default applies. Commits so a subsequent get_single reads it."""
	frappe.db.sql(
		"delete from `tabSingles` where doctype=%s and field=%s",
		(SETTINGS, "wiki_enabled"),
	)
	if value is not None:
		frappe.db.set_single_value(SETTINGS, "wiki_enabled", value, update_modified=False)
	frappe.db.commit()


class _WikiToggleTestCase(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self._wiki_enabled_before = _raw_wiki_enabled()

	def tearDown(self):
		_set_raw_wiki_enabled(self._wiki_enabled_before)
		super().tearDown()


class TestWikiScrubGuardUnit(_WikiToggleTestCase):
	"""The guard method reads exactly one flag; validate() owns the detection."""

	def _guard(self, flag):
		settings = frappe.get_single(SETTINGS)
		if flag is None:
			settings.flags.pop("wiki_disabled_transition", None)
		else:
			settings.flags.wiki_disabled_transition = flag
		with mock.patch.object(wiki_mirror, "enqueue_scrub") as enq:
			settings._maybe_scrub_wiki_on_disable()
		return enq

	def test_fires_when_the_transition_flag_is_set(self):
		self.assertTrue(self._guard(True).called)

	def test_does_nothing_when_the_flag_is_false(self):
		self.assertFalse(self._guard(False).called)

	def test_does_nothing_when_the_flag_is_missing(self):
		# validate() skipped -> no captured state -> err toward not scrubbing.
		self.assertFalse(self._guard(None).called)


class TestWikiScrubGuardOnSave(_WikiToggleTestCase):
	"""The full save lifecycle: validate() detects the change, on_update() fires."""

	def _save(self, *, set_wiki_enabled=None, touch_unrelated=False):
		"""Save Jarvis Settings with LLM dispatch stubbed and enqueue_scrub
		captured. ``set_wiki_enabled`` explicitly sets the toggle; leaving it None
		models a save that never touched the checkbox. ``touch_unrelated`` changes
		a benign field so the save is a real write, not a no-op."""
		settings = frappe.get_single(SETTINGS)
		if set_wiki_enabled is not None:
			settings.wiki_enabled = set_wiki_enabled
		if touch_unrelated:
			settings.wiki_nudge_cooldown_hours = (
				frappe.utils.cint(getattr(settings, "wiki_nudge_cooldown_hours", 0)) + 1
			)
		with (
			mock.patch.object(type(settings), "_on_update_unified_llm"),
			mock.patch.object(type(settings), "_on_update_single_model_legacy"),
			mock.patch.object(wiki_mirror, "enqueue_scrub") as enq,
		):
			settings.save(ignore_permissions=True)
		return enq

	def test_an_explicit_disable_scrubs(self):
		_set_raw_wiki_enabled(1)
		self.assertTrue(self._save(set_wiki_enabled=0).called)

	def test_an_unrelated_save_on_an_enabled_tenant_does_not_scrub(self):
		# The mirror must survive a settings save that never touched the wiki.
		_set_raw_wiki_enabled(1)
		self.assertFalse(self._save(touch_unrelated=True).called)

	def test_an_unrelated_save_on_a_pre_v2_null_tenant_does_not_scrub(self):
		# THE regression: a NULL row coerces to 0 in memory, but has_value_changed
		# compares 0 (loaded) against 0 (unchanged) and sees no change. A state
		# guard would wipe the mirror here on every save.
		_set_raw_wiki_enabled(None)
		self.assertFalse(self._save(touch_unrelated=True).called)

	def test_a_save_while_already_off_does_nothing(self):
		_set_raw_wiki_enabled(0)
		self.assertFalse(self._save(set_wiki_enabled=0, touch_unrelated=True).called)

	def test_a_re_enable_does_nothing(self):
		_set_raw_wiki_enabled(0)
		self.assertFalse(self._save(set_wiki_enabled=1).called)

	def test_a_pre_v2_tenant_scrubs_after_the_backfill(self):
		# The backfill turns a NULL row into an explicit 1, so a subsequent disable
		# is a visible 1 -> 0 change and the kill switch works for pre-v2 tenants.
		_set_raw_wiki_enabled(None)
		v2_14_backfill_wiki_enabled.execute()
		frappe.db.commit()
		self.assertEqual(frappe.utils.cint(_raw_wiki_enabled()), 1)
		self.assertTrue(self._save(set_wiki_enabled=0).called)


class TestWikiEnabledBackfillPatch(_WikiToggleTestCase):
	"""v2_14: seed an explicit ON row for NULL=ON tenants without ever re-enabling
	a wiki an admin deliberately turned off."""

	def test_backfill_seeds_an_absent_row(self):
		_set_raw_wiki_enabled(None)
		v2_14_backfill_wiki_enabled.execute()
		frappe.db.commit()
		self.assertEqual(frappe.utils.cint(_raw_wiki_enabled()), 1)

	def test_backfill_seeds_a_null_valued_row(self):
		_set_raw_wiki_enabled(1)
		frappe.db.sql(
			"update `tabSingles` set value=NULL where doctype=%s and field=%s",
			(SETTINGS, "wiki_enabled"),
		)
		frappe.db.commit()
		v2_14_backfill_wiki_enabled.execute()
		frappe.db.commit()
		self.assertEqual(frappe.utils.cint(_raw_wiki_enabled()), 1)

	def test_backfill_leaves_an_explicit_disable_alone(self):
		_set_raw_wiki_enabled(0)
		v2_14_backfill_wiki_enabled.execute()
		frappe.db.commit()
		self.assertEqual(frappe.utils.cint(_raw_wiki_enabled()), 0)
