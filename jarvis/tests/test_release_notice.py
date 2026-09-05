"""Tests for jarvis.release_notice: persist, boot payload and the gate's refresh."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import __version__, release_notice

# A notice is active only while its version is above the installed one. A fixed
# far-future sentinel reads as "newer" on every line (0.0.1 on develop, 16.x and
# 15.x on the stable branches) without depending on the parser under test.
NEWER = "99.0.0"
NEWER_2 = "99.0.1"

_FIELDS = (
	"release_notice_active",
	"latest_jarvis_version",
	"release_notice_message",
	"release_notice_tier",
	"release_notice_behind",
	"release_banner_interval_days",
)


def _snapshot() -> dict:
	s = frappe.get_single("Jarvis Settings")
	return {f: s.get(f) for f in _FIELDS}


def _restore(snap: dict) -> None:
	s = frappe.get_single("Jarvis Settings")
	for f, v in snap.items():
		s.db_set(f, v)
	frappe.db.commit()


class TestBootPayload(FrappeTestCase):
	def setUp(self):
		self._snap = _snapshot()
		frappe.cache().delete_value(release_notice._CHECK_CACHE_KEY)

	def tearDown(self):
		_restore(self._snap)
		frappe.cache().delete_value(release_notice._CHECK_CACHE_KEY)

	def _set(self, **kw):
		s = frappe.get_single("Jarvis Settings")
		for k, v in kw.items():
			s.db_set(k, v)
		frappe.db.commit()

	def test_active_notice_shape(self):
		self._set(
			release_notice_active=1,
			latest_jarvis_version=NEWER,
			release_notice_message="New dashboards.",
		)
		p = release_notice.boot_payload()
		self.assertTrue(p["active"])
		self.assertEqual(p["version"], NEWER)
		self.assertEqual(p["message"], "New dashboards.")
		# No authored title/url travel — the SPA composes the heading.
		self.assertNotIn("title", p)
		self.assertNotIn("url", p)

	def test_inactive_notice(self):
		self._set(release_notice_active=0, release_notice_message="m")
		self.assertFalse(release_notice.boot_payload()["active"])

	def test_check_refreshes_from_admin_and_returns_payload(self):
		self._set(release_notice_active=1, latest_jarvis_version=NEWER, release_notice_message="old")
		fresh = {"active": True, "version": NEWER_2, "message": "new"}
		with patch("jarvis.admin_client.get_connection", return_value={"release_notice": fresh}) as gc:
			out = release_notice.check()
		gc.assert_called_once_with(timeout_s=8)
		self.assertEqual(out["version"], NEWER_2)
		self.assertEqual(out["message"], "new")

	def test_check_clears_when_admin_sends_none(self):
		# The gate polls this; a cleared notice is what lets an updated tenant back in.
		self._set(release_notice_active=1, latest_jarvis_version=NEWER, release_notice_message="m")
		with patch("jarvis.admin_client.get_connection", return_value={}):
			out = release_notice.check()
		self.assertFalse(out["active"])

	def test_self_clears_once_this_bench_is_current(self):
		"""The bench holds both versions, so it must not stay blocked waiting on an
		unreachable or mis-credentialed control plane."""
		self._set(release_notice_active=1, latest_jarvis_version=__version__, release_notice_message="m")
		self.assertFalse(release_notice.boot_payload()["active"])

	def test_stays_blocked_while_behind(self):
		self._set(release_notice_active=1, latest_jarvis_version="99.0.0", release_notice_message="m")
		self.assertTrue(release_notice.boot_payload()["active"])

	def test_persist_skips_write_when_unchanged(self):
		notice = {"active": True, "version": NEWER, "message": "m"}
		release_notice.persist(notice)
		before = frappe.db.get_value("Jarvis Settings", "Jarvis Settings", "modified")
		release_notice.persist(notice)
		self.assertEqual(frappe.db.get_value("Jarvis Settings", "Jarvis Settings", "modified"), before)

	def test_check_keeps_mirror_when_admin_unreachable(self):
		self._set(release_notice_active=1, latest_jarvis_version=NEWER, release_notice_message="m")
		with patch("jarvis.admin_client.get_connection", side_effect=RuntimeError("boom")):
			out = release_notice.check()
		self.assertTrue(out["active"])

	def test_persist_then_clear_round_trip(self):
		release_notice.persist({"active": True, "version": NEWER, "message": "M"})
		p = release_notice.boot_payload()
		self.assertTrue(p["active"])
		self.assertEqual(p["version"], NEWER)
		self.assertEqual(p["message"], "M")
		# Empty dict clears every field.
		release_notice.persist({})
		p = release_notice.boot_payload()
		self.assertFalse(p["active"])
		self.assertEqual(p["version"], "")
		self.assertEqual(p["message"], "")

	# -- tier (Slice 2) -------------------------------------------------------

	def test_persist_and_boot_carry_tier(self):
		release_notice.persist({"active": 0, "tier": "soft", "version": NEWER, "message": "m"})
		p = release_notice.boot_payload()
		self.assertEqual(p["tier"], "soft")
		self.assertFalse(p["active"])

	def test_hard_tier_sets_active(self):
		release_notice.persist({"active": 1, "tier": "hard", "version": NEWER, "message": "m"})
		p = release_notice.boot_payload()
		self.assertEqual(p["tier"], "hard")
		self.assertTrue(p["active"])

	def test_missing_tier_derives_from_active(self):
		# Old CP omits the tier key -> derive it from active (a hard gate reads hard).
		release_notice.persist({"active": 1, "version": NEWER, "message": "m"})
		self.assertEqual(release_notice.boot_payload()["tier"], "hard")

	def test_self_clear_zeroes_both_tiers(self):
		# Bench already at target -> both active and tier clear, even a stored hard.
		release_notice.persist({"active": 1, "tier": "hard", "version": __version__, "message": "m"})
		p = release_notice.boot_payload()
		self.assertFalse(p["active"])
		self.assertEqual(p["tier"], "none")

	def test_empty_notice_clears_tier(self):
		release_notice.persist({"active": 1, "tier": "hard", "version": NEWER, "message": "m"})
		release_notice.persist({})
		self.assertEqual(release_notice.boot_payload()["tier"], "none")

	# -- behind / banner interval (Slice 3b) ----------------------------------

	def test_boot_soft(self):
		self._set(
			release_notice_active=0,
			latest_jarvis_version=NEWER,
			release_notice_tier="soft",
			release_notice_behind=2,
			release_banner_interval_days=14,
		)
		p = release_notice.boot_payload()
		self.assertEqual(p["tier"], "soft")
		self.assertEqual(p["behind"], 2)
		self.assertFalse(p["active"])
		self.assertEqual(p["banner_interval_days"], 14)

	def test_boot_hard_active_true(self):
		self._set(
			release_notice_active=1,
			latest_jarvis_version=NEWER,
			release_notice_tier="hard",
			release_notice_behind=4,
		)
		p = release_notice.boot_payload()
		self.assertEqual(p["tier"], "hard")
		self.assertTrue(p["active"])
		self.assertEqual(p["behind"], 4)

	def test_boot_current_zeroes_behind(self):
		# Bench already at/past target: `behind` reads 0 regardless of a stale stored value.
		self._set(
			release_notice_active=1,
			latest_jarvis_version=__version__,
			release_notice_tier="hard",
			release_notice_behind=9,
		)
		p = release_notice.boot_payload()
		self.assertEqual(p["behind"], 0)
		self.assertEqual(p["tier"], "none")
		self.assertFalse(p["active"])

	def test_self_clear_forces_current(self):
		# The bench holds the target version -> everything reads current even with a
		# stored hard tier and a non-zero behind (control plane need not be reachable).
		release_notice.persist(
			{"active": 1, "tier": "hard", "version": __version__, "message": "m", "behind": 5}
		)
		p = release_notice.boot_payload()
		self.assertFalse(p["active"])
		self.assertEqual(p["tier"], "none")
		self.assertEqual(p["behind"], 0)

	def test_active_equals_tier_hard(self):
		# The load-bearing invariant: active is true exactly when the tier is hard,
		# across every producible tier.
		for tier, act in (("hard", 1), ("soft", 0), ("none", 0)):
			release_notice.persist(
				{"active": act, "tier": tier, "version": NEWER, "message": "m", "behind": 1}
			)
			p = release_notice.boot_payload()
			self.assertEqual(p["active"], p["tier"] == "hard", tier)

	def test_oldcp_no_behind_defaults_zero(self):
		# An old CP omits `behind`; persist stores 0 and boot reports 0 (no crash, no stale).
		release_notice.persist({"active": 0, "tier": "soft", "version": NEWER, "message": "m"})
		self.assertEqual(release_notice.boot_payload()["behind"], 0)

	def test_oldcp_no_interval_defaults_seven(self):
		# An old CP omits `banner_interval_days`; persist/boot default it to 7.
		release_notice.persist({"active": 0, "tier": "soft", "version": NEWER, "message": "m"})
		self.assertEqual(release_notice.boot_payload()["banner_interval_days"], 7)


class TestNotes(FrappeTestCase):
	"""jarvis.release_notice.notes() derives the track + since-version from the bench's
	own version and degrades every admin failure to an empty list."""

	def test_notes_derives_track_and_since(self):
		with (
			patch.object(release_notice, "__version__", "16.2.0"),
			patch(
				"jarvis.admin_client.get_release_notes",
				return_value={"notes": [{"version": "16.4.0"}]},
			) as gn,
		):
			out = release_notice.notes()
		gn.assert_called_once_with("16", "16.2.0", timeout_s=8)
		self.assertEqual(out, {"notes": [{"version": "16.4.0"}]})
		# A clean success never carries the error flag -> the panel shows notes /
		# "all caught up", never its error state.
		self.assertNotIn("error", out)

	def test_notes_swallows_and_logs_admin_error(self):
		with (
			patch.object(release_notice, "__version__", "16.2.0"),
			patch("jarvis.admin_client.get_release_notes", side_effect=RuntimeError("boom")),
			patch("frappe.log_error") as log,
		):
			out = release_notice.notes()
		# A genuine fetch failure flags the error so the panel shows its error state
		# (+ Retry), NOT the misleading "all caught up" empty state.
		self.assertEqual(out, {"notes": [], "error": True})
		log.assert_called_once()

	def test_notes_auth_error_returns_empty(self):
		# A lapsed customer behind a stale pill: get_release_notes raises AdminAuthError.
		# notes() must swallow it (never propagate raw CP prose) and flag the error state.
		from jarvis.exceptions import AdminAuthError

		with (
			patch.object(release_notice, "__version__", "16.2.0"),
			patch(
				"jarvis.admin_client.get_release_notes",
				side_effect=AdminAuthError("lapsed", status_code=403),
			),
			patch("frappe.log_error"),
		):
			out = release_notice.notes()
		self.assertEqual(out, {"notes": [], "error": True})

	def test_notes_unversioned_empty(self):
		# major < 15 short-circuits before any admin round-trip. "Nothing to show" is
		# NOT an error, so no error flag travels -> the panel shows "all caught up".
		with (
			patch.object(release_notice, "__version__", "0.0.1"),
			patch("jarvis.admin_client.get_release_notes") as gn,
		):
			out = release_notice.notes()
		self.assertEqual(out, {"notes": []})
		self.assertNotIn("error", out)
		gn.assert_not_called()


class TestVersionParse(FrappeTestCase):
	"""_version is the parser both sides of the self-clear compare go through,
	so pin its edge cases directly rather than through the notice tests."""

	def test_dotted_ints(self):
		self.assertEqual(release_notice._version("16.2.0"), (16, 2, 0))

	def test_short_and_long_forms_pad_or_truncate(self):
		self.assertEqual(release_notice._version("16.2"), (16, 2, 0))
		self.assertEqual(release_notice._version("16.2.0.7"), (16, 2, 0))

	def test_unparseable_is_zero(self):
		for raw in ("", None, "abc", "16.2.0rc1", "v16.2.0"):
			self.assertEqual(release_notice._version(raw), (0, 0, 0), raw)

	def test_unparseable_never_lifts_the_notice(self):
		with patch.object(release_notice, "__version__", "16.2.0rc1"):
			self.assertFalse(release_notice._already_current("16.2.0"))
		self.assertFalse(release_notice._already_current("garbage"))
