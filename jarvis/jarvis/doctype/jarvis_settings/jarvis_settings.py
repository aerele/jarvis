import re

import frappe
from frappe.model.document import Document

LLM_FIELDS_TRIGGERING_SYNC = (
	"llm_provider",
	"llm_model",
	"llm_api_key",
	"llm_base_url",
)

# Whitelabel branding (Single-stored, tenant-admin editable). AGENT_NAME_MAX
# also bounds the per-turn [Context:] clause turn_handler injects.
AGENT_NAME_MAX = 40
_BRAND_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".svg", ".webp", ".ico", ".gif")


def validate_branding_inputs(agent_name, logo_url, favicon_url):
	"""Normalize + validate whitelabel inputs; return the cleaned triple or
	throw. Shared by the doctype validate() and the branding API so the two
	never drift."""
	name = (agent_name or "").strip()
	if len(name) > AGENT_NAME_MAX:
		frappe.throw(
			f"Assistant name must be {AGENT_NAME_MAX} characters or fewer.",
			frappe.ValidationError,
		)
	logo = (logo_url or "").strip()
	favicon = (favicon_url or "").strip()
	for url in (logo, favicon):
		if url and not url.lower().split("?")[0].endswith(_BRAND_IMAGE_EXTS):
			frappe.throw(
				"Logo and favicon must be image files (png, jpg, jpeg, svg, webp, ico, gif).",
				frappe.ValidationError,
			)
	return name, logo, favicon


# Subscription-mode auth modes - the container owns credentials, so the
# bench's classifier treats a save with no structural change as a no-op.
# We accept both "oauth" (REV-1 canonical) and the legacy "subscription"
# value migrated tenants might still carry.
_CONTAINER_OWNED_MODES = {"oauth", "subscription"}

# Shared budget for the admin-sync background jobs (single-model + pool).
# One constant, three consumers, one invariant chain:
#
#   RQ envelope > worst-case work it wraps, AND lock TTL >= RQ envelope.
#
# Worst-case pool work: <=60s redis-lock wait + up to 2 POST attempts x 150s
# admin HTTP budget (post_update_llm_pool, now on DEFAULT_TIMEOUT_S) + 1x5s
# retry sleep + a bounded in-job convergence poll (_POOL_CONVERGE_DEADLINE_S,
# ~120s) that absorbs an "applying"/timeout apply outcome. That is
# ~60 + 305 + 120 = 485s. Worst-case single-model work: <=60s lock wait + 150s
# admin HTTP budget + the post-restart skills resyncs. 600s clears both with
# headroom, so the rq SIGALRM (JobTimeoutException) only fires on something
# genuinely wedged - not on a routine cold provision (JARVIS-2026-07-08,
# fault c).
#
# The lock TTL must be >= the RQ envelope: the TTL bounds how long a
# CRASHED holder can block others, but a healthy holder may legitimately
# run right up to its SIGALRM - a shorter TTL would expire mid-run and
# let a second sync mutate the container in parallel with the first,
# exactly the interleaving the lock exists to prevent.
ADMIN_SYNC_RQ_TIMEOUT_S = 600
ADMIN_SYNC_LOCK_TIMEOUT_S = ADMIN_SYNC_RQ_TIMEOUT_S

# Lock-acquisition waits for the sync workers. A dead (SIGKILLed/OOMed)
# holder blocks the lock for up to the full TTL (600s) - nothing releases
# the key early in that case - so the retry chain's CUMULATIVE wait must
# exceed the TTL or a fresh tenant's first sync can exhaust every attempt
# against a corpse and strand on a terminal "failed: skipped". Chain:
# primary 60s + 4 retries x 150s = 660s > 600s TTL. Each retry runs in its
# own fresh RQ envelope, and per-attempt lock wait + POST-ONLY work must
# fit that envelope. POST-only worst case (lock wait excluded - the wait
# is the other term of the sum): pool = 2x150s attempts + 1x5s sleep +
# ~120s convergence poll = 425s; single-model = 150s + skills resyncs. So
# 150s + 425s = 575s <= 600s. These are the SAME figures the budget test
# in test_unified_llm_config.TestOnboardingAuditFixes asserts - tune the
# waits and the test together, and against the POOL figure (the larger
# of the two paths).
ADMIN_SYNC_PRIMARY_LOCK_WAIT_S = 60.0
ADMIN_SYNC_RETRY_LOCK_WAIT_S = 150.0
ADMIN_SYNC_LOCK_RETRIES = 4


# ---------------------------------------------------------------------------
# Convergence (audit F2): "applying" is NOT failure.
# ---------------------------------------------------------------------------
# The admin now persists the customer's desired LLM config, COMMITS it, and only
# THEN drives the agent apply. On a read-timeout it returns an accepted
# ("applying") outcome instead of a 502 and a */5 admin reconcile cron finishes
# the apply server-side. Historically the bench stamped a terminal "failed:" the
# moment the inline POST didn't come back "ok", so a late-succeeding apply left
# the bench pinned at llm_pool_provisioning FOREVER while the container was
# actually fine - THE onboarding livelock.
#
# Now: an "applying"/timeout outcome is recorded as PENDING (not failed) and the
# bench CONVERGES from its own end - it polls admin get_connection, whose
# chat_readiness flips to "Ready" once the apply lands (admin gates Ready on
# applied_version >= desired_version, so it never reports Ready from mere
# intent). Convergence stamps the durable success markers; until it converges the
# status stays "pending: ..." and the UI shows a calm "finishing setup" state.
_PENDING_APPLYING_STATUS = "pending: admin applying config"

# In-job fast-path convergence poll. Bounded well under the RQ envelope (see the
# budget note above); anything not converged inside it is finished by the
# scheduled safety net (reconcile_pending_llm_sync, every 5 min). Probes use a
# short HTTP timeout so one slow admin read can't stretch the loop past budget.
_POOL_CONVERGE_DEADLINE_S = 120.0
_POOL_CONVERGE_INTERVAL_S = 20.0
_POOL_CONVERGE_PROBE_TIMEOUT_S = 15


def creds_wire_auth_mode(stored: str | None) -> str:
	"""Translate the STORED auth mode into the ``/llm-creds`` wire vocabulary.

	The two vocabularies do not match, and the mismatch is silent. This DocType
	mirrors ``models[0].credential_type``, whose options are exactly
	``api_key`` / ``subscription``. The fleet wire contract is
	``api_key`` / ``oauth``: fleet's own ``models.py`` says "the earlier
	'subscription' string was the customer-facing UI term; the wire+template
	contract is 'oauth'", and it maps a literal ``subscription`` to the API-KEY
	branch. For a chat subscription that renders a config pointing at a key file
	that does not exist, so the container comes up and fails every turn.

	Translated on the WIRE only. The stored value stays ``subscription`` because
	other consumers read it, and widening those guards is what force-disconnected
	healthy pool tenants before (jarvis-admin-v2#89). Nothing here changes what is
	persisted or what any other reader sees.

	No-op until ``compute_pool_mode`` stops forcing a lone subscription onto the
	pool leg (jarvis#715 step 3): today such a config never reaches this path.
	It is a prerequisite for that flip, not a behaviour change on its own.
	"""
	# Strip BEFORE defaulting. A whitespace-only value is truthy, so defaulting
	# first would carry " " through and return "" - not a mode fleet branches on,
	# which silently lands the tenant on whatever its undefined path does rather
	# than the api_key default this promises. Only a validation-bypassing write
	# (db_set, a patch) can produce one, since the field is a Select, but that is
	# exactly the kind of write that reaches a Single.
	mode = (stored or "").strip() or "api_key"
	return "oauth" if mode == "subscription" else mode


def _is_applying_result(result) -> bool:
	"""True iff an admin apply response is an ACCEPTED-but-still-converging
	outcome (C5) rather than a confirmed apply. The creds/pool fleet layer
	reports this as ``status == "applying"`` (or ``result == "applying"``); a
	genuine apply is ``status == "applied"`` / ``result == "ok"`` / absent (older
	contract). Absent keys => treat as a confirmed apply so a fleet still on the
	pre-C5 contract keeps its old "ok" semantics."""
	if not isinstance(result, dict):
		return False
	return result.get("status") == "applying" or result.get("result") == "applying"


def _admin_chat_readiness(*, timeout_s: int = _POOL_CONVERGE_PROBE_TIMEOUT_S):
	"""Probe admin get_connection for (chat_readiness, reason). Returns
	(state, reason); (None, "<err>") on any failure. Never raises - a convergence
	probe must not blow up the sync job or the scheduled reconcile."""
	from jarvis import admin_client

	try:
		data = admin_client.get_connection(timeout_s=timeout_s) or {}
	except Exception as e:
		return None, str(e)
	return (data.get("chat_readiness") or ""), (data.get("chat_readiness_reason") or "")


#: How many times a Jarvis Settings status write is replayed through a write
#: conflict before the caller falls back to the reconcile-owned pending state.
#: Three is enough for a conflict class whose window is one competing commit: each
#: replay starts from a FRESH snapshot, so it only loses again if another writer
#: commits inside the few milliseconds the retry takes.
_SETTINGS_WRITE_ATTEMPTS = 3
_SETTINGS_WRITE_BACKOFF_S = 0.05


#: The exception classes that mean "another connection got there first". ONE
#: definition, shared by ``_is_write_conflict`` and by the ``except`` clauses in
#: both sync workers, so a later widening cannot reach the retry loop while
#: silently missing the handlers, or the reverse.
_WRITE_CONFLICT_ERRORS: tuple[type[BaseException], ...] = (frappe.QueryDeadlockError,)


def _is_write_conflict(e: BaseException) -> bool:
	"""Did MariaDB refuse this statement because another connection moved the row
	after our transaction's snapshot opened (#713)?

	The bench runs MariaDB 11.6+ with ``innodb_snapshot_isolation=ON``, where a
	LOCKING read or write that meets a record newer than the transaction's read
	view fails immediately with ER_CHECKREAD 1020 ("Record has changed since last
	read") rather than waiting or re-reading. Frappe folds that into
	``QueryDeadlockError`` alongside a true ER_LOCK_DEADLOCK 1213
	(``frappe/database/mariadb/database.py``: ``is_deadlocked`` tests for both), so
	one class covers both and both want the same treatment here: the write did not
	land, nothing downstream of it ran, and a replay from a fresh snapshot is the
	whole recovery.

	Matching the frappe class rather than the errno is deliberate. The errno is not
	reachable without unwrapping ``QueryDeadlockError``'s argument, and an older
	MariaDB that reports the same lost race as a plain 1213 has to take this branch
	too or the customer-facing half of the fix only works on 11.6+."""
	return isinstance(e, _WRITE_CONFLICT_ERRORS)


def _owns_transaction() -> bool:
	"""May this code END the current transaction (commit or roll back)?

	True only where this process started one and nothing upstream is depending on
	it: a background job (``frappe.local.job`` is set exclusively by
	``execute_job``) or a migrate patch, where committing between units is normal.

	False in a web request, whose transaction belongs to the request: frappe commits
	it on success and rolls it back on any exception, and code that ends it halfway
	takes that decision away. False from ``bench execute`` and the CLI too, which is
	imprecise (they do own their transaction) but harmless: the only thing withheld
	there is a retry, and every caller treats a skipped retry as a lost race it will
	re-attempt."""
	return bool(getattr(frappe.local, "job", None) or frappe.flags.in_migrate)


def _inside_a_save() -> bool:
	"""Is a ``Jarvis Settings`` ``.save()`` in flight further up this stack?

	The precise question ``_owns_transaction`` cannot answer. ``frappe.flags.
	currently_saving`` holds ``(doctype, name)`` from ``set_user_and_timestamp``
	until the end of ``run_post_save_methods``, so it spans ``on_update`` - which is
	exactly where ``_enqueue_pool_sync`` and ``_on_update_single_model_legacy``
	write their pending status from.

	It matters because that caller has uncommitted field writes of its own in this
	transaction. A conflict there must be RE-RAISED so the whole save fails as one:
	rolling back and re-persisting only ``last_sync_status`` would leave the rest of
	the save discarded while every frame up the stack carries on and reports
	success. That hazard is about NESTING, not about being in a job, so it is asked
	separately - a job that ever calls ``settings.save()`` inherits the same
	protection without anyone having to remember."""
	return ("Jarvis Settings", "Jarvis Settings") in (frappe.flags.currently_saving or [])


def _refresh_db_snapshot() -> None:
	"""End the worker's transaction so the NEXT statement opens a fresh REPEATABLE
	READ snapshot.

	This is the structural half of #713. A sync worker's read view opens at the
	``frappe.get_single`` that loads the doc and then stays open across the admin
	push and up to ``_POOL_CONVERGE_DEADLINE_S`` of convergence polling - minutes,
	most of it spent in ``time.sleep`` and HTTP. Every Jarvis Settings write that
	commits anywhere in that window (the SPA's readiness POST stamping
	``chat_was_ready_at``, its sync poller converging a pending apply, the */5
	reconcile, the apply-operation poll folding in probe verdicts) makes the
	worker's own terminal write fail. Calling this before each probe shrinks the
	exposure from "the whole job" to "one HTTP round trip".

	Gated on ``_owns_transaction`` for the reason that predicate exists: ending a
	transaction we did not start is not ours to do. It also happens to keep
	FrappeTestCase isolation intact, and a single-connection context has no
	competing writer to lose to, so skipping the refresh there costs nothing."""
	if _owns_transaction():
		frappe.db.commit()


def _write_settings_fields(settings, fields: dict) -> bool:
	"""Write status/marker fields onto Jarvis Settings so that a concurrent writer
	cannot turn the write into a customer-visible failure (#713). Returns whether
	it landed.

	IT IS THE PRE-READ THAT FAILED LIVE, not the write. On the first ``db_set`` of a
	freshly loaded doc, ``Document.load_doc_before_save`` runs

	    SELECT field,value FROM tabSingles WHERE doctype='Jarvis Settings' FOR UPDATE

	which locks EVERY Jarvis Settings row - 100 of them on a live workspace - to
	write three. Under snapshot isolation that makes a status stamp conflict with a
	concurrent write to any unrelated field, which is exactly what happened: a
	readiness marker killed a sync. ``frappe.db.set_single_value`` is what ``db_set``
	delegates the actual write to, so going straight to it drops the pre-read and
	narrows the conflict surface to the fields actually being written. Nothing is
	lost by skipping ``db_set``: the only other things it does are ``before_change``
	/ ``on_change``, and neither this controller nor ``hooks.doc_events`` binds
	either for this doctype (the Jarvis Triggers wildcard covers ``on_update``, not
	``on_change``). The in-memory doc is still updated so callers reading it back
	see their own write.

	The residual same-field conflict is REPLAYED. Safe because every field written
	through here is an ABSOLUTE value - a status string, a timestamp, an apply
	marker - computed from an outcome already in hand, never a read-modify-write of
	what is in the row. Replaying one cannot lose an interleaved update the way a
	compare-and-set would; the last writer of a given outcome wins, which is what
	last_sync_status has always meant.

	THE ROLLBACK BEFORE EACH REPLAY IS THE POINT, not tidying up. ER_CHECKREAD
	fails BECAUSE our snapshot is stale, so a retry on that same snapshot fails
	identically forever. Ending the transaction is what makes the next attempt read
	the present. It discards nothing: MariaDB aborts the transaction itself on
	1020/1213, which is directly visible in the #713 trace - the try/finally
	backstop's own db_set ran 2ms after the failure and SUCCEEDED, which is only
	possible on a read view the server had already replaced. (jarvis-admin-v2 PR
	#264, for its issue #263, reasoned the other way - that ER_CHECKREAD is
	statement-level and leaves the transaction usable - and deliberately did not
	roll back; its recovery path commits first, so it gets a fresh snapshot
	regardless.)

	AND THE REPLAY ONLY RUNS WHERE THE TRANSACTION IS OURS TO END
	(``_owns_transaction``). Elsewhere the conflict is reported, not replayed: a
	1020 or 1213 has already destroyed the transaction server-side, so rolling back
	and quietly re-persisting our one field would publish OUR write while the
	caller's is gone. #713 happened in a background job, which owns its transaction
	outright, so scoping the replay there costs the fix nothing.

	Reported rather than RAISED, though, and the distinction is the point. Returning
	False lets a poller or a Resync click carry on and say "still applying", which is
	true and self-correcting; raising would put a transient race back in front of a
	customer, which is the entire complaint in #713 and would hit hardest in
	``_reconcile_pending_applying``, the highest-frequency caller of all. The one
	place a raise IS correct is nested inside a save (``_inside_a_save``), where the
	caller's other field writes have to fail with it.

	ALWAYS ``update_modified=False``, which is a real trade-off and not an
	oversight. Several of these writes previously took ``db_set``'s default and
	bumped ``modified``, so a Desk save of Jarvis Settings that straddled one of
	them was caught by ``check_if_latest`` and refused. That protection is given up
	deliberately: ``modified`` is the single most contended row in this doctype
	(every ``db_set`` in the app that does not opt out writes it), so stamping it
	from four writers already racing each other would reintroduce a good share of
	the conflict this function exists to remove. It also matches what the rest of
	the app already does for Jarvis Settings bookkeeping - the oauth, chat-device,
	suggestions, wiki-mirror and skills writers all pass
	``update_modified=False``."""
	import time as _time

	last_error: BaseException | None = None
	for attempt in range(_SETTINGS_WRITE_ATTEMPTS):
		try:
			frappe.db.set_single_value("Jarvis Settings", dict(fields), update_modified=False)
			settings.update(dict(fields))
			return True
		except Exception as e:
			# Three outcomes, and only the first is a raise. Nested in a save, the
			# caller's uncommitted work makes the failure theirs to own. Otherwise the
			# lost race is REPORTED, never raised - a poller or a Resync click has no
			# business turning it into a customer-visible error, which was the whole
			# of #713 - and the replay is added only where the transaction is ours.
			if not _is_write_conflict(e) or _inside_a_save():
				raise
			if not _owns_transaction():
				return False
			last_error = e
			if attempt < _SETTINGS_WRITE_ATTEMPTS - 1:
				frappe.db.rollback()
				_time.sleep(_SETTINGS_WRITE_BACKOFF_S * (attempt + 1))
	frappe.logger().warning(
		"jarvis_settings: settings write lost %d write-conflict races on fields %s (%s)",
		_SETTINGS_WRITE_ATTEMPTS,
		sorted(fields),
		last_error,
	)
	return False


def _stamp_converged_ok(settings, *, is_pool: bool) -> bool:
	"""Record a converged apply as a terminal success. last_sync_status keeps the
	literal "ok" prefix (the _pool_sync_is_redundant dedup gate + the onboarding
	poller both key off it); the durable evidence-of-successful-apply marker
	(llm_pool_synced_at for a pool tenant, llm_direct_synced_at for a single-model
	tenant — round-4 R4-P0-6) is is_ready_for_chat's first-activation gate and
	must never be set from mere intent or an "applying" 200. A converged apply is
	exactly the confirmation it wants: admin gates chat_readiness "Ready" on
	applied_version >= desired_version. Without the direct marker here, a first
	direct apply that converged via reconcile would flip "ok" yet strand the
	tenant at llm_provisioning forever (an "ok" status stops every reconcile).

	Returns whether the stamp landed. FIVE callers race on these same three rows -
	the in-band sync worker's convergence poll, the pool worker's, the */5
	``reconcile_pending_llm_sync``, the SPA's own ``get_llm_sync_status`` poller via
	``onboarding._reconcile_pending_applying``, and a customer's ``resync_llm``
	click - nearly all of them driven by the same event, admin flipping
	chat_readiness to Ready. Losing that race is ordinary and
	self-correcting (whoever won wrote the identical outcome, and every one of the
	five re-probes), so a lost stamp is reported as False and never raised: it is
	the caller's cue to leave the reconcile-owned pending state behind rather than
	a failure to put in front of a customer (#713)."""
	import frappe as _frappe

	now = _frappe.utils.now()
	fields = {
		"last_sync_at": now,
		"last_sync_status": "ok (converged via admin reconcile)",
	}
	if is_pool:
		fields["llm_pool_synced_at"] = now
	else:
		fields["llm_direct_synced_at"] = now
	if not _write_settings_fields(settings, fields):
		return False
	_commit_terminal_sync_status()
	return True


def _converge_via_admin(
	settings,
	*,
	is_pool: bool,
	deadline_s: float = _POOL_CONVERGE_DEADLINE_S,
	interval_s: float = _POOL_CONVERGE_INTERVAL_S,
) -> bool:
	"""Poll admin get_connection until chat_readiness == "Ready" (bounded by
	deadline_s). On Ready: stamp the terminal success markers and return True. On
	deadline - or on a stamp that lost a write-conflict race - return False so the
	caller records the pending state for the scheduled safety net to finish. Under
	frappe.flags.in_test the loop probes exactly once (no sleep) so tests observe a
	single deterministic outcome.

	The snapshot refresh at the top of each pass is what keeps the stamp writable
	(#713). Everything this loop does between passes is HTTP and sleep, so holding
	the transaction open across it buys nothing and costs the stamp: it is exactly
	the window in which the SPA's readiness POST - reacting to the SAME Ready this
	loop is waiting for - commits its own Jarvis Settings write. Refreshing here
	means the stamp runs on a read view no older than the probe that decided to
	stamp."""
	import time as _time

	import frappe as _frappe

	deadline = _time.monotonic() + deadline_s
	while True:
		_refresh_db_snapshot()
		state, _reason = _admin_chat_readiness()
		if state == "Ready":
			return _stamp_converged_ok(settings, is_pool=is_pool)
		if _frappe.flags.in_test or _time.monotonic() + interval_s >= deadline:
			return False
		_time.sleep(interval_s)


def _commit_terminal_sync_status() -> None:
	"""Make a terminal last_sync_* write durable - ONLY in a real worker.

	Frappe's execute_job wraps every background job in "rollback on any
	exception", which silently reverted uncommitted terminal statuses back
	to 'pending:' when the rq SIGALRM fired (JARVIS-2026-07-08, fault c) -
	hence the explicit commit. But enqueue(now=True) (tests and the
	run_admin_sync_inline path) invokes the worker via frappe.call, OUTSIDE
	execute_job: there is no rollback to defeat there, and committing would
	break FrappeTestCase transaction isolation (leaking every fixture write
	of the calling test). frappe.local.job is set exclusively by
	execute_job, so it is the exact "real worker" signal to gate on.

	Also commits under frappe.flags.in_migrate: when Redis is down during
	a bench migrate, frappe.enqueue falls back to frappe.call - outside
	execute_job, so frappe.local.job is unset - yet a later exception in
	the same migrate transaction would roll the terminal write back.
	Committing mid-migrate is normal (the patch runner itself commits
	between patches)."""
	if getattr(frappe.local, "job", None) or frappe.flags.in_migrate:
		frappe.db.commit()


def _sync_lock_wait_s(retry_left: int) -> float:
	"""Lock wait for a sync attempt: short primary, longer retries."""
	return (
		ADMIN_SYNC_PRIMARY_LOCK_WAIT_S
		if retry_left >= ADMIN_SYNC_LOCK_RETRIES
		else ADMIN_SYNC_RETRY_LOCK_WAIT_S
	)


def _schedule_sync_lock_retry(*, method: str, job_base: str, retry_left: int, **enqueue_kwargs) -> None:
	"""Shared lock-loss retry scheduling for BOTH sync workers.

	One implementation so the retry-chain length, waits, status message,
	and job-id scheme can never silently diverge between the pool and the
	single-model paths (divergence re-arms the "stranded on failed:
	skipped" failure for whichever path missed the tuning).

	Writes the retry-pending status, then enqueues the next chain level:
	- Per-LEVEL job id: this still-running job holds its own id, so
	  reusing it would be dedup-dropped and the chain would stop.
	- Per-CHAIN random suffix: two independently-triggered chains can
	  reach the same level while the earlier one's job is still
	  queued/started; a level-only id would dedup-drop the newer chain's
	  retry, stranding its "pending: waiting..." status with no job
	  working on it. Uniqueness makes an occasional duplicate run instead
	  - harmless: workers re-read CURRENT settings and serialize on the
	  redis lock.
	"""
	settings = frappe.get_single("Jarvis Settings")
	_write_settings_fields(
		settings, {"last_sync_status": "pending: waiting for a concurrent sync to finish (will retry)"}
	)
	frappe.enqueue(
		method,
		queue="long",
		timeout=ADMIN_SYNC_RQ_TIMEOUT_S,
		job_id=f"{job_base}:retry:{retry_left - 1}:{frappe.generate_hash(length=6)}",
		deduplicate=True,
		retry_left=retry_left - 1,
		**enqueue_kwargs,
	)


class JarvisSettings(Document):
	def before_validate(self):
		"""Mirror models[0] into legacy fields BEFORE validate() runs.

		This ensures _validate_auth_mode_requirements sees fresh auth mode +
		api_key from the models table, not stale legacy fields.
		Only mirrors when models table has at least one enabled row.

		Note: llm_provider isn't needed by _validate_auth_mode_requirements;
		kept in db_set (in _on_update_unified_llm) to avoid in-memory drift.
		It is NOT mirrored here to avoid _validate_selects rejecting the pool
		model's internal provider ID (e.g. "openai_compat"). The db_set in
		_on_update_unified_llm bypasses validation and writes the internal ID.
		"""
		if not getattr(self, "models", None):
			return
		enabled = [m for m in self.models if m.enabled]
		if not enabled:
			return
		m0 = enabled[0]
		cred_type = (
			m0.credential_type
			if hasattr(m0, "credential_type")
			else (m0.get("credential_type") if hasattr(m0, "get") else "api_key")
		) or "api_key"
		# Mirror auth mode so _validate_auth_mode_requirements sees the right mode.
		self.llm_auth_mode = cred_type
		# Mirror api_key in-memory so _validate_auth_mode_requirements sees it.
		# The encrypted write happens in on_update.
		# Guard: if get_password raises (decrypt error on a previously saved row),
		# skip the mirror silently rather than crashing through save().
		if cred_type == "api_key":
			from jarvis.jarvis.pool_serialize import _get_password

			try:
				api_key_val = _get_password(m0, "api_key")
			except Exception:
				api_key_val = None  # leave prior encrypted value; skip mirror
			if api_key_val and not (
				getattr(self, "llm_api_key", None) and not self.is_dummy_password(self.llm_api_key or "")
			):
				self.llm_api_key = api_key_val

	def validate(self):
		# Detect a new llm_api_key before _save_passwords() masks it to '****'.
		current_key = getattr(self, "llm_api_key", None) or ""
		if not current_key or self.is_dummy_password(current_key):
			self.flags.llm_api_key_changed = False
		else:
			old = self.get_doc_before_save()
			old_key = (getattr(old, "llm_api_key", None) or "") if old else ""
			self.flags.llm_api_key_changed = current_key != old_key

		# Plain Select field - direct change comparison via has_value_changed.
		self.flags.llm_auth_mode_changed = bool(self.has_value_changed("llm_auth_mode"))

		# Pool change-detection snapshot (see _pool_state_snapshot). Captured
		# HERE - before _validate()'s _save_passwords masks freshly-typed child
		# row secrets to '*'*len(value) - because a new api_key of the SAME
		# LENGTH as the old one would otherwise mask to an identical string by
		# on_update time, and a real key rotation would compare as "unchanged"
		# (the pool sync would be skipped and the container would keep serving
		# the revoked key). At validate() time a freshly-typed key is still
		# plaintext, which never equals the stored mask, so any newly-supplied
		# secret reliably reads as a change.
		self.flags.pool_state_snapshot = self._pool_state_snapshot(self)

		self._validate_auth_mode_requirements()
		self._validate_pattern_window()
		self._validate_conversation_retention()
		self._validate_branding()

	def _validate_conversation_retention(self):
		"""Retention floor. The daily sweep frees idle chats' agent sessions
		past this many days, so a fumbled tiny value would mass-free on the very
		next cron (the batch cap only spreads that over days). 0 disables (keep
		sessions forever); otherwise require >= 7. Unset is left untouched -
		readers default it to 30 (Single defaults are not backfilled on migrate)."""
		raw = getattr(self, "conversation_retention_days", None)
		if raw in (None, ""):
			return
		days = frappe.utils.cint(raw)
		if days != 0 and days < 7:
			frappe.throw(
				"Reclaim idle chat memory after must be 0 (never) or at least 7 days.",
				frappe.ValidationError,
			)

	def _validate_branding(self):
		"""Whitelabel identity store-side guard. The name's per-turn injection
		is sanitized separately in turn_handler (trusted-bracket safety)."""
		name, logo, favicon = validate_branding_inputs(
			getattr(self, "agent_name", ""),
			getattr(self, "brand_logo", ""),
			getattr(self, "brand_favicon", ""),
		)
		self.agent_name = name
		self.brand_logo = logo
		self.brand_favicon = favicon

	def _validate_pattern_window(self):
		"""Behavioural-learning window must be at least 1 hour when enabled.

		Wrap-aware: start > end is legal and means the window crosses
		midnight (e.g. 23:00-03:00). start == end reads as zero-length,
		not 24 hours. Engine status fields (pattern_last_run_at etc.) are
		written via db_set(update_modified=False) and never pass through
		here or _classify_llm_change.
		"""
		if not frappe.utils.cint(getattr(self, "pattern_learning_enabled", 0)):
			return
		start = getattr(self, "pattern_window_start", None)
		end = getattr(self, "pattern_window_end", None)
		if not start or not end:
			frappe.throw(
				"Pattern learning requires both an analysis window start and end time.",
				frappe.ValidationError,
			)

		def seconds_of_day(value) -> int:
			# Time fields surface as "HH:MM:SS" strings or timedelta
			# depending on load path; get_time normalizes both.
			t = frappe.utils.get_time(str(value))
			return t.hour * 3600 + t.minute * 60 + t.second

		duration = (seconds_of_day(end) - seconds_of_day(start)) % (24 * 3600)
		if duration < 3600:
			frappe.throw(
				"The pattern learning analysis window must be at least 1 hour long "
				"(a start after the end is allowed - the window crosses midnight).",
				frappe.ValidationError,
			)

	def _validate_auth_mode_requirements(self):
		"""Each auth mode requires its own credential field.

		REV-1: oauth/subscription mode has no bench-side credential
		requirement - agent owns the credential blob on the container.

		Scope: this validates ONLY the legacy single-model DIRECT path (the
		flat ``llm_*`` fields, with no ``models`` rows and no ``preset``).
		When the models table or a preset is present the config is unified -
		``validate_models()`` (run in ``on_update``) owns credential
		validation, and the flat ``llm_*`` fields are only a derived mirror
		that ``before_validate`` populates for an ENABLED row. Re-checking that
		mirror here would race it and throw spuriously for a disabled-only
		table, a bare preset, or a ``models[0]`` decrypt error.

		For the legacy path, an unconfigured/pre-onboarding Settings (no model,
		no base_url, no connected oauth account) is skipped so unrelated saves
		(e.g. an early onboarding step touching an unrelated field) aren't
		blocked - even though ``llm_auth_mode`` DEFAULTS to ``api_key`` before anything is
		chosen. ``reset_onboarding`` leaves ``llm_provider`` at a default but
		clears model/base_url/key, so it correctly reads as unconfigured.
		"""
		# Unified config (any models rows or a preset) -> validate_models owns it.
		if getattr(self, "models", None) or getattr(self, "preset", None):
			return

		# Legacy direct path: only enforce once a real direct config exists.
		# llm_base_url covers custom-endpoint configs where llm_model is blank;
		# llm_oauth_connected_at is the canonical oauth signal (is_ready_for_chat
		# keys off it too), not the display-only llm_oauth_account_email.
		configured = bool(
			(getattr(self, "llm_model", None) or "")
			or (getattr(self, "llm_base_url", None) or "")
			or getattr(self, "llm_oauth_connected_at", None)
		)
		if not configured:
			return

		def is_password_set(fieldname: str) -> bool:
			in_memory = getattr(self, fieldname, None) or ""
			if in_memory and not self.is_dummy_password(in_memory):
				return True
			db_value = self.get_password(fieldname, raise_exception=False)
			return bool(db_value)

		mode = getattr(self, "llm_auth_mode", None) or "api_key"
		if mode == "api_key" and not is_password_set("llm_api_key"):
			frappe.throw(
				"API-key auth mode requires llm_api_key",
				frappe.ValidationError,
			)

	def _resolve_llm_secret_for_push(self) -> str:
		"""Return the bytes to push to agent's llm.key.

		REV-1: only api_key mode pushes a secret. Oauth mode's credentials
		live in the container's auth-profiles.json - pushed via the separate
		push_oauth_blob path, not through this resolver.
		"""
		return self.get_password("llm_api_key", raise_exception=False) or ""

	def on_update(self):
		# ------------------------------------------------------------------ #
		# Unified LLM path (2026-06-26): models table rows or preset present.
		# ------------------------------------------------------------------ #
		has_models = bool(getattr(self, "models", None))
		has_preset = bool(getattr(self, "preset", None))

		if has_models or has_preset:
			self._on_update_unified_llm()
			return

		# ------------------------------------------------------------------ #
		# Back-compat (legacy path): no models rows, no preset.
		# Runs the existing single-model classify/sync path unchanged.
		# Reset any stale proxy flags so UI/workers don't think it's in
		# pool mode (handles the proxy→direct transition when all models
		# are removed).
		# ------------------------------------------------------------------ #
		self.db_set("proxy_active", 0, update_modified=False)
		self.db_set("proxy_recommended", 0, update_modified=False)
		self._on_update_single_model_legacy()

	def _on_update_unified_llm(self):
		"""New LLM path: validate → derive proxy_active/proxy_recommended →
		mirror models[0] into legacy fields → route to pool or single-model path.

		Runs when the models table has rows OR a preset is set.
		Validate fires BEFORE any mutation so that errors surface clean without
		partially applying state.
		"""
		from jarvis.jarvis.pool_serialize import (
			build_pool_payload,
			compute_pool_mode,
			compute_proxy_active,
			validate_models,
		)

		# Step 1: Validate first — clean error before any state mutation.
		errors = validate_models(self)
		if errors:
			frappe.throw("<br>".join(errors), title="LLM Configuration")

		# Step 2: Compute and persist derived flags (read-only, no modified bump).
		# pool_mode picks the SYNC LEG (/llm-pool vs /llm-creds); proxy_active is
		# the narrower "a Bifrost+cliproxy sidecar is deployed" and is persisted
		# for the chat/monitor surfaces. A BYO api-key pool is pool_mode WITHOUT
		# proxy_active — the fleet renders it agent-direct, no sidecar.
		pool_mode = compute_pool_mode(self)
		proxy_active = compute_proxy_active(self)
		enabled_models = [m for m in (self.models or []) if m.enabled]
		proxy_recommended = len(enabled_models) == 1 and not bool(getattr(self, "preset", None))
		self.db_set("proxy_active", 1 if proxy_active else 0, update_modified=False)
		self.db_set("proxy_recommended", 1 if proxy_recommended else 0, update_modified=False)

		# Step 3: Mirror models[0] into the read-only legacy fields so that
		# the chat worker + onboarding gate continue to read llm_model / llm_auth_mode
		# correctly in direct (single-model) mode.
		if enabled_models:
			m0 = enabled_models[0]
			cred_type = (
				m0.credential_type
				if hasattr(m0, "credential_type")
				else (m0.get("credential_type") if hasattr(m0, "get") else "api_key")
			) or "api_key"
			legacy_updates = {
				"llm_provider": (m0.provider if hasattr(m0, "provider") else m0.get("provider", "")) or "",
				"llm_model": (m0.model if hasattr(m0, "model") else m0.get("model", "")) or "",
				"llm_base_url": (m0.base_url if hasattr(m0, "base_url") else m0.get("base_url", "")) or "",
				"llm_auth_mode": cred_type,
			}
			for field, value in legacy_updates.items():
				self.db_set(field, value, update_modified=False)
			# Mirror api_key secret for api_key mode via the encrypted path.
			# IMPORTANT: db_set on a Password field writes PLAINTEXT into Singles
			# (it bypasses Frappe's __Auth encryption). Use set_encrypted_password
			# so the secret is stored in the __Auth table, never in plaintext.
			if cred_type == "api_key":
				from frappe.utils.password import set_encrypted_password

				from jarvis.jarvis.pool_serialize import _get_password

				api_key_val = _get_password(m0, "api_key")
				if api_key_val:
					set_encrypted_password(
						"Jarvis Settings",
						"Jarvis Settings",
						api_key_val,
						"llm_api_key",
					)
					# Mask in-memory so nothing downstream re-writes plaintext.
					self.llm_api_key = "*" * 10

		# Step 4: Route to pool or single-model path. Keyed on pool_mode, NOT
		# proxy_active: an agent-direct pool still has to be pushed as a whole
		# spec through /llm-pool, and pushing its models[0] through /llm-creds
		# instead would knock the container down to a single credential.
		if pool_mode:
			# Pool path: enqueue the admin call. The worker re-reads
			# Jarvis Settings at run time so no snapshot is needed here.
			# validate_models() already ran above (Step 1) so we know the
			# current config is clean before enqueuing.
			#
			# Diff gate (pool analog of _classify_llm_change): every save of
			# this Single lands here when pool_mode - including saves that
			# touch nothing pool-related (pattern-learning windows, chat-device
			# writes through save()) - and each one
			# re-POSTed the FULL pool spec + secrets to admin. Skip the
			# enqueue only when all three hold: a before-doc exists, the
			# pool-relevant snapshot is identical, and the last sync ended
			# "ok" (a failed sync must stay retryable by re-saving). When
			# skipping, last_sync_status is left untouched (no "pending:"
			# write - nothing was enqueued to complete it).
			if self.flags.get("suppress_pool_enqueue"):
				# save_llm_pool is pushing this pool SYNCHRONOUSLY (plan-05 D2,
				# sync_pool_now) so it can return the durable apply-operation
				# descriptor. Suppress the async enqueue here to avoid a second
				# container mutation; the caller does the same admin work + stamping
				# inline. Only the onboarding/settings save sets this flag - every
				# other write of this Single (pattern-learning, chat-device, ...) still
				# takes the async path below.
				frappe.logger().debug(
					"jarvis_settings: skipping async pool enqueue; caller pushes synchronously"
				)
			elif self._pool_sync_is_redundant():
				frappe.logger().debug(
					"jarvis_settings: skipping pool sync enqueue; pool state unchanged and last sync ok"
				)
			else:
				self._enqueue_pool_sync()
		else:
			# Single-model path (1 model, no preset): reset any stale proxy
			# flags so UI/workers don't think the tenant is still in pool mode.
			# (proxy_active/proxy_recommended were already written above in step 2,
			# but we explicitly reset here in case a tenant removed all models
			# and routed to the legacy path instead of the unified path.)
			self.db_set("proxy_active", 0, update_modified=False)
			self.db_set(
				"proxy_recommended",
				1 if (len(enabled_models) == 1) else 0,
				update_modified=False,
			)
			# A tenant LEAVING pool mode still has to be pushed through /llm-pool
			# one final time (#550). Dropping from 2 models to 1 flips pool_mode
			# off, so the save routes here, and _classify_llm_change compares only
			# the legacy mirror fields - which are mirrored from models[0] and are
			# UNCHANGED when the model that was removed was not the primary. It
			# returns "reload", which merely rotates the secret file: admin keeps
			# the old llm_pool_config, openclaw.json keeps declaring the removed
			# provider, and that model's llm_key_N.key stays on disk. The agent can
			# still fail over to a model the customer deleted, and the credential
			# they may have been trying to revoke is never revoked.
			#
			# Forcing "restart" would not be enough: /llm-creds re-renders
			# openclaw.json but never prunes llm_key_*.key, never rewrites
			# docker-compose.yml, and never tears the Bifrost/cliproxy sidecars
			# down. /llm-pool does all three, and llm_proxy.validate() accepts a
			# single-model spec (it rejects only an EMPTY pool), so the honest
			# convergence for a shrink is one final pool push carrying the reduced
			# spec.
			if self._is_leaving_pool_mode():
				self._enqueue_pool_sync(converge_teardown=True)
			else:
				# Single-model path: reuse the existing classify/enqueue path.
				# The legacy fields are now mirrored, so _classify_llm_change
				# will correctly see any structural change.
				self._on_update_single_model_legacy()

	def _is_leaving_pool_mode(self) -> bool:
		"""True when this save drops the tenant OUT of pool mode.

		Only meaningful on the single-model leg, where the caller has already
		established that ``compute_pool_mode(self)`` is False. A first-ever save
		has no before-doc and so cannot be leaving anything: it reads False and
		takes the ordinary single-model path.
		"""
		from jarvis.jarvis.pool_serialize import compute_pool_mode

		before = self.get_doc_before_save()
		if before is None:
			return False
		return bool(compute_pool_mode(before))

	@staticmethod
	def _pool_state_snapshot(doc) -> tuple:
		"""Comparable snapshot of the pool-RELEVANT state of a settings doc.

		Covers exactly the inputs that feed the admin pool push:
		``preset`` + ``routing_mode`` (read by compute_pool_mode /
		build_pool_payload) and, per models[] child row, every field
		build_pool_payload serializes: provider, model, base_url, tier,
		order, credential_type, enabled, rotation, plus the two row
		secrets - api_key and subscription_accounts (the JSON string
		holding account_ref/upstream/label/oauth_blob per account).
		Timestamps/metadata (modified, name, idx) are deliberately
		excluded so a no-op re-save compares equal.

		Secrets are compared BY VALUE AS STORED on the row - an untouched
		DB-loaded row carries the '*'-mask, so mask == mask reads as
		unchanged, while a freshly-typed plaintext secret differs from any
		mask and reads as changed (see the validate() comment for why the
		current doc's snapshot must be captured pre-masking). They are
		sha256-digested into the snapshot rather than embedded raw so a
		stray log/repr of doc.flags can never leak a live credential.
		"""
		import hashlib

		def _get(row, field):
			if hasattr(row, "get"):
				return row.get(field)
			return getattr(row, field, None)

		def _digest(value) -> str:
			value = value or ""
			if not value:
				return ""
			return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

		rows = []
		for m in doc.get("models") or []:
			rows.append(
				(
					(_get(m, "provider") or ""),
					(_get(m, "model") or ""),
					(_get(m, "base_url") or ""),
					(_get(m, "tier") or "strong"),
					int(_get(m, "order") or 0),
					(_get(m, "credential_type") or "api_key"),
					1 if _get(m, "enabled") else 0,
					(_get(m, "rotation") or ""),
					_digest(_get(m, "api_key")),
					_digest(_get(m, "subscription_accounts")),
				)
			)
		return (
			(doc.get("preset") or ""),
			(doc.get("routing_mode") or ""),
			tuple(rows),
		)

	def _pool_sync_is_redundant(self) -> bool:
		"""True iff this save changes nothing the pool push would transmit
		AND the container is already in a known-good state.

		Skip conditions (ALL must hold; anything unknown falls through to
		"not redundant" so the sync always errs toward firing):
		- no caller-forced sync (flags.force_admin_sync - the same
		  save_llm_creds(force=True) override the legacy diff gate honors),
		- a doc_before_save exists (first-ever save always syncs),
		- validate() captured a snapshot for the current doc (a save path
		  that skipped validate - flags.ignore_validate - always syncs),
		- the snapshots compare equal,
		- last_sync_status starts with "ok": a prior failed/pending/skipped
		  sync means the container may not hold the current pool, so an
		  unchanged re-save is the operator's retry lever and must enqueue.
		"""
		if self.flags.get("force_admin_sync"):
			return False
		before = self.get_doc_before_save()
		if before is None:
			return False
		current = self.flags.get("pool_state_snapshot")
		if current is None:
			return False
		if current != self._pool_state_snapshot(before):
			return False
		return (self.get("last_sync_status") or "").startswith("ok")

	def _enqueue_pool_sync(
		self, *, converge_teardown: bool = False, idempotency_key: str | None = None
	) -> None:
		"""Enqueue the pool-sync admin call for the proxy path.

		Mirrors the existing ``on_update`` enqueue pattern:
		- Writes a ``pending:`` status synchronously so the UI can render
		  "provisioning..." immediately.
		- Runs inline under ``frappe.flags.in_test`` so tests see the final
		  status without polling.
		- Uses a stable ``job_id`` + ``deduplicate=True`` so two close-together
		  saves coalesce into one worker invocation.
		- The worker re-reads Jarvis Settings at run time (no snapshot args),
		  so a correction saved while the first job is still queued is
		  naturally included when the job eventually executes.
		- Admin errors are caught and written to ``last_sync_status``; the
		  save is never aborted on an admin failure.

		``idempotency_key`` (plan-05 D2, F2/F3): when the synchronous
		descriptor-obtain (``sync_pool_now``) hands the long push/converge back to
		this worker, it passes the SAME key so the worker's admin call dedupes to the
		operation already created - it converges + stamps the markers WITHOUT driving
		a second push.
		"""
		_write_settings_fields(self, {"last_sync_status": "pending: provisioning container (pool)"})
		run_inline = bool(frappe.flags.in_test or frappe.flags.run_admin_sync_inline)
		# Budget rationale lives on ADMIN_SYNC_RQ_TIMEOUT_S.
		frappe.enqueue(
			"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_sync_via_admin_pool",
			queue="long",
			timeout=ADMIN_SYNC_RQ_TIMEOUT_S,
			enqueue_after_commit=not run_inline,
			now=run_inline,
			# A teardown push carries its own job id. Sharing the ordinary one
			# would let dedup drop it behind an already-queued normal sync, and
			# the surviving job would run WITHOUT converge_teardown, hit the
			# pool-mode gate, and skip - silently restoring the #550 bug. The two
			# still serialize on the redis lock, so an occasional extra run is
			# harmless.
			job_id="jarvis_settings_sync:pool:teardown" if converge_teardown else "jarvis_settings_sync:pool",
			deduplicate=True,
			converge_teardown=converge_teardown,
			idempotency_key=idempotency_key,
		)

	def _on_update_single_model_legacy(self):
		"""The existing single-model on_update logic, extracted for reuse."""
		action = self._classify_llm_change()
		if action is None:
			return
		# Async path (2026-06-09): a container restart can take 30-60s on
		# the admin side waiting for healthz to come back up. Blocking the
		# save call for that long stalls the onboarding UI and feels
		# broken. Instead, mark the status as "pending: ..." synchronously
		# so the UI can render a "provisioning..." state, then enqueue the
		# real admin call on the long queue. The UI polls
		# ``onboarding.get_llm_sync_status`` until the status flips from
		# ``pending:`` to ``ok ...`` or ``failed: ...``.
		pending_label = (
			"pending: provisioning container" if action == "restart" else "pending: rotating credentials"
		)
		# Through the guarded writer like every other status write, so the rule is
		# uniform: a status write on THIS doctype never uses db_set, whether or not
		# the caller happens to be inside a save (#713).
		_write_settings_fields(self, {"last_sync_status": pending_label})
		# In tests, run inline so existing assertions on the final status
		# don't have to poll. Set ``frappe.flags.run_admin_sync_inline``
		# from app code that needs the synchronous behavior (rare).
		run_inline = bool(frappe.flags.in_test or frappe.flags.run_admin_sync_inline)
		# Coalesce duplicate close-together saves: enqueue under a fixed
		# job_id keyed by the action. Two saves that produce the same
		# action within the worker-poll window resolve to one job. The
		# worker re-reads the doc fresh so it always sees the latest
		# committed state, not whatever was in flight when each save
		# fired. Different actions still both enqueue (one "reload" and
		# one "restart" are not the same op) but the in-worker Redis
		# lock makes them run serially, not interleaved.
		# Budget rationale lives on ADMIN_SYNC_RQ_TIMEOUT_S.
		frappe.enqueue(
			"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_sync_via_admin",
			queue="long",
			timeout=ADMIN_SYNC_RQ_TIMEOUT_S,
			enqueue_after_commit=not run_inline,
			now=run_inline,
			job_id=f"jarvis_settings_sync:{action}",
			deduplicate=True,
			action=action,
		)

	def _sync_via_admin(self, action: str) -> None:
		"""Prod path: route LLM creds through admin → fleet → agent container.

		``action`` is the classifier output:
		- "reload" calls post_rotate_llm_secret (hot-rotate /secrets/llm.key
		  for api-key rotation; no restart).
		- "restart" calls post_update_llm_creds (re-render openclaw.json
		  and restart container) - used for mode switches and
		  provider/model/base_url changes.

		Sprint-3 (2026-06-16 review): the previous shape silently swallowed
		AdminRateLimitedError (logged only; last_sync_status stayed at
		"pending: ..." forever). The UI poller spins on that, never showing
		the user a state they can act on. Now the rate-limit branch ALSO
		writes a terminal failure status with the admin-provided
		retry_after_seconds hint so the UI can render a retry timer.

		Additionally a try/finally backstop guarantees last_sync_status
		never stays at "pending: ..." on an unexpected exception path -
		the UI poller flips off pending no matter what blew up.

		#713: that backstop was reporting a LOST RACE as "failed: unexpected
		error; see Error Log". Every status write here goes through
		``_write_settings_fields`` instead, and a write conflict that survives
		its replays lands on the reconcile-owned pending marker rather than a
		terminal failure - see the QueryDeadlockError branch below.
		"""
		from jarvis import admin_client

		terminal_written = False
		try:
			if action == "reload":
				secret = self._resolve_llm_secret_for_push()
				result = admin_client.post_rotate_llm_secret(secret=secret) or {}
				resolved_action = result.get("action", "reload")
			else:  # "restart"
				# In oauth mode the api_key body is empty - container reads
				# credentials from auth-profiles.json instead.
				secret = self._resolve_llm_secret_for_push()
				result = (
					admin_client.post_update_llm_creds(
						provider=self.llm_provider or "",
						model=self.llm_model or "",
						base_url=self.llm_base_url or "",
						api_key=secret,
						auth_mode=creds_wire_auth_mode(self.llm_auth_mode),
					)
					or {}
				)
				# The payload carried installed_apps; admin persisted it
				# desired-first, so stamp even if the apply is converging.
				from jarvis.installed_apps_sync import record_synced_snapshot

				record_synced_snapshot()
				resolved_action = result.get("action", "restart")
			# The push is the long part of this job, and until now every status
			# write below still ran on the snapshot taken before it (#713). Ending
			# the transaction here makes the apps snapshot above durable and gives
			# the writes that follow a read view minutes younger than the job.
			_refresh_db_snapshot()
			# C5/F2 + round-4 R4-P0-6 — CONVERGENCE, not HTTP success: a sync may
			# come back accepted-but-still-converging. Admin deliberately returns
			# 200 with status="applying" on a busy apply lock / fleet read-timeout /
			# applied-version CAS refusal — the container is NOT yet on the new
			# creds. Treat anything but a demonstrable "applied" as PENDING:
			# converge via get_connection (is_pool=False — stamps the direct
			# llm_direct_synced_at marker on Ready), else record pending for the
			# */5 reconcile / onboarding poller to finish. This gates BOTH actions:
			# since round-4 the admin's rotate path also returns status="applying"
			# when a newer generation raced the rotation, so "reload" is no longer
			# applying-free. A missing status defaults to "applied" (an admin too
			# old to thread it predates this contract).
			if _is_applying_result(result) or (result.get("status") or "applied") != "applied":
				if not _converge_via_admin(self, is_pool=False):
					_write_settings_fields(self, {"last_sync_status": _PENDING_APPLYING_STATUS})
					_commit_terminal_sync_status()
				terminal_written = True
				return
			applied_ok = _write_settings_fields(
				self,
				{
					"last_sync_at": frappe.utils.now(),
					"last_sync_status": f"ok ({resolved_action} via admin)",
					# Durable "a direct config has been CONFIRMED-applied at least once"
					# marker — stamped ONLY on status=applied (R4-P0-6 / P1-10). A first
					# direct activation is gated on this so local key/provider/model
					# presence alone can no longer open chat on an unconfirmed apply.
					"llm_direct_synced_at": frappe.utils.now(),
				},
			)
			if not applied_ok:
				# The apply is CONFIRMED and only the record of it lost a race, so
				# the recoverable pending state is the honest thing to leave behind:
				# it names something outstanding (write down an apply that happened)
				# that three converging paths can finish, and it never reports the
				# workspace as broken (#713).
				_write_settings_fields(self, {"last_sync_status": _PENDING_APPLYING_STATUS})
			# Commit EVERY terminal status (ok and each failed branch), not
			# just the finally-backstop: the rq SIGALRM can fire at any
			# later point in this job (log_error, lock release, skills
			# resync), and execute_job's rollback would silently revert an
			# uncommitted terminal write back to "pending:" - the stuck
			# status this whole block exists to prevent.
			_commit_terminal_sync_status()
			terminal_written = True
			# A "restart" means the container may be freshly (re)provisioned
			# (rebind / reboot recovery / image upgrade) with an EMPTY
			# custom_skills/ AND learned_skills/. Re-push the customer's custom
			# skills and the compiled learned skills so a rebuilt container
			# repopulates them from the DB - no manual re-save needed.
			if action == "restart":
				self._resync_custom_skills_after_restart()
				self._resync_learned_skills_after_restart()
		except admin_client.AdminAuthError as e:
			_write_settings_fields(
				self,
				{
					"last_sync_at": frappe.utils.now(),
					"last_sync_status": f"failed: auth: {e}",
				},
			)
			_commit_terminal_sync_status()
			terminal_written = True
			frappe.log_error(
				title="Jarvis: admin auth failed",
				message=frappe.get_traceback(),
			)
		except admin_client.AdminRejectedError as e:
			# jarvis #542: admin was REACHED and permanently refused this config
			# (an unknown provider slug, an unusable spec). The F2 handling below
			# is right for a timeout - admin persists desired-first and reconciles
			# a late apply - but its premise is false here: admin threw during
			# validation and stored NOTHING, so there is no desired state for the
			# */5 reconcile to converge to and "pending:" would never resolve.
			# The customer sat on "Applying your changes" forever while admin
			# already knew the exact reason. Terminal, carrying that reason, like
			# the neighbouring auth/rate-limit branches.
			reason = _admin_rejection_reason(e)
			_write_settings_fields(
				self,
				{
					"last_sync_at": frappe.utils.now(),
					"last_sync_status": f"failed: {reason}",
				},
			)
			_commit_terminal_sync_status()
			terminal_written = True
			frappe.log_error(
				title="Jarvis: admin rejected the LLM config",
				message=frappe.get_traceback(),
			)
		except admin_client.AdminUnreachableError as e:
			# F2: for a "restart" (creds re-render), an unreachable/timeout is an
			# apply the admin persisted desired-first and will reconcile - not a
			# lost change. Converge via get_connection and record PENDING (not
			# failed) when it hasn't landed yet, mirroring the pool path. "reload"
			# (hot secret rotation) is a fast, non-desired-first op with no
			# reconcile, so an unreachable there stays terminal-failed as before.
			if action == "restart":
				if not _converge_via_admin(self, is_pool=False):
					_write_settings_fields(self, {"last_sync_status": _PENDING_APPLYING_STATUS})
					_commit_terminal_sync_status()
					frappe.logger().warning(
						"jarvis_settings: creds sync admin-unreachable; recorded pending for reconcile (%s)",
						e,
					)
				terminal_written = True
			else:
				_write_settings_fields(
					self,
					{
						"last_sync_at": frappe.utils.now(),
						"last_sync_status": f"failed: admin unreachable: {e}",
					},
				)
				_commit_terminal_sync_status()
				terminal_written = True
				frappe.log_error(
					title="Jarvis: admin unreachable",
					message=frappe.get_traceback(),
				)
		except admin_client.AdminRateLimitedError as e:
			retry = e.retry_after_seconds or 0
			retry_str = f"retry_after={retry}s" if retry > 0 else "retry shortly"
			_write_settings_fields(
				self,
				{
					"last_sync_at": frappe.utils.now(),
					"last_sync_status": f"failed: rate-limited; {retry_str}",
				},
			)
			_commit_terminal_sync_status()
			terminal_written = True
			frappe.logger().info(f"admin_client: rate-limited; retry_after={retry}s")
		except _WRITE_CONFLICT_ERRORS:
			# #713, a BACKSTOP rather than the primary path: the status writes above
			# all go through _write_settings_fields, which reports a lost race instead
			# of raising, so what reaches here is a conflict from some OTHER statement
			# in this body. Handled the same way, because the reasoning does not depend
			# on which statement lost. A lost write-conflict race is NOT a failed sync,
			# and it must never read like one: the admin call succeeded, the container has the
			# config, and the only thing that did not happen is this bench writing
			# down that it did. The customer's workspace was reported broken for a
			# race between two writers that had both just been told everything was
			# fine.
			#
			# The pending marker is the RECOVERY, not a softer wording. It is the one
			# status three independent converging paths act on - the SPA's own
			# get_llm_sync_status poller (seconds), the */5 reconcile, and a Resync
			# click - and a "failed:" here reaches NONE of them once
			# llm_direct_synced_at is already set, which is exactly how the workspace
			# in #713 was left with no way forward. Swallowed rather than re-raised
			# for the same reason: the state is recoverable and self-correcting, and
			# a raise would only hand execute_job an exception to log and re-log.
			_write_settings_fields(self, {"last_sync_status": _PENDING_APPLYING_STATUS})
			_commit_terminal_sync_status()
			terminal_written = True
			frappe.log_error(
				title="Jarvis: LLM sync lost a write-conflict race (recorded pending)",
				message=frappe.get_traceback(),
			)
		finally:
			# Final backstop: if a non-Admin* exception path blew through
			# (network exception class admin_client doesn't translate,
			# rq JobTimeoutException, programmer error, etc.) the status
			# would otherwise stay 'pending: ...' indefinitely. Flip it to
			# a terminal failure so the UI poller stops spinning.
			#
			# The commit is load-bearing (JARVIS-2026-07-08, fault c): the
			# exception keeps propagating after this finally, and Frappe's
			# execute_job catches it with frappe.db.rollback() - an
			# UNcommitted status write here is silently undone and the
			# status sticks at "pending:" forever. Committing makes the
			# terminal write durable before the rollback runs.
			#
			# #713 narrowed what reaches this branch rather than reworking it: a
			# write conflict now has its own handler above, so "unexpected error"
			# once again means an error nobody anticipated, which is the only thing
			# that wording can honestly describe. The write itself goes through
			# _write_settings_fields because the backstop is the LAST chance to move
			# the status off "pending:", and losing it to the same conflict class
			# that brought us here is how a status gets stuck.
			if not terminal_written:
				try:
					_write_settings_fields(
						self,
						{
							"last_sync_at": frappe.utils.now(),
							"last_sync_status": "failed: unexpected error; see Error Log",
						},
					)
					_commit_terminal_sync_status()
				except Exception:
					# If even the status write fails, swallow - we're
					# already in an error path and re-raising would mask
					# the real exception.
					pass

	def _resync_custom_skills_after_restart(self) -> None:
		"""Re-push the customer's custom skills to a (re)provisioned container.

		On a container rebuild the per-container ``custom_skills/`` is empty, so
		the durable ``Jarvis Custom Skill`` rows must be re-pushed. Enqueued (the
		same deduped job the SPA "save" uses) so it runs after this restart and
		does its own container restart. No-op when there are no custom skills, so
		customers without skills never pay an extra restart.
		"""
		try:
			if not frappe.db.count("Jarvis Custom Skill"):
				return
			frappe.db.set_single_value(
				"Jarvis Settings",
				"custom_skills_sync_status",
				"pending: applying skills",
				update_modified=False,
			)
			frappe.enqueue(
				"jarvis.chat.custom_skills_api._enqueued_push_custom_skills",
				queue="long",
				timeout=180,
				job_id="jarvis_custom_skills_push",
				deduplicate=True,
			)
		except Exception:
			frappe.log_error(
				title="Jarvis: custom-skills resync after restart failed",
				message=frappe.get_traceback(),
			)

	def _resync_learned_skills_after_restart(self) -> None:
		"""Re-push the compiled learned skills to a (re)provisioned container.

		The learned-namespace sibling of ``_resync_custom_skills_after_restart``
		(Behavioural Pattern Learning Phase 2): on a container rebuild the
		per-container ``learned_skills/`` is empty, so the managed
		``Jarvis Custom Skill`` rows (``managed_by_learning=1`` - the durable
		bench-side storage) must be re-pushed through the dedicated learned
		chain. Enqueued (the same deduped job Apply uses) so it runs after this
		restart and does its own container restart. No-op when there are no
		managed rows, so customers without learned skills never pay an extra
		restart.
		"""
		try:
			if not frappe.db.count("Jarvis Custom Skill", {"managed_by_learning": 1}):
				return
			frappe.db.set_single_value(
				"Jarvis Settings",
				"learned_skills_sync_status",
				"pending: applying learned skills",
				update_modified=False,
			)
			frappe.enqueue(
				"jarvis.chat.learned_skills_api._enqueued_push_learned_skills",
				queue="long",
				timeout=180,
				job_id="jarvis_learned_skills_push",
				deduplicate=True,
			)
		except Exception:
			frappe.log_error(
				title="Jarvis: learned-skills resync after restart failed",
				message=frappe.get_traceback(),
			)

	def _classify_llm_change(self) -> str | None:
		"""Return one of: None | 'reload' | 'restart'.

		- None: no LLM field changed; no action needed (in oauth mode this
		  is the common case - agent owns refresh).
		- 'reload': api_key rotation only; hot-reload via rotate-secret.
		- 'restart': structural change (mode switch, provider/model/base_url).

		``flags.force_admin_sync`` (set by save_llm_creds(force=True))
		overrides the no-diff gate and always returns 'restart' so the
		complete_paste_signin path can re-render openclaw.json + restart
		the container even when nothing structural changed on the bench.
		"""
		# Caller-forced sync (e.g. complete_paste_signin re-authorize):
		# bypass the diff gate so admin actually fires.
		if self.flags.get("force_admin_sync"):
			return "restart"
		# Structural triggers.
		if self.flags.get("llm_auth_mode_changed"):
			return "restart"

		old = self.get_doc_before_save()
		if old is None:
			# First-ever save: treat as restart only if at least one of
			# provider/model is set now.
			if any(getattr(self, f, None) for f in ("llm_provider", "llm_model", "llm_base_url")):
				return "restart"
			if getattr(self, "llm_api_key", None):
				return "reload"
			return None

		structural_fields = ("llm_provider", "llm_model", "llm_base_url")
		structural_changed = any(
			(getattr(self, f, None) or "") != (getattr(old, f, None) or "") for f in structural_fields
		)
		if structural_changed:
			return "restart"

		# Credential-only rotations - api_key only in REV-1. OAuth tokens
		# are agent-owned and don't trip the classifier.
		if self.flags.get("llm_api_key_changed"):
			return "reload"

		# F5: a re-save of IDENTICAL creds after a sync that DEMONSTRABLY did not
		# succeed is the customer's natural retry lever - it MUST re-run the full
		# render+restart apply, not no-op. Before this, an unchanged re-save
		# classified as None (nothing changed) even when the previous apply
		# failed/timed out, so the broken container was never re-applied and
		# onboarding could never recover by saving again. Mirror
		# _pool_sync_is_redundant's ok-gate, but only when there is a REAL prior
		# verdict to act on: a non-empty last_sync_status that is not "ok"
		# (failed:/pending:/skipped:). An EMPTY status is "never attempted" (the
		# first-ever save is handled by the old is None branch above; an unrelated
		# field save on a baseline pre-config must stay a genuine no-op), so it is
		# deliberately NOT forced. Only fires when there is real config to apply.
		last_status = self.get("last_sync_status") or ""
		if last_status and not last_status.startswith("ok"):
			configured = any(
				getattr(self, f, None) for f in ("llm_provider", "llm_model", "llm_base_url")
			) or bool(getattr(self, "llm_api_key", None))
			if configured:
				return "restart"

		return None


# Auto-retry transient pool-provisioning failures. The fleet-agent can 500
# ("admin unreachable: … agent_error: Internal Server Error") on a first cold
# provision that succeeds moments later (e.g. a sidecar not yet healthy within
# the health-poll window). A bounded retry self-heals the FAST hiccup so it never
# strands the customer at the Connect-AI step. Only AdminUnreachableError (the
# 502/agent_error/connection class) is retried; auth/validation are terminal.
#
# 2 (was 3): a genuine read-TIMEOUT surfaces as the same AdminUnreachableError,
# so retrying it 3x150s would storm the budget - and it no longer needs to, since
# an unreachable outcome now drains through the convergence poll (which absorbs a
# still-applying apply) rather than a blind re-POST. Two attempts keep the cheap
# fast-500 self-heal; the convergence loop + the */5 reconcile own everything
# slower. Keep this in lockstep with the budget arithmetic on ADMIN_SYNC_*.
_POOL_SYNC_RETRIES = 2
_POOL_SYNC_RETRY_DELAY_S = 5


def _cleared_subscription_status_fields() -> dict:
	"""Merge into a FAILED pool-worker db_set() dict so a stale
	subscription_status/warnings pair from a PRIOR successful apply can't
	linger next to a `failed:` status the next poll reads. Never merged into
	the "ok (...)" success write, nor into a skip path where the container's
	last real apply is still the truth (the pre-enqueue redundant-sync skip,
	or the run-time "no longer pool-valid" skip - neither one touched the
	container, so whatever it's currently running is unchanged)."""
	return {
		"last_subscription_status": "",
		"last_sync_warnings": "[]",
		"last_model_statuses": "[]",
	}


# admin_client wraps EVERY non-2xx/4xx admin response in the SAME
# AdminUnreachableError class - a genuine network timeout, an admin-side 500
# traceback, AND a DEFINITIVE, already-decided rejection (e.g. fleet's hard
# gate on a subscription-only pool's first activation, which rolls back and
# NEVER records applied - see jarvis_admin_v2's ProvisionError raise) all
# surface identically here. The first two are exactly what the F2 "converge,
# don't fail" handling below exists for; the third is not "maybe still
# landing" at all - there is nothing to converge to until the customer's own
# account changes (e.g. its usage limit resets), so treating it as pending
# would strand the customer in a spinner despite admin already knowing the
# real, customer-facing reason (the 2026-07-23 out-of-quota trace).
#
# There is no extra structured field on this wire to key off (the customer
# bench only ever sees the flattened string admin_client assembles), so this
# recognises admin's OWN sentence convention instead: jarvis_admin_v2.fleet.
# pool._pool_route_reason / _quota_exhausted_sentence are the only places
# admin deliberately writes second-person customer prose, and both always
# read "Your <something> ..." - every other message this exception class
# carries (a network error, a raw diagnostic, an internal 500) reads as
# technical text and never takes this shape.
_ADMIN_WRAPPED_ERROR_RE = re.compile(r"^admin returned an? \d+ error: (.*)$", re.S)


def _admin_customer_facing_reason(message: str) -> str:
	"""Extract admin's own customer-facing sentence out of an
	AdminUnreachableError's message, or "" when the message does not look
	like one (a network failure, a generic diagnostic, ...). See the module
	note above _ADMIN_WRAPPED_ERROR_RE for why this text-shape check is the
	only signal available."""
	text = (message or "").strip()
	wrapped = _ADMIN_WRAPPED_ERROR_RE.match(text)
	if wrapped:
		text = wrapped.group(1).strip()
	if text.startswith("Your ") and text.endswith("."):
		return text
	return ""


def _admin_rejection_reason(e) -> str:
	"""The customer-facing reason to record for an AdminRejectedError.

	Admin sometimes writes the sentence itself (fleet's "Your ..." convention -
	see _admin_customer_facing_reason above); that is already prose aimed at a
	customer, so it passes straight through and reads identically to the
	quota-exhausted case the pool path has recorded since 2026-07-23.

	Everything else is a raw diagnostic written for an engineer ("unknown
	llm_provider: 'gemini'"). It is still the single most useful thing the
	customer can be told, so it IS surfaced verbatim - but behind a short
	lead-in of our own, never standing alone as though Jarvis had phrased it.
	Same shape the AI-models list uses for a probe's contract-1.12 ``detail``
	("Not working: <detail>"), so the two upstream-reason surfaces read alike.
	"""
	detail = (getattr(e, "detail", "") or "").strip()
	if not detail:
		# No structured detail (a hand-built error, or a raise site added later
		# that forgot it): fall back to the message with admin_client's "admin
		# returned a NNN error: " wrapper stripped, so the plumbing never shows.
		wrapped = _ADMIN_WRAPPED_ERROR_RE.match(str(e).strip())
		detail = (wrapped.group(1) if wrapped else str(e)).strip()
	sentence = _admin_customer_facing_reason(detail)
	if sentence:
		return sentence
	if not detail:
		return "Your AI configuration was rejected"
	return f"Your AI configuration was rejected: {detail}"


def _pool_spec_pushable(settings, converge_teardown: bool = False) -> bool:
	"""True when this settings doc may be pushed through the /llm-pool leg.

	Normally that means it IS a pool (``compute_pool_mode``). A
	``converge_teardown`` job is the deliberate exception: it was enqueued
	PRECISELY BECAUSE the tenant left pool mode (#550), so compute_pool_mode is
	expected to be False and gating on it would skip the teardown that is the
	whole point of the job. Such a push still needs a non-empty spec, since
	``llm_proxy.validate`` rejects an empty pool, so at least one enabled model
	is required either way.
	"""
	from jarvis.jarvis.pool_serialize import compute_pool_mode

	if compute_pool_mode(settings):
		return True
	if not converge_teardown:
		return False
	return any(m.enabled for m in (settings.models or []))


def _stamp_pool_applied_ok(settings, result: dict) -> bool:
	"""Record a CONFIRMED pool apply (status=applied) as a terminal success.
	Returns whether the stamp landed (#713: a lost write-conflict race leaves the
	caller to record the reconcile-owned pending state instead).

	Extracted so the async pool-sync worker (``_enqueued_sync_via_admin_pool``)
	and the synchronous onboarding/settings push (``sync_pool_now``) stamp the
	SAME markers - most importantly ``llm_pool_synced_at``, which is
	``is_ready_for_chat``'s first-activation gate for a pool tenant (R4-P0-6):
	proxy_active alone is config INTENT and must never read as provisioning
	success. ``last_sync_status`` keeps the literal "ok" prefix (the
	``_pool_sync_is_redundant`` dedup gate + the settings poller both key off it).

	A NO-OP apply (contract 1.10 ``unchanged: true``) ran no probe by design, so
	the fleet reports subscription_status "unchecked" / warnings [] - persisting
	those would DISCARD the last real apply's verdict (a healthy "verified" decays
	to "unchecked", a genuine model_unreachable warning gets cleared). Nothing on
	the running pool changed, so the prior verdict still describes it: leave those
	fields alone on an unchanged apply.
	"""
	import frappe as _frappe

	resolved_action = result.get("action", "pool_update")
	_synced = {
		"last_sync_at": _frappe.utils.now(),
		"last_sync_status": f"ok ({resolved_action} via admin)",
		"llm_pool_synced_at": _frappe.utils.now(),
	}
	if not result.get("unchanged"):
		_synced["last_subscription_status"] = str(result.get("subscription_status") or "")
		_synced["last_sync_warnings"] = _frappe.as_json(result.get("warnings") or [])
		# Per-model verdicts (contract 1.11/1.12) forwarded verbatim - the AI-models
		# list keys each api-key row's health off this.
		_synced["last_model_statuses"] = _frappe.as_json(result.get("model_statuses") or [])
	if not _write_settings_fields(settings, _synced):
		return False
	# Commit every terminal write (matches _sync_via_admin) so a propagating
	# JobTimeoutException can't roll the terminal status back to "pending:", and so
	# llm_pool_synced_at is durable.
	_commit_terminal_sync_status()
	return True


def _post_pool_with_retry(spec, api_keys, oauth_blobs, idempotency_key=None):
	"""post_update_llm_pool, retrying only the transient AdminUnreachableError.
	Re-raises the last unreachable error after exhausting retries; other Admin*
	errors propagate immediately (not retried) - including AdminRejectedError,
	which IS an AdminUnreachableError subclass but names a spec admin already
	refused, so re-POSTing the identical payload can only be refused again.

	``idempotency_key`` (plan-05 D2): when threaded, admin dedupes a retry carrying
	the same key to the SAME durable apply operation, allocating no new desired
	version - so the unreachable-retry above resumes the existing operation rather
	than starting a second apply, and the descriptor comes back on the retry."""
	import time as _time

	import frappe as _frappe

	from jarvis import admin_client

	last = None
	for attempt in range(_POOL_SYNC_RETRIES):
		try:
			result = admin_client.post_update_llm_pool(
				spec=spec,
				api_keys=api_keys,
				oauth_blobs=oauth_blobs,
				idempotency_key=idempotency_key,
			)
			# Stamp ONLY when admin echoes installed_apps_persisted - an
			# older admin ignored the field and the signal is still stale.
			if isinstance(result, dict) and result.get("installed_apps_persisted"):
				from jarvis.installed_apps_sync import record_synced_snapshot

				record_synced_snapshot()
			return result
		except admin_client.AdminRejectedError:
			# Permanent: admin validated the spec and refused it. Burning the
			# second attempt (and its 5s sleep) buys a second identical refusal.
			raise
		except admin_client.AdminUnreachableError as e:
			last = e
			_frappe.logger().warning(
				f"jarvis_settings: pool sync unreachable (attempt {attempt + 1}/{_POOL_SYNC_RETRIES}): {e}"
			)
			if attempt < _POOL_SYNC_RETRIES - 1 and not _frappe.flags.in_test:
				_time.sleep(_POOL_SYNC_RETRY_DELAY_S)
	raise last


def _enqueued_sync_via_admin_pool(
	retry_left: int = ADMIN_SYNC_LOCK_RETRIES,
	converge_teardown: bool = False,
	idempotency_key: str | None = None,
) -> None:
	"""Background-queue wrapper for the proxy (pool) sync path.

	Re-reads Jarvis Settings at run time and rebuilds the pool payload via
	``build_pool_payload``. This means a correction saved while the first
	job is still queued is naturally included when the job eventually runs —
	the dedup (fixed job_id + deduplicate=True) drops the duplicate job but
	the single job that executes always sees the LATEST committed config.

	Mirrors the Redis-lock + error-handling pattern of ``_enqueued_sync_via_admin``
	so admin failures set last_sync_status (terminal) without aborting the save.

	Sprint-3 hardening (matching single-model path):
	- Redis lock prevents parallel pool + creds calls racing on the container.
	- AdminRateLimitedError writes a terminal failure with retry hint.
	- try/finally backstop ensures the status never sticks at "pending:".

	Apply-warning propagation (2026-07-10): the admin response to a
	successful apply also carries ``subscription_status`` and ``warnings``
	(e.g. a subscription credential that loaded but failed an upstream
	probe). Both are persisted alongside the "ok (...)" write into
	``last_subscription_status`` / ``last_sync_warnings`` and are CLEARED on
	every failed/skipped-on-retries-exhausted terminal write so a stale
	warning from a prior successful apply never lingers next to a
	"failed:" status. The run-time "no longer pool-valid" skip below
	leaves them untouched, like the pre-enqueue redundant-sync skip: the
	container itself was never touched, so its last real apply is still
	the truth.

	``retry_left``: losing the lock race must not strand a FRESH tenant on a
	terminal "failed: skipped" (their first pool apply would never happen and
	is_ready_for_chat would gate them out of chat indefinitely). Each loss
	re-enqueues a follow-up run under its own job_id with a longer lock wait,
	down a chain sized so the CUMULATIVE wait outlives even a dead holder's
	full lock TTL (see ADMIN_SYNC_LOCK_RETRIES); only the last loss is
	terminal.
	"""
	import frappe as _frappe

	from jarvis import admin_client
	from jarvis._redis_lock import redis_lock
	from jarvis.jarvis.pool_serialize import build_pool_payload

	with redis_lock(
		"jarvis_settings_admin_sync",
		# TTL must cover a healthy holder running to its rq SIGALRM - see
		# ADMIN_SYNC_LOCK_TIMEOUT_S. A 120s TTL under a 600s job would
		# expire mid-run and admit a concurrent container mutation.
		timeout_s=ADMIN_SYNC_LOCK_TIMEOUT_S,
		blocking_timeout_s=_sync_lock_wait_s(retry_left),
	) as acquired:
		if not acquired:
			settings = _frappe.get_single("Jarvis Settings")
			if retry_left > 0:
				_frappe.logger().warning(
					"jarvis_settings: pool admin sync lost the lock race; scheduling retry (%d left)",
					retry_left - 1,
				)
				_schedule_sync_lock_retry(
					method="jarvis.jarvis.doctype.jarvis_settings.jarvis_settings"
					"._enqueued_sync_via_admin_pool",
					job_base="jarvis_settings_sync:pool",
					retry_left=retry_left,
					# Carry the teardown intent down the retry chain: a level that
					# dropped it would re-arm the pool-mode gate and skip (#550).
					converge_teardown=converge_teardown,
					# Carry the operation's key so a lock-loss retry still dedupes to
					# the operation the synchronous descriptor-obtain created (F2/F3).
					idempotency_key=idempotency_key,
				)
				return
			_frappe.logger().warning(
				"jarvis_settings: skipping pool admin sync; "
				"another worker held the lock past blocking timeout (retries exhausted)",
			)
			# Terminal "failed:" write - clear any stale warnings/subscription_status
			# from a prior successful apply alongside it (see
			# _cleared_subscription_status_fields).
			_write_settings_fields(
				settings,
				{
					"last_sync_status": "failed: skipped (concurrent sync did not finish in time)",
					**_cleared_subscription_status_fields(),
				},
			)
			return

		# Re-read CURRENT settings at run time (not a snapshot from job args)
		# so a correction saved between enqueue and execution is included.
		settings = _frappe.get_single("Jarvis Settings")

		# Re-validate: the config may have changed between enqueue and run.
		# If it is no longer a pool, skip the push. (Pool MODE, not proxy_active:
		# a BYO api-key pool has no sidecar but is still pushed through /llm-pool.)
		from jarvis.jarvis.pool_serialize import validate_models

		revalidation_errors = validate_models(settings)
		if revalidation_errors or not _pool_spec_pushable(settings, converge_teardown):
			reason = "; ".join(revalidation_errors) if revalidation_errors else "no longer a pool"
			_write_settings_fields(
				settings, {"last_sync_status": f"skipped: no longer pool-valid after re-read ({reason})"}
			)
			return

		spec, api_keys, oauth_blobs = build_pool_payload(settings)

		terminal_written = False
		try:
			result = _post_pool_with_retry(spec, api_keys, oauth_blobs, idempotency_key=idempotency_key) or {}
			# The push is the long part of this job; end the transaction so every
			# status write below runs on a read view younger than it (#713).
			_refresh_db_snapshot()
			# CONVERGENCE STATUS, not HTTP success (C5/F2 + round-4 R4-P0-6).
			# Admin deliberately returns HTTP 200 with status="applying" when the
			# apply lock was busy, the fleet read timed out, or the applied-version
			# CAS refused — the container is NOT yet on the new pool — and status=
			# "blocked" when a subscription pool has no persisted OAuth blobs.
			# Stamping the durable "ever applied" marker (llm_pool_synced_at) on
			# those made is_ready_for_chat open chat on a container still running
			# the stub. "blocked" is terminal-failed: only the customer
			# re-authenticating fixes it, so no reconcile poll can converge it.
			# Anything else short of a demonstrable "applied" converges via
			# get_connection (the cheap in-job fast path — on Ready
			# _stamp_converged_ok sets the markers) or records the pending state
			# for the */5 reconcile to finish. A missing status (an admin too old
			# to thread it) defaults to "applied" — its own contract predates this.
			status = result.get("status") or "applied"
			if status == "blocked":
				_write_settings_fields(
					settings,
					{"last_sync_status": "failed: subscription needs re-authentication (blocked)"},
				)
				_commit_terminal_sync_status()
				terminal_written = True  # preserve this status past the finally backstop
				return
			if _is_applying_result(result) or status != "applied":
				if not _converge_via_admin(settings, is_pool=True):
					_write_settings_fields(settings, {"last_sync_status": _PENDING_APPLYING_STATUS})
					_commit_terminal_sync_status()
				terminal_written = True
				return
			# A stamp that lost a write-conflict race falls back to the pending
			# marker so the SPA poller / the */5 reconcile finish it, exactly as a
			# non-converged apply does (#713).
			if not _stamp_pool_applied_ok(settings, result):
				_write_settings_fields(settings, {"last_sync_status": _PENDING_APPLYING_STATUS})
				_commit_terminal_sync_status()
			terminal_written = True
		except admin_client.AdminAuthError as e:
			_write_settings_fields(
				settings,
				{
					"last_sync_at": _frappe.utils.now(),
					"last_sync_status": f"failed: auth: {e}",
					**_cleared_subscription_status_fields(),
				},
			)
			_commit_terminal_sync_status()
			terminal_written = True
			_frappe.log_error(
				title="Jarvis: admin auth failed (pool sync)",
				message=_frappe.get_traceback(),
			)
		except admin_client.AdminRejectedError as e:
			# jarvis #542, the structured half of the branch below: admin named
			# the refusal itself (an ``error.code`` on _PERMANENT_REJECTION_CODES,
			# e.g. FleetConfigError on an unknown provider slug) instead of only
			# implying it through a "Your ..." sentence. Nothing was persisted, so
			# no reconcile can ever finish it - terminal, with admin's reason.
			reason = _admin_rejection_reason(e)
			_write_settings_fields(
				settings,
				{
					"last_sync_at": _frappe.utils.now(),
					"last_sync_status": f"failed: {reason}",
					**_cleared_subscription_status_fields(),
				},
			)
			_commit_terminal_sync_status()
			terminal_written = True
			_frappe.log_error(
				title="Jarvis: admin rejected the LLM pool config",
				message=_frappe.get_traceback(),
			)
			return
		except admin_client.AdminUnreachableError as e:
			# This fix (2026-07-23 out-of-quota trace): admin's fleet layer can
			# raise a DEFINITIVE, already-decided rejection - e.g. a
			# subscription-only pool's first activation whose probe came back
			# exhausted (fleet's hard gate rolls back and NEVER records
			# applied) - wrapped in this SAME AdminUnreachableError class as a
			# genuine transient timeout/connection failure. There is nothing
			# to converge to for that case (the account's own quota has to
			# reset; no reconcile poll will ever see chat_readiness go
			# "Ready" until it does), so the F2 handling below would strand
			# the customer in "pending:" despite admin already knowing the
			# real, customer-facing reason. Recognise it first and write it
			# TERMINAL, exactly like the neighbouring auth/rate-limit/
			# validation branches, before falling through to F2's converge.
			reason = _admin_customer_facing_reason(str(e))
			if reason:
				_write_settings_fields(
					settings,
					{
						"last_sync_at": _frappe.utils.now(),
						"last_sync_status": f"failed: {reason}",
						**_cleared_subscription_status_fields(),
					},
				)
				_commit_terminal_sync_status()
				terminal_written = True
				_frappe.logger().info(
					"jarvis_settings: pool sync rejected with a customer-facing reason: %s", reason
				)
				return
			# F2: an unreachable/timeout is NOT a lost apply. The admin persists
			# desired-first (committed) and reconciles a late-landing apply, so
			# writing a terminal "failed:" here is exactly the livelock that
			# blocked onboarding - the container often applied the pool moments
			# after the bench hung up. Converge instead: poll get_connection for
			# chat_readiness == "Ready" (stamps the success markers on a hit);
			# otherwise record PENDING (not failed) and let the */5 reconcile
			# finish it. Only genuine auth/validation/rate-limit stay terminal.
			if not _converge_via_admin(settings, is_pool=True):
				_write_settings_fields(settings, {"last_sync_status": _PENDING_APPLYING_STATUS})
				_commit_terminal_sync_status()
				_frappe.logger().warning(
					"jarvis_settings: pool sync admin-unreachable; recorded pending for reconcile (%s)",
					e,
				)
			terminal_written = True
		except admin_client.AdminRateLimitedError as e:
			retry = e.retry_after_seconds or 0
			retry_str = f"retry_after={retry}s" if retry > 0 else "retry shortly"
			_write_settings_fields(
				settings,
				{
					"last_sync_at": _frappe.utils.now(),
					"last_sync_status": f"failed: rate-limited; {retry_str}",
					**_cleared_subscription_status_fields(),
				},
			)
			_commit_terminal_sync_status()
			terminal_written = True
			_frappe.logger().info(f"admin_client: pool sync rate-limited; retry_after={retry}s")
		except admin_client.AdminValidationError as e:
			_write_settings_fields(
				settings,
				{
					"last_sync_at": _frappe.utils.now(),
					"last_sync_status": f"failed: validation: {e}",
					**_cleared_subscription_status_fields(),
				},
			)
			_commit_terminal_sync_status()
			terminal_written = True
			_frappe.log_error(
				title="Jarvis: admin validation failed (pool sync)",
				message=_frappe.get_traceback(),
			)
		except _WRITE_CONFLICT_ERRORS:
			# #713, the pool twin of the branch in _sync_via_admin, and a backstop for
			# the same reason: the guarded writer already absorbs the status writes, so
			# this catches a conflict raised by anything else in this body. Same
			# reasoning either way:
			# admin already has the config, only the bookkeeping lost a race, and the
			# pending marker is the state three converging paths act on.
			_write_settings_fields(settings, {"last_sync_status": _PENDING_APPLYING_STATUS})
			_commit_terminal_sync_status()
			terminal_written = True
			_frappe.log_error(
				title="Jarvis: LLM pool sync lost a write-conflict race (recorded pending)",
				message=_frappe.get_traceback(),
			)
		finally:
			# Commit is load-bearing - see the matching backstop in
			# _sync_via_admin. Without it, a propagating exception (rq
			# JobTimeoutException in particular: the pool POST alone may
			# consume the whole HTTP budget) reaches execute_job's
			# frappe.db.rollback() and the terminal write is undone,
			# pinning the UI poller on "pending:" forever
			# (JARVIS-2026-07-08, fault c).
			if not terminal_written:
				try:
					_write_settings_fields(
						settings,
						{
							"last_sync_at": _frappe.utils.now(),
							"last_sync_status": "failed: unexpected error; see Error Log",
							**_cleared_subscription_status_fields(),
						},
					)
					_commit_terminal_sync_status()
				except Exception:
					pass


# How long the descriptor-obtain waits for the admin-sync lock before handing the
# push to the async worker and resume-following. Short on purpose.
SYNC_PUSH_LOCK_WAIT_S = 10
# The hard bound on the synchronous descriptor-obtain admin call - WELL under the
# deployed gunicorn budget (config: gunicorn -t 180, nginx proxy_read_timeout 240).
# The click must never own the long push (Fable corrected ruling F2/F3); it obtains
# the operation descriptor within this bound, then follows the operation. A timeout
# here is not a lost apply - admin commits desired + operation before the fleet
# push, so the operation exists and the SPA resumes via the same idempotency key
# while the async worker converges it.
SYNC_DESCRIPTOR_TIMEOUT_S = 20


def sync_pool_now(idempotency_key: str | None = None) -> dict:
	"""SHORT, tightly-bounded round-trip to OBTAIN the durable apply-operation
	descriptor for the onboarding/settings save (Fable corrected ruling F2/F3,
	review P0-02). The Start-chatting click owns the OPERATION, not the PUSH: this
	does ONE bounded admin call (``SYNC_DESCRIPTOR_TIMEOUT_S``, well under the
	gunicorn budget) and hands ALL long-running push/converge/stamp work to the
	async worker (``_enqueue_pool_sync``, which keeps its ``finally`` backstop). The
	SPA then follows the operation via ``get_llm_apply_operation`` exactly as it
	would anyway.

	Returns ``{"apply_operation": <§8.4 descriptor|None>, "resumable": bool,
	"retry_after_seconds": int, "legacy_capability": bool}``:

	- descriptor present → the SPA follows it to a terminal state. When it is a
	  still-converging apply, the worker is enqueued to converge + stamp the markers
	  (``llm_pool_synced_at`` gates ``is_ready_for_chat`` for a pool tenant); a
	  confirmed apply is stamped inline (fast, no polling).
	- ``resumable: True`` (lock contention, an admin read-timeout, or a permanent
	  reject where the operation was already committed) → the SPA re-calls
	  ``save_llm_pool`` with the SAME ``idempotency_key``; admin dedupes to the
	  existing operation (no new desired version, no second push). The worker is
	  enqueued so the config converges regardless of whether the SPA resumes.
	- ``legacy_capability: True`` → an OLD admin (no plan-05 apply-operation) returned
	  success WITHOUT a descriptor; ``save_llm_pool`` degrades to the bounded,
	  fail-closed readiness path rather than a support dead-end (F1).
	- ``retry_after_seconds`` on a rate-limit refusal (no operation allocated).

	Budget invariant: no path here exceeds the gunicorn timeout, and an exception
	anywhere after the desired-state commit hands the config to the worker rather
	than stranding it (F3).
	"""
	import frappe as _frappe

	from jarvis import admin_client
	from jarvis._redis_lock import redis_lock
	from jarvis.jarvis.pool_serialize import build_pool_payload, validate_models

	settings = None
	outcome: dict = {"apply_operation": None, "resumable": True}
	# The long push/converge is ALWAYS handed to the async worker OUTSIDE the lock
	# below - never inline here (that is what blew the gunicorn budget, F2/F3) and
	# never from INSIDE the lock (the worker takes the same lock; an inline test run
	# would deadlock on it). `handoff` records whether this outcome needs the worker.
	handoff = True
	try:
		with redis_lock(
			"jarvis_settings_admin_sync",
			timeout_s=ADMIN_SYNC_LOCK_TIMEOUT_S,
			blocking_timeout_s=SYNC_PUSH_LOCK_WAIT_S,
		) as acquired:
			settings = _frappe.get_single("Jarvis Settings")
			if not acquired:
				# A sibling container mutation holds the lock: hand the push/converge to
				# the worker (dedupes on the key) and resume-follow rather than blocking.
				outcome = {"apply_operation": None, "resumable": True}
			elif validate_models(settings) or not _pool_spec_pushable(settings):
				# Not a pushable pool after the save (e.g. a single-model config routed
				# through the creds path). No pool operation exists; nothing to converge.
				outcome = {"apply_operation": None, "resumable": False, "skipped": True}
				handoff = False
			else:
				spec, api_keys, oauth_blobs = build_pool_payload(settings)
				try:
					result = (
						admin_client.post_update_llm_pool(
							spec=spec,
							api_keys=api_keys,
							oauth_blobs=oauth_blobs,
							idempotency_key=idempotency_key,
							timeout_s=SYNC_DESCRIPTOR_TIMEOUT_S,
						)
						or {}
					)
				except admin_client.AdminRateLimitedError as e:
					retry = int(e.retry_after_seconds or 0)
					retry_str = f"retry_after={retry}s" if retry > 0 else "retry shortly"
					_write_settings_fields(
						settings,
						{
							"last_sync_at": _frappe.utils.now(),
							"last_sync_status": f"failed: rate-limited; {retry_str}",
							**_cleared_subscription_status_fields(),
						},
					)
					_commit_terminal_sync_status()
					# Enforced BEFORE admin allocates an operation: nothing to resume-follow
					# or converge - surface the cooldown truthfully.
					outcome = {"apply_operation": None, "resumable": False, "retry_after_seconds": retry}
					handoff = False
				except admin_client.AdminRejectedError as e:
					# Permanent: admin refused the spec (the operation was created before the
					# fleet apply and is now marked failed). Terminal status for the settings
					# strip; stay resumable so the SPA resume-by-key reads the REJECTED
					# descriptor. No reconcile can fix a permanent rejection, so no handoff.
					_write_settings_fields(
						settings,
						{
							"last_sync_at": _frappe.utils.now(),
							"last_sync_status": f"failed: {_admin_rejection_reason(e)}",
							**_cleared_subscription_status_fields(),
						},
					)
					_commit_terminal_sync_status()
					outcome = {"apply_operation": None, "resumable": True}
					handoff = False
				except admin_client.AdminUnreachableError:
					# Timeout/5xx is NOT a lost apply: admin committed desired + the operation
					# before the fleet call. Resume-follow via the operation; the worker
					# (handed off below, dedupes on the key) converges + stamps.
					outcome = {"apply_operation": None, "resumable": True}
				except (admin_client.AdminAuthError, admin_client.AdminValidationError) as e:
					# Fail BEFORE any operation is allocated (auth / spec validation): a
					# terminal, non-resumable error, nothing to follow or converge.
					_write_settings_fields(
						settings,
						{
							"last_sync_at": _frappe.utils.now(),
							"last_sync_status": f"failed: {e}",
							**_cleared_subscription_status_fields(),
						},
					)
					_commit_terminal_sync_status()
					outcome = {"apply_operation": None, "resumable": False}
					handoff = False
				else:
					op = result.get("apply_operation")
					# An OLD admin (pre-plan05) returns success with NO apply_operation ->
					# degrade to the honest bounded-readiness path, not a support dead-end (F1).
					legacy = op is None
					status = result.get("status") or "applied"
					outcome = {"apply_operation": op, "resumable": False, "legacy_capability": legacy}
					if status == "blocked":
						_write_settings_fields(
							settings,
							{"last_sync_status": "failed: subscription needs re-authentication (blocked)"},
						)
						_commit_terminal_sync_status()
						handoff = False
					elif _is_applying_result(result) or status != "applied":
						# Still converging: DO NOT converge inline (the long work that blew
						# the budget). The worker (handed off below) polls + stamps; the SPA
						# follows the descriptor meanwhile.
						pass  # handoff stays True
					else:
						# Confirmed applied within the short bound: stamp inline (a fast db
						# write, no polling) so is_ready_for_chat's pool marker is prompt.
						# A stamp that lost a write-conflict race keeps the hand-off (#713):
						# the worker re-drives the same apply and stamps it, which is the
						# same recovery a still-converging apply already gets.
						handoff = not _stamp_pool_applied_ok(settings, result)
	except Exception:
		# F3 backstop: any unexpected failure AFTER save_llm_pool committed the desired
		# state must not strand the config with nothing converging it.
		_frappe.log_error(
			title="jarvis_settings: sync_pool_now unexpected failure (handed to async worker)",
			message=_frappe.get_traceback(),
		)
		outcome = {"apply_operation": None, "resumable": True}
		handoff = True

	# Hand the long push/converge to the async worker OUTSIDE the lock (released by the
	# `with` exit above), so it can take the lock and - in tests, where enqueue runs
	# inline - not deadlock on a lock this function still held.
	if handoff:
		try:
			(settings or _frappe.get_single("Jarvis Settings"))._enqueue_pool_sync(
				idempotency_key=idempotency_key
			)
		except Exception:
			_frappe.log_error(
				title="jarvis_settings: sync_pool_now worker hand-off failed",
				message=_frappe.get_traceback(),
			)
	return outcome


def _enqueued_sync_via_admin(action: str, retry_left: int = ADMIN_SYNC_LOCK_RETRIES) -> None:
	"""Background-queue wrapper: re-load Jarvis Settings + run _sync_via_admin.

	Loading a fresh Single is necessary because the queue worker runs in a
	separate request context - we can't pass the Document instance across
	the queue boundary safely.

	Updates ``last_sync_status`` from ``pending: ...`` to either
	``ok (... via admin)`` or ``failed: ...`` - the UI polls
	``onboarding.get_llm_sync_status`` to observe the transition.

	Sprint-2 (2026-06-16 review): serialize concurrent sync workers
	with a Redis lock. Two close saves on the same Single can still
	enqueue two jobs with different actions ("reload" then "restart");
	both must run, but they must NOT run in parallel - one calling
	post_rotate_llm_secret while the other calls post_update_llm_creds
	crosses container state in unpredictable ways. The lock yields one
	serial run; the late arrival waits up to 60s for the early one to
	finish, then runs against the now-current doc state.
	"""
	from jarvis._redis_lock import redis_lock

	with redis_lock(
		"jarvis_settings_admin_sync",
		# TTL must cover a healthy holder running to its rq SIGALRM - see
		# ADMIN_SYNC_LOCK_TIMEOUT_S. A 120s TTL under a 600s job would
		# expire mid-run and admit a concurrent container mutation.
		timeout_s=ADMIN_SYNC_LOCK_TIMEOUT_S,
		blocking_timeout_s=_sync_lock_wait_s(retry_left),
	) as acquired:
		if not acquired:
			settings = frappe.get_single("Jarvis Settings")
			if retry_left > 0:
				# Same retry chain as the pool worker: a sibling sync may
				# now legitimately hold the lock for minutes (600s
				# envelope), so a single 60s wait + terminal "failed:
				# skipped" would silently DROP this credential change -
				# chat would keep running on the old key with only a
				# status line to notice.
				frappe.logger().warning(
					"jarvis_settings: admin sync (action=%s) lost the lock race; scheduling retry (%d left)",
					action,
					retry_left - 1,
				)
				_schedule_sync_lock_retry(
					method="jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_sync_via_admin",
					job_base=f"jarvis_settings_sync:{action}",
					retry_left=retry_left,
					action=action,
				)
				return
			frappe.logger().warning(
				"jarvis_settings: skipping admin sync (action=%s); "
				"another worker held the lock past blocking timeout (retries exhausted)",
				action,
			)
			_write_settings_fields(
				settings, {"last_sync_status": "failed: skipped (concurrent sync did not finish in time)"}
			)
			return
		settings = frappe.get_single("Jarvis Settings")
		settings._sync_via_admin(action)


def request_resync(settings) -> str:
	"""Re-drive the CURRENT LLM configuration through admin and report which leg
	was queued: ``"pool"`` or ``"direct"``.

	The retry verb behind ``onboarding.resync_llm``. It exists because neither
	normal path can be re-triggered on demand: ``on_update`` only syncs what
	``_classify_llm_change`` sees CHANGE, so re-saving an unchanged config is a
	no-op, and the reconcile only looks at workspaces whose state says something is
	outstanding. A workspace whose sync failed for a reason that has since passed
	had nothing left that would ever try again (#713).

	Writes the pending label first so the poller the SPA is already running flips
	to "Applying your changes" on the same round trip, then enqueues under the SAME
	job ids and the SAME redis lock as an ordinary save - so a Resync during an
	in-flight sync coalesces into it instead of racing it or restarting the
	container twice. (Only while that sync is still queued or running; the caller
	owns the longer-term throttle.)

	The direct leg is always ``"restart"``, never ``_classify_llm_change``'s lighter
	``"reload"``. Two reasons, and they agree: the classifier needs
	``get_doc_before_save()``, which is None outside an actual save, and its own
	rule for a re-push of UNCHANGED creds after a non-ok status is "restart" anyway,
	because a hot secret rotation cannot repair a container that never took the
	config."""
	from jarvis.jarvis.pool_serialize import compute_pool_mode

	if compute_pool_mode(settings):
		settings._enqueue_pool_sync()
		return "pool"
	_write_settings_fields(settings, {"last_sync_status": "pending: provisioning container"})
	frappe.enqueue(
		"jarvis.jarvis.doctype.jarvis_settings.jarvis_settings._enqueued_sync_via_admin",
		queue="long",
		timeout=ADMIN_SYNC_RQ_TIMEOUT_S,
		enqueue_after_commit=not (frappe.flags.in_test or frappe.flags.run_admin_sync_inline),
		now=bool(frappe.flags.in_test or frappe.flags.run_admin_sync_inline),
		job_id="jarvis_settings_sync:restart",
		deduplicate=True,
		action="restart",
	)
	return "direct"


#: The clause admin puts in ``chat_readiness_reason`` when its OWN tenant row
#: holds no LLM credential of any kind.
#:
#: Source of truth: ``jarvis_admin_v2/fleet/pool.py``. ``compute_chat_readiness``
#: appends exactly this sentence to ``missing`` when ``_llm_creds_ready(row)`` is
#: False, i.e. no api key, no pool config and no oauth blob. ``disconnect_llm``
#: blanks all three as a committed desired generation BEFORE it touches the
#: container, so this clause appears the moment admin has processed a disconnect
#: and stays until the customer reconnects.
#:
#: Matching a SENTENCE is not something to do lightly (the resume guard that
#: prose-matched a message admin had stopped sending is the cautionary tale), so
#: note which way this one fails. If admin rewords it, the clause stops matching,
#: the branch below never fires, and the bench is back to exactly today's
#: behaviour - the split persists until someone reconnects. It can never start
#: firing on a workspace admin is happy with. A drift here loses the fix; it
#: cannot cost a customer their keys.
_ADMIN_NO_LLM_CREDS_CLAUSE = "waiting for an llm key or subscription"


def _admin_says_llm_gone(state, reason) -> bool:
	"""Does admin's OWN state say this workspace has no LLM credentials left?

	Not "is the workspace unhealthy" - specifically "admin's tenant row holds
	nothing". Every other Configuring reason (applying your LLM configuration, ERP
	tools not connected, a rejected pool spec, an unverified route) describes a
	tenant whose credentials admin still HAS, and must never reach the clear.

	``state`` is required to be exactly ``Configuring`` on top of the clause:
	  * ``None``   - the probe failed. Unknown is not disconnected.
	  * ``Ready``  - contradicts the clause outright.
	  * ``Provisioning`` / ``Suspended`` / ``SupportRequired`` - admin is answering
	    about a container, a subscription or an authority incident, and says
	    nothing about credentials either way. A suspended tenant in particular
	    still owns its keys and gets them back on renewal.
	"""
	if state != "Configuring":
		return False
	return _ADMIN_NO_LLM_CREDS_CLAUSE in (reason or "").strip().lower()


def _local_llm_survived_an_admin_disconnect(settings, pool_mode: bool) -> bool:
	"""Could this workspace be the losing half of a split disconnect? Local-only.

	Three conditions. The second and third both exist to keep a working customer's
	credentials safe, and they cover two different ways admin can legitimately be
	holding none.

	1. The bench still HOLDS a credential (``_has_llm_config``). Nothing to clear
	   otherwise, which is also what makes the clear idempotent - a converged
	   workspace fails here on every later tick.

	2. The fleet has CONFIRMED an apply of the leg this workspace syncs through
	   (``_llm_apply_confirmed``: llm_pool_synced_at / llm_oauth_connected_at /
	   llm_direct_synced_at). This is the discriminator between the two ways admin
	   can be holding no credentials for a container we ARE serving:

	     * admin HAD them and deleted them -> the marker is set, because it was
	       stamped when admin confirmed the apply and only a completed disconnect
	       clears it. Converge.
	     * admin NEVER RECEIVED them -> a customer who just typed a key into a
	       bench that could not reach admin, or whose first push failed. No marker
	       was ever stamped. Their key is real, wanted, and MUST NOT be destroyed;
	       the pending-sync machinery is what carries that case.

	   Without (2) the reconcile would read "admin has no creds" identically in
	   both and wipe the second, which is the one failure mode strictly worse than
	   the bug being fixed.

	3. NO WORKSPACE RESET IS IN FLIGHT. (2) is not sufficient alone, because a reset
	   points the customer at a BRAND NEW container while deliberately keeping the
	   pool and the ``*_synced_at`` markers - the control plane carries them across,
	   and ``_prepare_for_reset`` says in as many words that clearing them would
	   eject the customer into the setup wizard. So between "new container Running"
	   and "re-pushed spec applied", admin truthfully reports no credentials about a
	   workspace whose markers are set, and (1) and (2) both pass. Firing there
	   would destroy a pool nobody asked to disconnect, mid-reset. The reset owns
	   its own convergence (``reconcile_pending_workspace_reset``); this leg stands
	   aside until that has finished.

	Deliberately reuses ``account``'s predicates rather than restating them: they
	are the same evidence ``is_ready_for_chat`` opens chat on and the Connection
	badge colours itself from, so this branch cannot come to a different
	conclusion about "connected" than the rest of the app.
	"""
	from jarvis.account import _has_llm_config, _llm_apply_confirmed
	from jarvis.onboarding import _RESETTING_STATUS

	if (settings.get("last_sync_status") or "").startswith(_RESETTING_STATUS):
		return False
	return _has_llm_config(settings) and _llm_apply_confirmed(settings, pool_mode)


def _llm_config_revision() -> str:
	"""Digest of this bench's LOCAL LLM configuration, read uncached.

	The reconcile decides whether to destroy credentials from an answer admin gave
	up to a probe-timeout ago, holding a ``settings`` doc it loaded before asking.
	A customer who RECONNECTS inside that window - types a new key, saves, has it
	pushed - would otherwise have the credential they just entered deleted by a
	verdict about the connection they replaced. Snapshot this before the probe,
	compare after, skip the tick if anything moved; the next tick re-reads a
	consistent pair. A narrow window, but it is the exact failure mode this whole
	branch exists to avoid, so it does not get to survive as a race.

	Reuses ``account``'s gate-revision digest rather than a bespoke field list: it
	already covers every field a save or a reconnect moves (provider, model, auth
	mode, preset, routing, sync status, the three apply markers), it is uncached by
	construction, and one definition means a field added for the chat gate is
	respected here for free.
	"""
	from jarvis.account import _GATE_REVISION_FIELDS, _gate_revision, _settings_raw

	return _gate_revision(_settings_raw(_GATE_REVISION_FIELDS))


def reconcile_pending_llm_sync() -> None:
	"""Scheduled safety net (*/5, hooks.py): finish a sync the in-band job left
	PENDING because the admin apply was still converging (F2).

	Mirrors the admin-side */5 reconcile from the bench end so the two systems
	converge from either direction. It is deliberately minimal and defensive:

	- No-op unless the tenant is in a state the admin reconcile might have
	  resolved since the bench last looked: either the explicit
	  ``pending: admin applying config`` marker, OR a pool whose FIRST apply is
	  still unproven (pool mode + no llm_pool_synced_at) sitting at a
	  pending/failed status (the onboarding livelock class), OR a workspace whose
	  credentials admin has already destroyed (see ``_admin_says_llm_gone``).
	- Probes admin get_connection EXACTLY ONCE (chat_readiness); stamps the
	  terminal success markers only on "Ready" (admin gates Ready on
	  applied_version >= desired_version, so it never reports Ready from intent).
	- Never flips a status to a new "failed:" and never touches a healthy /
	  already-"ok" tenant. Swallows every error - a scheduled task must not
	  raise. Un-onboarded sites short-circuit.

	THE DISCONNECT LEG (#534). ``onboarding.disconnect_llm`` calls admin inside a
	whitelisted request and clears local secrets only after it answers, so a
	worker killed at gunicorn's ``-t`` AFTER admin committed its blanked row left
	admin and the container disconnected while this bench kept advertising a live
	model. Nothing converged that: the branches above only look at pending/failed
	states, and a half-disconnected workspace reads perfectly healthy locally.

	This leg closes it WITHOUT a bench-side intent flag - admin deliberately owns
	that state (jarvis-admin-v2 #136), and a local flag would only reintroduce the
	same "did the worker survive long enough to write it" question one field
	earlier. It converges from what admin already publishes instead, so a killed
	worker, a dropped connection and a customer who closed the tab all land in the
	same place on the next tick.
	"""
	try:
		settings = frappe.get_single("Jarvis Settings")
		# Un-onboarded: no admin credentials -> get_connection would just raise.
		admin_api_key = (settings.get_password("jarvis_admin_api_key", raise_exception=False) or "").strip()
		customer_pw = (
			settings.get_password("jarvis_admin_customer_password", raise_exception=False) or ""
		).strip()
		if not admin_api_key and not customer_pw:
			return

		from jarvis.jarvis.pool_serialize import compute_pool_mode

		status = settings.get("last_sync_status") or ""
		# The evidence marker follows the SYNC LEG, so this is pool mode, not the
		# narrower proxy_active (an agent-direct pool stamps llm_pool_synced_at).
		pool_mode = compute_pool_mode(settings)
		pool_synced = bool(settings.get("llm_pool_synced_at"))
		direct_synced = bool(settings.get("llm_direct_synced_at"))

		is_applying_pending = status.startswith(_PENDING_APPLYING_STATUS)
		# A tenant whose FIRST apply never stamped its evidence marker may have
		# been converged by the admin reconcile cron since the bench last wrote
		# its (pending/failed) status - re-probe so the customer isn't stranded
		# at llm_pool_provisioning / llm_provisioning while the container is
		# actually serving the config. Direct analogue added in round-4
		# (R4-P0-6): is_ready_for_chat now gates a first single-model activation
		# on llm_direct_synced_at, so an unproven direct tenant is the same
		# stranding class as an unproven pool.
		is_stuck = status.startswith("pending:") or status.startswith("failed:")
		is_unproven_pool = pool_mode and not pool_synced and is_stuck
		is_unproven_direct = (
			not pool_mode
			and not direct_synced
			and is_stuck
			and (settings.get("llm_auth_mode") or "api_key") == "api_key"
			and bool((settings.get("llm_provider") or "").strip())
		)
		# The disconnect leg's LOCAL precondition (see the docstring). It is true of
		# every healthy CONNECTED workspace, so this leg does cost one extra
		# get_connection per tick on those - unavoidable, because a bench that lost
		# the disconnect looks identical to a working one from the inside, which is
		# the whole defect. The probe is the same cheap read the SPA already makes
		# far more often, and every tenant that holds no credential still skips it.
		may_be_disconnected = _local_llm_survived_an_admin_disconnect(settings, pool_mode)
		if not (is_applying_pending or is_unproven_pool or is_unproven_direct or may_be_disconnected):
			return

		revision_before = _llm_config_revision() if may_be_disconnected else ""
		# End the transaction before the probe (#713). Two things depend on it: the
		# stamp below has to be writable against whatever committed while we were
		# asking admin, and the reconnect guard further down has to be able to SEE
		# that commit - a re-read on the snapshot this tick opened with would answer
		# "nothing moved" no matter what the customer did.
		_refresh_db_snapshot()
		state, reason = _admin_chat_readiness()
		if state == "Ready":
			if is_applying_pending or is_unproven_pool or is_unproven_direct:
				# Stamps the evidence marker for whichever mode is active:
				# llm_pool_synced_at for pool tenants, llm_direct_synced_at for
				# single-model tenants (is_ready_for_chat's first-activation gates).
				# A lost race needs no handling here: this tick changes nothing and
				# the next one (or the SPA's own poller) re-probes and re-stamps.
				_stamp_converged_ok(settings, is_pool=pool_mode)
			return
		if not (may_be_disconnected and _admin_says_llm_gone(state, reason)):
			return
		if _llm_config_revision() != revision_before:
			# The customer reconnected while we were asking. Admin's answer is about
			# the connection they just replaced, and acting on it would delete the
			# credential they just entered. Drop the tick; the next one re-reads both.
			frappe.logger().info(
				"jarvis_settings: local LLM config changed during the disconnect probe; "
				"discarding a stale admin verdict"
			)
			return

		from jarvis.onboarding import apply_local_disconnect

		frappe.logger().warning(
			"jarvis_settings: admin reports this workspace's LLM credentials are gone "
			"while the bench still holds them; completing the local disconnect"
		)
		# Safe to write through the doc loaded before the probe: the revision check
		# above just proved nothing moved, and every write it makes is absolute
		# (delete the rows, write _DISCONNECTED_LLM_FIELDS' constants) rather than
		# derived from a field this doc is holding.
		apply_local_disconnect(settings)
	except Exception:
		frappe.log_error(
			title="Jarvis: reconcile_pending_llm_sync failed",
			message=frappe.get_traceback(),
		)
