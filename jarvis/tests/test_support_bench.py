from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client
from jarvis.permissions import (
	JARVIS_SUPPORT_ADMIN_ROLE,
	JARVIS_SUPPORT_USER_ROLE,
	JARVIS_USER_ROLE,
	ensure_support_roles,
	grant_default_support,
	support_scope,
)


def _user(roles):
	u = frappe.get_doc(
		{
			"doctype": "User",
			"email": f"{frappe.generate_hash(length=8)}@sup.test",
			"first_name": "S",
			"send_welcome_email": 0,
		}
	)
	for r in roles:
		u.append("roles", {"role": r})
	return u.insert(ignore_permissions=True).name


class TestSupportScope(FrappeTestCase):
	def setUp(self):
		ensure_support_roles()

	def test_none_without_role(self):
		self.assertIsNone(support_scope(_user([])))

	def test_own_for_support_user(self):
		self.assertEqual(support_scope(_user([JARVIS_SUPPORT_USER_ROLE])), "own")

	def test_all_for_support_admin(self):
		self.assertEqual(support_scope(_user([JARVIS_SUPPORT_ADMIN_ROLE])), "all")

	def test_default_grant_gives_own_to_jarvis_user(self):
		u = _user([JARVIS_USER_ROLE])
		self.assertIsNone(support_scope(u))
		grant_default_support(u)
		self.assertEqual(support_scope(u), "own")

	def test_default_grant_skips_administrator_and_guest(self):
		for u in ("Administrator", "Guest"):
			grant_default_support(u)
			self.assertFalse(frappe.db.exists("Has Role", {"parent": u, "role": JARVIS_SUPPORT_USER_ROLE}))


class TestAdminClientSupport(FrappeTestCase):
	def test_list_tickets_posts_to_support_path(self):
		with patch.object(admin_client, "_post", return_value={"ok": True, "data": {"tickets": []}}) as post:
			admin_client.support_list_tickets(requesting_user="u@x", scope="own")
			self.assertIn("support.api.list_tickets", post.call_args.kwargs["path"])
			self.assertEqual(post.call_args.kwargs["body"]["scope"], "own")

	def test_upload_posts_b64_via_support_path(self):
		with patch.object(
			admin_client, "_post", return_value={"ok": True, "data": {"file_url": "/f/x"}}
		) as post:
			admin_client.support_upload(
				ticket="T1", filename="x.png", content_b64="aGk=", requesting_user="u@x", scope="own"
			)
			self.assertIn("support.media.upload", post.call_args.kwargs["path"])
			self.assertEqual(post.call_args.kwargs["body"]["content_b64"], "aGk=")

	def test_authenticated_raw_remints_on_401_then_legacy(self):
		# bearer 401 -> re-mint bearer 401 -> legacy token 200 (preserves _post's ladder)
		settings = MagicMock()
		settings.get_password.side_effect = lambda f, **k: {
			"jarvis_admin_api_key": "k",
			"jarvis_admin_api_secret": "s",
		}.get(f)
		r401, r200 = MagicMock(status_code=401), MagicMock(status_code=200)
		with (
			patch("frappe.get_single", return_value=settings),
			patch.object(admin_client, "_admin_url", return_value="http://cp"),
			patch.object(admin_client, "_admin_access_token", side_effect=["tok1", "tok2"]),
			patch.object(admin_client, "requests") as rq,
		):
			rq.post.side_effect = [r401, r401, r200]
			resp = admin_client._authenticated_raw("/p", {}, timeout_s=10)
			self.assertIs(resp, r200)
			self.assertIn("token k:s", rq.post.call_args.kwargs["headers"]["Authorization"])


class TestSupportBenchEndpoints(FrappeTestCase):
	def test_list_refused_without_scope(self):
		from jarvis.support import api as sapi

		with patch("jarvis.support.api.support_scope", return_value=None):
			with self.assertRaises(frappe.PermissionError):
				sapi.list_tickets()

	def test_list_forwards_user_and_scope(self):
		from jarvis.support import api as sapi

		with (
			patch("jarvis.support.api.support_scope", return_value="own"),
			patch.object(sapi.admin_client, "support_list_tickets", return_value={"tickets": []}) as f,
		):
			out = sapi.list_tickets()
			self.assertTrue(out["ok"])
			self.assertEqual(f.call_args.kwargs["scope"], "own")
			# requesting_user is forwarded as Helpdesk's raised_by (must be a valid
			# email), so the endpoint resolves the User's email via _requesting_user,
			# not the bare login name (see the dedicated tests below).
			self.assertEqual(f.call_args.kwargs["requesting_user"], sapi._requesting_user())

	def test_requesting_user_resolves_email_not_login_name(self):
		"""_requesting_user forwards the User's email — Helpdesk's raised_by must
		be a valid email, and the bare login name breaks system accounts like
		Administrator, which Helpdesk 417s with InvalidEmailAddressError."""
		from jarvis.support import api as sapi

		with patch.object(sapi.frappe.db, "get_value", return_value="picked@email.com") as gv:
			self.assertEqual(sapi._requesting_user(), "picked@email.com")
		gv.assert_called_once_with("User", frappe.session.user, "email")

	def test_requesting_user_falls_back_to_login_name_when_email_unset(self):
		from jarvis.support import api as sapi

		with patch.object(sapi.frappe.db, "get_value", return_value=None):
			self.assertEqual(sapi._requesting_user(), frappe.session.user)

	def test_download_returns_response(self):
		from jarvis.support import media as smedia

		with (
			patch("jarvis.support.media.support_scope", return_value="own"),
			patch.object(
				smedia.admin_client,
				"support_download",
				return_value=(b"png", "image/png", "inline; filename=x"),
			) as dl,
		):
			out = smedia.download(ticket="T1", file_url="/f/x.png")
			self.assertEqual(out.headers["Content-Type"], "image/png")
			self.assertEqual(out.get_data(), b"png")
			# C1: media must forward the SAME resolved identity as api.py (the email),
			# not the bare login name — else own-scope attachment access 403s for any
			# user whose email != login name.
			self.assertEqual(dl.call_args.kwargs["requesting_user"], smedia._requesting_user())

	def test_upload_forwards_b64(self):
		import base64

		from jarvis.support import media as smedia

		req = MagicMock()
		f = MagicMock()
		f.filename = "x.png"
		f.read.return_value = b"hi"
		req.files.get.return_value = f
		frappe.local.request = req
		self.addCleanup(lambda: setattr(frappe.local, "request", None))
		with (
			patch("jarvis.support.media.support_scope", return_value="own"),
			patch.object(smedia.admin_client, "support_upload", return_value={"file_url": "/f/x"}) as up,
		):
			out = smedia.upload(ticket="T1")
			self.assertTrue(out["ok"])
			self.assertEqual(up.call_args.kwargs["content_b64"], base64.b64encode(b"hi").decode())
			# C1: same resolved identity as api.py, not the bare login name.
			self.assertEqual(up.call_args.kwargs["requesting_user"], smedia._requesting_user())

	def test_create_ticket_rejects_a_user_with_no_valid_email(self):
		"""raised_by must be a valid email; a blank/invalid one is caught here with
		an actionable message instead of surfacing as an opaque Helpdesk 417."""
		from jarvis.support import api as sapi

		with (
			patch("jarvis.support.api.support_scope", return_value="own"),
			patch.object(
				sapi.frappe.db, "get_value", return_value=None
			),  # no email → falls back to login name
			patch.object(sapi.admin_client, "support_create_ticket") as create,
		):
			with self.assertRaises(frappe.ValidationError):
				sapi.create_ticket(subject="S", body="B")
			create.assert_not_called()  # never forwarded to the control plane

	def test_create_ticket_forwards_the_resolved_email_when_valid(self):
		from jarvis.support import api as sapi

		with (
			patch("jarvis.support.api.support_scope", return_value="own"),
			patch.object(sapi.frappe.db, "get_value", return_value="real@user.com"),
			patch.object(sapi.admin_client, "support_create_ticket", return_value={"ticket": "T9"}) as create,
		):
			out = sapi.create_ticket(subject="S", body="B")
			self.assertTrue(out["ok"])
			self.assertEqual(create.call_args.kwargs["requesting_user"], "real@user.com")

	def test_upload_rejects_an_empty_file(self):
		"""M1: a 0-byte file is rejected with a clear message, not forwarded to the
		CP (which would reject it with an opaque 'content_b64 required')."""
		from jarvis.support import media as smedia

		req = MagicMock()
		f = MagicMock()
		f.filename = "empty.png"
		f.read.return_value = b""
		req.files.get.return_value = f
		frappe.local.request = req
		self.addCleanup(lambda: setattr(frappe.local, "request", None))
		with patch("jarvis.support.media.support_scope", return_value="own"):
			with self.assertRaises(frappe.ValidationError):
				smedia.upload(ticket="T1")


class TestSupportBoot(FrappeTestCase):
	def setUp(self):
		from jarvis.www import jarvis as jw

		frappe.cache().delete_value(jw._SUPPORT_AVAILABLE_CACHE_KEY)

	def test_support_available_false_on_error_and_cached(self):
		from jarvis.www import jarvis as jw

		with patch("jarvis.admin_client.support_status", side_effect=Exception("down")) as ss:
			self.assertFalse(jw._support_available())
			self.assertFalse(jw._support_available())  # cached, not re-called
			self.assertEqual(ss.call_count, 1)

	def test_support_available_true_when_status_available(self):
		from jarvis.www import jarvis as jw

		with patch("jarvis.admin_client.support_status", return_value={"available": True}):
			self.assertTrue(jw._support_available())

	# ---- the state the boolean cannot express ----

	def test_error_is_distinct_from_unconfigured_and_retries_sooner(self):
		# A transient CP blip must NOT read as "support was never set up" — it stays
		# hidden and re-checks on the short TTL.
		from jarvis.www import jarvis as jw

		with patch("jarvis.admin_client.support_status", side_effect=Exception("down")):
			self.assertEqual(jw._support_state(), jw.SUPPORT_ERROR)

	def test_unconfigured_reason_is_carried_through(self):
		from jarvis.www import jarvis as jw

		with patch(
			"jarvis.admin_client.support_status",
			return_value={"available": False, "reason": "unconfigured"},
		):
			self.assertEqual(jw._support_state(), jw.SUPPORT_UNCONFIGURED)
			self.assertFalse(jw._support_available())

	def test_off_reason_is_carried_through(self):
		from jarvis.www import jarvis as jw

		with patch("jarvis.admin_client.support_status", return_value={"available": False, "reason": "off"}):
			self.assertEqual(jw._support_state(), jw.SUPPORT_OFF)

	def test_old_cp_without_a_reason_degrades_to_off(self):
		# A CP that predates `reason` sends only `available` — keep today's hide-it
		# behaviour rather than claiming the fleet is unconfigured.
		from jarvis.www import jarvis as jw

		with patch("jarvis.admin_client.support_status", return_value={"available": False}):
			self.assertEqual(jw._support_state(), jw.SUPPORT_OFF)

	def test_unknown_reason_degrades_to_off(self):
		from jarvis.www import jarvis as jw

		with patch(
			"jarvis.admin_client.support_status",
			return_value={"available": False, "reason": "something-new"},
		):
			self.assertEqual(jw._support_state(), jw.SUPPORT_OFF)


class TestAuthenticatedRawErrors(FrappeTestCase):
	def test_bearer_4xx_raises_validation_not_returned_as_success(self):
		# R1-3: a CP 404/413/500 on the bearer path must raise, not come back as a 200 body.
		from jarvis.exceptions import AdminValidationError

		settings = MagicMock()
		settings.get_password.side_effect = lambda f, **k: None
		r404 = MagicMock(status_code=404, text="not found")
		with (
			patch("frappe.get_single", return_value=settings),
			patch.object(admin_client, "_admin_url", return_value="http://cp"),
			patch.object(admin_client, "_admin_access_token", return_value="tok"),
			patch.object(admin_client, "requests") as rq,
		):
			rq.post.return_value = r404
			with self.assertRaises(AdminValidationError):
				admin_client._authenticated_raw("/p", {}, timeout_s=10)
