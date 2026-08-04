"""Learned-skills push chain (Behavioural Pattern Learning Phase 2, plan 6.2/13 Q5).

The learned-namespace sibling of ``custom_skills_api``'s apply half: compiled
``learned-<domain>`` bundles no longer ride the customer's custom-skills push -
they go through their OWN deduped redis-locked worker to admin's
``jarvis_admin.api.tenant.push_learned_skills`` (-> fleet
``PUT /v1/containers/{name}/learned-skills`` -> full reconcile of the container's
separate ``learned_skills`` dir + restart). Separate namespace, separate cap
(<=10, ``compiler.LEARNED_SKILL_CAP``), separate ``learned_skills_sync_*``
status pair on Jarvis Settings.

The managed rows themselves REMAIN ``Jarvis Custom Skill`` rows (bench-side
storage + role semantics stay bench-side - plan 13 Q5); only the WIRE moved off
the custom push. ``build_push_payload`` excludes them, so the next custom
reconcile deletes any pre-cutover ``custom-learned-<domain>`` dirs.

CUTOVER CHAIN: on the one-time namespace-cutover Apply the compiler passes
``chain_custom_reconcile=True`` and the worker, ONLY after its push is
confirmed ok, stamps ``custom_skills_sync_status='pending: applying skills'``
and enqueues the GRACEFUL custom worker (strict=False build - never the strict
interactive ``apply_custom_skills``) to reconcile away the stale
``custom-learned-*`` dirs. Chaining serializes the two container restarts and
keeps the old guidance live if the learned push fails (see the CUTOVER MARKER
in ``compiler._apply_learned_skills_locked`` for the full guarantees).

Status discipline clones the custom chain exactly (pending -> terminal ok/failed
with a try/finally backstop so the poller never spins forever), except that ALL
status writes pass ``update_modified=False`` - a background push must never bump
the Settings modified stamp and trip a concurrent editor's timestamp check.

Entry points:
  - ``enqueue_learned_skills_push()`` - NOT whitelisted: the SM gate lives in
    ``learned_api.apply_learned_skills`` (the only UI path), which delegates via
    ``compiler.apply_learned_skills``. The restart resync
    (``JarvisSettings._resync_learned_skills_after_restart``) enqueues the
    worker directly.
  - ``get_learned_skills_sync_status()`` - whitelisted poller, proxied by the
    SM-gated ``learned_api.get_learned_apply_status``.
"""

from __future__ import annotations

import frappe

from jarvis.permissions import require_jarvis_user

_SETTINGS = "Jarvis Settings"
_PUSH_JOB_ID = "jarvis_learned_skills_push"
_LOCK_NAME = "jarvis_learned_skills_push"

_PENDING_STATUS = "pending: applying learned skills"


@frappe.whitelist()
@require_jarvis_user
def get_learned_skills_sync_status() -> dict:
	"""Lightweight poller mirroring ``get_custom_skills_sync_status``."""
	return _learned_sync_status()


def _learned_sync_status() -> dict:
	"""Undecorated core of :func:`get_learned_skills_sync_status`. Split out so the
	reviewer-facing Apply board — ``learned_api.get_learned_apply_status`` and
	``learned_api._apply_pending``, both gated on ``_REVIEWER_ROLES`` which admits a
	reviewer / admin who lacks Jarvis User / System Manager — can read the sync
	status without tripping the ``@require_jarvis_user`` HTTP gate. It is a read of
	two ``Jarvis Settings`` fields the reviewer is already authorized to see."""
	s = frappe.get_single(_SETTINGS)
	status = s.get("learned_skills_sync_status") or ""
	return {
		"last_sync_at": str(s.get("learned_skills_synced_at") or ""),
		"last_sync_status": status,
		"pending": status.startswith("pending:"),
	}


def enqueue_learned_skills_push(chain_custom_reconcile: bool = False) -> dict:
	"""Push the compiled learned skills to the assistant (one restart).

	Builds the payload synchronously so cap/shape errors surface immediately to
	the Apply caller, marks a pending status, then enqueues the deduped worker -
	mirrors ``custom_skills_api.apply_custom_skills``. Deliberately NOT
	whitelisted: callers are ``compiler.apply_learned_skills`` (behind the SM
	gate + apply lock) and tests.

	``chain_custom_reconcile`` (the one-time namespace cutover): the flag rides
	the job kwargs; the worker enqueues the graceful custom-skills reconcile
	only after a confirmed-ok push (see the module docstring). If dedup drops
	this enqueue because a learned push job is already queued (a restart-resync
	racing the first-ever Apply), the flag is lost and the reconcile is simply
	skipped - the next custom apply / restart resync's full reconcile
	self-heals; no status is left pending.
	"""
	from jarvis.learning.compiler import learned_push_split

	payload, held_back = learned_push_split()
	frappe.db.set_single_value(
		_SETTINGS, "learned_skills_sync_status", _PENDING_STATUS, update_modified=False
	)
	frappe.db.commit()
	run_inline = bool(frappe.flags.in_test or frappe.flags.run_admin_sync_inline)
	frappe.enqueue(
		"jarvis.chat.learned_skills_api._enqueued_push_learned_skills",
		queue="long",
		timeout=180,
		enqueue_after_commit=not run_inline,
		now=run_inline,
		job_id=_PUSH_JOB_ID,
		deduplicate=True,
		chain_custom_reconcile=bool(chain_custom_reconcile),
	)
	return {
		"ok": True,
		"learned_skills_sync_status": _PENDING_STATUS,
		# ``count`` is what actually reaches the container. ``role_restricted`` is
		# the rest: compiled, live, and served on demand by jarvis__get_skill
		# instead of written to the shared blob (#479).
		"count": len(payload),
		"role_restricted": held_back,
	}


def _enqueued_push_learned_skills(chain_custom_reconcile: bool = False) -> None:
	"""Background worker: push the compiled learned bundles via admin -> fleet ->
	container. Re-builds the payload fresh from the managed rows (never trust a
	payload across the queue boundary) and mirrors
	``_enqueued_push_custom_skills``'s try/except/finally so the status never
	stays ``pending:`` forever.

	``chain_custom_reconcile`` (namespace cutover): after a CONFIRMED-ok push -
	admin returned, terminal ok stamped, committed - enqueue the graceful custom
	reconcile that deletes the stale ``custom-learned-*`` dirs. Chained here
	(not at Apply time) so the custom restart can only start after the learned
	restart finished; on any failure/skip path the reconcile is NOT enqueued
	(the stale dirs keep the OLD guidance live; a later custom apply / restart
	resync self-heals)."""
	from jarvis import admin_client
	from jarvis._redis_lock import redis_lock
	from jarvis.learning.compiler import learned_push_split

	with redis_lock(_LOCK_NAME, timeout_s=180, blocking_timeout_s=60.0) as acquired:
		if not acquired:
			frappe.db.set_single_value(
				_SETTINGS,
				"learned_skills_sync_status",
				"failed: skipped (concurrent sync)",
				update_modified=False,
			)
			frappe.db.commit()
			return

		terminal_written = False
		push_ok = False
		try:
			payload, held_back = learned_push_split()
			admin_client.post_push_learned_skills(learned_skills=payload)
			frappe.db.set_value(
				_SETTINGS,
				_SETTINGS,
				{
					"learned_skills_synced_at": frappe.utils.now(),
					"learned_skills_sync_status": _ok_status(len(payload), held_back),
				},
				update_modified=False,
			)
			terminal_written = True
			push_ok = True
		except admin_client.AdminAuthError as e:
			_fail(f"failed: auth: {e}")
			terminal_written = True
			frappe.log_error(title="Jarvis: learned-skills admin auth failed", message=frappe.get_traceback())
		except admin_client.AdminUnreachableError as e:
			_fail(f"failed: admin unreachable: {e}")
			terminal_written = True
			frappe.log_error(title="Jarvis: learned-skills admin unreachable", message=frappe.get_traceback())
		except admin_client.AdminRateLimitedError as e:
			retry = getattr(e, "retry_after_seconds", 0) or 0
			retry_str = f"retry_after={retry}s" if retry > 0 else "retry shortly"
			_fail(f"failed: rate-limited; {retry_str}")
			terminal_written = True
		except admin_client.AdminValidationError as e:
			_fail(f"failed: invalid: {e}")
			terminal_written = True
		except Exception:
			_fail("failed: unexpected error; see Error Log")
			terminal_written = True
			frappe.log_error(title="Jarvis: learned-skills push failed", message=frappe.get_traceback())
		finally:
			if not terminal_written:
				try:
					_fail("failed: unexpected error; see Error Log")
				except Exception:
					pass
		frappe.db.commit()
		if push_ok and chain_custom_reconcile:
			_enqueue_cutover_custom_reconcile()


def _enqueue_cutover_custom_reconcile() -> None:
	"""Cutover chain step 2 (see ``compiler._apply_learned_skills_locked``'s
	CUTOVER MARKER): the learned push is confirmed ok, so stamp the custom pair
	pending and enqueue the GRACEFUL custom-skills worker (strict=False build:
	a >25-skill bench truncates + logs) whose full reconcile deletes the stale
	pre-namespace ``custom-learned-*`` dirs. Enqueued directly - NEVER via the
	strict interactive ``apply_custom_skills`` - so the learned Apply's success
	stays decoupled from the customer's custom-skill count. Stamp + dedup
	semantics mirror ``apply_custom_skills``/the restart resync (same job id:
	an already-queued customer apply does the same full reconcile, so dedup
	folds the two into one restart). Best-effort: a failure here is logged and
	the next custom apply / restart resync self-heals."""
	try:
		frappe.db.set_single_value(
			_SETTINGS,
			"custom_skills_sync_status",
			"pending: applying skills",
			update_modified=False,
		)
		frappe.db.commit()
		run_inline = bool(frappe.flags.in_test or frappe.flags.run_admin_sync_inline)
		frappe.enqueue(
			"jarvis.chat.custom_skills_api._enqueued_push_custom_skills",
			queue="long",
			timeout=180,
			now=run_inline,
			job_id="jarvis_custom_skills_push",
			deduplicate=True,
		)
	except Exception:
		frappe.log_error(
			title="Jarvis: cutover custom reconcile enqueue failed",
			message=frappe.get_traceback(),
		)


def _ok_status(installed: int, role_restricted: int) -> str:
	"""The reviewer-facing terminal status, which has to distinguish two very
	different outcomes that the old "ok (applied N via admin)" wording merged.

	Since #479 a role-restricted learned skill is deliberately NOT written into
	the container: it is live, it still applies to its role-holders, and its body
	is served on demand by ``jarvis__get_skill``. Every compiled row carries
	``allowed_roles``, so a reviewer who approves four domains and then reads
	"applied 1" would reasonably file that as a bug. Naming the held-back count
	is the whole fix - it says the other three are working, not missing."""
	if role_restricted:
		return (
			f"ok ({installed} installed via admin, "
			f"{role_restricted} role-restricted and fetched on demand)"
		)
	return f"ok ({installed} installed via admin)"


def _fail(status: str) -> None:
	frappe.db.set_value(
		_SETTINGS,
		_SETTINGS,
		{"learned_skills_synced_at": frappe.utils.now(), "learned_skills_sync_status": status},
		update_modified=False,
	)
