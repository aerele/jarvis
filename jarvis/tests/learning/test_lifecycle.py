"""Learned-pattern lifecycle tests (plan section 6.5): dedupe / draft-freeze /
durable-but-reversible suppression / snooze expiry / retention / overlap /
drift re-validation (Phase 2 A1).
"""

from __future__ import annotations

from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from jarvis.learning import lifecycle
from jarvis.learning.executor import DetectorResult

JLP = "Jarvis Learned Pattern"
JLP_ROLE = "Jarvis Learned Pattern Role"
RUN = "Jarvis Pattern Run"
SKILL = "Jarvis Custom Skill"

KEY = "_lc-key-1"
OVERLAP_SKILL = "lc-overlap-skill"


def _candidate(pattern_key=KEY, **over):
	c = {
		"detector_id": "buy-supplier-stockness",
		"pattern_key": pattern_key,
		"domain": "buying",
		"company": None,
		"roles": ["Purchase User"],
		"pattern_statement": "Supplier ThreadCo supplies only non-stock items.",
		"skill_draft": (
			'- Supplier "ThreadCo" supplies only non-stock items. '
			"Evidence: 95% of 40 Purchase Orders since 2025-01."
		),
		"support_n": 40,
		"n_rows": 40,
		"exception_n": 2,
		"confidence_pct": 95.0,
		"wilson_low": 0.85,
		"gap": 0.30,
		"strength_band": "Medium",
		"temporal_spread": {"distinct_days": 10},
		"evidence": {"antecedent": "ThreadCo"},
		"exceptions": [],
		"exceptions_cluster": None,
		"sensitivity": "B",
		"effective_sensitivity": "B",
		"not_applicable": False,
	}
	c.update(over)
	return c


class TestLifecycle(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.run = frappe.get_doc({"doctype": RUN, "trigger": "manual", "status": "Running"}).insert(
			ignore_permissions=True
		)
		frappe.local._jarvis_overlap_index = None

	def tearDown(self):
		frappe.db.delete(JLP_ROLE, {"parenttype": JLP})
		frappe.db.delete(JLP, {"pattern_key": ["like", "_lc-%"]})
		frappe.db.delete(SKILL, {"skill_name": OVERLAP_SKILL})
		frappe.db.delete(RUN, {"name": self.run.name})
		frappe.local._jarvis_overlap_index = None
		frappe.db.commit()
		super().tearDown()

	def _row(self, key=KEY):
		name = frappe.db.exists(JLP, {"pattern_key": key})
		return frappe.get_doc(JLP, name) if name else None

	def _reject(self, key=KEY, **fields):
		name = frappe.db.exists(JLP, {"pattern_key": key})
		payload = {"status": "Rejected"}
		payload.update(fields)
		frappe.db.set_value(JLP, name, payload, update_modified=False)

	# --- create / dedupe ---------------------------------------------------- #
	def test_insert_creates_proposed_with_roles(self):
		self.assertEqual(lifecycle.upsert_candidate(_candidate(), self.run), "created")
		row = self._row()
		self.assertEqual(row.status, "Proposed")
		self.assertEqual(row.support_n, 40)
		self.assertEqual(row.first_seen_run, self.run.name)
		self.assertIn("Purchase User", [r.role for r in row.roles])

	def test_dedupe_refreshes_non_terminal(self):
		lifecycle.upsert_candidate(_candidate(), self.run)
		out = lifecycle.upsert_candidate(_candidate(support_n=55), self.run)
		self.assertEqual(out, "updated")
		self.assertEqual(self._row().support_n, 55)
		self.assertEqual(self._row().status, "Proposed")

	def test_edited_draft_is_not_overwritten(self):
		lifecycle.upsert_candidate(_candidate(), self.run)
		name = self._row().name
		frappe.db.set_value(
			JLP,
			name,
			{"draft_edited": 1, "skill_draft": "- SM edited rule."},
			update_modified=False,
		)
		lifecycle.upsert_candidate(_candidate(skill_draft="- fresh detector text."), self.run)
		self.assertEqual(self._row().skill_draft, "- SM edited rule.")

	def test_polished_draft_survives_re_detection(self):
		# draft_polished freezes the WORDING only: the evidence/stat refresh
		# still lands (unlike draft_edited it does not imply a frozen evidence
		# line), and the polished text must survive nightly mining and approval.
		lifecycle.upsert_candidate(_candidate(), self.run)
		name = self._row().name
		frappe.db.set_value(
			JLP,
			name,
			{"draft_polished": 1, "skill_draft": "- polished wording."},
			update_modified=False,
		)
		out = lifecycle.upsert_candidate(
			_candidate(skill_draft="- fresh detector text.", support_n=99), self.run
		)
		self.assertEqual(out, "updated")
		row = self._row()
		self.assertEqual(row.skill_draft, "- polished wording.")
		self.assertEqual(row.draft_polished, 1)
		self.assertEqual(row.support_n, 99)  # evidence still refreshed

	def test_statement_change_clears_polish_and_refreshes_draft(self):
		# A materially different pattern_statement means the detector
		# re-measured a different pattern: the stale polish is dropped and the
		# deterministic draft refreshes.
		lifecycle.upsert_candidate(_candidate(), self.run)
		name = self._row().name
		frappe.db.set_value(
			JLP,
			name,
			{"draft_polished": 1, "skill_draft": "- polished wording."},
			update_modified=False,
		)
		lifecycle.upsert_candidate(
			_candidate(
				pattern_statement="Supplier ThreadCo now supplies mostly stock items.",
				skill_draft="- fresh detector text.",
			),
			self.run,
		)
		row = self._row()
		self.assertEqual(row.draft_polished, 0)
		self.assertEqual(row.skill_draft, "- fresh detector text.")

	def test_flag_band_cap_clamps_mining_band_write(self):
		# Correction-loop contract: the nightly refresh must not silently
		# revert a flag-driven demotion - the band write clamps to the cap.
		lifecycle.upsert_candidate(_candidate(strength_band="High"), self.run)
		name = self._row().name
		frappe.db.set_value(
			JLP,
			name,
			{"flag_band_cap": "Low", "strength_band": "Low"},
			update_modified=False,
		)
		lifecycle.upsert_candidate(_candidate(strength_band="High"), self.run)
		self.assertEqual(self._row().strength_band, "Low")

	def test_empty_flag_band_cap_leaves_band_free(self):
		lifecycle.upsert_candidate(_candidate(strength_band="Medium"), self.run)
		lifecycle.upsert_candidate(_candidate(strength_band="High"), self.run)
		self.assertEqual(self._row().strength_band, "High")

	# --- suppression (durable but reversible) ------------------------------- #
	def test_rejected_stays_suppressed_when_not_stronger(self):
		lifecycle.upsert_candidate(_candidate(), self.run)
		self._reject(strength_band="Medium", support_n=40)
		out = lifecycle.upsert_candidate(_candidate(strength_band="Medium", support_n=42), self.run)
		self.assertEqual(out, "duplicate")
		row = self._row()
		self.assertEqual(row.status, "Rejected")
		self.assertEqual(row.last_seen_run, self.run.name)

	def test_rejected_reappears_on_band_rise(self):
		lifecycle.upsert_candidate(_candidate(), self.run)
		self._reject(strength_band="Medium", support_n=40, review_note="not now")
		out = lifecycle.upsert_candidate(_candidate(strength_band="High", wilson_low=0.95), self.run)
		self.assertEqual(out, "created")
		row = self._row()
		self.assertEqual(row.status, "Proposed")
		self.assertEqual(row.surfaced, 0)
		self.assertIn("re-proposed", (row.review_note or "").lower())

	def test_rejected_reappears_on_support_growth(self):
		lifecycle.upsert_candidate(_candidate(), self.run)
		self._reject(strength_band="Medium", support_n=40)
		out = lifecycle.upsert_candidate(_candidate(strength_band="Medium", support_n=70), self.run)
		self.assertEqual(out, "created")
		self.assertEqual(self._row().status, "Proposed")

	def test_superseded_is_skipped(self):
		lifecycle.upsert_candidate(_candidate(), self.run)
		name = self._row().name
		frappe.db.set_value(JLP, name, {"status": "Superseded"}, update_modified=False)
		out = lifecycle.upsert_candidate(_candidate(support_n=99), self.run)
		self.assertEqual(out, "duplicate")
		row = self._row()
		self.assertEqual(row.status, "Superseded")
		self.assertEqual(row.support_n, 40)  # not refreshed
		self.assertEqual(row.last_seen_run, self.run.name)

	# --- snooze expiry / retention ------------------------------------------ #
	def test_snooze_expiry_unsnoozes_elapsed(self):
		lifecycle.upsert_candidate(_candidate(), self.run)
		name = self._row().name
		past = str(add_to_date(now_datetime(), days=-1).date())
		frappe.db.set_value(
			JLP,
			name,
			{"status": "Snoozed", "snoozed_until": past, "surfaced": 1},
			update_modified=False,
		)
		res = lifecycle.snooze_expiry()
		self.assertGreaterEqual(res["unsnoozed"], 1)
		row = self._row()
		self.assertEqual(row.status, "Proposed")
		self.assertEqual(row.surfaced, 0)

	def test_retention_archives_old_rejected_and_trims_evidence(self):
		lifecycle.upsert_candidate(_candidate(), self.run)
		name = self._row().name
		old = str(add_to_date(now_datetime(), days=-200))
		frappe.db.set_value(JLP, name, {"status": "Rejected"}, update_modified=False)
		frappe.db.set_value(JLP, name, {"modified": old}, update_modified=False)
		res = lifecycle.retention()
		self.assertGreaterEqual(res["archived"], 1)
		row = self._row()
		self.assertEqual(row.status, "Archived")
		self.assertIn(row.evidence, (None, ""))

	def test_recent_rejected_not_archived(self):
		lifecycle.upsert_candidate(_candidate(), self.run)
		name = self._row().name
		frappe.db.set_value(JLP, name, {"status": "Rejected"}, update_modified=False)
		lifecycle.retention()
		self.assertEqual(self._row().status, "Rejected")

	# --- overlap warning ---------------------------------------------------- #
	def test_overlap_warning_set_against_enabled_custom_skill(self):
		# Low-level insert bypasses the per-owner cap (this dev site is near it).
		d = frappe.new_doc(SKILL)
		d.update(
			{
				"skill_name": OVERLAP_SKILL,
				"description": "Supplier ThreadCo supplies only non-stock items handling",
				"instructions": "Body about supplier non-stock items handling.",
				"enabled": 1,
				"user_invocable": 0,
				"managed_by_learning": 0,
			}
		)
		d.owner = "Administrator"
		d.name = "lc-overlap-row"
		d.flags.name_set = True
		d.db_insert()
		frappe.local._jarvis_overlap_index = None  # rebuild against the new skill
		lifecycle.upsert_candidate(_candidate(), self.run)
		self.assertTrue((self._row().overlap_warning or "").strip())


class TestRevalidateActive(FrappeTestCase):
	"""Drift re-validation (plan 6.5, Phase 2 A1): refresh-in-place, the Stale
	triggers (confidence drop / undetectable), the checker-version guard, the
	skipped-detector no-op, edited-draft preservation, and the SM notification.

	``run_detector`` is mocked at the executor module (revalidate imports it
	lazily) and a sentinel ``patterndb`` is passed so no READ ONLY fence is
	opened - these tests exercise the lifecycle logic, not the detectors."""

	SM = "lc-drift-sm@example.com"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Notification Log.for_user is a Link, so the recipient must exist
		# (get_users_with_role is mocked; roles are irrelevant here).
		if not frappe.db.exists("User", cls.SM):
			u = frappe.get_doc(
				{
					"doctype": "User",
					"email": cls.SM,
					"first_name": "lc-drift-sm",
					"send_welcome_email": 0,
					"enabled": 1,
				}
			)
			u.flags.ignore_permissions = True
			u.insert()
			frappe.db.commit()

	def setUp(self):
		super().setUp()
		self.run = frappe.get_doc({"doctype": RUN, "trigger": "manual", "status": "Running"}).insert(
			ignore_permissions=True
		)
		frappe.local._jarvis_overlap_index = None

	def tearDown(self):
		frappe.db.delete(JLP_ROLE, {"parenttype": JLP})
		frappe.db.delete(JLP, {"pattern_key": ["like", "_lc-%"]})
		frappe.db.delete("Notification Log", {"for_user": self.SM})
		frappe.db.delete(RUN, {"name": self.run.name})
		frappe.local._jarvis_overlap_index = None
		frappe.db.commit()
		super().tearDown()

	def _row(self, key=KEY):
		name = frappe.db.exists(JLP, {"pattern_key": key})
		return frappe.get_doc(JLP, name) if name else None

	def _live(self, status="Active", **over):
		"""Insert one pattern via the engine path and move it to Approved/Active
		(db.set_value, like the board would after review + apply)."""
		lifecycle.upsert_candidate(_candidate(**over), self.run)
		name = frappe.db.exists(JLP, {"pattern_key": over.get("pattern_key", KEY)})
		frappe.db.set_value(JLP, name, {"status": status}, update_modified=False)
		frappe.db.commit()
		return name

	def _reval(self, result):
		with mock.patch("jarvis.learning.executor.run_detector", return_value=result) as rd:
			out = lifecycle.revalidate_active(self.run, patterndb=object())
		return out, rd

	# --- refresh in place ----------------------------------------------------- #
	def test_matched_candidate_refreshes_stats_in_place(self):
		self._live(status="Active")
		fresh = _candidate(
			support_n=60,
			n_rows=61,
			exception_n=1,
			confidence_pct=97.0,
			wilson_low=0.91,
			gap=0.4,
			strength_band="High",
		)
		out, _rd = self._reval(DetectorResult([fresh], None))
		row = self._row()
		self.assertEqual(row.status, "Active")  # still healthy: no transition
		self.assertEqual(row.support_n, 60)
		self.assertEqual(row.exception_n, 1)
		self.assertEqual(row.strength_band, "High")
		self.assertAlmostEqual(row.wilson_low, 0.91, places=4)
		self.assertTrue(row.last_validated_at)
		self.assertEqual(out["revalidated"], 1)
		self.assertEqual(out["staled"], 0)

	def test_drift_never_touches_an_edited_draft(self):
		name = self._live(status="Approved")
		frappe.db.set_value(
			JLP,
			name,
			{"draft_edited": 1, "skill_draft": "- SM edited rule."},
			update_modified=False,
		)
		fresh = _candidate(support_n=80, skill_draft="- fresh detector text.")
		self._reval(DetectorResult([fresh], None))
		row = self._row()
		self.assertEqual(row.skill_draft, "- SM edited rule.")
		self.assertEqual(row.support_n, 80)  # stats still refreshed
		self.assertEqual(row.status, "Approved")

	# --- stale triggers --------------------------------------------------------#
	def test_confidence_drop_marks_stale_and_notifies(self):
		self._live(status="Active")
		fresh = _candidate(confidence_pct=71.0, wilson_low=0.55, strength_band="Low")
		with mock.patch("frappe.utils.user.get_users_with_role", return_value=[self.SM]):
			out, _rd = self._reval(DetectorResult([fresh], None))
		row = self._row()
		self.assertEqual(row.status, "Stale")
		self.assertIn("confidence dropped", row.stale_reason)
		self.assertIn("95% -> 71%", row.stale_reason)
		self.assertAlmostEqual(row.confidence_pct, 71.0, places=1)  # fresh truth kept
		self.assertTrue(row.last_validated_at)
		self.assertEqual(out["staled"], 1)
		subjects = frappe.get_all("Notification Log", filters={"for_user": self.SM}, pluck="subject")
		self.assertTrue(any("stale" in (s or "").lower() for s in subjects))

	def test_undetectable_marks_stale(self):
		self._live(status="Approved")
		out, _rd = self._reval(DetectorResult([], None))  # ran clean, no candidates
		row = self._row()
		self.assertEqual(row.status, "Stale")
		self.assertIn("no longer detectable", row.stale_reason)
		self.assertEqual(out["revalidated"], 1)
		self.assertEqual(out["staled"], 1)

	def test_skipped_detector_is_not_drift(self):
		self._live(status="Active")
		out, _rd = self._reval(DetectorResult([], "missing field Item.is_stock_item"))
		self.assertEqual(self._row().status, "Active")
		self.assertEqual(out["staled"], 0)
		self.assertEqual(out["unchecked"], 1)

	# --- admission-consistent thresholds (never stricter than approval) -------- #
	def test_low_band_pattern_is_not_staled_while_its_numbers_hold(self):
		# An approved Low-band pattern was ADMITTED with wilson_low below the
		# 0.80 drift floor (detection gates on confidence + precision, not on
		# wilson >= 0.80). Re-detecting it with the SAME numbers is not drift:
		# it must never be staled on a floor stricter than admission.
		self._live(status="Active", confidence_pct=90.0, wilson_low=0.74, strength_band="Low")
		fresh = _candidate(confidence_pct=90.0, wilson_low=0.74, strength_band="Low")
		out, _rd = self._reval(DetectorResult([fresh], None))
		row = self._row()
		self.assertEqual(row.status, "Active")
		self.assertEqual(out["staled"], 0)
		self.assertEqual(out["revalidated"], 1)

	def test_wilson_regression_stales_with_a_wilson_reason(self):
		# Confidence holds (95% >= c_min 90%) but the Wilson bound regressed
		# below both the floor and the stored bound: stale, and the reason must
		# name the actual trigger - never the bogus "dropped 95% -> 95%".
		self._live(status="Active")  # stored wilson_low 0.85
		fresh = _candidate(confidence_pct=95.0, wilson_low=0.70, strength_band="Low")
		with mock.patch("frappe.utils.user.get_users_with_role", return_value=[self.SM]):
			out, _rd = self._reval(DetectorResult([fresh], None))
		row = self._row()
		self.assertEqual(row.status, "Stale")
		self.assertIn("wilson lower bound dropped", row.stale_reason)
		self.assertNotIn("confidence dropped", row.stale_reason)
		self.assertEqual(out["staled"], 1)

	def test_flag_band_cap_clamps_drift_band_refresh(self):
		# Correction-loop contract at the drift write: the refreshed band
		# clamps to flag_band_cap so a demotion survives re-validation.
		name = self._live(status="Active")
		frappe.db.set_value(
			JLP,
			name,
			{"flag_band_cap": "Low", "strength_band": "Low"},
			update_modified=False,
		)
		fresh = _candidate(support_n=60, confidence_pct=97.0, wilson_low=0.91, strength_band="High")
		out, _rd = self._reval(DetectorResult([fresh], None))
		row = self._row()
		self.assertEqual(row.status, "Active")
		self.assertEqual(row.strength_band, "Low")
		self.assertEqual(row.support_n, 60)  # stats still refreshed
		self.assertEqual(out["staled"], 0)

	# --- checker-version guard ------------------------------------------------ #
	def test_version_bump_skips_drift_compare(self):
		# Stored checker version 0 != registry spec version (1): the comparison
		# would be apples-to-oranges. Leave the status, annotate the evidence,
		# and do not even run the checker for a fully version-skipped group.
		self._live(
			status="Active",
			evidence={"antecedent": "ThreadCo", "detector_version": 0},
		)
		out, rd = self._reval(DetectorResult([], None))  # would stale if compared
		row = self._row()
		self.assertEqual(row.status, "Active")
		self.assertEqual(out["version_skipped"], 1)
		self.assertEqual(out["staled"], 0)
		rd.assert_not_called()
		ev = frappe.parse_json(row.evidence)
		self.assertIn("revalidation", ev)
		self.assertIn("version", ev["revalidation"]["note"])


class TestRevalidateActiveMined(FrappeTestCase):
	"""Drift re-validation over the engine's mining stash (Phase 2): no
	``run_detector`` call at all - the pass consumes the candidates mining just
	computed, honors cap-truncation (absence under a truncated list proves
	nothing), skips keys the stash was not tracking, and stales matched
	patterns whose candidate carries a recency divergence."""

	DET = "buy-supplier-stockness"

	def setUp(self):
		super().setUp()
		self.run = frappe.get_doc({"doctype": RUN, "trigger": "manual", "status": "Running"}).insert(
			ignore_permissions=True
		)
		frappe.local._jarvis_overlap_index = None

	def tearDown(self):
		frappe.db.delete(JLP_ROLE, {"parenttype": JLP})
		frappe.db.delete(JLP, {"pattern_key": ["like", "_lc-%"]})
		frappe.db.delete(RUN, {"name": self.run.name})
		frappe.local._jarvis_overlap_index = None
		frappe.db.commit()
		super().tearDown()

	def _row(self, key=KEY):
		name = frappe.db.exists(JLP, {"pattern_key": key})
		return frappe.get_doc(JLP, name) if name else None

	def _live(self, status="Active", **over):
		lifecycle.upsert_candidate(_candidate(**over), self.run)
		name = frappe.db.exists(JLP, {"pattern_key": over.get("pattern_key", KEY)})
		frappe.db.set_value(JLP, name, {"status": status}, update_modified=False)
		frappe.db.commit()
		return name

	def _mined(self, by_key, tracked=None, cap_truncated=False):
		return {
			(self.DET, None): {
				"by_key": by_key,
				"tracked": tracked if tracked is not None else {KEY},
				"cap_truncated": cap_truncated,
			}
		}

	def _reval_mined(self, mined):
		"""Run the mined path and prove the checker is never re-run."""
		with (
			mock.patch(
				"jarvis.learning.executor.run_detector",
				side_effect=AssertionError("mined path must not run the checker"),
			),
			mock.patch("frappe.utils.user.get_users_with_role", return_value=[]),
		):
			return lifecycle.revalidate_active(self.run, mined=mined)

	def test_mined_match_refreshes_without_running_the_checker(self):
		self._live(status="Active")
		fresh = _candidate(support_n=70, confidence_pct=96.0, wilson_low=0.90)
		out = self._reval_mined(self._mined({KEY: fresh}))
		row = self._row()
		self.assertEqual(row.status, "Active")
		self.assertEqual(row.support_n, 70)
		self.assertEqual(out["revalidated"], 1)
		self.assertEqual(out["staled"], 0)

	def test_mined_missing_key_stales_when_not_truncated(self):
		self._live(status="Approved")
		out = self._reval_mined(self._mined({}))
		row = self._row()
		self.assertEqual(row.status, "Stale")
		self.assertIn("no longer detectable", row.stale_reason)
		self.assertEqual(out["staled"], 1)

	def test_mined_cap_truncated_unit_defers_instead_of_staling(self):
		# The unit hit the per-detector candidate cap: a pattern_key missing
		# from the truncated list is NOT evidence - leave the row untouched.
		self._live(status="Active")
		out = self._reval_mined(self._mined({}, cap_truncated=True))
		row = self._row()
		self.assertEqual(row.status, "Active")
		self.assertFalse((row.stale_reason or "").strip())
		self.assertEqual(out["cap_deferred"], 1)
		self.assertEqual(out["staled"], 0)
		self.assertEqual(out["revalidated"], 0)

	def test_unit_not_mined_is_left_unchecked(self):
		self._live(status="Active")
		out = self._reval_mined({})  # the run never mined this (detector, company)
		self.assertEqual(self._row().status, "Active")
		self.assertEqual(out["unchecked"], 1)
		self.assertEqual(out["staled"], 0)

	def test_untracked_key_is_left_unchecked(self):
		# The row was approved mid-run, AFTER the stash snapshot: its key was
		# not tracked when the unit was mined, so absence proves nothing.
		self._live(status="Approved")
		out = self._reval_mined(self._mined({}, tracked=set()))
		self.assertEqual(self._row().status, "Approved")
		self.assertEqual(out["unchecked"], 1)
		self.assertEqual(out["staled"], 0)

	def test_recency_divergence_stales_matched_pattern(self):
		# Grandfathered transition: the aggregate still clears every gate but
		# mining stamped a recency divergence (>=0.2 recent-share shift or a
		# mode flip) - the approved text may no longer describe current
		# behaviour, so the pattern goes Stale with the divergence reason.
		self._live(status="Active")
		fresh = _candidate(
			confidence_pct=95.0,
			wilson_low=0.85,
			evidence={
				"antecedent": "ThreadCo",
				"recency": "behavior changed around 2026-04-01",
				"recency_changed_around": "2026-04-01",
			},
		)
		out = self._reval_mined(self._mined({KEY: fresh}))
		row = self._row()
		self.assertEqual(row.status, "Stale")
		self.assertIn("behavior changed around 2026-04-01", row.stale_reason)
		self.assertEqual(out["staled"], 1)

	def test_recency_divergence_reviewed_after_onset_does_not_restale(self):
		# The SM re-approved AFTER the divergence window opened: they accepted
		# the recent behaviour, so the same divergence must not loop the row
		# back to Stale every night.
		name = self._live(status="Active")
		frappe.db.set_value(JLP, name, {"reviewed_at": now_datetime()}, update_modified=False)
		fresh = _candidate(
			confidence_pct=95.0,
			wilson_low=0.85,
			evidence={
				"antecedent": "ThreadCo",
				"recency": "behavior changed around 2026-04-01",
				"recency_changed_around": "2026-04-01",
			},
		)
		out = self._reval_mined(self._mined({KEY: fresh}))
		row = self._row()
		self.assertEqual(row.status, "Active")
		self.assertEqual(out["staled"], 0)
		self.assertEqual(out["revalidated"], 1)

	def test_grandfathered_partial_adoption_stales_via_real_reduce(self):
		# End-to-end drift integration WITHOUT mocking the reduce: a
		# grandfathered transition ("new terms for NEW deals only, legacy
		# accounts grandfathered") keeps the recent plurality on the old value
		# and the recent-share shift under the 0.2 threshold, but the recent
		# window falls under the detector's own c_min while the full-window
		# aggregate still clears every admission gate. The REAL reduce must
		# stamp the divergence on its candidate, and that candidate - fed
		# through the mined stash - must stale the Active row.
		from jarvis.learning import registry
		from jarvis.learning.executor import _finalize, reduce_units

		det = "buy-supplier-tax-template"  # c_min 0.95
		spec = registry.get_detector(det)

		def batch(supplier, consequent, start, count, tag):
			rows = []
			for i in range(count):
				day = frappe.utils.add_days(start, i)
				rows.append(
					{
						"unit_id": f"{supplier}-{tag}-{i}",
						"antecedent": supplier,
						"consequent": consequent,
						"day": day,
						"created": f"{day} 10:00:00",
					}
				)
			return rows

		rows = batch("SupGrand", "OldGST", "2025-01-01", 150, "old")
		rows += batch("SupGrand", "OldGST", "2025-11-01", 27, "legacy")
		rows += batch("SupGrand", "NewGST", "2025-11-28", 3, "new")
		# ballast segment: the site-wide variance gate and the leave-segment-
		# out base rate need a rest population
		rows += batch("SupB", "ConstGST", "2025-06-01", 40, "ballast")

		raws = reduce_units(rows, spec, None)
		raw = next(r for r in raws if r["antecedent_value"] == "SupGrand")
		cand = _finalize(spec, None, raw)
		# the full-window aggregate still passes admission (177/180 = 98.3%)
		# but the recent window (27/30 = 0.90 < 0.95) stamped the divergence
		self.assertGreaterEqual(cand["confidence_pct"], 95.0)
		self.assertIn("recency_changed_around", cand["evidence"])

		key = "_lc-grandfather"  # tearDown cleans _lc-% rows
		cand["pattern_key"] = key
		self._live(status="Active", pattern_key=key, detector_id=det)
		mined = {
			(det, None): {
				"by_key": {key: cand},
				"tracked": {key},
				"cap_truncated": False,
			}
		}
		out = self._reval_mined(mined)
		row = self._row(key)
		self.assertEqual(row.status, "Stale")
		self.assertIn("behavior changed around", row.stale_reason)
		self.assertIn("recent behavior diverged", row.stale_reason)
		self.assertEqual(out["staled"], 1)


class TestSurfacingSortKey(FrappeTestCase):
	"""Plan section 6.4 surfacing priority (fix 8): party-specific personalization
	(effective_sensitivity B/C) ranked ahead of A config-cleanup, then band, then
	support_n desc. Pure ordering helper - no DB."""

	def test_party_ranked_ahead_of_config_cleanup(self):
		# A-class config-cleanup, strong band, big support...
		a = {"effective_sensitivity": "A", "strength_band": "High", "support_n": 500}
		# ...still ranks BELOW a weaker party-specific B row.
		b = {"effective_sensitivity": "B", "strength_band": "Medium", "support_n": 30}
		ordered = sorted([a, b], key=lifecycle.surfacing_sort_key)
		self.assertIs(ordered[0], b)
		self.assertIs(ordered[1], a)

	def test_within_class_band_then_support(self):
		high = {"effective_sensitivity": "B", "strength_band": "High", "support_n": 10}
		med = {"effective_sensitivity": "B", "strength_band": "Medium", "support_n": 999}
		self.assertEqual(sorted([med, high], key=lifecycle.surfacing_sort_key), [high, med])
		a_small = {"effective_sensitivity": "A", "strength_band": "High", "support_n": 5}
		a_big = {"effective_sensitivity": "A", "strength_band": "High", "support_n": 50}
		self.assertEqual(sorted([a_small, a_big], key=lifecycle.surfacing_sort_key), [a_big, a_small])

	def test_c_class_also_counts_as_party(self):
		c = {"effective_sensitivity": "C", "strength_band": "Low", "support_n": 1}
		a = {"effective_sensitivity": "A", "strength_band": "High", "support_n": 999}
		self.assertEqual(sorted([a, c], key=lifecycle.surfacing_sort_key), [c, a])


# --------------------------------------------------------------------------- #
# #482: an approved pattern's reviewed text is frozen
# --------------------------------------------------------------------------- #
A_KEY = "_lc-482-key"

# The real wording pair skill_drafts ships for buy-supplier-stockness: the
# executor swaps to the strict "-only" variant purely on evidence thresholds
# (n >= 60, zero exceptions), which IMPROVES every drift axis, so no staling
# check based on statistics can ever intervene.
TENDENCY_DRAFT = (
	'- Supplier "ThreadCo" mostly supplies non-stock items: default is_stock_item to 0 '
	"and flag if a stock item appears. Evidence: 96.6% of 58 Purchase Receipt since 2024-09. "
	"2 known exceptions (see board)."
)
STRICT_DRAFT = (
	'- Supplier "ThreadCo" supplies only non-stock items: default is_stock_item to 0 '
	"and flag if any stock item appears. Evidence: 100% of 75 Purchase Receipt since 2024-11."
)
TENDENCY_STATEMENT = "Supplier ThreadCo usually supplies non-stock items."
STRICT_STATEMENT = "Supplier ThreadCo supplies only non-stock items."


def _a_class(**over):
	"""An A-class candidate: only A-class rows are approvable AND compilable."""
	fields = {
		"pattern_key": A_KEY,
		"sensitivity": "A",
		"effective_sensitivity": "A",
		"pattern_statement": TENDENCY_STATEMENT,
		"skill_draft": TENDENCY_DRAFT,
	}
	fields.update(over)
	return _candidate(**fields)


class TestApprovedDraftFreeze(FrappeTestCase):
	"""#482. A reviewer approves a pattern; the next nightly run re-detects the
	same pattern_key and rewrites ``skill_draft`` in place. Before the fix the
	compiler read that live field, so the org ran wording nobody reviewed."""

	def setUp(self):
		super().setUp()
		self.run = frappe.get_doc({"doctype": RUN, "trigger": "manual", "status": "Running"}).insert(
			ignore_permissions=True
		)
		frappe.local._jarvis_overlap_index = None
		frappe.local.jarvis_redraft_stale = []

	def tearDown(self):
		frappe.db.delete(JLP_ROLE, {"parenttype": JLP})
		frappe.db.delete(JLP, {"pattern_key": ["like", "_lc-%"]})
		frappe.db.delete(RUN, {"name": self.run.name})
		frappe.local._jarvis_overlap_index = None
		frappe.local.jarvis_redraft_stale = []
		frappe.db.commit()
		super().tearDown()

	def _row(self):
		name = frappe.db.exists(JLP, {"pattern_key": A_KEY})
		return frappe.get_doc(JLP, name) if name else None

	def _propose_and_approve(self, edited=None):
		"""Mine the pattern, then approve it through the real board endpoint."""
		from jarvis.chat import learned_api

		lifecycle.upsert_candidate(_a_class(), self.run)
		name = self._row().name
		learned_api.approve_learned_pattern(name, edited_skill_draft=edited)
		return name

	def _compiled_body(self):
		from jarvis.learning import compiler

		return (compiler.compile_domain_skills().get("buying") or {}).get("body", "")

	# --- the reviewed text is what ships ------------------------------------ #
	def test_plain_approve_then_redetection_still_compiles_the_approved_text(self):
		# A plain "looks good" approve leaves draft_edited=0, which is exactly
		# the row whose skill_draft the nightly refresh overwrites.
		name = self._propose_and_approve()
		self.assertEqual(self._row().draft_edited, 0)
		self.assertEqual(self._row().approved_draft, TENDENCY_DRAFT)

		# Re-detection restates the SAME rule with fresher measurements, and a
		# pattern_statement that moved with them.
		refreshed = TENDENCY_DRAFT.replace("96.6% of 58", "97.4% of 77")
		lifecycle.upsert_candidate(
			_a_class(
				skill_draft=refreshed,
				pattern_statement="Supplier ThreadCo usually supplies non-stock items (77 receipts).",
				support_n=77,
			),
			self.run,
		)

		row = self._row()
		self.assertEqual(row.status, "Approved", "an evidence refresh must not move the row")
		self.assertEqual(row.skill_draft, refreshed, "the live draft still tracks what is detected")
		self.assertEqual(row.approved_draft, TENDENCY_DRAFT, "the reviewed text must not move")

		from jarvis.learning import compiler

		bullet = compiler.preview_bullet(name)
		self.assertIn("96.6% of 58", bullet)
		self.assertNotIn("97.4% of 77", bullet)
		body = self._compiled_body()
		self.assertIn("96.6% of 58", body)
		self.assertNotIn("97.4% of 77", body)

	def test_unsnapshotted_row_falls_back_to_the_live_draft(self):
		# Rows approved before #482 shipped (and before the backfill patch ran)
		# must still compile - the compiler falls back to skill_draft.
		name = self._propose_and_approve()
		frappe.db.set_value(JLP, name, {"approved_draft": ""}, update_modified=False)
		from jarvis.learning import compiler

		self.assertIn("96.6% of 58", compiler.preview_bullet(name))

	# --- the materially-changed case ---------------------------------------- #
	def test_material_rule_change_stales_the_row_naming_both_wordings(self):
		# The airtight case: "mostly supplies" -> "supplies only" while every
		# statistical drift axis improves. The approved text is not rewritten
		# AND the new wording is not shipped: the row goes back to the board.
		name = self._propose_and_approve()
		out = lifecycle.upsert_candidate(
			_a_class(
				skill_draft=STRICT_DRAFT,
				pattern_statement=STRICT_STATEMENT,
				support_n=75,
				exception_n=0,
				confidence_pct=100.0,
				wilson_low=0.95,
			),
			self.run,
		)
		self.assertEqual(out, "updated")

		row = self._row()
		self.assertEqual(row.status, "Stale")
		self.assertEqual(row.approved_draft, TENDENCY_DRAFT)
		self.assertTrue(row.stale_reason.startswith(lifecycle.REDRAFT_STALE_PREFIX))
		self.assertIn("mostly supplies non-stock items", row.stale_reason)
		self.assertIn("supplies only non-stock items", row.stale_reason)
		self.assertNotIn("Evidence:", row.stale_reason, "the reason compares rules, not numbers")

		# Stale drops out of the compile: neither wording ships unreviewed.
		body = self._compiled_body()
		self.assertNotIn("ThreadCo", body)
		self.assertNotIn(name, body)

	def test_active_row_is_staled_too(self):
		# An Active row is already IN the pushed body, so it must go back to the
		# board (and out of the next Apply) rather than be silently reworded.
		name = self._propose_and_approve()
		frappe.db.set_value(JLP, name, {"status": "Active"}, update_modified=False)
		lifecycle.upsert_candidate(
			_a_class(skill_draft=STRICT_DRAFT, pattern_statement=STRICT_STATEMENT), self.run
		)
		row = self._row()
		self.assertEqual(row.status, "Stale")
		self.assertEqual(row.approved_draft, TENDENCY_DRAFT)

	def test_evidence_only_change_never_stales(self):
		# The numbers move on EVERY run; only the rule sentence decides.
		self._propose_and_approve()
		lifecycle.upsert_candidate(
			_a_class(
				skill_draft=TENDENCY_DRAFT.replace("96.6% of 58", "95.1% of 61"),
				support_n=61,
			),
			self.run,
		)
		row = self._row()
		self.assertEqual(row.status, "Approved")
		self.assertFalse(row.stale_reason)

	def test_sm_edited_approval_is_never_redraft_staled(self):
		# An SM-edited draft is already frozen against the refresh and is not a
		# template render, so comparing it to one would stale it every night.
		name = self._propose_and_approve(edited="- ThreadCo: never default a stock item on this supplier.")
		self.assertEqual(frappe.db.get_value(JLP, name, "draft_edited"), 1)
		lifecycle.upsert_candidate(
			_a_class(skill_draft=STRICT_DRAFT, pattern_statement=STRICT_STATEMENT), self.run
		)
		row = self._row()
		self.assertEqual(row.status, "Approved")
		self.assertEqual(row.approved_draft, "- ThreadCo: never default a stock item on this supplier.")

	def test_polished_approval_is_never_redraft_staled(self):
		name = self._propose_and_approve()
		frappe.db.set_value(JLP, name, {"draft_polished": 1}, update_modified=False)
		lifecycle.upsert_candidate(
			_a_class(skill_draft=STRICT_DRAFT, pattern_statement=STRICT_STATEMENT), self.run
		)
		self.assertEqual(self._row().status, "Approved")

	def test_proposed_row_is_never_redraft_staled(self):
		# Nothing was approved yet: the refresh is the whole point of the board.
		lifecycle.upsert_candidate(_a_class(), self.run)
		lifecycle.upsert_candidate(
			_a_class(skill_draft=STRICT_DRAFT, pattern_statement=STRICT_STATEMENT), self.run
		)
		row = self._row()
		self.assertEqual(row.status, "Proposed")
		self.assertEqual(row.skill_draft, STRICT_DRAFT)

	def test_re_approving_accepts_the_new_wording_and_clears_the_reason(self):
		# The staled row must be actionable, not a dead end: Stale -> Approved is
		# a legal transition and re-approval re-freezes the CURRENT text.
		from jarvis.chat import learned_api

		name = self._propose_and_approve()
		lifecycle.upsert_candidate(
			_a_class(skill_draft=STRICT_DRAFT, pattern_statement=STRICT_STATEMENT), self.run
		)
		learned_api.approve_learned_pattern(name)
		row = self._row()
		self.assertEqual(row.status, "Approved")
		self.assertEqual(row.approved_draft, STRICT_DRAFT)
		self.assertFalse(row.stale_reason)

	def test_unapprove_clears_the_snapshot(self):
		from jarvis.chat import learned_api

		name = self._propose_and_approve()
		learned_api.unapprove_learned_pattern(name)
		row = self._row()
		self.assertEqual(row.status, "Proposed")
		self.assertFalse(row.approved_draft)

	# --- one notification per run ------------------------------------------- #
	def test_redraft_stales_ride_the_single_summary_notification(self):
		self._propose_and_approve()
		lifecycle.upsert_candidate(
			_a_class(skill_draft=STRICT_DRAFT, pattern_statement=STRICT_STATEMENT), self.run
		)
		with mock.patch("jarvis.learning.lifecycle._notify_stale") as notify:
			out = lifecycle.revalidate_active(self.run, mined={})
		self.assertEqual(notify.call_count, 1)
		lines = notify.call_args[0][0]
		self.assertEqual(len(lines), 1)
		self.assertIn(lifecycle.REDRAFT_STALE_PREFIX, lines[0])
		self.assertGreaterEqual(out["staled"], 1)

	# --- the rule/evidence split -------------------------------------------- #
	def test_rule_sentence_strips_the_bullet_marker_and_evidence_tail(self):
		self.assertEqual(
			lifecycle.rule_sentence("- Do the thing. Evidence: 96% of 10 Sales Invoice since 2024-01."),
			"Do the thing.",
		)
		self.assertEqual(lifecycle.rule_sentence("  - Do   the thing.  "), "Do the thing.")
		self.assertEqual(lifecycle.rule_sentence(None), "")


class TestApprovedDraftBackfill(FrappeTestCase):
	"""The v2_12 patch freezes the reviewed text of rows approved before the
	snapshot field existed. Patches can rerun, so it must be idempotent."""

	KEYS = ("_lc-bf-approved", "_lc-bf-active", "_lc-bf-proposed")

	def setUp(self):
		super().setUp()
		self.run = frappe.get_doc({"doctype": RUN, "trigger": "manual", "status": "Running"}).insert(
			ignore_permissions=True
		)
		frappe.local._jarvis_overlap_index = None

	def tearDown(self):
		frappe.db.delete(JLP_ROLE, {"parenttype": JLP})
		frappe.db.delete(JLP, {"pattern_key": ["like", "_lc-%"]})
		frappe.db.delete(RUN, {"name": self.run.name})
		frappe.local._jarvis_overlap_index = None
		frappe.db.commit()
		super().tearDown()

	def _legacy_row(self, key, status, draft):
		lifecycle.upsert_candidate(_candidate(pattern_key=key, skill_draft=draft), self.run)
		name = frappe.db.exists(JLP, {"pattern_key": key})
		frappe.db.set_value(JLP, name, {"status": status, "approved_draft": None}, update_modified=False)
		return name

	def test_backfill_freezes_approved_rows_only_and_is_idempotent(self):
		from jarvis.patches.v2_12_backfill_approved_draft import execute as backfill

		approved = self._legacy_row(self.KEYS[0], "Approved", "- approved rule. Evidence: 90% of 10 x.")
		active = self._legacy_row(self.KEYS[1], "Active", "- active rule. Evidence: 91% of 11 x.")
		proposed = self._legacy_row(self.KEYS[2], "Proposed", "- proposed rule. Evidence: 92% of 12 x.")

		backfill()
		self.assertEqual(
			frappe.db.get_value(JLP, approved, "approved_draft"), "- approved rule. Evidence: 90% of 10 x."
		)
		self.assertEqual(
			frappe.db.get_value(JLP, active, "approved_draft"), "- active rule. Evidence: 91% of 11 x."
		)
		self.assertFalse(
			frappe.db.get_value(JLP, proposed, "approved_draft"),
			"an un-reviewed row has nothing to freeze",
		)

		# Re-run after the live draft moved on: the frozen text must not follow.
		frappe.db.set_value(JLP, approved, {"skill_draft": "- REWRITTEN."}, update_modified=False)
		backfill()
		self.assertEqual(
			frappe.db.get_value(JLP, approved, "approved_draft"), "- approved rule. Evidence: 90% of 10 x."
		)
