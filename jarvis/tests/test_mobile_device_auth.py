"""JF-016 — per-device, revocable mobile credentials.

The defect these tests pin down: `jarvis.mobile.auth.get_mobile_token` used to
hand the phone the user's account-wide Frappe api_key/api_secret. It was not
device-scoped (two phones shared one credential), logout revoked nothing, and
revoking it to kill a lost handset broke every other API integration the user
owned.

Six things have to hold at once, and each has a test here:

1. Pairing mints a NEW credential per device and NEVER discloses the global
   api_secret.
2. The three-segment `jmd:<id>:<secret>` shape is load-bearing — core's
   api-key parser must DECLINE it (a two-segment unknown key raises
   AuthenticationError before any app hook can run; the negative control below
   proves that is not hypothetical).
3. Revoking one device kills exactly that device, leaving the user's other
   devices AND the untouched global keypair working.
4. Nothing recoverable is at rest: only an HMAC-SHA256 of the secret, keyed by
   the token id.
5. A session authenticated BY a device token is lower-trust than a password
   session: it cannot mint a sibling token, and it cannot revoke a sibling
   device selectively — only itself, or everything including itself. Both
   holes were live and both let a stolen phone outlive its own revocation.
6. Revoked rows do not accumulate forever.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
from types import SimpleNamespace

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, get_datetime, now_datetime

from jarvis.mobile import auth as mobile_auth
from jarvis.mobile import device_auth

DEVICE_DOCTYPE = device_auth.DEVICE_DOCTYPE

USER_A = "jf016-usera@example.com"
USER_B = "jf016-userb@example.com"
DISABLED = "jf016-disabled@example.com"
WEBSITE = "jf016-website@example.com"
PFX = "jf016"

_MANAGEABLE = {"System Manager", "Jarvis User", "Jarvis Admin", "Jarvis Skill Reviewer"}


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


@contextlib.contextmanager
def _request(authorization: str | None, method: str = "POST", session_user: str = "Guest"):
	"""Minimal stand-in for a live request so the auth hook can be exercised.

	`frappe.get_request_header` reads `frappe.local.request.headers`, and the
	hook mirrors core by consulting `frappe.local.login_manager.user`, so both
	are what a request has to provide."""
	sentinel = object()
	prev_request = getattr(frappe.local, "request", sentinel)
	prev_login_manager = getattr(frappe.local, "login_manager", sentinel)
	prev_user = frappe.session.user
	prev_device = frappe.local.flags.get("jarvis_mobile_device")

	headers = {"Authorization": authorization} if authorization else {}
	frappe.local.request = SimpleNamespace(method=method, headers=headers)
	frappe.local.login_manager = SimpleNamespace(user=session_user)
	frappe.local.flags.jarvis_mobile_device = None
	frappe.set_user(session_user)
	try:
		yield
	finally:
		frappe.set_user(prev_user)
		frappe.local.flags.jarvis_mobile_device = prev_device
		_restore(frappe.local, "request", prev_request, sentinel)
		_restore(frappe.local, "login_manager", prev_login_manager, sentinel)


def _restore(target, attr: str, value, sentinel) -> None:
	"""Put an attribute back exactly as it was — ABSENT if it was absent, since
	a leftover None would make `hasattr(frappe.local, "login_manager")` lie."""
	if value is sentinel:
		with contextlib.suppress(AttributeError):
			delattr(target, attr)
	else:
		setattr(target, attr, value)


def _ensure_user(email: str, roles: list[str], user_type: str = "System User", enabled: int = 1) -> str:
	from jarvis.permissions import ensure_jarvis_user_role

	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": PFX,
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": user_type,
			}
		)
		doc.insert(ignore_permissions=True)
	frappe.db.set_value("User", email, {"user_type": user_type, "enabled": 1})
	desired = set(roles)
	current = set(frappe.get_roles(email))
	doc = frappe.get_doc("User", email)
	if desired - current:
		doc.add_roles(*(desired - current))
	if (_MANAGEABLE & current) - desired:
		doc.remove_roles(*((_MANAGEABLE & current) - desired))
	# `enabled` last: add_roles/remove_roles save the User doc, and a disabled
	# user cannot be saved without tripping core's validation.
	frappe.db.set_value("User", email, "enabled", enabled)
	frappe.clear_cache(user=email)
	return email


class MobileDeviceBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_user(USER_A, ["Jarvis User"])
		_ensure_user(USER_B, ["Jarvis User"])
		_ensure_user(DISABLED, ["Jarvis User"], enabled=0)
		_ensure_user(WEBSITE, [], user_type="Website User")

	def setUp(self):
		super().setUp()
		for user in (USER_A, USER_B, DISABLED, WEBSITE):
			frappe.db.delete(DEVICE_DOCTYPE, {"user": user})
			frappe.cache.delete(mobile_auth._pairing_cache_key(user))
		frappe.local.flags.jarvis_mobile_device = None


# --------------------------------------------------------------------------- #
# 1. Pairing mints a per-device credential
# --------------------------------------------------------------------------- #
class TestPairingMintsPerDeviceTokens(MobileDeviceBase):
	def test_each_pairing_issues_a_distinct_token(self):
		with _as(USER_A):
			first = mobile_auth.get_mobile_token(device_label="Pixel 8", platform="android")
			second = mobile_auth.get_mobile_token(device_label="iPad", platform="ios")

		self.assertNotEqual(first["device"], second["device"])
		self.assertNotEqual(first["api_secret"], second["api_secret"])
		self.assertNotEqual(first["device_token"], second["device_token"])
		# Both are live, and both authenticate as the same user.
		self.assertEqual(device_auth.resolve(first["device_token"])["user"], USER_A)
		self.assertEqual(device_auth.resolve(second["device_token"])["user"], USER_A)
		self.assertEqual(
			frappe.db.count(DEVICE_DOCTYPE, {"user": USER_A, "enabled": 1}),
			2,
			"each pairing must create its own device row",
		)

	def test_legacy_api_key_secret_pair_joins_into_the_device_token(self):
		"""Shipped app builds send `token <api_key>:<api_secret>` — that join
		has to reproduce the device token exactly, or an un-updated app breaks
		on upgrade."""
		with _as(USER_A):
			minted = mobile_auth.get_mobile_token(device_label="Old build")
		joined = f"{minted['api_key']}:{minted['api_secret']}"
		self.assertEqual(joined, minted["device_token"])
		self.assertTrue(joined.startswith(f"{device_auth.DEVICE_TOKEN_PREFIX}:"))
		self.assertEqual(device_auth.resolve(joined)["user"], USER_A)

	def test_response_never_carries_the_global_api_secret(self):
		global_secret = frappe.generate_hash(length=15)
		user = frappe.get_doc("User", USER_A)
		user.api_key = frappe.generate_hash(length=15)
		user.api_secret = global_secret
		user.save(ignore_permissions=True)

		with _as(USER_A):
			minted = mobile_auth.get_mobile_token(device_label="Phone")

		self.assertNotEqual(minted["api_secret"], global_secret)
		self.assertNotIn(global_secret, minted["device_token"])
		# ...and the global pair is left exactly as it was (rotating it would
		# break the user's other API integrations).
		from frappe.utils.password import get_decrypted_password

		self.assertEqual(
			get_decrypted_password("User", USER_A, "api_secret", raise_exception=False),
			global_secret,
		)
		self.assertEqual(frappe.db.get_value("User", USER_A, "api_key"), user.api_key)

	def test_device_label_and_platform_are_bounded(self):
		with _as(USER_A):
			minted = mobile_auth.get_mobile_token(device_label="x" * 500, platform="ANDROID" * 40)
		row = frappe.db.get_value(
			DEVICE_DOCTYPE, {"token_id": minted["device"]}, ["device_label", "platform"], as_dict=True
		)
		self.assertLessEqual(len(row.device_label), 140)
		self.assertLessEqual(len(row.platform), 40)
		self.assertEqual(row.platform, row.platform.lower())

	def test_website_user_cannot_pair(self):
		with _as(WEBSITE), self.assertRaises(frappe.PermissionError):
			mobile_auth.get_mobile_token(device_label="Portal phone")

	def test_guest_cannot_pair(self):
		with _as("Guest"), self.assertRaises(frappe.AuthenticationError):
			mobile_auth.get_mobile_token()

	def test_pairing_is_rate_limited_per_user(self):
		# A throwaway identity so the counter is clean regardless of what else
		# ran on this site in the last window.
		who = f"jf016-throttle-{frappe.generate_hash(length=8)}@example.com"
		frappe.cache.delete(mobile_auth._pairing_cache_key(who))
		try:
			for _ in range(mobile_auth.PAIRING_LIMIT):
				mobile_auth._throttle_pairing(who)
			with self.assertRaises(frappe.RateLimitExceededError):
				mobile_auth._throttle_pairing(who)
		finally:
			frappe.cache.delete(mobile_auth._pairing_cache_key(who))

	def test_device_limit_prunes_the_oldest_live_device(self):
		minted = [device_auth.mint(USER_A, f"phone-{i}") for i in range(device_auth.MAX_DEVICES_PER_USER + 2)]
		live = frappe.db.count(DEVICE_DOCTYPE, {"user": USER_A, "enabled": 1})
		self.assertEqual(live, device_auth.MAX_DEVICES_PER_USER)
		self.assertIsNone(device_auth.resolve(minted[0]["token"]), "oldest device should be pruned")
		self.assertIsNotNone(device_auth.resolve(minted[-1]["token"]), "newest device must survive")


# --------------------------------------------------------------------------- #
# 2. Token shape: core's api-key parser must decline it
# --------------------------------------------------------------------------- #
class TestTokenShape(MobileDeviceBase):
	def test_core_api_key_path_declines_a_device_token(self):
		from frappe.auth import validate_auth_via_api_keys

		minted = device_auth.mint(USER_A, "Phone")
		with _request(f"token {minted['token']}"):
			# Must NOT raise: three segments trip the ValueError that core
			# swallows, so the request survives to reach the auth hooks.
			validate_auth_via_api_keys(["token", minted["token"]])
			self.assertEqual(frappe.session.user, "Guest")

	def test_two_segment_unknown_key_still_raises_in_core(self):
		"""Negative control for the shape decision: had the device token been a
		plain `key:secret`, core would 401 it before any app hook ran."""
		from frappe.auth import validate_auth_via_api_keys

		with _request("token deadbeef:cafebabe"), self.assertRaises(frappe.AuthenticationError):
			validate_auth_via_api_keys(["token", "deadbeef:cafebabe"])

	def test_parse_token_rejects_a_frappe_keypair(self):
		self.assertIsNone(device_auth.parse_token("abc123:def456"))
		self.assertIsNone(device_auth.parse_token("jmd:onlyid"))
		self.assertIsNone(device_auth.parse_token("other:id:secret"))
		self.assertIsNone(device_auth.parse_token("jmd::secret"))
		self.assertIsNone(device_auth.parse_token(""))
		self.assertIsNone(device_auth.parse_token(None))
		self.assertEqual(device_auth.parse_token("jmd:id:secret"), ("id", "secret"))


# --------------------------------------------------------------------------- #
# 3. The auth hook
# --------------------------------------------------------------------------- #
class TestAuthHook(MobileDeviceBase):
	def test_valid_token_authenticates_as_the_user(self):
		minted = device_auth.mint(USER_A, "Phone")
		with _request(f"token {minted['token']}"):
			device_auth.authenticate_device_token()
			self.assertEqual(frappe.session.user, USER_A)
			self.assertEqual(device_auth.current_device_token_id(), minted["token_id"])

	def test_tampered_secret_fails_closed(self):
		minted = device_auth.mint(USER_A, "Phone")
		forged = device_auth.format_token(minted["token_id"], frappe.generate_hash(length=48))
		with _request(f"token {forged}"):
			device_auth.authenticate_device_token()
			self.assertEqual(frappe.session.user, "Guest")

	def test_unknown_token_id_fails_closed(self):
		with _request(f"token jmd:{frappe.generate_hash(length=24)}:{frappe.generate_hash(length=48)}"):
			device_auth.authenticate_device_token()
			self.assertEqual(frappe.session.user, "Guest")

	def test_non_token_authorization_is_left_alone(self):
		minted = device_auth.mint(USER_A, "Phone")
		with _request(f"Bearer {minted['token']}"):
			device_auth.authenticate_device_token()
			self.assertEqual(frappe.session.user, "Guest")

	def test_established_cookie_session_wins(self):
		"""Mirrors core's api-key path, which only sets the user when the
		resumed session is still Guest."""
		minted = device_auth.mint(USER_A, "Phone")
		with _request(f"token {minted['token']}", session_user=USER_B):
			device_auth.authenticate_device_token()
			self.assertEqual(frappe.session.user, USER_B)
			self.assertIsNone(device_auth.current_device_token_id())

	def test_form_dict_survives_authentication(self):
		"""frappe.set_user() wipes form_dict; losing it would drop every
		argument of the request."""
		minted = device_auth.mint(USER_A, "Phone")
		with _request(f"token {minted['token']}"):
			frappe.local.form_dict = frappe._dict({"cmd": "some.method", "conversation": "abc"})
			device_auth.authenticate_device_token()
			self.assertEqual(frappe.local.form_dict.get("conversation"), "abc")

	def test_disabled_user_token_is_refused(self):
		minted = device_auth.mint(DISABLED, "Phone")
		self.assertIsNone(device_auth.resolve(minted["token"]))
		with _request(f"token {minted['token']}"):
			device_auth.authenticate_device_token()
			self.assertEqual(frappe.session.user, "Guest")

	def test_realtime_handshake_get_authenticates(self):
		"""The socket.io polling handshake is a GET that Frappe's realtime
		middleware replays against /api/method/frappe.realtime.get_user_info
		with the same Authorization header, so the hook has to serve it too.
		`user_type` is absent from a set_user() session by design — core falls
		back to a cached User read for exactly this case."""
		minted = device_auth.mint(USER_A, "Phone")
		# Pre-stamp last_used so the throttled stamp short-circuits and this
		# read-only request takes no write (and therefore no commit) path.
		frappe.db.set_value(
			DEVICE_DOCTYPE,
			{"token_id": minted["token_id"]},
			"last_used",
			now_datetime(),
			update_modified=False,
		)
		with _request(f"token {minted['token']}", method="GET"):
			device_auth.authenticate_device_token()
			self.assertEqual(frappe.session.user, USER_A)
			self.assertFalse(frappe.session.data.get("user_type"))
			self.assertEqual(frappe.get_cached_value("User", frappe.session.user, "user_type"), "System User")


# --------------------------------------------------------------------------- #
# 4. Revocation
# --------------------------------------------------------------------------- #
class TestRevocation(MobileDeviceBase):
	def test_revoke_one_kills_exactly_that_device(self):
		phone = device_auth.mint(USER_A, "Phone")
		tablet = device_auth.mint(USER_A, "Tablet")
		other_user = device_auth.mint(USER_B, "Phone")

		with _as(USER_A):
			result = mobile_auth.revoke_mobile_device(phone["token_id"])
		self.assertEqual(result["revoked"], 1)

		self.assertIsNone(device_auth.resolve(phone["token"]), "revoked device still authenticates")
		self.assertEqual(device_auth.resolve(tablet["token"])["user"], USER_A)
		self.assertEqual(device_auth.resolve(other_user["token"])["user"], USER_B)

		row = frappe.db.get_value(
			DEVICE_DOCTYPE,
			{"token_id": phone["token_id"]},
			["enabled", "revoked_at", "revoked_by"],
			as_dict=True,
		)
		self.assertEqual(row.enabled, 0)
		self.assertIsNotNone(row.revoked_at)
		self.assertEqual(row.revoked_by, USER_A)

	def test_revoking_a_device_leaves_the_global_keypair_working(self):
		"""The whole point of the fix: killing a lost handset must not break
		the user's other API integrations."""
		from frappe.auth import validate_api_key_secret

		global_secret = frappe.generate_hash(length=15)
		user = frappe.get_doc("User", USER_A)
		user.api_key = frappe.generate_hash(length=15)
		user.api_secret = global_secret
		user.save(ignore_permissions=True)

		phone = device_auth.mint(USER_A, "Phone")
		with _as(USER_A):
			mobile_auth.revoke_mobile_device(phone["token_id"])

		self.assertIsNone(device_auth.resolve(phone["token"]))
		with _request(f"token {user.api_key}:{global_secret}"):
			validate_api_key_secret(user.api_key, global_secret)
			self.assertEqual(frappe.session.user, USER_A)

	def test_revoke_is_idempotent(self):
		phone = device_auth.mint(USER_A, "Phone")
		with _as(USER_A):
			self.assertEqual(mobile_auth.revoke_mobile_device(phone["token_id"])["revoked"], 1)
			self.assertEqual(mobile_auth.revoke_mobile_device(phone["token_id"])["revoked"], 0)

	def test_cannot_revoke_another_users_device(self):
		victim = device_auth.mint(USER_B, "Victim phone")
		with _as(USER_A), self.assertRaises(frappe.PermissionError):
			mobile_auth.revoke_mobile_device(victim["token_id"])
		self.assertEqual(device_auth.resolve(victim["token"])["user"], USER_B)

	def test_unknown_device_is_indistinguishable_from_someone_elses(self):
		with _as(USER_A), self.assertRaises(frappe.PermissionError):
			mobile_auth.revoke_mobile_device(frappe.generate_hash(length=24))

	def test_revoke_all_from_a_password_session_kills_every_device(self):
		tokens = [device_auth.mint(USER_A, f"phone-{i}") for i in range(3)]
		survivor = device_auth.mint(USER_B, "Other user")

		with _as(USER_A):
			self.assertEqual(mobile_auth.revoke_all_mobile_devices()["revoked"], 3)

		for token in tokens:
			self.assertIsNone(device_auth.resolve(token["token"]))
		self.assertEqual(device_auth.resolve(survivor["token"])["user"], USER_B)

	def test_revoke_all_from_a_device_session_takes_the_caller_with_it(self):
		"""Sign-out-everywhere from a phone must NOT spare the phone it was called
		from. `keep_current` used to do exactly that, which made one POST with a
		stolen token evict every legitimate device and preserve the thief's."""
		caller = device_auth.mint(USER_A, "This phone")
		other = device_auth.mint(USER_A, "Old phone")
		with _request(f"token {caller['token']}"):
			device_auth.authenticate_device_token()
			self.assertEqual(mobile_auth.revoke_all_mobile_devices()["revoked"], 2)
		self.assertIsNone(device_auth.resolve(caller["token"]), "the calling device must be revoked too")
		self.assertIsNone(device_auth.resolve(other["token"]))

	def test_inventory_lists_only_own_devices_and_no_secret(self):
		mine = device_auth.mint(USER_A, "Phone")
		device_auth.mint(USER_B, "Other phone")
		with _as(USER_A):
			rows = mobile_auth.list_mobile_devices()
		self.assertEqual([r["token_id"] for r in rows], [mine["token_id"]])
		self.assertNotIn("secret_hash", rows[0])
		self.assertEqual(rows[0]["device_label"], "Phone")


# --------------------------------------------------------------------------- #
# 5. A device-token session is lower-trust than a password session
# --------------------------------------------------------------------------- #
class TestDeviceSessionTrustBoundary(MobileDeviceBase):
	"""A device token lives on a handset that can be lost, so it may not do the
	two things that would let a stolen phone outlive its own revocation: mint a
	sibling credential, or pick another device off while sparing itself."""

	def test_a_device_token_cannot_mint_a_sibling(self):
		"""Nothing legitimate hits this path: the mobile client's `login()`
		clears the stored token before it signs in (src/api/client.ts, step 0 of
		`login`), so every real pairing is authenticated by the password just
		typed. Without the guard, a stolen token mints siblings that survive
		revoking the phone they came from."""
		stolen = device_auth.mint(USER_A, "Stolen phone")
		with _request(f"token {stolen['token']}"):
			device_auth.authenticate_device_token()
			self.assertEqual(frappe.session.user, USER_A)
			with self.assertRaises(frappe.PermissionError):
				mobile_auth.get_mobile_token(device_label="Sibling")
		self.assertEqual(
			frappe.db.count(DEVICE_DOCTYPE, {"user": USER_A, "enabled": 1}),
			1,
			"no sibling row may be created",
		)

	def test_a_password_session_still_pairs(self):
		"""The negative control for the guard above: pairing itself is untouched
		when the session is not a device-token one."""
		with _as(USER_A):
			minted = mobile_auth.get_mobile_token(device_label="New phone")
		self.assertEqual(device_auth.resolve(minted["device_token"])["user"], USER_A)

	def test_a_device_token_cannot_revoke_a_sibling(self):
		"""The retail version of the eviction attack: without this, a thief
		loops revoke-one over the inventory and keeps their own access — exactly
		what dropping `keep_current` from revoke-all was meant to prevent."""
		stolen = device_auth.mint(USER_A, "Stolen phone")
		victim = device_auth.mint(USER_A, "Real phone")
		with _request(f"token {stolen['token']}"):
			device_auth.authenticate_device_token()
			with self.assertRaises(frappe.PermissionError):
				mobile_auth.revoke_mobile_device(victim["token_id"])
		self.assertEqual(device_auth.resolve(victim["token"])["user"], USER_A)
		self.assertEqual(device_auth.resolve(stolen["token"])["user"], USER_A)

	def test_a_device_token_may_revoke_itself(self):
		"""The ordinary sign-out path — the app revokes its own credential."""
		phone = device_auth.mint(USER_A, "This phone")
		other = device_auth.mint(USER_A, "Other phone")
		with _request(f"token {phone['token']}"):
			device_auth.authenticate_device_token()
			self.assertEqual(mobile_auth.revoke_mobile_device(phone["token_id"])["revoked"], 1)
		self.assertIsNone(device_auth.resolve(phone["token"]))
		self.assertEqual(device_auth.resolve(other["token"])["user"], USER_A)

	def test_a_password_session_may_revoke_any_own_device(self):
		"""Selective remote revocation stays possible — from the web app / Desk,
		where the session is authenticated by the password a thief lacks."""
		lost = device_auth.mint(USER_A, "Lost phone")
		kept = device_auth.mint(USER_A, "Desk browser")
		with _as(USER_A):
			self.assertEqual(mobile_auth.revoke_mobile_device(lost["token_id"])["revoked"], 1)
		self.assertIsNone(device_auth.resolve(lost["token"]))
		self.assertEqual(device_auth.resolve(kept["token"])["user"], USER_A)

	def test_inventory_is_readable_from_a_device_session_and_marks_the_caller(self):
		"""The phone's Devices screen runs on this: it needs to know which row is
		the handset in the user's hand."""
		here = device_auth.mint(USER_A, "This phone")
		device_auth.mint(USER_A, "Other phone")
		with _request(f"token {here['token']}"):
			device_auth.authenticate_device_token()
			rows = mobile_auth.list_mobile_devices()
		current = [r for r in rows if r["current"]]
		self.assertEqual(len(rows), 2)
		self.assertEqual([r["token_id"] for r in current], [here["token_id"]])


# --------------------------------------------------------------------------- #
# 6. Revoked-row hygiene
# --------------------------------------------------------------------------- #
class TestRevokedRowPruning(MobileDeviceBase):
	def _age(self, token_id: str, field: str, days: int) -> str:
		name = frappe.db.get_value(DEVICE_DOCTYPE, {"token_id": token_id}, "name")
		frappe.db.set_value(
			DEVICE_DOCTYPE, name, field, add_to_date(now_datetime(), days=-days), update_modified=False
		)
		return name

	def test_old_revoked_rows_are_deleted_and_live_ones_are_not(self):
		old = device_auth.mint(USER_A, "Ancient phone")
		recent = device_auth.mint(USER_A, "Yesterday's phone")
		live = device_auth.mint(USER_A, "Working phone")
		with _as(USER_A):
			mobile_auth.revoke_mobile_device(old["token_id"])
			mobile_auth.revoke_mobile_device(recent["token_id"])
		self._age(old["token_id"], "revoked_at", device_auth.REVOKED_RETENTION_DAYS + 5)
		# A live credential is never deleted by the sweep, however old it is.
		self._age(live["token_id"], "modified", device_auth.REVOKED_RETENTION_DAYS * 4)

		# Not an equality check: the sweep is site-wide, so unrelated garbage
		# left by another test on this site may be collected in the same pass.
		self.assertGreaterEqual(device_auth.prune_revoked_devices(), 1)

		self.assertFalse(frappe.db.exists(DEVICE_DOCTYPE, {"token_id": old["token_id"]}))
		self.assertTrue(frappe.db.exists(DEVICE_DOCTYPE, {"token_id": recent["token_id"]}))
		self.assertEqual(device_auth.resolve(live["token"])["user"], USER_A)

	def test_rows_disabled_outside_the_module_are_not_immortal(self):
		"""A System Manager unticking `enabled` in Desk leaves no revoked_at, so
		the sweep falls back to `modified`."""
		orphan = device_auth.mint(USER_A, "Disabled in Desk")
		name = frappe.db.get_value(DEVICE_DOCTYPE, {"token_id": orphan["token_id"]}, "name")
		frappe.db.set_value(DEVICE_DOCTYPE, name, "enabled", 0, update_modified=False)
		self._age(orphan["token_id"], "modified", device_auth.REVOKED_RETENTION_DAYS + 5)

		self.assertGreaterEqual(device_auth.prune_revoked_devices(), 1)
		self.assertFalse(frappe.db.exists(DEVICE_DOCTYPE, name))


# --------------------------------------------------------------------------- #
# 7. Storage + last_used
# --------------------------------------------------------------------------- #
class TestStorageAndTelemetry(MobileDeviceBase):
	def test_secret_is_hashed_at_rest(self):
		minted = device_auth.mint(USER_A, "Phone")
		secret = minted["secret"]
		row = frappe.db.sql(
			f"select * from `tab{DEVICE_DOCTYPE}` where token_id = %s",
			(minted["token_id"],),
			as_dict=True,
		)[0]
		for field, value in row.items():
			if isinstance(value, str):
				self.assertNotIn(secret, value, f"plaintext secret leaked into `{field}`")
		self.assertEqual(
			row["secret_hash"],
			hmac.new(minted["token_id"].encode(), secret.encode(), hashlib.sha256).hexdigest(),
		)
		# Salt binding is unambiguous: a token id / secret split cannot be
		# shifted across the boundary to produce the same digest.
		self.assertNotEqual(device_auth._hash_secret("a", "b:c"), device_auth._hash_secret("a:b", "c"))
		# Not a Password field either — nothing recoverable in __Auth.
		self.assertEqual(
			frappe.db.sql("select count(*) from `__Auth` where doctype = %s", (DEVICE_DOCTYPE,))[0][0],
			0,
		)

	def test_last_used_is_throttled(self):
		minted = device_auth.mint(USER_A, "Phone")
		name = frappe.db.get_value(DEVICE_DOCTYPE, {"token_id": minted["token_id"]}, "name")

		device_auth.resolve(minted["token"])
		first = frappe.db.get_value(DEVICE_DOCTYPE, name, "last_used")
		self.assertIsNotNone(first, "first authentication should stamp last_used")

		device_auth.resolve(minted["token"])
		self.assertEqual(
			frappe.db.get_value(DEVICE_DOCTYPE, name, "last_used"),
			first,
			"a second authentication inside the window must not write",
		)

		stale = add_to_date(now_datetime(), seconds=-(device_auth.LAST_USED_THROTTLE_SECONDS + 60))
		frappe.db.set_value(DEVICE_DOCTYPE, name, "last_used", stale, update_modified=False)
		device_auth.resolve(minted["token"])
		self.assertGreater(
			get_datetime(frappe.db.get_value(DEVICE_DOCTYPE, name, "last_used")),
			get_datetime(stale),
			"an authentication past the window must refresh last_used",
		)
