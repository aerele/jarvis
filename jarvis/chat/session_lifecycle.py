"""Session lifecycle: idle-session reclaim + empty-chat + orphan sweeps.

Every Jarvis Conversation maps to one openclaw session, created lazily on the
first turn. Without a sweep, state accumulates forever:

- Dormant conversation sessions: the user stopped chatting weeks ago but the
  session (and its growing working context) sits on the container.
- Empty conversations: opening "New Chat" and closing the tab leaves a
  0-message row cluttering history.
- Orphaned throwaway sessions: deleted conversations leave their sessions behind,
  and so do the three throwaway kinds (auto-title, pattern polish, prefix
  prewarm) whenever their own cleanup is missed. All three DO now reclaim their
  own through ``reclaim_throwaway_session`` below - title and polish in a
  finally, prewarm by reclaiming its predecessor on the next warm - so this
  sweep is their backstop, not their only collector. It used to be the only one,
  and could not keep up: a 4-minute warm cooldown alone minted up to ~350
  sessions/day against a sweep capped at 25/day.

The bench is the durable owner of chat history (Jarvis Chat Message rows); the
openclaw session is a cache of working context, not the record. Deleting one is
safe: the gateway archives the transcript first (sessions.delete
deleteTranscript=true default), canvas/media artifacts were pulled to ERP Files
at turn end, and the next message lazily creates a fresh session through the
existing ``_ensure_session_key`` path - same UX, empty working context.

The hourly ``rotate_dormant_sessions`` cron:

1. FREE IDLE SESSIONS: conversations idle past the configured retention window
   (Jarvis Settings.conversation_retention_days; 0 disables) that still hold a
   live openclaw session, with no in-flight rows -> free the session (delete the
   gateway session, clear ``conv.session_key``, drop ``Jarvis Chat Session``
   lookup rows). The conversation is LEFT ACTIVE AND VISIBLE - only the
   container-side working memory is reclaimed. Returning to the chat lazily
   mints a fresh session (full history, empty working context). Starred chats
   are freed too: starring pins a chat in the list, it does not hold a session
   hostage for a month of idleness.
2. REAP EMPTY CHATS: Active, non-starred conversations with ZERO messages, idle
   past ``EMPTY_GRACE_DAYS`` with nothing in-flight -> hard-delete the row. A
   0-message chat has no messages / approvals / runs / files to cascade, so the
   delete is trivial. This clears the "opened New Chat, closed the tab" ghost
   (empty chats are also hidden from the sidebar list; this reaps the row so it
   doesn't linger in the DB forever).
3. ORPHANS: gateway sessions in the chat namespace that no conversation
   references (throwaways, deleted conversations) and that have been inactive
   past their grace -> delete, plus any stale lookup rows. The grace is
   per-session (``_grace_ms``): a short ``THROWAWAY_GRACE_HOURS`` for the known
   throwaway labels, the conservative ``ORPHAN_GRACE_HOURS`` for everything
   else. Runs regardless of the retention setting - these are not user chats -
   and on its own ``ORPHAN_BATCH_MAX`` budget.

Parts 1 + 2 are the retention sweep, honour ``conversation_retention_days``
(0 => keep everything, do nothing), and share ``BATCH_MAX``. Part 3 is pure
gateway hygiene, always runs, and has its own budget so parts 1 + 2 cannot
starve it. Everything is best-effort on a dedicated connection (never the pool -
a sweep must not contend with live turns), batch-capped so a backlog drains over
a few runs instead of stampeding a gateway, and managed-mode only.

``reclaim_throwaway_session`` is the other half: the same "is this session
really finished" gate the sweep applies, exposed to the throwaway minters so
they can collect their own session immediately without racing the run that owns
it. Cron and minters therefore agree on one rule - never delete a session the
gateway still reports an active run for.
"""

from __future__ import annotations

import logging
import time

import frappe

logger = logging.getLogger(__name__)

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
CHAT_SESSION = "Jarvis Chat Session"

# Retention: a conversation idle this long has its openclaw session freed (its
# working memory reclaimed). The conversation itself stays Active and visible.
# Configurable per-tenant via Jarvis Settings.conversation_retention_days; these
# are the fallbacks a reader applies. DEFAULT is used when the Single field is
# unset (Single defaults are NOT backfilled on migrate, so None must read as 30,
# not 0 - 0 means "keep forever / never free"). MIN is a defensive floor
# mirroring the settings validator, so a value that slipped in below it can't
# mass-free on the very next cron.
DEFAULT_RETENTION_DAYS = 30
MIN_RETENTION_DAYS = 7

# An Active conversation with ZERO messages that has been idle this long is
# hard-deleted (the abandoned "New Chat" ghost). Comfortably longer than any
# realistic gap between opening a new chat and typing into it, so a chat the
# user is about to use is never reaped out from under an open tab.
EMPTY_GRACE_DAYS = 7


def _retention_days() -> int:
	"""Effective idle-retention window in days. 0 => disabled (keep forever).

	Read the RAW tabSingles value, not ``get_single_value``: the latter casts an
	unset Int Single field to 0, which is indistinguishable from an explicit 0
	(=disabled). The raw value is None when the field was never set (Single
	defaults are not backfilled on migrate) -> the 30-day default (on by
	default). An explicit '0' stays 0 (never)."""
	rows = frappe.db.sql(
		"SELECT value FROM `tabSingles` "
		"WHERE doctype = 'Jarvis Settings' AND field = 'conversation_retention_days'"
	)
	raw = rows[0][0] if rows else None
	if raw is None or raw == "":
		return DEFAULT_RETENTION_DAYS
	days = frappe.utils.cint(raw)
	if days <= 0:
		return 0
	return max(days, MIN_RETENTION_DAYS)


# An unreferenced gateway session younger than this is skipped: it may be
# an in-flight title/prewarm throwaway, or a conversation whose freshly
# created session_key has not committed yet.
ORPHAN_GRACE_HOURS = 24

# Per-run cap for the retention sweeps (parts 1 + 2), so a month of backlog
# drains over a few runs instead of hammering the gateway in one cron tick.
BATCH_MAX = 25

# Orphans (part 3) get their OWN, larger per-run cap instead of parts 1+2's
# leftovers. They are the only unbounded population in this sweep - every prefix
# warm, auto-title and pattern polish mints one - and unlike parts 1+2 they touch
# no user data, so a bigger batch is cheap. Sharing one 25-slot budget let a
# backlog of idle conversations starve the sweep that actually needed the slots.
ORPHAN_BATCH_MAX = 200

# Only sessions in the chat namespace are ever considered for the orphan
# sweep; the agent's main session is additionally refused server-side.
_CHAT_NAMESPACE_MARKER = ":dashboard:"

# Labels of every throwaway session kind the bench mints: prewarm.warm_prefix,
# title._generate_via_gateway, and learning.polish._run_gateway_turn. All three
# now delete their own sessions, so these only turn up here when that cleanup was
# missed (a crash, a lost cache pointer, a gateway blip) - never as live state.
#
# A short grace is SAFE for these specifically because the labels are namespaced:
# a real conversation session is always "jarvis-chat-<user>-<ms>" (api.py
# _ensure_session_key), so a throwaway label can never be a conversation whose
# freshly-minted session_key has not committed yet. That race is exactly what
# ORPHAN_GRACE_HOURS protects, and it still gets the full 24h.
_THROWAWAY_LABEL_PREFIXES = ("jarvis-prewarm-", "jarvis-title-", "jarvis-polish-")
THROWAWAY_GRACE_HOURS = 1


def rotate_dormant_sessions() -> dict:
	"""Hourly cron: free idle conversations' openclaw sessions, reap abandoned
	empty chats, and reap orphaned throwaway sessions. Returns a summary dict
	(also logged) so a manual ``bench execute`` run shows what happened. (Name
	kept for the scheduler entry in hooks.py.)"""
	settings = frappe.get_single("Jarvis Settings")
	gateway_url = (settings.agent_url or "").replace("http://", "ws://").replace("https://", "wss://")
	if not gateway_url:
		return {"skipped": "no agent_url"}

	from jarvis.chat.agent_client import OpenclawSession

	summary = {"sessions_freed": 0, "empty_reaped": 0, "orphans_reaped": 0, "skipped": 0, "errors": 0}
	budget = BATCH_MAX
	try:
		sess = OpenclawSession.connect(gateway_url)
	except Exception:
		frappe.log_error(
			title="session_lifecycle: connect failed",
			message=frappe.get_traceback(),
		)
		return {"skipped": "connect failed"}
	try:
		# Parts 1 + 2 are the retention sweep (0 = disabled, keep everything).
		# Part 3 (orphans) is gateway hygiene and runs regardless.
		days = _retention_days()
		if days > 0:
			budget = _free_idle_sessions(sess, budget, summary, days)
			if budget > 0:
				_reap_empty(sess, budget, summary)
		# Part 3 runs on its own budget (see ORPHAN_BATCH_MAX), so a backlog in
		# parts 1+2 can no longer starve it, and it runs even when retention is
		# disabled - orphaned throwaways are gateway hygiene, not user chats.
		_reap_orphans(sess, ORPHAN_BATCH_MAX, summary)
	finally:
		try:
			sess.close()
		except Exception:
			pass
	logger.info("session_lifecycle: %s", summary)
	return summary


def _free_idle_sessions(sess, budget: int, summary: dict, days: int) -> int:
	"""Part 1: conversations idle past the retention window that still hold an
	openclaw session -> free the session (delete gateway session, null
	``session_key``, drop the lookup rows). The conversation is left Active and
	visible; only the container-side working memory is reclaimed. Starred and
	status are irrelevant here - any idle chat with a live session qualifies.
	Returns leftover budget for the remaining sweeps."""
	cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-days)
	rows = frappe.db.sql(
		"""
		SELECT c.name, c.session_key
		FROM `tabJarvis Conversation` c
		WHERE c.session_key IS NOT NULL AND c.session_key != ''
		  AND c.last_active_at IS NOT NULL AND c.last_active_at < %(cutoff)s
		  AND NOT EXISTS (
			SELECT 1 FROM `tabJarvis Chat Message` m
			WHERE m.conversation = c.name
			  AND (m.streaming = 1 OR m.recovering = 1)
		  )
		ORDER BY c.last_active_at ASC
		LIMIT %(limit)s
		""",
		{"cutoff": cutoff, "limit": budget},
		as_dict=True,
	)
	for row in rows:
		if budget <= 0:
			break  # defensive; the SQL LIMIT already bounds rows to budget
		budget -= 1
		# Per-row isolation (turn_recovery's loop pattern): one bad row must
		# never abort the batch, and a failure between the gateway delete and
		# the local commit must not strand the sweep - the idempotent not-found
		# handling in _delete_gateway_session lets the next run finish cleanup.
		try:
			# Free the openclaw session FIRST; only detach the bench side once
			# the gateway side is gone, else a crash would strand a live session
			# under a nulled key. A gateway-delete failure leaves the row intact
			# for the next run.
			# KNOWN LIMITATION: a row whose gateway delete PERMANENTLY fails is the
			# oldest, so ORDER BY last_active_at ASC re-selects it at the front every
			# run, consuming a batch slot; >= BATCH_MAX such corpses would starve the
			# empty-reap and orphan sweeps that run on the leftover budget. Planned
			# guard: a last_free_error_at cooldown column. (Inherited from the prior
			# rotate sweep.)
			if not _delete_gateway_session(sess, row.session_key, summary):
				continue
			# NULL, not "": session_key is UNIQUE and two "" rows collide. The
			# conversation write lands before the lookup-row delete so a per-row
			# failure at the primary write skips cleanly with no partial detach.
			frappe.db.set_value(CONV, row.name, {"session_key": None})
			frappe.db.delete(CHAT_SESSION, {"session_key": row.session_key})
			frappe.db.commit()
			summary["sessions_freed"] += 1
		except Exception:
			# Discard any partial write from this row (e.g. the conversation
			# update landed but the lookup-row delete then failed) so it can't
			# ride to the NEXT row's commit; the idempotent gateway delete lets
			# the next run finish cleanly. Rollback before log_error.
			frappe.db.rollback()
			frappe.log_error(
				title="session_lifecycle: free-session row failed",
				message=f"conversation={row.name}\n{frappe.get_traceback()}",
			)
			summary["errors"] += 1
	return budget


def _reap_empty(sess, budget: int, summary: dict) -> int:
	"""Part 2: hard-delete the abandoned "New Chat" ghost - an Active,
	non-starred conversation with ZERO messages, idle past ``EMPTY_GRACE_DAYS``.
	Such a row has no messages / approvals / runs / voice notes hanging off it,
	so ``delete_doc(force)`` is a clean removal. A stray session is freed first,
	defensively. Returns the leftover retention budget (part 3 no longer runs on
	it - it has its own ORPHAN_BATCH_MAX).

	Two exclusions guard against destroying real data via ``delete_doc``'s
	cascade to attached Files (frappe ``remove_all`` on delete):
	- ``file_box = 0``: a File-Box drop (``filebox.drop_file``) creates the
	  conversation, attaches the uploaded File, and only THEN sends; a drop that
	  fails (usage cap, paused sub) leaves a 0-message file_box conversation the
	  user is meant to retry - reaping it would delete their uploaded file.
	- no attached File of any kind, as belt-and-suspenders for the same cascade.
	"""
	cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-EMPTY_GRACE_DAYS)
	rows = frappe.db.sql(
		"""
		SELECT c.name, c.session_key
		FROM `tabJarvis Conversation` c
		WHERE c.status = 'Active'
		  AND c.starred = 0
		  AND c.file_box = 0
		  AND c.last_active_at IS NOT NULL AND c.last_active_at < %(cutoff)s
		  -- HARD DATA-LOSS GUARD: only a conversation with ZERO messages is ever
		  -- reapable. A conversation that holds ANY message - a user message, or
		  -- even an assistant row from a turn that FAILED (a terminal agent error
		  -- leaves an errored/empty assistant row; see turn_handler + the
		  -- agent_client failed_final mapping) - is real chat history and is
		  -- never auto-deleted. Do NOT loosen this to reap "conversations whose
		  -- turns all failed / produced no visible content": that would delete the
		  -- user's message. Empty ones may go; anything with a message stays.
		  AND NOT EXISTS (
			SELECT 1 FROM `tabJarvis Chat Message` m WHERE m.conversation = c.name
		  )
		  AND NOT EXISTS (
			SELECT 1 FROM `tabFile` f
			WHERE f.attached_to_doctype = 'Jarvis Conversation'
			  AND f.attached_to_name = c.name
		  )
		ORDER BY c.last_active_at ASC
		LIMIT %(limit)s
		""",
		{"cutoff": cutoff, "limit": budget},
		as_dict=True,
	)
	for row in rows:
		if budget <= 0:
			break
		budget -= 1
		try:
			# Re-check emptiness before the destructive delete: a user returning to
			# a just-past-grace empty could have inserted their first message
			# (committed by send_message) between the SELECT and here; deleting then
			# would orphan that fresh message and kill the live turn. commit() first
			# so this read opens a FRESH snapshot - under MariaDB's default
			# REPEATABLE READ a read inside the batch-SELECT's transaction would
			# still see the row as message-less. Nothing is pending to commit here
			# (the prior row committed or rolled back at the end of its iteration).
			frappe.db.commit()
			if frappe.db.exists(MSG, {"conversation": row.name}):
				continue
			# A 0-message chat almost never holds a session (session_key is set
			# on the first turn), but free one defensively so a hard delete never
			# strands gateway state. A gateway-delete failure leaves the row for
			# the next run rather than orphaning the session.
			if row.session_key:
				if not _delete_gateway_session(sess, row.session_key, summary):
					continue
				frappe.db.delete(CHAT_SESSION, {"session_key": row.session_key})
			frappe.delete_doc(CONV, row.name, force=True, ignore_permissions=True)
			frappe.db.commit()
			summary["empty_reaped"] += 1
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title="session_lifecycle: empty reap failed",
				message=f"conversation={row.name}\n{frappe.get_traceback()}",
			)
			summary["errors"] += 1
	return budget


def _grace_ms(entry: dict) -> int:
	"""How long this session must have been idle before it can be reaped.

	Known throwaway labels get THROWAWAY_GRACE_HOURS; everything else keeps the
	conservative ORPHAN_GRACE_HOURS. See _THROWAWAY_LABEL_PREFIXES for why the
	split is safe. An absent or non-string label falls through to the long
	grace."""
	label = entry.get("label") or ""
	if isinstance(label, str) and label.startswith(_THROWAWAY_LABEL_PREFIXES):
		return THROWAWAY_GRACE_HOURS * 3600 * 1000
	return ORPHAN_GRACE_HOURS * 3600 * 1000


def _reap_orphans(sess, budget: int, summary: dict) -> None:
	"""Part 3: chat-namespace gateway sessions no conversation references
	(title/prewarm/polish throwaways, deleted conversations), inactive past their
	grace window (per-session, see _grace_ms) and with no active run."""
	try:
		entries = sess.list_sessions()
	except Exception:
		frappe.log_error(
			title="session_lifecycle: sessions.list failed",
			message=frappe.get_traceback(),
		)
		summary["errors"] += 1
		return
	known = {
		k
		for (k,) in frappe.db.sql(
			"SELECT session_key FROM `tabJarvis Conversation` "
			"WHERE session_key IS NOT NULL AND session_key != ''"
		)
	}
	now_ms = int(time.time() * 1000)
	for entry in entries:
		if budget <= 0:
			return
		key = entry.get("key") or ""
		if _CHAT_NAMESPACE_MARKER not in key or key in known:
			continue
		if entry.get("hasActiveRun"):
			summary["skipped"] += 1
			continue
		updated = entry.get("updatedAt")
		if not isinstance(updated, (int, float)) or (now_ms - updated) < _grace_ms(entry):
			# No usable activity timestamp -> conservative skip; a fresh
			# throwaway or a just-created conversation session survives.
			summary["skipped"] += 1
			continue
		budget -= 1
		try:
			if _delete_gateway_session(sess, key, summary):
				# Deleted-conversation case: the lookup row may still exist.
				frappe.db.delete(CHAT_SESSION, {"session_key": key})
				frappe.db.commit()
				summary["orphans_reaped"] += 1
		except Exception:
			frappe.log_error(
				title="session_lifecycle: orphan cleanup failed",
				message=f"session_key={key}\n{frappe.get_traceback()}",
			)
			summary["errors"] += 1


def _delete_gateway_session(sess, session_key: str, summary: dict) -> bool:
	"""Best-effort sessions.delete. False (and an Error Log) on failure so
	the caller leaves the bench pointers intact for a retry next run. The
	gateway's refusal to delete the main session lands here as a normal
	failure - logged once per run at most per key, never fatal."""
	try:
		sess.delete_session(session_key)
		return True
	except Exception as e:
		# Idempotent: a session that is ALREADY gone counts as success.
		# This self-heals the crashed-between-delete-and-commit window -
		# the next run's delete "fails" as not-found and the bench
		# pointers finally get cleared instead of sticking forever.
		if "not found" in str(e).lower() or "unknown session" in str(e).lower():
			return True
		frappe.log_error(
			title="session_lifecycle: sessions.delete failed",
			message=f"session_key={session_key}\n{frappe.get_traceback()}",
		)
		summary["errors"] += 1
		return False


# --------------------------------------------------------------------------- #
# throwaway reclaim (called by the minters, not by the cron)
# --------------------------------------------------------------------------- #

# A throwaway one-shot used to call sessions.delete the instant its own turn
# returned. That is too early, and the gateway paid for it (issue #525):
#
# - stream_agent_turn returns on the run's lifecycle-end frame, but openclaw's
#   embedded run is still finalising the session file for a beat after that;
# - on EVERY error path stream_agent_turn RAISES while the run keeps going
#   server side (openclaw's run lane deliberately survives a client drop), and
#   the delete then ran from a finally with the run mid-flight;
# - prewarm's fire_agent never waits at all, so its session is by definition
#   still running when the next warm reclaims it.
#
# Deleting underneath a live run renames the session file out from under it.
# Observed on jarvis-pool-bf4097: an auto-title session deleted 194ms after its
# reply landed was RE-CREATED by the same run 113ms later (a fresh orphan the
# sweep then has to collect), and another died outright with
# "EmbeddedAttemptSessionTakeoverError: session file changed while embedded
# prompt lock was released". The failed run surfaces as a decision=surface_error
# ... next=none line that is indistinguishable in the log from a genuine
# failover failure, which is the expensive part of the bug.
#
# So ask the gateway before deleting. sessions.list -> hasActiveRun is the same
# signal _reap_orphans already trusts.
#
# PROBE FIRST, sleep only between retries. One of these callers
# (learning.polish, via learned_api's "Polish with AI" and follow-up-rephrase
# endpoints) runs INSIDE a synchronous whitelisted request, and title/polish
# hold one of only POOL_MAX_PER_GATEWAY=3 pooled connections while they do
# this. An unconditional settle would tax both on every call; a probe costs one
# sessions.list and only turns into a wait when the gateway actually says the
# run is live, which is precisely when waiting is the correct answer.
RECLAIM_PROBE_ATTEMPTS = 6
RECLAIM_PROBE_DELAY_S = 0.5

# ...but a probe alone cannot close the window, because openclaw ACCEPTS a run
# well before it STARTS one, and sessions.list reports "accepted, not started"
# exactly like "finished": hasActiveRun is false in both.
#
# Measured on jarvis-pool-bf4097, 269 sessions, as the gap between the session
# file's creation stamp (the sessions.create the bench issues immediately before
# it fires) and the run's own session.started trajectory event:
#
#     p50 0.67s   p90 2.90s   p95 4.84s   (86 over 1s, 25 over 3s)
#
# So for a median 670ms after a fire-and-forget the gateway will happily tell a
# reclaim "no run here", and the reclaim deletes the session out from under a
# run that is about to start. That is a check-then-act race, and no amount of
# probing fixes it: the probe is reading a signal that has not been written yet.
#
# What closes it is a caller that knows it never saw the run END. For those, a
# "no active run" answer is only believed once RUN_START_GRACE_S has passed
# since the fire, OR once a probe has actually caught the run active (positive
# evidence beats the clock - see `seen_active`). Callers that DID watch the run
# reach its terminal frame pass no fire time and keep the immediate reclaim.
#
# Sized just past the p95 above. We do not WAIT this out - waiting would tax
# polish's synchronous request for nothing - we simply decline to guess and let
# the orphan sweep collect it, which is the same trade the rest of this function
# already makes.
RUN_START_GRACE_S = 5.0


def reclaim_throwaway_session(
	sess,
	session_key: str,
	*,
	logger_name: str,
	fired_at: float | None = None,
) -> bool:
	"""Delete a throwaway session once the gateway stops reporting a run on it.

	Probes immediately, then re-checks after ``RECLAIM_PROBE_DELAY_S`` up to
	``RECLAIM_PROBE_ATTEMPTS`` times. Returns True when the session was deleted,
	False when it was left behind - still busy, not started yet, or the gateway
	would not answer.

	``fired_at`` is the wall-clock ``time.time()`` at which the run was fired,
	and MUST be passed by any caller that did not watch that run reach its
	terminal frame: prewarm (fire-and-forget by construction) and title/polish
	on the path where ``stream_agent_turn`` raised. It marks the answer
	"no active run" as untrustworthy until ``RUN_START_GRACE_S`` has elapsed,
	which is what keeps a not-yet-started run from being deleted. Callers that
	consumed the stream to its end pass nothing and reclaim immediately, as
	before - there is no unstarted run to protect.

	Leaving it behind is safe and deliberate: every throwaway label carries a
	``THROWAWAY_GRACE_HOURS`` grace and its own ``ORPHAN_BATCH_MAX`` budget in
	the hourly sweep above, so a skipped reclaim costs a delayed collection, not
	a leak. Killing a live run to hit the budget would be the worse trade - that
	is the whole defect this function exists to remove.

	RESIDUAL: ``hasActiveRun`` is the strongest finished-signal the gateway
	exposes, so a session openclaw has stopped counting as active but is still
	writing to would still be deleted underneath. Every case actually observed
	on jarvis-pool-bf4097 had the run's lane demonstrably still working when the
	delete landed (it re-created the session file afterwards), so the probe
	covers them; the sweep covers whatever it does not. On the start side, a run
	that takes longer than ``RUN_START_GRACE_S`` to appear is still exposed - 5%
	of the measured sample - and the sweep is the backstop there too.

	Never raises: every caller is a best-effort path whose real work is already
	done by the time it reclaims."""
	if not session_key or not isinstance(session_key, str):
		return False
	log = frappe.logger(logger_name)
	# Before this instant an idle answer only means "the run has not started
	# yet". 0.0 (no fire time given) => the caller saw the run end, trust at once.
	#
	# The isinstance check is not decoration: the arithmetic below sits OUTSIDE
	# the per-probe try, so a caller that ever handed over a non-numeric fire
	# time (a str from a cache round-trip, say) would raise straight through the
	# "never raises" contract every caller here relies on.
	trust_idle_after = (
		fired_at + RUN_START_GRACE_S if isinstance(fired_at, (int, float)) and fired_at else 0.0
	)
	seen_active = False
	for attempt in range(RECLAIM_PROBE_ATTEMPTS):
		try:
			if sess.is_run_active(session_key):
				# Positive evidence the run exists. Every later idle answer is now
				# a real finished-signal, so the grace above stops applying.
				seen_active = True
			elif seen_active or time.time() >= trust_idle_after:
				sess.delete_session(session_key)
				return True
		except Exception:
			# A gateway blip during probe or delete: hand it to the sweep
			# rather than retrying a delete whose outcome we cannot read.
			log.debug("throwaway session reclaim failed key=%s", session_key, exc_info=True)
			return False
		if attempt < RECLAIM_PROBE_ATTEMPTS - 1:
			time.sleep(RECLAIM_PROBE_DELAY_S)
	log.debug("throwaway session not confirmed finished, left for the sweep key=%s", session_key)
	return False
