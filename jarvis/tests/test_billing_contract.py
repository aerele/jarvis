"""Plan 01 billing-object contract (bench side).

Pins the shared fixture sha (byte-identical twin in the admin repo) and asserts
the bench forwards the typed billing object to admin UNMODIFIED — the bench never
reshapes or flattens it; admin owns the normalizer.
"""

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client

_FIXTURE = Path(__file__).parent / "fixtures" / "billing_contract" / "billing_snapshot.json"
# sha256 of the shared billing_snapshot.json; the admin twin pins the same value.
_FIXTURE_SHA = "95b5460b850e45bc1c8c3de198f7ddd74d747daa541a0d3b6f16984d46c2ddc1"


class TestBillingContract(FrappeTestCase):
	def setUp(self):
		self.fx = json.loads(_FIXTURE.read_text())

	def test_fixture_sha_pinned(self):
		self.assertEqual(hashlib.sha256(_FIXTURE.read_bytes()).hexdigest(), _FIXTURE_SHA)

	def test_admin_client_forwards_billing_object_unmodified(self):
		captured = {}

		def _fake_post_guest(path, body):
			captured["body"] = body
			return {}

		with patch("jarvis.admin_client._post_guest", side_effect=_fake_post_guest):
			admin_client.signup("a@b.com", "Acme", "some-plan", billing=self.fx["request_billing"])
		self.assertEqual(captured["body"]["billing"], self.fx["request_billing"])
