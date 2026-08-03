"""Tests for jarvis.account wrappers and admin_client shims for the
/jarvis/billing SPA page. admin_client is mocked - these are unit tests of
the customer-side glue, not of the admin endpoints themselves."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import account, admin_client
from jarvis.exceptions import AdminValidationError

_SNAPSHOTTED_FIELDS = (
	"jarvis_admin_url",
	"jarvis_admin_api_key",
	"jarvis_admin_api_secret",
	"agent_url",
	"agent_token",
)


def _snapshot_settings() -> dict:
	s = frappe.get_single("Jarvis Settings")
	snap = {}
	for f in _SNAPSHOTTED_FIELDS:
		v = (
			s.get_password(f, raise_exception=False)
			if f.endswith(("_key", "_secret", "_token"))
			else s.get(f)
		)
		snap[f] = v or ""
	return snap


def _restore_settings(snap: dict) -> None:
	s = frappe.get_single("Jarvis Settings")
	for f, v in snap.items():
		s.db_set(f, v)
	frappe.db.commit()


class TestIsOnboarded(FrappeTestCase):
	def setUp(self):
		self._snap = _snapshot_settings()

	def tearDown(self):
		_restore_settings(self._snap)

	def test_true_when_admin_key_set(self):
		s = frappe.get_single("Jarvis Settings")
		s.db_set("jarvis_admin_api_key", "ak-abc")
		s.db_set("jarvis_admin_api_secret", "as-abc")
		frappe.db.commit()
		self.assertEqual(account.is_onboarded(), {"onboarded": True})

	def test_false_when_admin_key_blank(self):
		s = frappe.get_single("Jarvis Settings")
		s.db_set("jarvis_admin_api_key", "")
		s.db_set("jarvis_admin_api_secret", "")
		frappe.db.commit()
		self.assertEqual(account.is_onboarded(), {"onboarded": False})


class TestAccountWrappers(FrappeTestCase):
	def test_get_account_returns_admin_payload(self):
		fake = {
			"subscription_status": "Active",
			"plan": {"name": "p1"},
			"days_remaining": 12,
			"upgrade_plans": [],
		}
		with patch.object(admin_client, "get_account_summary", return_value=fake) as m:
			out = account.get_account()
		m.assert_called_once_with()
		self.assertEqual(out, fake)

	def test_preview_upgrade_passes_target_plan_through(self):
		fake = {"prorated_inr": 500, "diff_per_day": 50.0, "days_remaining": 10, "total_period_days": 30}
		with patch.object(admin_client, "preview_upgrade", return_value=fake) as m:
			out = account.preview_upgrade("plan-pro")
		m.assert_called_once_with("plan-pro")
		self.assertEqual(out, fake)

	def test_start_upgrade_passes_target_plan_through(self):
		fake = {
			"razorpay_order_id": "order_X",
			"razorpay_key_id": "k",
			"amount_inr": 500,
			"target_plan": "plan-pro",
		}
		with patch.object(admin_client, "start_upgrade", return_value=fake) as m:
			out = account.start_upgrade("plan-pro")
		m.assert_called_once_with("plan-pro", provider=None)
		self.assertEqual(out, fake)

	def test_get_account_surfaces_admin_validation_error_as_frappe_throw(self):
		"""_surface() converts AdminValidationError to frappe.throw so the
		page sees Frappe's standard red toast text instead of a traceback."""
		with patch.object(
			admin_client, "get_account_summary", side_effect=AdminValidationError("plan disabled")
		):
			with self.assertRaises(frappe.ValidationError) as cm:
				account.get_account()
		self.assertIn("plan disabled", str(cm.exception))

	def test_preview_upgrade_surfaces_validation_error(self):
		with patch.object(
			admin_client, "preview_upgrade", side_effect=AdminValidationError("downgrade not supported")
		):
			with self.assertRaises(frappe.ValidationError) as cm:
				account.preview_upgrade("plan-cheap")
		self.assertIn("downgrade not supported", str(cm.exception))


class TestAccountGatesFailClosed(FrappeTestCase):
	"""get_account and preview_upgrade are System-Manager-only.

	The rejection itself is covered by the canonical parametrized sweep in
	test_role_gates.py (both endpoints are entries in GATED_ENDPOINTS, which
	asserts Guest is refused and Administrator is not). This class adds the one
	property that sweep cannot express: the gate must run BEFORE _surface()
	reaches admin_client, so an unauthorised caller can neither leak the
	payload into admin's logs nor burn an admin request per attempt.
	"""

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_rejection_happens_before_any_admin_round_trip(self):
		frappe.set_user("Guest")
		with (
			patch.object(admin_client, "get_account_summary") as get_summary,
			patch.object(admin_client, "preview_upgrade") as prev,
		):
			with self.assertRaises(frappe.PermissionError):
				account.get_account()
			with self.assertRaises(frappe.PermissionError):
				account.preview_upgrade("plan-pro")
		get_summary.assert_not_called()
		prev.assert_not_called()


def _set_ready_marker(value) -> None:
	"""Put this site in the ESTABLISHED cohort (a timestamp) or the never-ready
	one (None). Written raw + update_modified=False, exactly as the gate writes
	it, so the gate's own revision is unaffected."""
	frappe.db.set_value(
		"Jarvis Settings", "Jarvis Settings", "chat_was_ready_at", value, update_modified=False
	)


def _set_stored_connection(present: bool):
	"""The other half of "established": _has_been_chat_ready reads the RAW
	jarvis_admin_api_key column (a mask, never the secret - see
	jarvis/_password_utils.py), so a test that wants a cohort must say which,
	rather than inherit whatever this site's onboarding state happens to be.

	Returns the previous raw value so the caller can put it back."""
	prior = account._settings_raw(("jarvis_admin_api_key",)).get("jarvis_admin_api_key")
	frappe.db.set_value(
		"Jarvis Settings",
		"Jarvis Settings",
		"jarvis_admin_api_key",
		"*" * 10 if present else "",
		update_modified=False,
	)
	return prior


class TestAdminChatGate(FrappeTestCase):
	"""jarvis.account._admin_chat_gate — the final managed ready-gate for
	is_ready_for_chat. v1-tolerant; positive verdict cached against the config
	revision. admin_client.get_connection is mocked.

	Every test here pins the ESTABLISHED cohort (chat_was_ready_at set) unless it
	is specifically about the never-ready one, so a verdict never depends on
	whatever state the site happens to carry."""

	# The gate now mirrors the release notice onto Jarvis Settings as a side
	# effect (persist({}) when the mock carries none), and stamps the ready
	# marker, so snapshot/restore those fields to avoid clobbering a real site's
	# operator state.
	_RELEASE_FIELDS = (
		"release_notice_active",
		"latest_jarvis_version",
		"release_notice_message",
	)

	def setUp(self):
		account._bust_chat_gate()
		s = frappe.get_single("Jarvis Settings")
		self._rn_snap = {f: s.get(f) for f in self._RELEASE_FIELDS}
		self._marker_snap = account._settings_raw(("chat_was_ready_at",)).get("chat_was_ready_at")
		self._key_snap = _set_stored_connection(True)
		_set_ready_marker("2026-01-01 00:00:00")

	def tearDown(self):
		account._bust_chat_gate()
		_set_ready_marker(self._marker_snap)
		frappe.db.set_value(
			"Jarvis Settings",
			"Jarvis Settings",
			"jarvis_admin_api_key",
			self._key_snap,
			update_modified=False,
		)
		s = frappe.get_single("Jarvis Settings")
		for f, v in self._rn_snap.items():
			s.db_set(f, v)
		frappe.db.commit()

	def _cached_verdict(self):
		"""The live revision's cached entry, or None."""
		raw = account._settings_raw(account._GATE_STATE_FIELDS)
		return frappe.cache().get_value(f"{account._CHAT_GATE_CACHE_KEY}:{account._gate_revision(raw)}")

	def test_release_notice_persisted_on_gate(self):
		# The gate mirrors an active notice so boot can read it; the returned
		# verdict shape is unchanged (release_notice never rides the gate reply).
		notice = {"active": True, "version": "9.9.9", "message": "Please update"}
		with patch.object(
			admin_client,
			"get_connection",
			return_value={"chat_readiness": "Ready", "release_notice": notice},
		):
			out = account._admin_chat_gate()
		self.assertEqual(out, {"ready": True, "reason": None, "billing_notice": {}})
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.release_notice_active, 1)
		self.assertEqual(s.latest_jarvis_version, "9.9.9")
		self.assertEqual(s.release_notice_message, "Please update")

	def test_release_notice_cleared_on_gate(self):
		"""The transition that unblocks an updated tenant: admin stops sending a
		notice, so the mirror must zero rather than keep the stale block up."""
		s = frappe.get_single("Jarvis Settings")
		s.db_set("release_notice_active", 1)
		s.db_set("latest_jarvis_version", "9.9.9")
		s.db_set("release_notice_message", "stale")
		frappe.db.commit()
		with patch.object(admin_client, "get_connection", return_value={"chat_readiness": "Ready"}):
			account._admin_chat_gate()
		s = frappe.get_single("Jarvis Settings")
		self.assertEqual(s.release_notice_active, 0)
		self.assertEqual(s.release_notice_message, "")

	def test_blocks_when_admin_not_ready(self):
		with patch.object(
			admin_client, "get_connection", return_value={"chat_readiness": "Provisioning"}
		) as gc:
			self.assertEqual(
				account._admin_chat_gate(),
				{"ready": False, "reason": "container_provisioning", "detail": "", "billing_notice": {}},
			)
		# Uses the short 8s budget so a slow admin can't stall the SPA/boot path.
		gc.assert_called_once_with(timeout_s=8)

	def test_suspended_is_distinct_from_provisioning(self):
		"""A revoked subscription must NOT read as "still starting up": that
		tells the customer to wait for a container that is never coming back
		instead of renewing."""
		with patch.object(
			admin_client,
			"get_connection",
			return_value={
				"chat_readiness": "Suspended",
				"chat_readiness_reason": "Your subscription has expired.",
			},
		):
			self.assertEqual(
				account._admin_chat_gate(),
				{
					"ready": False,
					"reason": "subscription_suspended",
					"detail": "Your subscription has expired.",
					"billing_notice": {},
				},
			)

	def test_suspended_without_reason_still_classifies(self):
		"""Older admin sends the state with no sentence — the code must still be
		the billing one; the SPA supplies its own fallback copy."""
		with patch.object(admin_client, "get_connection", return_value={"chat_readiness": "Suspended"}):
			self.assertEqual(
				account._admin_chat_gate(),
				{"ready": False, "reason": "subscription_suspended", "detail": "", "billing_notice": {}},
			)

	def test_allows_when_admin_ready(self):
		with patch.object(admin_client, "get_connection", return_value={"chat_readiness": "Ready"}):
			self.assertEqual(
				account._admin_chat_gate(), {"ready": True, "reason": None, "billing_notice": {}}
			)

	def test_v1_tolerant_when_key_absent(self):
		# v1 admin (or a v2 not surfacing chat_readiness) → no opinion → allow.
		with patch.object(
			admin_client, "get_connection", return_value={"agent_url": "ws://x", "tenant_status": "running"}
		):
			self.assertEqual(
				account._admin_chat_gate(), {"ready": True, "reason": None, "billing_notice": {}}
			)

	def test_established_workspace_fails_open_on_admin_error(self):
		"""An outage of the control plane is not an outage of the container. A
		workspace admin has already confirmed Ready keeps its chat."""
		from jarvis.exceptions import AdminUnreachableError

		with patch.object(
			admin_client, "get_connection", side_effect=AdminUnreachableError("admin is unreachable")
		):
			self.assertEqual(
				account._admin_chat_gate(), {"ready": True, "reason": None, "billing_notice": {}}
			)
		# Fail-open verdict must NOT be cached: a recovered admin is re-probed on
		# the next call rather than this shrug standing in for a verdict.
		self.assertIsNone(self._cached_verdict())

	def test_never_ready_workspace_fails_closed_on_admin_error(self):
		"""The inversion of the old blanket fail-open. Nothing has ever confirmed a
		container is serving this workspace, so "probably fine" is a guess - and
		acting on it is what drops a half-onboarded customer into a chat that
		cannot answer. Retryable: it is the absence of a verdict, not one."""
		from jarvis.exceptions import AdminUnreachableError

		_set_ready_marker(None)
		with patch.object(
			admin_client, "get_connection", side_effect=AdminUnreachableError("admin is unreachable")
		):
			out = account._admin_chat_gate()
		self.assertFalse(out["ready"])
		self.assertEqual(out["reason"], "readiness_unconfirmed")
		self.assertTrue(out["retryable"])
		self.assertTrue(out["detail"], "the customer must be told something they can act on")

	def test_the_unconfirmed_verdict_is_briefly_cached(self):
		"""The wizard polls this every 2.5s. Re-asking a control plane that is
		already failing, once per beat per open tab, is how a bench turns an outage
		into a second one - so the UNCONFIRMED code (and only it) gets a few
		seconds. The verdict served from that entry is identical."""
		from jarvis.exceptions import AdminUnreachableError

		_set_ready_marker(None)
		with patch.object(
			admin_client, "get_connection", side_effect=AdminUnreachableError("admin is unreachable")
		) as gc:
			first = account._admin_chat_gate()
			second = account._admin_chat_gate()
		gc.assert_called_once()
		self.assertEqual(first, second)
		self.assertLessEqual(account._UNCONFIRMED_CACHE_TTL_S, 10, "'retryable' has to stay true")

	def test_the_cached_unconfirmed_entry_re_derives_the_cohort(self):
		"""Only the FACT of the outage is cached, never the cohort decision: a
		workspace that becomes established inside the window is answered as one."""
		from jarvis.exceptions import AdminUnreachableError

		_set_ready_marker(None)
		with patch.object(
			admin_client, "get_connection", side_effect=AdminUnreachableError("admin is unreachable")
		):
			self.assertFalse(account._admin_chat_gate()["ready"])
			_set_ready_marker("2026-01-01 00:00:00")
			self.assertTrue(account._admin_chat_gate()["ready"])

	def test_a_rendered_not_ready_verdict_is_still_never_cached(self):
		"""The short cache is for "nobody answered", not for an answer. A container
		that finishes provisioning must be seen on the next call, not in 5s."""
		with patch.object(
			admin_client, "get_connection", return_value={"chat_readiness": "Provisioning"}
		) as gc:
			account._admin_chat_gate()
			account._admin_chat_gate()
		self.assertEqual(gc.call_count, 2)
		self.assertIsNone(self._cached_verdict())

	def test_pending_payment_403_is_not_a_blind_ready(self):
		"""jarvis_admin_v2.api._auth.current_customer answers a Pending Payment
		customer with 403, so this gate NEVER hears "not ready" for them - it hears
		an exception. The old code shrugged that off as ready."""
		from jarvis.exceptions import AdminAuthError

		_set_ready_marker(None)
		with patch.object(
			admin_client, "get_connection", side_effect=AdminAuthError("admin returned 403", status_code=403)
		):
			out = account._admin_chat_gate()
		self.assertFalse(out["ready"])
		self.assertEqual(out["reason"], "readiness_unconfirmed")

	def test_ready_stamps_the_established_marker(self):
		_set_ready_marker(None)
		with patch.object(admin_client, "get_connection", return_value={"chat_readiness": "Ready"}):
			account._admin_chat_gate()
		self.assertTrue(account._settings_raw(("chat_was_ready_at",)).get("chat_was_ready_at"))

	def test_a_fresh_marker_is_not_rewritten_on_every_pass(self):
		"""The gate runs on every uncached page load; the marker is only ever read
		as "is it set", so re-stamping it would be a DB write per load for nothing."""
		fresh = frappe.utils.now()
		_set_ready_marker(fresh)
		with patch.object(admin_client, "get_connection", return_value={"chat_readiness": "Ready"}):
			account._admin_chat_gate()
		self.assertEqual(account._settings_raw(("chat_was_ready_at",)).get("chat_was_ready_at"), fresh)

	def test_a_blocking_verdict_still_wins_over_the_cohort(self):
		"""Cohort only decides what to do when admin cannot be ASKED. An answer of
		"Suspended" is an answer, and an established workspace gets it."""
		with patch.object(admin_client, "get_connection", return_value={"chat_readiness": "Suspended"}):
			self.assertEqual(account._admin_chat_gate()["reason"], "subscription_suspended")

	def test_billing_notice_is_passed_through(self):
		# The expiry banner rides this verdict; admin owns the wording, the gate
		# only forwards it - on both the ready and the suspended paths.
		notice = {"phase": "expiring", "admin_message": "ends soon", "member_message": "ask admin"}
		with patch.object(
			admin_client,
			"get_connection",
			return_value={"chat_readiness": "Ready", "billing_notice": notice},
		):
			self.assertEqual(account._admin_chat_gate()["billing_notice"], notice)
		account._bust_chat_gate()
		with patch.object(
			admin_client,
			"get_connection",
			return_value={"chat_readiness": "Suspended", "billing_notice": notice},
		):
			self.assertEqual(account._admin_chat_gate()["billing_notice"], notice)

	def test_positive_verdict_is_cached(self):
		with patch.object(admin_client, "get_connection", return_value={"chat_readiness": "Ready"}) as gc:
			account._admin_chat_gate()
			account._admin_chat_gate()
		# Second call served from the positive cache → one admin round-trip.
		gc.assert_called_once()

	def test_not_ready_verdict_is_not_cached(self):
		# A transient block must clear on the next call, not stick for the TTL.
		with patch.object(admin_client, "get_connection", return_value={"chat_readiness": "Provisioning"}):
			account._admin_chat_gate()
		self.assertIsNone(self._cached_verdict())

	def test_a_config_change_drops_the_cached_verdict(self):
		"""The C05-1 stale window: a save changed what the container is being asked
		to run, so the verdict admin gave about the PREVIOUS config is finished -
		it must not be served for the rest of the TTL.

		The new status is unique per run rather than a literal: this site may
		already be sitting on any given status string, and an unchanged value is
		correctly NOT a new revision."""
		status_snap = account._settings_raw(("last_sync_status",)).get("last_sync_status")
		try:
			with patch.object(admin_client, "get_connection", return_value={"chat_readiness": "Ready"}) as gc:
				account._admin_chat_gate()
				frappe.db.set_value(
					"Jarvis Settings",
					"Jarvis Settings",
					"last_sync_status",
					f"pending: admin applying config ({frappe.generate_hash(length=8)})",
					update_modified=False,
				)
				account._admin_chat_gate()
			self.assertEqual(gc.call_count, 2, "a config change must force a fresh admin verdict")
		finally:
			frappe.db.set_value(
				"Jarvis Settings", "Jarvis Settings", "last_sync_status", status_snap, update_modified=False
			)

	def test_an_unchanged_config_keeps_serving_the_cached_verdict(self):
		"""The other half: rewriting the same values is not a new configuration, and
		must not cost an admin round-trip per page load."""
		with patch.object(admin_client, "get_connection", return_value={"chat_readiness": "Ready"}) as gc:
			account._admin_chat_gate()
			raw = account._settings_raw(account._GATE_STATE_FIELDS)
			frappe.db.set_value(
				"Jarvis Settings",
				"Jarvis Settings",
				"last_sync_status",
				raw.get("last_sync_status"),
				update_modified=False,
			)
			account._admin_chat_gate()
		gc.assert_called_once()

	def test_every_revision_is_dropped_by_a_bust(self):
		"""_bust_chat_gate is called after a save has ALREADY moved the revision, so
		deleting only the revision it can compute would miss the live entry."""
		with patch.object(admin_client, "get_connection", return_value={"chat_readiness": "Ready"}) as gc:
			account._admin_chat_gate()
			account._bust_chat_gate()
			account._admin_chat_gate()
		self.assertEqual(gc.call_count, 2)

	def test_the_revision_reads_an_empty_datetime_marker_as_unset(self):
		"""Regression pin for the read itself. frappe.db.get_value /
		get_single_value cast an empty Datetime single to datetime(1, 1, 1) —
		TRUTHY — which would have put every workspace that has merely SAVED its
		settings into the established cohort and quietly restored the fail-open the
		gate exists to remove."""
		frappe.db.sql(
			"""delete from `tabSingles` where doctype=%s and `field`=%s""",
			("Jarvis Settings", "chat_was_ready_at"),
		)
		frappe.db.sql(
			"""insert into `tabSingles` (doctype, `field`, `value`) values (%s,%s,%s)""",
			("Jarvis Settings", "chat_was_ready_at", None),
		)
		raw = account._settings_raw(account._GATE_STATE_FIELDS)
		self.assertIsNone(raw.get("chat_was_ready_at"))
		self.assertFalse(account._has_been_chat_ready(raw))
		self.assertTrue(
			frappe.db.get_value("Jarvis Settings", "Jarvis Settings", ["chat_was_ready_at"], as_dict=True)[
				"chat_was_ready_at"
			],
			"if this ever reads falsy the cast changed and the raw read can be simplified",
		)

	def test_an_established_workspace_needs_its_admin_credentials_too(self):
		"""The marker on its own would protect a workspace whose connection was torn
		down - the one workspace whose chat provably cannot work."""
		self.assertFalse(account._has_been_chat_ready({"chat_was_ready_at": "2026-01-01 00:00:00"}))
		self.assertFalse(account._has_been_chat_ready({"jarvis_admin_api_key": "**********"}))
		self.assertTrue(
			account._has_been_chat_ready(
				{"chat_was_ready_at": "2026-01-01 00:00:00", "jarvis_admin_api_key": "**********"}
			)
		)


class TestLlmMissingVerdict(FrappeTestCase):
	"""_llm_missing_verdict — the wizard-vs-banner split when LLM creds are
	absent. Only a never-synced workspace whose subscription never went Active
	hard-gates back to the wizard; everything else (and every failure mode)
	stays on the soft banner."""

	class _S:
		llm_direct_synced_at = None
		llm_pool_synced_at = None
		llm_oauth_connected_at = None

	def _verdict(self, settings, conn=None, raises=None):
		kw = {"side_effect": raises} if raises else {"return_value": conn or {}}
		with patch.object(admin_client, "get_connection", **kw) as gc:
			out = account._llm_missing_verdict(settings)
		return out, gc

	def test_ever_synced_stays_soft_without_admin_call(self):
		s = self._S()
		s.llm_pool_synced_at = "2026-01-01 00:00:00"
		out, gc = self._verdict(s)
		self.assertEqual(out["reason"], "llm_credentials")
		gc.assert_not_called()

	def test_never_synced_pending_payment_hard_gates(self):
		out, _ = self._verdict(self._S(), conn={"subscription_status": "Pending Payment"})
		self.assertEqual(out["reason"], "llm_setup")

	def test_never_synced_active_sub_stays_soft(self):
		# The workspace-reset revoke option clears every marker on an Active
		# customer — they reconnect via Settings, never the wizard.
		out, _ = self._verdict(self._S(), conn={"subscription_status": "Active"})
		self.assertEqual(out["reason"], "llm_credentials")

	def test_suspended_sub_stays_soft(self):
		# Suspended is an ESTABLISHED account — the renew banner owns it; the
		# wizard would dead-end it at signup's duplicate guard.
		out, _ = self._verdict(self._S(), conn={"subscription_status": "Suspended"})
		self.assertEqual(out["reason"], "llm_credentials")

	def test_admin_unreachable_fails_open_to_soft(self):
		out, _ = self._verdict(self._S(), raises=RuntimeError("admin down"))
		self.assertEqual(out["reason"], "llm_credentials")

	def test_unknown_subscription_status_fails_open_to_soft(self):
		out, _ = self._verdict(self._S(), conn={})
		self.assertEqual(out["reason"], "llm_credentials")

	def _auth_error(self, message, status_code=403):
		from jarvis.exceptions import AdminAuthError

		return AdminAuthError(message, status_code=status_code)

	def test_a_403_naming_a_never_paid_state_hard_gates(self):
		"""The dead-code fix. get_connection 403s an unpaid customer at
		jarvis_admin_v2.api._auth.current_customer (allow_pending False) instead of
		answering with a subscription_status, so the Pending Payment customer this
		hard gate was written for never reached it - they took the generic "admin
		unknown" path and landed in the chat app.

		Reachable only past never_synced, so no established workspace can hit it."""
		for message in (
			"customer status: Pending Payment",
			"customer status: Pending Verification",
			"not a Jarvis Customer",
		):
			with self.subTest(message=message):
				out, _ = self._verdict(self._S(), raises=self._auth_error(message))
				self.assertEqual(out["reason"], "llm_setup")

	def test_a_cancelled_customer_stays_soft(self):
		"""Cancelled is an ENDED account, not a half-finished signup: the renew
		banner owns it, and the wizard would dead-end it at signup's duplicate
		guard. Hard-gating would also take /billing - the one page that can fix
		it - away from them."""
		out, _ = self._verdict(self._S(), raises=self._auth_error("customer status: Cancelled"))
		self.assertEqual(out["reason"], "llm_credentials")

	def test_an_anonymous_403_stays_soft(self):
		"""A 403 with no body of admin's - a proxy, a WAF, a hardened control plane
		that ships exc_type without the sentence - is not evidence of anything about
		this customer. Guessing "never paid" from it would lock an Active customer
		out of chat on an infrastructure hiccup."""
		for message in ("admin returned 403", "PermissionError", "", "<html>403 Forbidden</html>"):
			with self.subTest(message=message):
				out, _ = self._verdict(self._S(), raises=self._auth_error(message))
				self.assertEqual(out["reason"], "llm_credentials")

	def test_a_401_is_still_treated_as_unknown(self):
		"""401 is a stale/rotated token - a bench problem, not a statement about the
		customer's subscription. It must not hard-gate anyone to the wizard, even
		carrying words that would qualify on a 403."""
		out, _ = self._verdict(
			self._S(), raises=self._auth_error("customer status: Pending Payment", status_code=401)
		)
		self.assertEqual(out["reason"], "llm_credentials")

	def test_an_established_workspace_never_reaches_the_403_branch(self):
		s = self._S()
		s.llm_direct_synced_at = "2026-01-01 00:00:00"
		out, gc = self._verdict(s, raises=AdminValidationError("should not be called"))
		self.assertEqual(out["reason"], "llm_credentials")
		gc.assert_not_called()


class TestReplacedSiteIsExplained(FrappeTestCase):
	"""A site whose account was reconnected elsewhere can no longer authenticate.
	Failing open sends it into a chat that cannot work; it asks the one question it
	can still ask instead."""

	def setUp(self):
		frappe.cache().delete_value(account._REPLACED_CACHE_KEY)
		account._bust_chat_gate()
		# The two "still fails open" cases below are about the ESTABLISHED cohort -
		# pin it rather than inherit whatever this site happens to be.
		self._marker_snap = account._settings_raw(("chat_was_ready_at",)).get("chat_was_ready_at")
		self._key_snap = _set_stored_connection(True)
		_set_ready_marker("2026-01-01 00:00:00")

	def tearDown(self):
		frappe.cache().delete_value(account._REPLACED_CACHE_KEY)
		account._bust_chat_gate()
		_set_ready_marker(self._marker_snap)
		frappe.db.set_value(
			"Jarvis Settings",
			"Jarvis Settings",
			"jarvis_admin_api_key",
			self._key_snap,
			update_modified=False,
		)

	def test_an_auth_failure_on_a_replaced_site_explains_itself(self):
		with (
			patch.object(account.admin_client, "get_connection", side_effect=Exception("401")),
			patch.object(
				account.admin_client,
				"site_replacement",
				return_value={"replaced": True, "at": "2026-07-30", "moved_to": "https://other.example.com"},
			),
		):
			out = account._admin_chat_gate()
		self.assertFalse(out["ready"])
		self.assertEqual(out["reason"], "site_replaced")
		self.assertEqual(out["replaced_notice"]["moved_to"], "https://other.example.com")

	def test_an_ordinary_admin_blip_still_fails_open(self):
		with (
			patch.object(account.admin_client, "get_connection", side_effect=Exception("timeout")),
			patch.object(account.admin_client, "site_replacement", return_value={"replaced": False}),
		):
			out = account._admin_chat_gate()
		self.assertTrue(out["ready"], "an outage must not lock a working site out of chat")

	def test_the_verdict_is_cached_so_the_guest_endpoint_is_not_hammered(self):
		with (
			patch.object(account.admin_client, "get_connection", side_effect=Exception("401")),
			patch.object(account.admin_client, "site_replacement", return_value={"replaced": True}) as probe,
		):
			account._admin_chat_gate()
			account._bust_chat_gate()
			account._admin_chat_gate()
		self.assertEqual(probe.call_count, 1)

	def test_a_failing_probe_is_treated_as_not_replaced(self):
		with (
			patch.object(account.admin_client, "get_connection", side_effect=Exception("401")),
			patch.object(account.admin_client, "site_replacement", side_effect=Exception("unreachable")),
		):
			out = account._admin_chat_gate()
		self.assertTrue(out["ready"], "cannot prove a replacement, so do not invent one")

	def test_a_replacement_probe_still_wins_on_a_never_ready_site(self):
		"""site_replaced names a specific, actionable state. The onboarding-stage
		fallback must not swallow it - it is checked first for both cohorts."""
		_set_ready_marker(None)
		with (
			patch.object(account.admin_client, "get_connection", side_effect=Exception("401")),
			patch.object(
				account.admin_client, "site_replacement", return_value={"replaced": True, "moved_to": "x"}
			),
		):
			out = account._admin_chat_gate()
		self.assertEqual(out["reason"], "site_replaced")


class TestResetEndsTheEstablishedClaim(FrappeTestCase):
	"""chat_was_ready_at is a claim about the TENANCY that earned it. A reset ends
	that tenancy, so the marker has to go with it - otherwise the site whose
	container is most certainly not serving yet is the one whose chat is held open
	through an outage."""

	def setUp(self):
		account._bust_chat_gate()
		self._marker_snap = account._settings_raw(("chat_was_ready_at",)).get("chat_was_ready_at")
		self._key_snap = _set_stored_connection(True)

	def tearDown(self):
		_set_ready_marker(self._marker_snap)
		frappe.db.set_value(
			"Jarvis Settings",
			"Jarvis Settings",
			"jarvis_admin_api_key",
			self._key_snap,
			update_modified=False,
		)
		account._bust_chat_gate()

	def test_the_marker_is_in_the_connection_reset_spec(self):
		"""That settings_reset.apply actually NULLs every field in ``null`` is
		covered generically by test_dev's reset harness, which plants a value in
		each and asserts it comes back NULL. Running a real CONNECTION reset from
		here instead would tear this site's credentials down for whatever runs
		next - the reset is deliberately not transactional about __Auth."""
		from jarvis import settings_reset

		self.assertIn("chat_was_ready_at", settings_reset.CONNECTION.null)
		self.assertIn("chat_was_ready_at", settings_reset.FULL.null)

	def test_a_reconnected_site_fails_closed_until_it_earns_a_new_ready(self):
		"""Why the marker has to be in that spec: the reconnect writes a fresh admin
		key within seconds, so the "stored connection" half of _has_been_chat_ready
		is satisfied again immediately. Only clearing the marker keeps the NEW
		tenancy - whose container is the one thing definitely not serving yet - in
		the onboarding-stage cohort."""
		_set_ready_marker(None)  # what the reset leaves behind
		_set_stored_connection(True)  # what the reconnect writes back
		raw = account._settings_raw(account._GATE_STATE_FIELDS)
		self.assertFalse(account._has_been_chat_ready(raw))
		with patch.object(admin_client, "get_connection", side_effect=Exception("admin 500")):
			out = account._admin_chat_gate()
		self.assertEqual(out["reason"], "readiness_unconfirmed")


class TestIsReadyForChatCohorts(FrappeTestCase):
	"""is_ready_for_chat end to end (its managed ready-exit), not just the gate:
	the reason the customer's browser actually receives when the control plane
	cannot be asked.

	The workspace is pinned onto the subscription/oauth leg (connected marker set,
	pool mode off) so the run reaches _admin_chat_gate deterministically instead of
	depending on whatever this site's LLM config happens to be. is_ready_for_chat
	reads the admin key through get_password, so this class provisions a real
	encrypted one for the duration rather than the raw mask the gate-level tests
	use."""

	_FIELDS = ("llm_auth_mode", "llm_oauth_connected_at", "chat_was_ready_at")

	def setUp(self):
		from jarvis._password_utils import set_settings_password

		account._bust_chat_gate()
		self._snap = account._settings_raw(self._FIELDS)
		self._key_snap = account._settings_raw(("jarvis_admin_api_key",)).get("jarvis_admin_api_key")
		set_settings_password(
			frappe.get_single("Jarvis Settings"), "jarvis_admin_api_key", "test-only-readiness-key"
		)
		self._write(
			{
				"llm_auth_mode": "subscription",
				"llm_oauth_connected_at": "2026-01-01 00:00:00",
				"chat_was_ready_at": None,
			}
		)
		self._pool_off = patch.object(account, "compute_pool_mode", return_value=False)
		self._pool_off.start()

	def tearDown(self):
		from jarvis._password_utils import clear_settings_password

		self._pool_off.stop()
		clear_settings_password(frappe.get_single("Jarvis Settings"), "jarvis_admin_api_key")
		self._write({f: self._snap.get(f) for f in self._FIELDS})
		frappe.db.set_value(
			"Jarvis Settings",
			"Jarvis Settings",
			"jarvis_admin_api_key",
			self._key_snap,
			update_modified=False,
		)
		account._bust_chat_gate()

	def _write(self, values: dict) -> None:
		for f, v in values.items():
			frappe.db.set_value("Jarvis Settings", "Jarvis Settings", f, v, update_modified=False)

	def test_a_never_ready_workspace_is_not_told_it_is_ready(self):
		with patch.object(admin_client, "get_connection", side_effect=Exception("admin 500")):
			out = account.is_ready_for_chat()
		self.assertFalse(out["ready"], "plan 05's whole premise: this cannot be labelled ready")
		self.assertEqual(out["reason"], "readiness_unconfirmed")

	def test_an_established_workspace_is_unaffected(self):
		self._write({"chat_was_ready_at": "2026-01-01 00:00:00"})
		with patch.object(admin_client, "get_connection", side_effect=Exception("admin 500")):
			out = account.is_ready_for_chat()
		self.assertTrue(out["ready"])

	def test_the_happy_path_is_unchanged(self):
		with patch.object(admin_client, "get_connection", return_value={"chat_readiness": "Ready"}):
			out = account.is_ready_for_chat()
		self.assertEqual(out, {"ready": True, "reason": None, "billing_notice": {}})


class TestExplicitReadyOnlyMarker(FrappeTestCase):
	"""Review P0-07: only an EXPLICIT admin `Ready` may mint the established marker.
	A reachable response that never mentions chat_readiness (v1 tolerance) still
	ALLOWS this page load, but must not promote the workspace into the cohort whose
	chat survives an outage."""

	def setUp(self):
		account._bust_chat_gate()
		self._key_snap = _set_stored_connection(True)
		self._marker_snap = account._settings_raw((account._READY_MARKER_FIELD,)).get(
			account._READY_MARKER_FIELD
		)
		_set_ready_marker(None)

	def tearDown(self):
		account._bust_chat_gate()
		_set_ready_marker(self._marker_snap)
		frappe.db.set_value(
			"Jarvis Settings",
			"Jarvis Settings",
			"jarvis_admin_api_key",
			self._key_snap,
			update_modified=False,
		)
		frappe.db.commit()

	def test_missing_chat_readiness_allows_but_does_not_mint_the_marker(self):
		with patch.object(
			admin_client, "get_connection", return_value={"agent_url": "ws://x", "tenant_status": "running"}
		):
			out = account._admin_chat_gate()
		self.assertTrue(out["ready"], "v1-tolerance still allows the page load")
		self.assertFalse(
			account._settings_raw((account._READY_MARKER_FIELD,)).get(account._READY_MARKER_FIELD),
			"a response that never said Ready must not earn the established marker (P0-07)",
		)

	def test_explicit_ready_mints_the_marker(self):
		with patch.object(admin_client, "get_connection", return_value={"chat_readiness": "Ready"}):
			account._admin_chat_gate()
		self.assertTrue(
			account._settings_raw((account._READY_MARKER_FIELD,)).get(account._READY_MARKER_FIELD),
			"an explicit Ready earns the marker",
		)


class TestAuthorityAnchorFence(FrappeTestCase):
	"""Review P0-06 / §8.6: the established claim is bound to (principal, container,
	generation). It survives an outage only while that authority is still current;
	when it moves, the claim ends mechanically - no writer has to remember to clear
	the marker."""

	_FIELDS = (
		account._READY_MARKER_FIELD,
		account._READY_ANCHOR_FIELD,
		"agent_url",
		"jarvis_admin_api_key",
	)

	def setUp(self):
		account._bust_chat_gate()
		self._snap = account._settings_raw(self._FIELDS)

	def tearDown(self):
		account._bust_chat_gate()
		for f in self._FIELDS:
			frappe.db.set_value(
				"Jarvis Settings", "Jarvis Settings", f, self._snap.get(f), update_modified=False
			)
		frappe.db.commit()

	def _write(self, **vals):
		for f, v in vals.items():
			frappe.db.set_value("Jarvis Settings", "Jarvis Settings", f, v, update_modified=False)
		frappe.db.commit()

	def test_a_matching_anchor_keeps_the_workspace_established(self):
		self._write(agent_url="ws://container-1", jarvis_admin_api_key="**********")
		raw = account._settings_raw(self._FIELDS)
		anchor = account._authority_anchor(raw)
		self._write(chat_was_ready_at="2026-01-01 00:00:00", chat_ready_authority=anchor)
		raw = account._settings_raw(account._GATE_STATE_FIELDS)
		self.assertTrue(account._has_been_chat_ready(raw), "an unchanged authority keeps the claim")

	def test_a_moved_container_ends_the_claim(self):
		self._write(agent_url="ws://container-1", jarvis_admin_api_key="**********")
		anchor = account._authority_anchor(account._settings_raw(self._FIELDS))
		self._write(chat_was_ready_at="2026-01-01 00:00:00", chat_ready_authority=anchor)
		# The container is replaced: same marker + admin key, different agent_url.
		self._write(agent_url="ws://container-2")
		raw = account._settings_raw(account._GATE_STATE_FIELDS)
		self.assertFalse(
			account._has_been_chat_ready(raw),
			"a marker bound to the old container must not carry to a new one",
		)

	def test_a_changed_principal_ends_the_claim(self):
		self._write(agent_url="ws://container-1", jarvis_admin_api_key="key-A-mask")
		anchor = account._authority_anchor(account._settings_raw(self._FIELDS))
		self._write(chat_was_ready_at="2026-01-01 00:00:00", chat_ready_authority=anchor)
		self._write(jarvis_admin_api_key="key-B-mask")  # reconnected to another account
		raw = account._settings_raw(account._GATE_STATE_FIELDS)
		self.assertFalse(account._has_been_chat_ready(raw))

	def test_a_legacy_marker_with_no_anchor_honours_the_presence_rule(self):
		# A pre-fence / backfilled claim carries no anchor; it must not be ejected
		# wholesale on upgrade - the explicit reset/reconnect clears still end it.
		self._write(
			agent_url="ws://container-1",
			jarvis_admin_api_key="**********",
			chat_was_ready_at="2026-01-01 00:00:00",
			chat_ready_authority="",
		)
		raw = account._settings_raw(account._GATE_STATE_FIELDS)
		self.assertTrue(account._has_been_chat_ready(raw))


class TestStructuredNeverPaidCode(FrappeTestCase):
	"""Review P0-05: the never-paid gate reads admin's STRUCTURED code first, so a
	hardened control plane that omits the human sentence is still classified. The
	prose markers remain only as old-admin fallback."""

	def _err(self, *, status_code=403, code="", message=""):
		from jarvis.exceptions import AdminAuthError

		return AdminAuthError(message, status_code=status_code, code=code)

	def test_structured_never_paid_code_hard_gates(self):
		# admin's concrete contract code (jarvis_admin_v2 CustomerNotPaidError).
		self.assertTrue(account._is_never_paid_403(self._err(code="CUSTOMER_NOT_PAID")))
		self.assertTrue(account._is_never_paid_403(self._err(code="customer_not_paid")))  # case-insensitive

	def test_an_unrecognised_code_does_not_hard_gate(self):
		# Cancelled / a proxy code / the authority-repair refusal / anything else
		# stays SOFT even on a 403 - only the never-paid code hard-gates.
		self.assertFalse(account._is_never_paid_403(self._err(code="CANCELLED")))
		self.assertFalse(account._is_never_paid_403(self._err(code="TENANT_AUTHORITY_REPAIR_REQUIRED")))
		self.assertFalse(account._is_never_paid_403(self._err(code="SOME_OTHER")))

	def test_prose_fallback_only_when_no_code(self):
		self.assertTrue(
			account._is_never_paid_403(self._err(message="Customer status: Pending Payment")),
			"old admin with no code still matches on the sentence",
		)
		# A structured code that is NOT never-paid must WIN over never-paid-looking
		# prose: the code is the authority once present.
		self.assertFalse(
			account._is_never_paid_403(
				self._err(code="CANCELLED", message="customer status: pending payment")
			)
		)

	def test_a_non_403_is_never_a_never_paid(self):
		self.assertFalse(account._is_never_paid_403(self._err(status_code=401, code="PENDING_PAYMENT")))


class TestDiagnosticClass(FrappeTestCase):
	"""Review P1-08: a readiness_unconfirmed verdict carries a safe structured
	diagnostic code + retryability, so the SPA can tell a transient outage (retry)
	from an authorization denial (act) without seeing admin's raw exception text."""

	def setUp(self):
		account._bust_chat_gate()
		self._key_snap = _set_stored_connection(True)
		self._marker_snap = account._settings_raw((account._READY_MARKER_FIELD,)).get(
			account._READY_MARKER_FIELD
		)
		_set_ready_marker(None)  # never-ready cohort, so the outage fails closed

	def tearDown(self):
		account._bust_chat_gate()
		_set_ready_marker(self._marker_snap)
		frappe.db.set_value(
			"Jarvis Settings",
			"Jarvis Settings",
			"jarvis_admin_api_key",
			self._key_snap,
			update_modified=False,
		)
		frappe.db.commit()

	def test_transport_outage_is_retryable(self):
		from jarvis.exceptions import AdminUnreachableError

		with patch.object(admin_client, "get_connection", side_effect=AdminUnreachableError("down")):
			out = account._admin_chat_gate()
		self.assertEqual(out["diag_code"], "admin_unreachable")
		self.assertTrue(out["retryable"])

	def test_a_403_denial_is_not_retryable(self):
		from jarvis.exceptions import AdminAuthError

		with patch.object(
			admin_client, "get_connection", side_effect=AdminAuthError("denied", status_code=403)
		):
			out = account._admin_chat_gate()
		self.assertEqual(out["diag_code"], "admin_forbidden")
		self.assertFalse(out["retryable"])

	def test_the_cached_verdict_carries_the_same_diagnostic(self):
		from jarvis.exceptions import AdminUnreachableError

		with patch.object(admin_client, "get_connection", side_effect=AdminUnreachableError("down")) as gc:
			first = account._admin_chat_gate()
			second = account._admin_chat_gate()
		gc.assert_called_once()  # second served from the short unconfirmed cache
		self.assertEqual(first, second, "a polling wizard must get an identical verdict each beat")


class TestBackfillExcludesOauthPushOnly(FrappeTestCase):
	"""Review P0-07: the v2_10 backfill grandfathers ONLY confirmed-apply evidence.
	llm_oauth_connected_at is a push, not a confirmation, so a workspace whose only
	evidence is an OAuth push must NOT be manufactured into the established cohort."""

	_FIELDS = (
		"chat_was_ready_at",
		"chat_ready_authority",
		"llm_pool_synced_at",
		"llm_direct_synced_at",
		"llm_oauth_connected_at",
		"agent_url",
		"jarvis_admin_api_key",
	)

	def setUp(self):
		self._snap = account._settings_raw(self._FIELDS)
		self._key_snap = frappe.get_single("Jarvis Settings").get_password(
			"jarvis_admin_api_key", raise_exception=False
		)

	def tearDown(self):
		for f in self._FIELDS:
			frappe.db.set_value(
				"Jarvis Settings", "Jarvis Settings", f, self._snap.get(f), update_modified=False
			)
		from jarvis._password_utils import clear_settings_password, set_settings_password

		s = frappe.get_single("Jarvis Settings")
		# _reset writes a real encrypted key; restore the ORIGINAL (or clear it, so a
		# site that had none is left with none - otherwise the leaked __Auth row makes
		# get_password read onboarded for the next test).
		if self._key_snap:
			set_settings_password(s, "jarvis_admin_api_key", self._key_snap)
		else:
			clear_settings_password(s, "jarvis_admin_api_key")
		frappe.db.commit()

	def _reset(self, **vals):
		base = dict.fromkeys(self._FIELDS[:-1], None)
		base.update(vals)
		for f, v in base.items():
			frappe.db.set_value("Jarvis Settings", "Jarvis Settings", f, v, update_modified=False)
		from jarvis._password_utils import set_settings_password

		set_settings_password(
			frappe.get_single("Jarvis Settings"), "jarvis_admin_api_key", "backfill-test-key"
		)
		frappe.db.commit()

	def test_oauth_push_only_is_not_grandfathered(self):
		from jarvis.patches.v2_10_backfill_chat_was_ready_at import execute

		self._reset(llm_oauth_connected_at="2026-01-01 00:00:00")
		execute()
		self.assertFalse(
			account._settings_raw(("chat_was_ready_at",)).get("chat_was_ready_at"),
			"an OAuth push alone is not a confirmed apply and must not mint the marker",
		)

	def test_confirmed_apply_is_grandfathered_and_anchor_bound(self):
		from jarvis.patches.v2_10_backfill_chat_was_ready_at import execute

		self._reset(llm_direct_synced_at="2026-01-01 00:00:00", agent_url="ws://c1")
		execute()
		raw = account._settings_raw(("chat_was_ready_at", account._READY_ANCHOR_FIELD))
		self.assertTrue(raw.get("chat_was_ready_at"), "a confirmed direct apply is grandfathered")
		self.assertTrue(raw.get(account._READY_ANCHOR_FIELD), "the grandfathered claim is anchor-bound")


class TestGetLlmApplyOperationShim(FrappeTestCase):
	"""jarvis.account.get_llm_apply_operation — the read-only bench shim the SPA's
	single Start-chatting controller polls (plan-05 D2). It forwards the opaque
	operation id to admin and surfaces the §8.4 status verbatim; it is
	System-Manager-gated and holds no operation state of its own."""

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_forwards_the_operation_id_and_returns_admin_status(self):
		status = {
			"operation_id": "llmapply_abc",
			"state": "applied_waiting_readiness",
			"code": "LLM_APPLIED_READINESS_PENDING",
			"message": "finishing setup",
			"tenant": "deadbeef" * 4,
			"tenant_authority_generation": 7,
			"desired_version": 12,
			"applied_version": 12,
			"chat_readiness": "Configuring",
			"chat_readiness_reason": "Applying the ERP plugin environment.",
			"retryable": True,
			"retry_after_seconds": 0,
		}
		with patch.object(admin_client, "get_llm_apply_operation", return_value=status) as m:
			out = account.get_llm_apply_operation("llmapply_abc")
		m.assert_called_once_with("llmapply_abc")
		self.assertEqual(out, status)

	def test_admin_validation_error_surfaces_as_frappe_throw(self):
		# e.g. UnknownOperation (404) -> AdminValidationError -> clean toast.
		with patch.object(
			admin_client, "get_llm_apply_operation", side_effect=AdminValidationError("no such operation")
		):
			with self.assertRaises(frappe.ValidationError) as cm:
				account.get_llm_apply_operation("llmapply_missing")
		self.assertIn("no such operation", str(cm.exception))

	def test_is_system_manager_gated_before_any_admin_round_trip(self):
		frappe.set_user("Guest")
		with patch.object(admin_client, "get_llm_apply_operation") as m:
			with self.assertRaises(frappe.PermissionError):
				account.get_llm_apply_operation("llmapply_abc")
		m.assert_not_called()


class TestOperationProbeVerdictPersistence(FrappeTestCase):
	"""Plan-05 D2 paired follow-up: admin moved the fleet push off the synchronous
	save (admin #193), so the AI-models probe verdicts now arrive on the CONVERGED
	operation status. Polling get_llm_apply_operation folds them into the same bench
	settings cache the settings panel already reads (get_llm_config) - without a new
	endpoint or a second call - and never blanks a prior verdict when they are
	absent."""

	_FIELDS = ("last_subscription_status", "last_sync_warnings", "last_model_statuses")

	def setUp(self):
		self._snap = {f: frappe.db.get_value("Jarvis Settings", "Jarvis Settings", f) for f in self._FIELDS}
		# A prior REAL verdict already on the row (the last confirmed apply's).
		frappe.db.set_value(
			"Jarvis Settings",
			"Jarvis Settings",
			{
				"last_subscription_status": "verified",
				"last_sync_warnings": frappe.as_json(["prior warning"]),
				"last_model_statuses": frappe.as_json(
					[{"provider": "openai", "model": "gpt", "status": "ok"}]
				),
			},
			update_modified=False,
		)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.set_value("Jarvis Settings", "Jarvis Settings", self._snap, update_modified=False)
		frappe.db.commit()
		frappe.set_user("Administrator")

	def _status(self, **over):
		base = {
			"operation_id": "llmapply_x",
			"state": "ready",
			"code": "LLM_READY",
			"message": "",
			"tenant": "d" * 32,
			"tenant_authority_generation": 7,
			"desired_version": 12,
			"applied_version": 12,
			"chat_readiness": "Ready",
			"chat_readiness_reason": "",
			"retryable": False,
			"retry_after_seconds": 0,
		}
		base.update(over)
		return base

	def _row(self):
		return frappe.db.get_value("Jarvis Settings", "Jarvis Settings", list(self._FIELDS), as_dict=True)

	def test_converged_poll_persists_probe_verdicts(self):
		status = self._status(
			subscription_status="usage_limited",
			warnings=["quota low"],
			model_statuses=[{"provider": "openai", "model": "gpt-5.5", "status": "ok"}],
		)
		with patch.object(admin_client, "get_llm_apply_operation", return_value=status):
			account.get_llm_apply_operation("llmapply_x")
		row = self._row()
		self.assertEqual(row.last_subscription_status, "usage_limited")
		self.assertEqual(frappe.parse_json(row.last_sync_warnings), ["quota low"])
		self.assertEqual(
			frappe.parse_json(row.last_model_statuses),
			[{"provider": "openai", "model": "gpt-5.5", "status": "ok"}],
		)

	def test_poll_without_probe_fields_leaves_prior_verdicts(self):
		# Still applying (or an old admin): no probe fields on the status -> the last
		# real verdict must be left intact, never blanked.
		status = self._status(state="applying", code="LLM_APPLYING", chat_readiness="Configuring")
		with patch.object(admin_client, "get_llm_apply_operation", return_value=status):
			account.get_llm_apply_operation("llmapply_x")
		row = self._row()
		self.assertEqual(row.last_subscription_status, "verified")
		self.assertEqual(frappe.parse_json(row.last_sync_warnings), ["prior warning"])
		self.assertEqual(
			frappe.parse_json(row.last_model_statuses),
			[{"provider": "openai", "model": "gpt", "status": "ok"}],
		)

	def test_unchanged_noop_poll_leaves_prior_verdicts(self):
		# A byte-identical no-op apply ran no probe: its "unchecked"/[] must not
		# discard the last real verdict.
		status = self._status(subscription_status="unchecked", warnings=[], model_statuses=[], unchanged=True)
		with patch.object(admin_client, "get_llm_apply_operation", return_value=status):
			account.get_llm_apply_operation("llmapply_x")
		row = self._row()
		self.assertEqual(row.last_subscription_status, "verified")
		self.assertEqual(frappe.parse_json(row.last_sync_warnings), ["prior warning"])
