"""A sync that loses a write-conflict race stays recoverable, and the customer
has a lever (#713).

THE BUG THESE PIN. On a workspace whose chat was already working, Settings read
"Last sync failed. unexpected error; see Error Log". Behind it:

    (1020, "Record has changed since last read in table 'tabsingles'")
      jarvis_settings.py  _enqueued_sync_via_admin (action='restart')
      jarvis_settings.py  _sync_via_admin -> _converge_via_admin(is_pool=False)

The statement that failed was not the UPDATE. It was the locking PRE-READ
``Document.db_set`` issues on the first write to a freshly loaded doc:

    SELECT `field`,`value` FROM `tabSingles` WHERE `doctype`='Jarvis Settings' FOR UPDATE

Under MariaDB 11.6+'s ``innodb_snapshot_isolation=ON`` that read fails outright
if ANY of the doctype's rows moved since the transaction's snapshot opened, and
the sync worker's snapshot had been open across the admin push plus two minutes
of convergence polling. In the live incident the competing writer was
``account._mark_chat_ready`` stamping ``chat_was_ready_at`` 19.7s earlier, from
the SPA's readiness POST, reacting to the SAME "Ready" the worker was waiting
for. So the two writers were not merely concurrent, they were correlated by
construction.

Three properties are asserted here, in the order the fix delivers them:

  * the write no longer takes a doctype-wide lock it never needed;
  * a conflict that does happen is replayed, and if the replays lose, the sync
    lands on the reconcile-owned pending state instead of a terminal failure;
  * a workspace stuck on a terminal failure has a way back (``resync_llm``),
    including the exact shape from #713 that no reconcile branch matches.

HONEST LIMIT OF THE INJECTION. ``_conflict_on`` raises a faithfully constructed
``QueryDeadlockError`` around a real ``pymysql.InternalError(1020, ...)``, from
inside ``frappe.db.sql`` at the statement that can still conflict after the fix
(the ``tabSingles`` DELETE). It is still raised from Python: MariaDB never sees
the statement, so no test here exercises a genuine engine-produced ER_CHECKREAD,
and nothing here would catch a divergence between the real error and this
reconstruction. That is the same limitation the admin-plane fix
(jarvis-admin-v2 PR #264) recorded, and it is not fixable in this repo's CI: CI runs
MariaDB 10.11, which has no ``innodb_snapshot_isolation`` at all and cannot raise
1020 for this reason under any query. Reproducing the real error needs MariaDB
11.6+ and two connections:

    conn A: START TRANSACTION;
            SELECT * FROM tabSingles WHERE doctype='Jarvis Settings';
    conn B: UPDATE tabSingles SET value=NOW() WHERE doctype='Jarvis Settings'
              AND field='chat_was_ready_at';
            COMMIT;
    conn A: SELECT * FROM tabSingles WHERE doctype='Jarvis Settings' FOR UPDATE;
            -- ERROR 1020: Record has changed since last read
"""

import contextlib
from unittest.mock import patch

import frappe
import pymysql
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client, onboarding
from jarvis.jarvis.doctype.jarvis_settings.jarvis_settings import (
	_PENDING_APPLYING_STATUS,
	_write_settings_fields,
	reconcile_pending_llm_sync,
)

SETTINGS = "Jarvis Settings"
POOL_MODEL_DT = "Jarvis LLM Pool Model"
_READINESS = "jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._admin_chat_readiness"
_UNEXPECTED = "failed: unexpected error; see Error Log"

# Everything these tests write. Frappe Singles are not rolled back between tests
# (and these tests commit on purpose - see _conflict_on), so the operator's real
# state is snapshotted and restored rather than trusted to the test transaction.
_TOUCHED = (
	"last_sync_at",
	"last_sync_status",
	"llm_direct_synced_at",
	"llm_pool_synced_at",
	"llm_provider",
	"llm_model",
	"llm_auth_mode",
	"llm_base_url",
	"proxy_active",
	"chat_was_ready_at",
	"preset",
	# _sync_via_admin's restart leg calls installed_apps_sync.record_synced_snapshot,
	# which writes this. Snapshotted so a test run leaves the operator's real record
	# exactly as it found it.
	"installed_apps_synced",
)
_SECRETS = ("llm_api_key", "jarvis_admin_api_key", "jarvis_admin_api_secret")


def _checkread() -> Exception:
	"""The exact error MariaDB raises under snapshot isolation, wrapped the way
	frappe wraps it. ``is_deadlocked`` folds ER.CHECKREAD in with ER.LOCK_DEADLOCK
	(frappe/database/mariadb/database.py), so ``frappe.db.sql`` turns both into
	``QueryDeadlockError`` - which is what any handler can actually match on."""
	return frappe.QueryDeadlockError(
		pymysql.InternalError(1020, "Record has changed since last read in table 'tabsingles'")
	)


@contextlib.contextmanager
def _conflict_on(marker: str, *, times: int | None = None):
	"""Fail every ``tabSingles`` statement that writes ``marker``, at the layer a
	conflict can still occur after the fix.

	Patching ``_write_settings_fields`` itself would prove nothing: the whole
	defect is that a raw statement raises mid-transaction, so the injection has to
	sit UNDER the function, not over it. Every other query in the request runs for
	real, which is what lets the recovery (the replay, the fallback status write,
	the reconcile that follows) be asserted honestly rather than mocked.

	Scoped to one field name so the FALLBACK write - itself a ``tabSingles``
	write - is not swallowed by the same injection and the recovery stays
	observable. ``times`` bounds how many statements fail, which is how a conflict
	that CLEARS (the ordinary case: the other writer has committed and moved on) is
	distinguished from one that never does.
	"""
	real = frappe.local.db.sql
	state = {"raised": 0}

	def fake(query, *args, **kwargs):
		blob = f"{query} {args} {kwargs}"
		if "tabSingles" in blob and marker in blob and (times is None or state["raised"] < times):
			state["raised"] += 1
			raise _checkread()
		return real(query, *args, **kwargs)

	with patch.object(frappe.local.db, "sql", side_effect=fake):
		yield state


def _status() -> str:
	return frappe.db.get_single_value(SETTINGS, "last_sync_status", cache=False) or ""


def _direct_marker():
	return frappe.db.get_single_value(SETTINGS, "llm_direct_synced_at", cache=False)


class _Base(FrappeTestCase):
	def setUp(self):
		from frappe.utils.password import remove_encrypted_password

		s = frappe.get_single(SETTINGS)
		self._snap = {f: s.get(f) for f in _TOUCHED}
		self._snap_secrets = {f: (s.get_password(f, raise_exception=False) or "") for f in _SECRETS}
		# A leftover pool row - from the v1_seed_llm_models migration on a configured
		# site, or from another module's pool tests against this same shared Single -
		# flips compute_pool_mode and _has_llm_config, which would route these
		# single-model assertions down the pool leg and fail them ORDER-DEPENDENTLY.
		# The sibling suites (test_settings_on_update, test_save_llm_pool_operation)
		# clear the same state for the same reason.
		frappe.db.delete(POOL_MODEL_DT, {"parenttype": SETTINGS, "parent": SETTINGS})
		# A connected SINGLE-MODEL workspace mid-apply: exactly #713's shape.
		frappe.db.set_single_value(
			SETTINGS,
			{
				"llm_provider": "deepseek",
				"llm_model": "deepseek-v4-flash",
				"llm_auth_mode": "api_key",
				"llm_base_url": "",
				"preset": "",
				"proxy_active": 0,
				"last_sync_status": "pending: provisioning container",
				"llm_direct_synced_at": None,
				"llm_pool_synced_at": None,
			},
			update_modified=False,
		)
		for f in _SECRETS:
			remove_encrypted_password(SETTINGS, SETTINGS, f)
		s = frappe.get_single(SETTINGS)
		s.db_set("llm_api_key", "sk-test-713")
		s.db_set("jarvis_admin_api_key", "adminkey")
		s.db_set("jarvis_admin_api_secret", "adminsecret")
		frappe.db.commit()
		self._clear_cooldown()

	def tearDown(self):
		from frappe.utils.password import remove_encrypted_password

		frappe.db.set_single_value(SETTINGS, dict(self._snap), update_modified=False)
		for f, v in self._snap_secrets.items():
			remove_encrypted_password(SETTINGS, SETTINGS, f)
			frappe.db.set_single_value(SETTINGS, f, v, update_modified=False)
		frappe.db.commit()
		self._clear_cooldown()

	@staticmethod
	@contextlib.contextmanager
	def _as_worker():
		"""Run the body as a background job, which is where #713 happened and the
		only context that owns its transaction outright.

		Not a convenience: ``_write_settings_fields`` replays a conflict ONLY when
		``_owns_transaction()`` is true, precisely so a request or a mid-save never
		has its transaction ended underneath it. ``frappe.local.job`` is what
		``execute_job`` sets, so setting it is what makes these tests exercise the
		real worker path rather than a shape production never takes."""
		previous = getattr(frappe.local, "job", None)
		frappe.local.job = frappe._dict(method="test", job_name="test", after_job=None)
		try:
			yield
		finally:
			frappe.local.job = previous

	@staticmethod
	def _clear_cooldown():
		"""The Resync push cooldown lives in redis, which no test transaction rolls
		back. Left behind, one test's push throttles the next test's for three
		minutes - and which tests those are depends on execution order."""
		frappe.cache().delete_value(onboarding._RESYNC_COOLDOWN_KEY)

	def _settings(self):
		return frappe.get_single(SETTINGS)


# ---------------------------------------------------------------------------
# the write itself: a smaller lock, and a replay for what is left
# ---------------------------------------------------------------------------


class TestSettingsWriteSurvivesAConflict(_Base):
	def test_it_does_not_take_the_doctype_wide_for_update_that_failed_in_713(self):
		"""The load-bearing structural fix. ``db_set``'s pre-read locks EVERY
		Jarvis Settings row, so a write of one status field conflicted with a
		concurrent write of any of the 100 a live workspace has - which is how a readiness marker
		killed a sync stamp. Asserted against ``db_set`` in the same test so the
		difference is the claim, not a snapshot of one implementation."""
		guarded, direct = [], []

		def _record(into):
			real = frappe.local.db.sql

			def fake(query, *args, **kwargs):
				into.append(str(query))
				return real(query, *args, **kwargs)

			return patch.object(frappe.local.db, "sql", side_effect=fake)

		with _record(guarded):
			_write_settings_fields(self._settings(), {"last_sync_status": "ok (test)"})
		with _record(direct):
			self._settings().db_set("last_sync_status", "ok (test)", update_modified=False)

		def _locking_reads(queries):
			return [q for q in queries if "FOR UPDATE" in q.upper() and "tabSingles" in q]

		self.assertEqual(_locking_reads(guarded), [], "the guarded write must not lock the whole doctype")
		self.assertTrue(
			_locking_reads(direct),
			"db_set is expected to still take the doctype-wide lock; if it no longer "
			"does, frappe changed and this fix's premise needs re-checking",
		)

	def test_a_conflict_that_clears_is_replayed_and_the_value_lands(self):
		"""The ordinary case. The competing writer has committed and moved on, so
		the replay - which runs on a FRESH snapshot, because the rollback is what
		ends the doomed transaction - simply succeeds."""
		with self._as_worker(), _conflict_on("last_sync_status", times=1) as state:
			landed = _write_settings_fields(self._settings(), {"last_sync_status": "ok (replayed)"})
		self.assertTrue(landed)
		self.assertEqual(state["raised"], 1)
		self.assertEqual(_status(), "ok (replayed)")

	def test_a_conflict_that_never_clears_reports_failure_instead_of_raising(self):
		"""Bounded, and it does not raise: the caller decides what a lost race
		means for the customer, and for every caller here the answer is a
		recoverable state, never an exception on its way to an Error Log."""
		with self._as_worker(), _conflict_on("last_sync_status") as state:
			landed = _write_settings_fields(self._settings(), {"last_sync_status": "ok (never)"})
		self.assertFalse(landed)
		self.assertGreaterEqual(state["raised"], 2, "the write is replayed, not attempted once")
		self.assertEqual(_status(), "pending: provisioning container", "the old value stands")

	def test_a_request_re_raises_instead_of_ending_a_transaction_it_does_not_own(self):
		"""The safety rule behind the replay. In a request or mid-save the caller's
		transaction is not ours to end, and a 1020/1213 has already destroyed its
		uncommitted work server-side. Rolling back and re-persisting only OUR field
		would leave a half-written save that every caller up the stack reports as a
		success, so the conflict propagates and frappe's own request rollback stays
		authoritative."""
		with _conflict_on("last_sync_status"), self.assertRaises(frappe.QueryDeadlockError):
			_write_settings_fields(self._settings(), {"last_sync_status": "ok (request)"})

	def test_it_swallows_nothing_but_a_write_conflict(self):
		"""A programmer error or a schema fault must not be absorbed into a
		"lost a race" story - that is how a real bug becomes a mystery."""
		real = frappe.local.db.sql

		def fake(query, *args, **kwargs):
			if "tabSingles" in str(query) and "last_sync_status" in f"{query} {args}":
				raise ValueError("not a write conflict")
			return real(query, *args, **kwargs)

		with patch.object(frappe.local.db, "sql", side_effect=fake), self.assertRaises(ValueError):
			_write_settings_fields(self._settings(), {"last_sync_status": "ok (boom)"})


# ---------------------------------------------------------------------------
# #713 proper: the sync's own recovery
# ---------------------------------------------------------------------------


class TestSyncRecoversFromAConflict(_Base):
	"""The live sequence: admin returns 200 status="applying", the convergence
	poll sees Ready, and the stamp loses a race with a writer reacting to that
	same Ready."""

	@contextlib.contextmanager
	def _applying_then_ready(self):
		with (
			patch.object(
				admin_client,
				"post_update_llm_creds",
				return_value={"action": "restart", "status": "applying"},
			),
			patch(_READINESS, return_value=("Ready", "")),
		):
			yield

	def test_a_lost_stamp_is_not_reported_as_an_unexpected_error(self):
		"""The customer-visible defect. "unexpected error" is what
		``humaniseSyncStatus`` renders as "Last sync failed", on a workspace whose
		container was serving the new config the whole time."""
		with self._as_worker(), self._applying_then_ready(), _conflict_on("llm_direct_synced_at"):
			self._settings()._sync_via_admin("restart")
		self.assertNotIn(_UNEXPECTED, _status())
		self.assertEqual(_status(), _PENDING_APPLYING_STATUS)

	def test_the_stamp_still_lands_when_the_conflict_clears(self):
		"""The fix must not make the happy path pessimistic: one lost race is
		replayed into a normal, fully stamped success."""
		with self._as_worker(), self._applying_then_ready(), _conflict_on("llm_direct_synced_at", times=1):
			self._settings()._sync_via_admin("restart")
		self.assertTrue(_status().startswith("ok"))
		self.assertTrue(_direct_marker())

	def test_the_recovered_state_is_one_a_converging_path_acts_on(self):
		"""Why the pending marker rather than softer failure wording: it is the
		state ``reconcile_pending_llm_sync`` (and the SPA's own poller) converges.
		The apply is real, so recording it is the only thing outstanding."""
		with self._as_worker(), self._applying_then_ready(), _conflict_on("llm_direct_synced_at"):
			self._settings()._sync_via_admin("restart")
		self.assertEqual(_status(), _PENDING_APPLYING_STATUS)

		with patch(_READINESS, return_value=("Ready", "")):
			reconcile_pending_llm_sync()
		self.assertTrue(_status().startswith("ok"), f"reconcile left the status at {_status()!r}")
		self.assertTrue(_direct_marker())

	def test_a_terminal_failure_after_a_proven_apply_is_a_dead_end(self):
		"""The other half of #713, and the reason a Resync lever had to exist.
		``reconcile_pending_llm_sync`` acts on a pending-applying marker or on a
		FIRST apply that never proved itself. A workspace with a terminal "failed:"
		AND a proven apply behind it (llm_direct_synced_at set from an earlier good
		sync) matches neither, so nothing in the product ever tried again.

		This documents the gap rather than closing it: widening the reconcile to
		re-probe every failed workspace would put every genuinely broken config on
		a */5 admin call forever.

		Admin is deliberately mocked as Ready, which makes the point sharper. The
		reconcile does reach the probe (through its disconnect leg, which every
		connected workspace satisfies), is told the container is serving, and still
		leaves the status alone: no branch it has repairs a failed status behind a
		proven apply."""
		frappe.db.set_single_value(
			SETTINGS,
			{"last_sync_status": _UNEXPECTED, "llm_direct_synced_at": frappe.utils.now()},
			update_modified=False,
		)
		with patch(_READINESS, return_value=("Ready", "")):
			reconcile_pending_llm_sync()
			self.assertEqual(_status(), _UNEXPECTED, "no reconcile branch repairs this workspace")
			# The lever that does. Same workspace, same admin verdict, one call.
			with patch("frappe.enqueue") as enq:
				out = onboarding.resync_llm()
		self.assertEqual(out["outcome"], "converged")
		self.assertTrue(_status().startswith("ok"))
		enq.assert_not_called()


# ---------------------------------------------------------------------------
# the way forward: onboarding.resync_llm
# ---------------------------------------------------------------------------


class TestResyncLlm(_Base):
	def test_it_stamps_and_does_not_touch_the_container_when_admin_already_serves_it(self):
		"""Probe before push. The state this exists to clear is bookkeeping that
		lost a race, where the container is fine - restarting it would take chat
		away from a working customer to fix a status string."""
		frappe.db.set_single_value(SETTINGS, "last_sync_status", _UNEXPECTED, update_modified=False)
		with patch(_READINESS, return_value=("Ready", "")), patch("frappe.enqueue") as enq:
			out = onboarding.resync_llm()
		self.assertEqual(out["outcome"], "converged")
		self.assertTrue(out["last_sync_status"].startswith("ok"))
		self.assertTrue(_direct_marker())
		enq.assert_not_called()

	def test_it_queues_a_re_push_when_admin_says_the_config_is_not_serving(self):
		frappe.db.set_single_value(SETTINGS, "last_sync_status", _UNEXPECTED, update_modified=False)
		with patch(_READINESS, return_value=("Configuring", "applying your LLM configuration")):
			with patch("frappe.enqueue") as enq:
				out = onboarding.resync_llm()
		self.assertEqual(out["outcome"], "queued")
		self.assertEqual(out["leg"], "direct")
		self.assertTrue(out["pending"], "the poller the SPA already runs must flip immediately")
		self.assertEqual(
			[c.kwargs.get("job_id") for c in enq.call_args_list],
			["jarvis_settings_sync:restart"],
			"a Resync must coalesce with an ordinary save, not race it",
		)

	def test_it_queues_the_pool_leg_for_a_pool_workspace(self):
		"""A pool is re-driven through the pool worker, not the creds one - the two
		push different admin endpoints. ``compute_pool_mode`` is stubbed rather than
		satisfied with real ``models[]`` rows: a genuine pool needs encrypted
		Password child rows and a full save, which would fire the whole on_update
		sync machinery this test is not about."""
		with (
			patch("jarvis.jarvis.pool_serialize.compute_pool_mode", return_value=True),
			patch(_READINESS, return_value=("Configuring", "")),
			patch("frappe.enqueue") as enq,
		):
			out = onboarding.resync_llm()
		self.assertEqual(out["leg"], "pool")
		self.assertEqual([c.kwargs.get("job_id") for c in enq.call_args_list], ["jarvis_settings_sync:pool"])

	def test_a_second_click_inside_the_cooldown_pushes_nothing(self):
		"""A status that has not moved is exactly what makes someone click again, and
		frappe's job dedupe stops covering the moment the first worker exits. Without
		the cooldown each further click is another container re-render against the
		customer's shared rotate-ops budget."""
		with patch(_READINESS, return_value=("Configuring", "")), patch("frappe.enqueue") as enq:
			first = onboarding.resync_llm()
			second = onboarding.resync_llm()
		self.assertEqual(first["outcome"], "queued")
		self.assertEqual(second["outcome"], "throttled")
		self.assertEqual(second["leg"], "")
		self.assertEqual(len(enq.call_args_list), 1, "the second click must enqueue nothing")

	def test_the_cooldown_never_blocks_the_probe_that_costs_nothing(self):
		"""Throttling the Ready path would be the wrong trade: it neither pushes nor
		restarts, and "your config is already live" is the best answer a Resync can
		give. It stays available even straight after a throttled click."""
		with patch(_READINESS, return_value=("Configuring", "")), patch("frappe.enqueue"):
			onboarding.resync_llm()
		with patch(_READINESS, return_value=("Ready", "")), patch("frappe.enqueue") as enq:
			out = onboarding.resync_llm()
		self.assertEqual(out["outcome"], "converged")
		enq.assert_not_called()

	def test_it_refuses_a_workspace_with_nothing_to_re_drive(self):
		"""No credential means no configuration to push; queueing one would spend a
		container restart proving that."""
		from frappe.utils.password import remove_encrypted_password

		remove_encrypted_password(SETTINGS, SETTINGS, "llm_api_key")
		frappe.db.set_single_value(SETTINGS, "llm_api_key", "", update_modified=False)
		with patch(_READINESS) as probe, patch("frappe.enqueue") as enq:
			out = onboarding.resync_llm()
		self.assertEqual(out["outcome"], "not_configured")
		enq.assert_not_called()
		probe.assert_not_called()

	def test_it_is_gated_on_the_workspace_admin_role(self):
		"""It drives a container mutation, so it carries the same gate every other
		write endpoint on this module does."""
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				onboarding.resync_llm()
		finally:
			frappe.set_user("Administrator")
