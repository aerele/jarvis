"""The agent_token's age is tracked and System Managers are told to rotate it
(#16b): the site stamps ``agent_token_issued_at`` whenever it receives a token,
a patch stamps the one already in place, and the daily check notifies at 14 and
7 days left and every day past the limit.

Nothing touches the real Jarvis Settings: the settings, the password store and
the notifier are stand-ins.
"""

import datetime
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from jarvis import onboarding
from jarvis.oauth import cron


def _settings(*, token: str = "old", issued_at=None):
	fake = MagicMock()
	fake.get_password.return_value = token
	fake.get.side_effect = lambda field: issued_at if field == "agent_token_issued_at" else None
	return fake


class TestTokenStamping(FrappeTestCase):
	def _store(self, settings, token):
		with patch("jarvis._password_utils.set_settings_password") as store:
			onboarding._store_agent_token(settings, token)
		store.assert_called_once_with(settings, "agent_token", token)

	def test_a_new_token_restarts_the_clock(self):
		settings = _settings(token="old", issued_at=datetime.datetime(2026, 1, 1))
		self._store(settings, "new")
		settings.db_set.assert_called_once()
		self.assertEqual(settings.db_set.call_args.args[0], "agent_token_issued_at")

	def test_a_token_with_no_stamp_gets_one(self):
		settings = _settings(token="same", issued_at=None)
		self._store(settings, "same")
		settings.db_set.assert_called_once()

	def test_only_sync_connection_is_an_endpoint(self):
		import frappe

		frappe.is_whitelisted(onboarding.sync_connection)  # raises if not
		with self.assertRaises(frappe.PermissionError):
			frappe.is_whitelisted(onboarding._store_agent_token)

	def test_the_same_stamped_token_keeps_its_date(self):
		settings = _settings(token="same", issued_at=datetime.datetime(2026, 1, 1))
		self._store(settings, "same")
		settings.db_set.assert_not_called()


class TestStampPatch(FrappeTestCase):
	def _run(self, *, issued_at, token):
		from jarvis.patches import v2_27_stamp_agent_token_issued_at as p

		with (
			patch.object(p.frappe.db, "get_single_value", return_value=issued_at),
			patch.object(p, "get_decrypted_password", return_value=token),
			patch.object(p.frappe.db, "set_single_value") as stamp,
		):
			p.execute()
		return stamp

	def test_stamps_a_token_with_no_date(self):
		self._run(issued_at=None, token="t").assert_called_once()

	def test_leaves_a_dated_token_and_a_missing_token_alone(self):
		self._run(issued_at="2026-01-01 00:00:00", token="t").assert_not_called()
		self._run(issued_at=None, token=None).assert_not_called()


class TestAgeNotice(FrappeTestCase):
	def _notices(self, *, age_days: int, max_age_days: int = 90):
		fake = MagicMock()
		fake.agent_token_max_age_days = max_age_days
		fake.agent_token_issued_at = datetime.datetime.now() - datetime.timedelta(days=age_days, hours=1)
		with (
			patch.object(cron.frappe, "get_single", return_value=fake),
			patch("jarvis.learning.lifecycle.notify_system_managers") as notify,
		):
			cron.check_agent_token_age()
		return notify

	def test_notifies_at_14_and_7_days_left_and_daily_past_the_limit(self):
		for age_days in (76, 83, 90, 120):
			with self.subTest(age_days=age_days):
				notify = self._notices(age_days=age_days)
				notify.assert_called_once()
				self.assertIn("Rotate Agent Token", notify.call_args.args[1])

	def test_quiet_otherwise_and_when_disabled(self):
		for age_days, max_age_days in ((30, 90), (80, 90), (500, 0)):
			with self.subTest(age_days=age_days, max_age_days=max_age_days):
				self._notices(age_days=age_days, max_age_days=max_age_days).assert_not_called()
