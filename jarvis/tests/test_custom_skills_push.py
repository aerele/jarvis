"""Over-cap handling of the custom-skills push (pattern-learning plan Phase 2:
'tenant audit + graceful resync, then build_push_payload raise').

``build_push_payload(strict=True)`` raises an actionable error above
``MAX_SKILLS_PER_PUSH`` for the interactive apply (a human is present to act);
``strict=False`` (the default; the unattended enqueued worker and the
post-restart resync) truncates exactly as before but logs a loud warning
naming the dropped slugs, so the truncation is never silent again. Managed
(``managed_by_learning=1``) rows stay excluded either way (Wave 4) and never
eat the 25-skill custom budget. The unattended paths (enqueued worker, the
two restart resyncs) must swallow ANY exception into a logged terminal
failure - a rebuilt container must never be left skill-less by an uncaught
raise in a background job.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import custom_skills_api
from jarvis.chat.custom_skills import MAX_SKILLS_PER_PUSH, build_push_payload

SKILL = "Jarvis Custom Skill"
SETTINGS = "Jarvis Settings"
# Fixture rows are owner-scoped so build_push_payload(owner=...) sees an EXACT
# set regardless of what else this site holds (the payload is bench-global in
# production; ``owner`` exists to scope tests).
OWNER = "jcsp-cap-owner@example.com"


def _mk(suffix, *, owner=OWNER, enabled=1, managed=0, skill_name=None):
	"""Low-level row insert (``db_insert`` bypasses the per-owner cap and slug
	validation, same as test_compiler's fixture) so a test can exceed
	MAX_SKILLS_PER_PUSH."""
	d = frappe.new_doc(SKILL)
	d.update(
		{
			"skill_name": skill_name or f"jcsp-{suffix}",
			"description": "cap fixture",
			"instructions": "body",
			"enabled": enabled,
			"user_invocable": 0,
			# Org scope: only Org skills join the shared container push (security
			# review PART 2 TASK 10 made User the default, which is never pushed).
			"scope": "Org",
			"managed_by_learning": managed,
		}
	)
	# db_insert stamps owner with the session user unless creation is already
	# set; pre-stamp both so the fixture owner sticks.
	d.creation = d.modified = frappe.utils.now()
	d.owner = d.modified_by = owner
	d.flags.name_set = True
	d.name = f"jcsp-row-{suffix}"
	d.db_insert()
	return d


def _cleanup_fixture_rows():
	"""Remove fixture rows even when a worker-path test COMMITTED them (the
	class-level rollback only undoes uncommitted work)."""
	frappe.db.rollback()
	frappe.db.delete(SKILL, {"name": ("like", "jcsp-row-%")})
	frappe.db.commit()


class TestBuildPushPayloadCap(FrappeTestCase):
	def tearDown(self):
		_cleanup_fixture_rows()
		super().tearDown()

	def test_strict_raises_actionable_error_over_cap(self):
		for i in range(30):
			_mk(f"cap-{i:02d}")
		with self.assertRaises(frappe.ValidationError) as ctx:
			build_push_payload(owner=OWNER, strict=True)
		msg = str(ctx.exception)
		# Actionable: the count, the cap, the fix, and the outcome.
		self.assertIn("30 enabled custom skills exceed the push cap of 25", msg)
		self.assertIn("disable 5 or consolidate", msg)
		self.assertIn("Nothing was pushed", msg)

	def test_non_strict_truncates_and_logs_loud_warning(self):
		for i in range(30):
			_mk(f"cap-{i:02d}")
		# A managed row must neither ride the payload nor shift the counts.
		_mk("managed", managed=1, skill_name="learned-jcspcap")
		with patch.object(frappe, "log_error") as log:
			payload = build_push_payload(owner=OWNER)
		# Return shape unchanged: first 25 by skill_name asc, as before.
		self.assertEqual(len(payload), MAX_SKILLS_PER_PUSH)
		self.assertEqual(
			[p["slug"] for p in payload],
			[f"custom-jcsp-cap-{i:02d}" for i in range(25)],
		)
		log.assert_called_once()
		kwargs = log.call_args.kwargs
		self.assertEqual(kwargs["title"], "Jarvis: custom-skills push truncated")
		self.assertIn(
			"custom-skills push truncated: 30 enabled, 25 pushed, 5 dropped",
			kwargs["message"],
		)
		for i in range(25, 30):
			self.assertIn(f"custom-jcsp-cap-{i:02d}", kwargs["message"])
		self.assertNotIn("learned-jcspcap", kwargs["message"])

	def test_under_cap_stays_silent_in_both_modes(self):
		for i in range(3):
			_mk(f"few-{i}")
		with patch.object(frappe, "log_error") as log:
			strict_payload = build_push_payload(owner=OWNER, strict=True)
			lax_payload = build_push_payload(owner=OWNER)
		self.assertEqual(len(strict_payload), 3)
		self.assertEqual(strict_payload, lax_payload)
		log.assert_not_called()

	def test_managed_rows_excluded_and_never_eat_the_cap(self):
		# Exactly 25 normal rows + 1 managed row: strict must NOT raise (the
		# managed row is off-budget) and the managed slug never appears.
		for i in range(25):
			_mk(f"cap-{i:02d}")
		_mk("managed", managed=1, skill_name="learned-jcspcap")
		with patch.object(frappe, "log_error") as log:
			payload = build_push_payload(owner=OWNER, strict=True)
		self.assertEqual(len(payload), 25)
		self.assertNotIn("custom-learned-jcspcap", [p["slug"] for p in payload])
		log.assert_not_called()

	def test_interactive_apply_surfaces_strict_error(self):
		# apply_custom_skills is bench-global; 26 fresh rows guarantee >25
		# whatever else this site holds.
		for i in range(26):
			_mk(f"apply-{i:02d}")
		before = frappe.db.get_single_value(SETTINGS, "custom_skills_sync_status", cache=False)
		with self.assertRaises(frappe.ValidationError) as ctx:
			custom_skills_api.apply_custom_skills()
		self.assertIn("exceed the push cap of 25", str(ctx.exception))
		# The throw happens BEFORE the pending-status write and the enqueue:
		# nothing was marked, nothing was pushed.
		after = frappe.db.get_single_value(SETTINGS, "custom_skills_sync_status", cache=False)
		self.assertEqual(after, before)


class TestUnattendedPushGraceful(FrappeTestCase):
	"""The worker + restart-resync paths run with nobody watching: any raise
	must degrade to a logged terminal failure, never propagate."""

	def setUp(self):
		super().setUp()
		self._status0 = frappe.db.get_single_value(SETTINGS, "custom_skills_sync_status", cache=False)
		self._synced0 = frappe.db.get_single_value(SETTINGS, "custom_skills_synced_at", cache=False)

	def tearDown(self):
		_cleanup_fixture_rows()
		# The worker COMMITS its status writes; put the committed values back.
		frappe.db.set_single_value(
			SETTINGS,
			{
				"custom_skills_sync_status": self._status0,
				"custom_skills_synced_at": self._synced0,
			},
			update_modified=False,
		)
		frappe.db.commit()
		super().tearDown()

	def test_worker_swallows_any_exception_and_logs(self):
		with (
			patch(
				"jarvis.chat.custom_skills_api.build_push_payload",
				side_effect=ValueError("boom"),
			),
			patch.object(frappe, "log_error") as log,
		):
			custom_skills_api._enqueued_push_custom_skills()  # must not raise
		status = frappe.db.get_single_value(SETTINGS, "custom_skills_sync_status", cache=False)
		self.assertEqual(status, "failed: unexpected error; see Error Log")
		titles = [c.kwargs.get("title") for c in log.call_args_list]
		self.assertIn("Jarvis: custom-skills push failed", titles)

	def test_worker_truncates_over_cap_instead_of_raising(self):
		# The unattended worker stays strict=False: over-cap benches keep their
		# first 25 skills after a container rebuild instead of losing all of
		# them to a raise.
		for i in range(30):
			_mk(f"wcap-{i:02d}")
		seen = {}

		def _capture(skills=None):
			seen["skills"] = skills

		with (
			patch("jarvis.admin_client.post_push_custom_skills", side_effect=_capture),
			patch.object(frappe, "log_error") as log,
		):
			custom_skills_api._enqueued_push_custom_skills()
		self.assertEqual(len(seen["skills"]), MAX_SKILLS_PER_PUSH)
		status = frappe.db.get_single_value(SETTINGS, "custom_skills_sync_status", cache=False)
		self.assertTrue(status.startswith("ok (applied 25"), status)
		titles = [c.kwargs.get("title") for c in log.call_args_list]
		self.assertIn("Jarvis: custom-skills push truncated", titles)

	def test_restart_resync_swallows_enqueue_failure(self):
		_mk("resync-seed")  # non-empty so the resync reaches the enqueue
		settings = frappe.get_single(SETTINGS)
		with (
			patch.object(frappe, "enqueue", side_effect=ValueError("boom")),
			patch.object(frappe, "log_error") as log,
		):
			settings._resync_custom_skills_after_restart()  # must not raise
		titles = [c.kwargs.get("title") for c in log.call_args_list]
		self.assertIn("Jarvis: custom-skills resync after restart failed", titles)

	def test_restart_resync_learned_sibling_swallows_too(self):
		_mk("resync-managed", managed=1, skill_name="learned-jcspresync")
		settings = frappe.get_single(SETTINGS)
		with (
			patch.object(frappe, "enqueue", side_effect=ValueError("boom")),
			patch.object(frappe, "log_error") as log,
		):
			settings._resync_learned_skills_after_restart()  # must not raise
		titles = [c.kwargs.get("title") for c in log.call_args_list]
		self.assertIn("Jarvis: learned-skills resync after restart failed", titles)
