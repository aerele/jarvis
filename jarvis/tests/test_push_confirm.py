from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import push_confirm
from jarvis.exceptions import AdminAmbiguousError, AdminUnreachableError


class TestPushConfirm(FrappeTestCase):
	def test_only_a_timed_out_answer_counts_as_unconfirmed(self):
		cases = [
			(AdminUnreachableError("admin returned a 502 error: Read timed out.", status_code=502), True),
			(AdminUnreachableError("admin returned a 504 error: upstream timed out", status_code=504), True),
			(AdminAmbiguousError("admin did not answer in time"), True),
			(AdminUnreachableError("admin returned a 502 error: invalid_spec", status_code=502), False),
			(AdminUnreachableError("admin is unreachable; check network / service status"), False),
			(ValueError("timed out"), False),
		]
		for err, expected in cases:
			with self.subTest(err=str(err)):
				self.assertEqual(push_confirm.is_unconfirmed(err), expected)

	def test_wait_for_agent_returns_once_the_assistant_answers(self):
		answers = [ConnectionError("down"), ConnectionError("down"), None]

		def _ping(url):
			res = answers.pop(0)
			if res:
				raise res

		with (
			patch("jarvis.agent_ws.ping", side_effect=_ping),
			patch.object(frappe.db, "get_single_value", return_value="ws://agent"),
		):
			self.assertTrue(push_confirm.wait_for_agent(timeout_s=60, poll_s=1, sleep=lambda s: None))
		self.assertEqual(answers, [])

	def test_wait_for_agent_gives_up_at_the_deadline(self):
		with (
			patch("jarvis.agent_ws.ping", side_effect=ConnectionError("down")),
			patch.object(frappe.db, "get_single_value", return_value="ws://agent"),
		):
			self.assertFalse(push_confirm.wait_for_agent(timeout_s=0, poll_s=1, sleep=lambda s: None))
