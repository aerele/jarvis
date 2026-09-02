"""egress_rules — the bench's cache + accessor for the control-plane-owned egress
redaction rules, plus the thin apply wrapper the chat egress path calls.

WHY THIS EXISTS. The customer app is white-label and names no runtime brand, so
the brand-token pattern list is owned by the control plane and delivered on the
``get_connection`` poll the bench already makes. :func:`persist` mirrors it onto
Jarvis Settings (the same "cache a CP-owned value locally" shape as
``release_notice``); :func:`get_rules` compiles + memoizes it; the generic,
literal-free :mod:`jarvis.chat.egress_redact` does the actual scrubbing.

FAIL-OPEN throughout: a bench that has never pulled the list (or hit any error)
simply does not redact — never breaks message delivery. The tool-deny and persona
layers are the other lines of defence, so a momentary no-redaction bench is a
degraded backstop, not an open leak.
"""

import frappe

from jarvis.chat import egress_redact

SETTINGS = "Jarvis Settings"
_PATTERNS_FIELD = "redaction_patterns"
_SYNCED_AT_FIELD = "redaction_patterns_synced_at"

#: Per-request memo of ``(raw_blob, compiled_rules)`` on ``frappe.local`` — the
#: raw blob is the key, so a mid-process pattern update is picked up (the blob
#: changes) rather than served stale, while an unchanged blob skips recompilation.
_MEMO_ATTR = "jarvis_egress_rules_memo"


def persist(patterns) -> None:
	"""Mirror the backend-sent redaction patterns onto Jarvis Settings.

	LAST-KNOWN-GOOD: ``patterns is None`` (the backend omitted the key — a degraded/
	pending/suspended connection payload, or a backend predating this
	feature) is treated as "no update", NOT as "clear" — a transient degraded poll
	must never wipe a working backstop. Only an explicit list (including ``[]``, the
	deliberate kill-switch) is written.

	Idempotent + best-effort: skips the write when nothing changed (churning
	``modified`` would collide with an operator editing the Settings form, and this
	runs on every connection poll), reads uncached so an in-request re-read sees the
	write, stamps ``synced_at`` only on a real change, and clears the per-request
	memo so a same-request reader never serves the stale compile."""
	try:
		if patterns is None:
			return  # key absent -> keep last-known-good; never wipe the backstop
		blob = frappe.as_json(patterns)
		current = frappe.db.get_single_value(SETTINGS, _PATTERNS_FIELD, cache=False) or ""
		if current == blob:
			return
		frappe.db.set_value(
			SETTINGS,
			SETTINGS,
			{_PATTERNS_FIELD: blob, _SYNCED_AT_FIELD: frappe.utils.now_datetime()},
			update_modified=False,
		)
		_invalidate_memo()
	except Exception as e:
		_log_swallowed("persist", e)


def _log_swallowed(where: str, exc: Exception) -> None:
	"""Record a swallowed redaction-layer error at most once per hour per site, so a
	genuine bug (corrupt cached blob, a ``compile_rules`` regression) is
	DISTINGUISHABLE from the benign "no patterns cached yet" state instead of
	vanishing silently into fail-open. Logs only the exception TYPE (never text, to
	re-leak nothing) and never raises (a logging failure must not break fail-open)."""
	try:
		key = f"jarvis:egress_log:{where}"
		if frappe.cache().get_value(key):
			return
		frappe.cache().set_value(key, "1", expires_in_sec=3600)
		frappe.logger("jarvis.egress").warning(f"egress redaction error in {where}: {type(exc).__name__}")
	except Exception:
		pass


def _invalidate_memo() -> None:
	"""Drop the per-request compiled-rules memo (see :data:`_MEMO_ATTR`)."""
	try:
		if hasattr(frappe.local, _MEMO_ATTR):
			delattr(frappe.local, _MEMO_ATTR)
	except Exception:
		pass


def get_rules() -> list:
	"""Compiled ``[regex, ...]`` (remove-only) from the cached CP patterns.

	Returns ``[]`` when nothing is cached or on ANY error — fail-open: a redactor
	that cannot load its rules must not break chat, and an empty rule set makes
	:func:`egress_redact.redact_egress` a no-op. The cached blob is read on every
	call (cheap — a cached Single read), but the regex compilation is memoized on
	``frappe.local`` keyed by that blob, so repeated per-frame calls in one turn
	recompile at most once, and a mid-process pattern change is still picked up."""
	try:
		blob = frappe.db.get_single_value(SETTINGS, _PATTERNS_FIELD, cache=True) or ""
		memo = getattr(frappe.local, _MEMO_ATTR, None)
		if memo is not None and memo[0] == blob:
			return memo[1]
		raw = frappe.parse_json(blob) if blob else []
		rules = egress_redact.compile_rules(raw if isinstance(raw, list) else [])
		setattr(frappe.local, _MEMO_ATTR, (blob, rules))
		return rules
	except Exception as e:
		_log_swallowed("get_rules", e)
		return []


def redact(text):
	"""Scrub the cached brand-family rules out of ``text`` and return the cleaned
	text (dropping the tripwire signal). For the streaming / tool-title / error
	boundaries that must not raise per frame. Total / fail-open: returns ``text``
	unchanged on any error, and is a fast no-op when no rules are cached."""
	try:
		out, _hit = egress_redact.redact_egress(text, get_rules())
		return out
	except Exception:
		return text


#: The tripwire's fixed message: brand-free and value-free (records NOTHING of the
#: leaked text - the app names no runtime brand, and a constant message fingerprints
#: stably so repeated hits fold into ONE feed row at count=N rather than flooding).
_TRIPWIRE_MESSAGE = "Outbound agent text matched an egress redaction rule (white-label backstop)."


def redact_and_flag(text, *, conversation=None, run_id=None):
	"""Like :func:`redact`, but on a hit ALSO fires a best-effort tripwire so
	operators learn a brand token reached an egress boundary (i.e. the tool-deny /
	persona layers had a gap). Use only at the AUTHORITATIVE per-turn boundaries
	(the final-text and recovery paths) - NOT per streaming frame - so the feed gets
	at most one row per turn, not one per delta. ``conversation``/``run_id``, when
	the caller has them, ride into the row so an operator can jump to the transcript.

	Short-circuits (and never flags) on a non-string / empty ``text``; never raises
	- a tripwire failure must not break message delivery."""
	try:
		if not isinstance(text, str) or not text:
			return text
		out, hit = egress_redact.redact_egress(text, get_rules())
		if hit:
			_fire_tripwire(conversation=conversation, run_id=run_id)
		return out
	except Exception:
		return text


def _fire_tripwire(*, conversation=None, run_id=None) -> None:
	"""Record ONE brand-free tripwire row via the existing client-error pipeline
	(Jarvis Client Error -> the */5 error_push rollup -> the admin tenant error
	feed). Records NO leaked text (only the constant message + identifiers). The
	``jarvis-`` surface keeps the row past api_errors' non-Jarvis origin filter.

	SYNCHRONOUS + best-effort: it only runs on a hit (rare - the backstop already
	failed), at most once per turn, and swallows every error. Under the rare case of
	two concurrent same-user leaking turns it may briefly wait on api_errors' per-
	(user, fingerprint) dedup lock; that is accepted for a rare backstop rather than
	adding an async hop."""
	try:
		from jarvis import api_errors

		api_errors._ingest_one(
			{
				"surface": "jarvis-egress-redaction",
				"error_code": "egress_redaction",
				"error_class": "EgressRedactionHit",
				"message": _TRIPWIRE_MESSAGE,
				"conversation": conversation or "",
				"run_id": run_id or "",
			},
			frappe.session.user,
		)
	except Exception:
		pass
