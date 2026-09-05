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
