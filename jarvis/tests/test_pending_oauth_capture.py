"""Durable pending OAuth capture (plan-05 D2, review P0-04 / §10.2).

Pins the security-critical invariants: the minted token never leaves the server
after the exchange, a capture is consumed at most once (atomic), a reload
rehydrates it, expiry revokes + erases, and captures fold on the stable subject.
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from jarvis.oauth import pending_capture as pc

DT = "Jarvis Pending OAuth Capture"

_BLOB = {
	"type": "oauth",
	"provider": "openai",
	"access": "AT-secret-do-not-leak",
	"refresh": "RT-secret-do-not-leak",
	"expires": 9999999999999,
	"email": "user@example.com",
	"accountId": "acct-stable-123",
}


def _mk(**over) -> dict:
	kw = dict(
		provider="OpenAI",
		upstream="openai",
		agent_provider="openai",
		oauth_blob=json.dumps(_BLOB),
		account_email="user@example.com",
		account_ref="SUB_deadbeefcafe0001",
		safe_label="user@example.com",
		provider_subject="acct-stable-123",
		nonce="nonce-xyz",
	)
	kw.update(over)
	return pc.create_capture(**kw)


class TestPendingCapture(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()
		# rollback undoes inserts; also clear any committed rows from sweep tests
		for name in frappe.get_all(DT, filters={"owner_user": frappe.session.user}, pluck="name"):
			frappe.delete_doc(DT, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	# ---- create + safe view (no secret ever leaves) ----

	def test_create_returns_safe_view_without_blob(self):
		view = _mk()
		self.assertTrue(view["capture_id"].startswith("oacap_"))
		self.assertEqual(view["account_ref"], "SUB_deadbeefcafe0001")
		self.assertEqual(view["label"], "user@example.com")
		# The blob and any token material must NOT appear in the returned view.
		flat = json.dumps(view)
		for secret in ("AT-secret", "RT-secret", "oauth_blob", "access", "refresh"):
			self.assertNotIn(secret, flat, f"{secret!r} leaked into the safe view")

	def test_blob_is_encrypted_at_rest_never_plaintext_in_column(self):
		view = _mk()
		name = frappe.db.get_value(DT, {"capture_id": view["capture_id"]}, "name")
		col = frappe.db.get_value(DT, name, "encrypted_oauth_blob")
		# The Password column holds only a mask, never the plaintext token.
		self.assertNotIn("AT-secret", col or "")
		self.assertNotIn("RT-secret", col or "")
		# But it decrypts back to the real blob for the consume path.
		from frappe.utils.password import get_decrypted_password

		blob = json.loads(get_decrypted_password(DT, name, "encrypted_oauth_blob"))
		self.assertEqual(blob["access"], "AT-secret-do-not-leak")

	# ---- consume-once (atomic claim) ----

	def test_consume_returns_blob_marks_consumed_erases_ciphertext(self):
		view = _mk()
		blob = json.loads(pc.consume_capture(view["capture_id"]))
		self.assertEqual(blob["refresh"], "RT-secret-do-not-leak")
		name = frappe.db.get_value(DT, {"capture_id": view["capture_id"]}, "name")
		row = frappe.db.get_value(
			DT, name, ["consumed_at", "revocation_state", "encrypted_oauth_blob"], as_dict=True
		)
		self.assertIsNotNone(row.consumed_at)
		self.assertEqual(row.revocation_state, "consumed")
		# Ciphertext erased: nothing decryptable remains.
		from frappe.utils.password import get_decrypted_password

		self.assertFalse(get_decrypted_password(DT, name, "encrypted_oauth_blob", raise_exception=False))

	def test_consume_twice_raises_already_consumed(self):
		view = _mk()
		pc.consume_capture(view["capture_id"])
		with self.assertRaises(pc.CaptureAlreadyConsumed):
			pc.consume_capture(view["capture_id"])

	def test_consume_unknown_or_foreign_is_opaque(self):
		with self.assertRaises(pc.CaptureError):
			pc.consume_capture("oacap_does_not_exist")
		# A capture owned by another user is indistinguishable from "unknown".
		view = _mk()
		name = frappe.db.get_value(DT, {"capture_id": view["capture_id"]}, "name")
		frappe.db.set_value(DT, name, "owner_user", "peer@example.com", update_modified=False)
		with self.assertRaises(pc.CaptureError) as cm:
			pc.consume_capture(view["capture_id"])
		# Not the already-consumed subclass - a foreign row reads as unknown.
		self.assertNotIsInstance(cm.exception, pc.CaptureAlreadyConsumed)

	def test_consume_expired_refuses(self):
		view = _mk()
		name = frappe.db.get_value(DT, {"capture_id": view["capture_id"]}, "name")
		frappe.db.set_value(
			DT, name, "expires_at", add_to_date(now_datetime(), minutes=-1), update_modified=False
		)
		with self.assertRaises(pc.CaptureError):
			pc.consume_capture(view["capture_id"])

	def test_consume_refuses_after_connection_identity_changes(self):
		# F10 / §10.2: a workspace reset / reconnect / tenant move between sign-in and
		# save changes the connection identity (agent_url) - a capture minted against
		# the old one must not be adopted afterwards. (rollback in tearDown restores
		# the settings write.)
		frappe.db.set_value(
			"Jarvis Settings", "Jarvis Settings", "agent_url", "https://old.example", update_modified=False
		)
		view = _mk()  # bound to https://old.example
		frappe.db.set_value(
			"Jarvis Settings", "Jarvis Settings", "agent_url", "https://new.example", update_modified=False
		)
		with self.assertRaises(pc.CaptureError) as cm:
			pc.consume_capture(view["capture_id"])
		self.assertNotIsInstance(cm.exception, pc.CaptureAlreadyConsumed)
		# The unadopted capture is still un-consumed (a later legitimate flow / the
		# sweeper handles it) - it was refused, not silently burned.
		name = frappe.db.get_value(DT, {"capture_id": view["capture_id"]}, "name")
		self.assertIsNone(frappe.db.get_value(DT, name, "consumed_at"))

	# ---- rehydrate (reload resume) ----

	def test_list_active_rehydrates_without_blob_and_hides_consumed(self):
		_mk(account_ref="SUB_live", provider_subject="subj-live")
		gone = _mk(account_ref="SUB_gone", provider_subject="subj-gone")
		pc.consume_capture(gone["capture_id"])
		refs = {c["account_ref"]: c for c in pc.list_active()}
		self.assertIn("SUB_live", refs)
		self.assertNotIn("SUB_gone", refs)  # consumed -> not resumable
		flat = json.dumps(list(refs.values()))
		for secret in ("AT-secret", "RT-secret", "oauth_blob"):
			self.assertNotIn(secret, flat)

	# ---- fold on stable subject (P1-07) ----

	def test_same_subject_folds_onto_one_row(self):
		a = _mk(account_ref="SUB_first")
		b = _mk(account_ref="SUB_second")  # same provider_subject "acct-stable-123"
		self.assertEqual(a["capture_id"], b["capture_id"], "recapture must refresh the same row")
		self.assertEqual(a["account_ref"], b["account_ref"], "the stable ref is kept on a fold")
		rows = frappe.get_all(
			DT, filters={"provider_subject_hash": pc._subject_hash("acct-stable-123")}, pluck="name"
		)
		self.assertEqual(len(rows), 1)

	def test_no_subject_never_folds(self):
		# Device-code (Kimi): no stable subject -> two distinct rows.
		a = _mk(account_ref="SUB_k1", provider_subject="", account_email="", safe_label="Kimi 0001")
		b = _mk(account_ref="SUB_k2", provider_subject="", account_email="", safe_label="Kimi 0002")
		self.assertNotEqual(a["capture_id"], b["capture_id"])

	def test_two_providers_sharing_a_subject_never_collide(self):
		# F4: a fold must be scoped to (provider, upstream, subject). Two DIFFERENT
		# providers that happen to share a subject value must NOT fold onto one row -
		# that clobbered one provider's live, unrevocable token.
		g_blob = {"type": "oauth", "provider": "google-gemini-cli", "access": "G-AT", "refresh": "G-RT"}
		x_blob = {"type": "oauth", "provider": "xai", "access": "X-AT", "refresh": "X-RT"}
		g = pc.create_capture(
			provider="Google Gemini",
			upstream="google",
			agent_provider="google-gemini-cli",
			oauth_blob=json.dumps(g_blob),
			account_email="u@example.com",
			account_ref="SUB_g",
			safe_label="u@example.com",
			provider_subject="collide",  # SAME subject value as the xai capture below
		)
		x = pc.create_capture(
			provider="xAI Grok",
			upstream="xai",
			agent_provider="xai",
			oauth_blob=json.dumps(x_blob),
			account_email="u@example.com",
			account_ref="SUB_x",
			safe_label="u@example.com",
			provider_subject="collide",
		)
		self.assertNotEqual(g["capture_id"], x["capture_id"], "different providers must not fold")
		from frappe.utils.password import get_decrypted_password

		gname = frappe.db.get_value(DT, {"capture_id": g["capture_id"]}, "name")
		# The Google token is intact - NOT overwritten by the xai capture.
		self.assertEqual(
			json.loads(get_decrypted_password(DT, gname, "encrypted_oauth_blob"))["access"], "G-AT"
		)

	def test_fold_is_owner_scoped(self):
		# The fold filter must include owner_user: a peer's capture with the same
		# provider/subject must never be folded into (or read by) this user.
		mine = _mk(provider_subject="owned", account_ref="SUB_mine")
		name = frappe.db.get_value(DT, {"capture_id": mine["capture_id"]}, "name")
		frappe.db.set_value(DT, name, "owner_user", "peer@example.com", update_modified=False)
		again = _mk(provider_subject="owned", account_ref="SUB_mine2")  # same subject, my user
		self.assertNotEqual(again["capture_id"], mine["capture_id"], "must not fold onto a peer's row")

	def test_consume_takes_a_row_lock(self):
		# The single most security-critical invariant of the store is consume-ONCE, which
		# rides a locking read. Pin it so a refactor that drops FOR UPDATE is caught even
		# though a single-connection test harness cannot stage a real two-connection race.
		view = _mk()
		seen = []
		orig = frappe.db.sql

		def _spy(query, *a, **k):
			seen.append(query if isinstance(query, str) else str(query))
			return orig(query, *a, **k)

		with patch.object(frappe.db, "sql", side_effect=_spy):
			pc.consume_capture(view["capture_id"])
		self.assertTrue(
			any("FOR UPDATE" in q.upper() for q in seen),
			"consume_capture must claim the row with a locking read (FOR UPDATE)",
		)

	def test_sweep_gives_up_and_erases_after_max_attempts(self):
		import requests as _rq

		# Google's revoke endpoint was the only real entry in _REVOKE_ENDPOINTS
		# and was removed with the Gemini chat subscription (2026-08-19); patch
		# in a synthetic one so this retry-then-give-up regression stays
		# covered independent of any specific provider.
		with patch.dict(pc._REVOKE_ENDPOINTS, {"test-oauth-provider": "https://revoke.example.com/revoke"}):
			view = _mk(agent_provider="test-oauth-provider", provider_subject="give-up", account_ref="SUB_gu")
			name = frappe.db.get_value(DT, {"capture_id": view["capture_id"]}, "name")
			frappe.db.set_value(
				DT, name, "expires_at", add_to_date(now_datetime(), minutes=-5), update_modified=False
			)
			frappe.db.commit()
			from frappe.utils.password import get_decrypted_password

			with patch(
				"jarvis.oauth.pending_capture.requests.post", side_effect=_rq.RequestException("down")
			):
				# One shy of the ceiling: still retryable, ciphertext KEPT for a later try.
				for _ in range(pc.REVOKE_MAX_ATTEMPTS - 1):
					pc.sweep_expired()
				self.assertTrue(
					get_decrypted_password(DT, name, "encrypted_oauth_blob", raise_exception=False),
					"ciphertext must survive until the retry ceiling",
				)
				# The ceiling sweep gives up: erase the ciphertext so a dead provider
				# cannot strand a live token here forever.
				pc.sweep_expired()
			row = frappe.db.get_value(DT, name, ["revocation_state", "revocation_attempts"], as_dict=True)
			self.assertEqual(row.revocation_state, "failed")
			self.assertEqual(int(row.revocation_attempts), pc.REVOKE_MAX_ATTEMPTS)
			self.assertFalse(
				get_decrypted_password(DT, name, "encrypted_oauth_blob", raise_exception=False),
				"ciphertext must be erased once revocation gives up",
			)

	# ---- expiry sweep: revoke + erase ----

	def test_sweep_revokes_and_erases_expired(self):
		view = _mk(provider_subject="subj-sweep", account_ref="SUB_sweep")
		name = frappe.db.get_value(DT, {"capture_id": view["capture_id"]}, "name")
		frappe.db.set_value(
			DT, name, "expires_at", add_to_date(now_datetime(), minutes=-5), update_modified=False
		)
		frappe.db.commit()
		# openai has no verifiable revoke endpoint -> unsupported; ciphertext erased.
		pc.sweep_expired()
		row = frappe.db.get_value(DT, name, ["revocation_state", "encrypted_oauth_blob"], as_dict=True)
		self.assertEqual(row.revocation_state, "unsupported")
		from frappe.utils.password import get_decrypted_password

		self.assertFalse(get_decrypted_password(DT, name, "encrypted_oauth_blob", raise_exception=False))

	def test_no_provider_has_a_revoke_endpoint_after_gemini_removal(self):
		# Google's oauth2.googleapis.com/revoke was the only real entry in
		# _REVOKE_ENDPOINTS; it was removed along with the Gemini chat
		# subscription (2026-08-19). openai/xai/kimi publish no revocation
		# endpoint this integration can verify, so the map is now empty and
		# every capture resolves to "unsupported" on sweep.
		self.assertEqual(pc._REVOKE_ENDPOINTS, {})
		self.assertNotIn("google-gemini-cli", pc._REVOKE_ENDPOINTS)

	def test_sweep_calls_revoke_endpoint_when_one_exists(self):
		# Route the capture through a provider WITH a revoke endpoint. Google's
		# was the only real one and was removed with the Gemini chat
		# subscription (2026-08-19); patch a synthetic one in so this
		# revoke-call regression stays covered independent of any specific
		# provider.
		with patch.dict(pc._REVOKE_ENDPOINTS, {"test-oauth-provider": "https://revoke.example.com/revoke"}):
			view = _mk(agent_provider="test-oauth-provider", provider_subject="subj-g", account_ref="SUB_g")
			name = frappe.db.get_value(DT, {"capture_id": view["capture_id"]}, "name")
			frappe.db.set_value(
				DT, name, "expires_at", add_to_date(now_datetime(), minutes=-5), update_modified=False
			)
			frappe.db.commit()

			class _Resp:
				ok = True
				status_code = 200

			with patch("jarvis.oauth.pending_capture.requests.post", return_value=_Resp()) as post:
				pc.sweep_expired()
			post.assert_called_once()
			# The token, not a leak, is what was posted; assert the endpoint + that a
			# token was sent (value is the real refresh token, by design).
			self.assertEqual(post.call_args.args[0], "https://revoke.example.com/revoke")
			self.assertEqual(frappe.db.get_value(DT, name, "revocation_state"), "revoked")

	def test_sweep_retries_transient_then_gives_up(self):
		import requests as _rq

		# Same synthetic-endpoint reasoning as the two tests above.
		with patch.dict(pc._REVOKE_ENDPOINTS, {"test-oauth-provider": "https://revoke.example.com/revoke"}):
			view = _mk(agent_provider="test-oauth-provider", provider_subject="subj-t", account_ref="SUB_t")
			name = frappe.db.get_value(DT, {"capture_id": view["capture_id"]}, "name")
			frappe.db.set_value(
				DT, name, "expires_at", add_to_date(now_datetime(), minutes=-5), update_modified=False
			)
			frappe.db.commit()
			with patch(
				"jarvis.oauth.pending_capture.requests.post",
				side_effect=_rq.RequestException("boom"),
			):
				# One sweep = one attempt; state stays failed and it is retryable next time.
				pc.sweep_expired()
			self.assertEqual(frappe.db.get_value(DT, name, "revocation_state"), "failed")
			self.assertEqual(int(frappe.db.get_value(DT, name, "revocation_attempts")), 1)
