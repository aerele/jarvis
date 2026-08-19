"""Monotonic tenant-authority generation guard for connection payloads.

Review plan 04 P0-5 (the stale-response race): admin resolves which container
serves a customer through an explicit ``authoritative_tenant`` link, and bumps a
``tenant_authority_generation`` counter by 1 on EVERY authority write (a move
cutover, an operator repair, a fresh assignment). Every connection-bearing
response from admin's ``get_connection`` now carries that generation plus, for a
live serving container, an opaque ``tenant_authority_handle`` (admin's sha256 of
the tenant docname - equal handle iff the same container, never the internal
``TEN-*`` id).

Without a bench-side rule the following race silently regresses a customer onto
their old container:

    1. bench poll P1 resolves old authority A and begins returning A's url/token;
    2. a move/repair advances authority generation N -> N+1 to container B;
    3. bench poll P2 returns B and is stored;
    4. slow P1 arrives LAST and overwrites the local connection with A.

The resolver can be perfectly correct at each read and the customer still ends on
the wrong container. This module is the bench's fix: it persists the last accepted
``(generation, handle)`` and, when a new connection payload arrives, decides
whether to accept it:

    * LOWER generation than stored  -> reject (ignore, keep current connection);
    * EQUAL generation:
        - same handle (or a handle we can't compare) -> accept, idempotent;
        - a DIFFERENT handle                          -> invariant breach, HOLD;
    * HIGHER generation             -> accept and advance the stored state;
    * NO generation at all (old admin) -> accept-and-warn WITHOUT regressing the
      stored generation (documented compatibility rule).

Generation ``0`` is the sentinel for "no authority-fenced connection accepted
yet": admin returns generation 0 while a customer's ``authoritative_tenant`` link
is still unset (the expected pre-backfill state, where the single live row is
served by recency). The monotonic/identity rules only bite once a real authority
write has advanced the generation to >= 1, so a customer in the pre-backfill
window is never falsely rejected or flagged.
"""

import frappe

GEN_FIELD = "tenant_authority_generation"
HANDLE_FIELD = "tenant_authority_handle"

# Decision outcomes.
ACCEPT = "accept"  # write the connection AND advance the stored (generation, handle)
COMPAT = "compat"  # old admin, no generation: write the connection, keep stored generation
REJECT = "reject"  # stale (lower) generation: ignore, keep the current connection

# One warning per distinct pair, so a bench polling every few seconds does not
# spam the error log while a slow (stale) or divergent (equal-gen, different
# handle) response keeps arriving. A transient dark-mode divergence that clears
# on the next poll logs at most once; a persistent one still surfaces.
_STALE_LOG_TTL_S = 300


class AuthorityInvariantError(Exception):
	"""A connection payload carries the SAME generation as the one we already
	accepted but names a DIFFERENT serving container (a different handle).

	Two distinct answers at one generation must never be resolved by guessing:
	overwriting could land the customer on the wrong container, so the bench HOLDS
	the current connection and lets the next poll retry ("hold + re-poll", never
	downgrade to the old container). Under strict authority the served row IS the
	authoritative row so this cannot happen; in dark mode generation and handle can
	momentarily diverge, which is exactly why the caller re-polls rather than
	treating it as fatal. It is still recorded (deduped) so a divergence that does
	NOT clear on the next poll is visible to operators."""


def _norm_generation(value) -> int | None:
	"""Normalise a stored/incoming generation to ``int`` or ``None``.

	``None`` (key absent) and ``0`` (admin's "no authority link yet" sentinel)
	both collapse to ``None`` for the STORED side, so the monotonic and identity
	rules apply only once a real authority write has advanced the generation to
	>= 1. An incoming value keeps its literal 0 (so a stored >= 1 can still reject
	a regression back to 0), which the callers below rely on."""
	if value in (None, "", 0, "0"):
		return None
	return int(value)


def decide(stored_gen, stored_handle, incoming_gen, incoming_handle) -> str:
	"""Pure monotonic-acceptance decision (unit-testable, no I/O).

	Returns :data:`ACCEPT`, :data:`COMPAT` or :data:`REJECT`; raises
	:class:`AuthorityInvariantError` on the equal-generation / different-handle
	breach. See the module docstring for the full rule table."""
	# Old admin sends no generation key at all: accept the connection but never
	# regress the stored generation to 0 (which would re-open the P0-5 race).
	if incoming_gen is None:
		return COMPAT

	incoming = int(incoming_gen)
	stored = _norm_generation(stored_gen)

	# First authority-fenced connection (fresh bench, post-reset, or pre-backfill
	# gen-0 window): nothing to compare against.
	if stored is None:
		return ACCEPT
	if incoming < stored:
		return REJECT
	if incoming == stored:
		# Identity check fires ONLY when both handles are known and differ - a
		# missing handle on either side (a non-serving payload, or the stored
		# state predating handles) cannot prove a mismatch, so it stays idempotent.
		if incoming_handle and stored_handle and incoming_handle != stored_handle:
			raise AuthorityInvariantError(
				f"tenant authority generation {incoming} names a different serving "
				f"container than the one already accepted at that generation "
				f"(stored handle {stored_handle!r}, incoming {incoming_handle!r})"
			)
		return ACCEPT
	# incoming > stored
	return ACCEPT


def guard(settings, data: dict) -> str:
	"""Decide whether ``data``'s connection may be written to ``settings``,
	advancing the stored ``(generation, handle)`` on :data:`ACCEPT`.

	Returns the outcome; raises :class:`AuthorityInvariantError` on the equal-gen
	/ different-handle breach. Never itself writes ``agent_url`` / ``agent_token``
	- the caller does that only when the outcome permits it - so the connection
	secrets and the authority receipt advance atomically together or not at all."""
	stored_gen = settings.get(GEN_FIELD)
	stored_handle = settings.get(HANDLE_FIELD) or None
	incoming_gen = data.get(GEN_FIELD)
	incoming_handle = data.get(HANDLE_FIELD) or None

	outcome = decide(stored_gen, stored_handle, incoming_gen, incoming_handle)
	if outcome == ACCEPT:
		settings.db_set(GEN_FIELD, int(incoming_gen))
		settings.db_set(HANDLE_FIELD, incoming_handle or "")
	return outcome


def log_stale_once(stored_gen, incoming_gen) -> None:
	"""Record a rejected stale connection payload, at most once per
	(stored, incoming) pair over :data:`_STALE_LOG_TTL_S`, so a bench polling on
	a tight cadence does not flood the error log while a slow response keeps
	arriving."""
	key = f"jarvis:tenant_authority_stale:{stored_gen}:{incoming_gen}"
	cache = frappe.cache()
	if cache.get_value(key, expires=True):
		return
	cache.set_value(key, 1, expires_in_sec=_STALE_LOG_TTL_S)
	frappe.log_error(
		title="tenant authority: ignored a stale connection (kept current)",
		message=(
			f"admin returned a connection at authority generation {incoming_gen}, "
			f"older than the {stored_gen} already accepted; kept the current "
			f"connection (review plan 04 P0-5)."
		),
	)


def log_invariant_once(gen, stored_handle, incoming_handle) -> None:
	"""Record an equal-generation / different-handle divergence, at most once per
	(stored, incoming) handle pair over :data:`_STALE_LOG_TTL_S`.

	The caller has already HELD the current connection and will re-poll; this is
	the observability half of "hold + re-poll". Deduped so a transient dark-mode
	divergence that clears on the next poll does not page repeatedly, while one
	that persists stays visible to operators."""
	key = f"jarvis:tenant_authority_invariant:{gen}:{stored_handle}:{incoming_handle}"
	cache = frappe.cache()
	if cache.get_value(key, expires=True):
		return
	cache.set_value(key, 1, expires_in_sec=_STALE_LOG_TTL_S)
	frappe.log_error(
		title="tenant authority: held connection on equal-gen handle divergence",
		message=(
			f"admin returned a connection at authority generation {gen} naming a "
			f"different serving container ({incoming_handle!r}) than the one already "
			f"accepted at that generation ({stored_handle!r}); held the current "
			f"connection and will re-poll, never downgraded (review plan 04 P0-5)."
		),
	)


def clear(settings) -> None:
	"""Forget the accepted authority state (generation -> 0, handle -> "").

	Called by the reset paths that deliberately tear down or re-point the
	connection - self-serve workspace reset and account reconnect - so the NEXT
	connection is accepted on its own terms rather than being rejected as "older"
	than a generation that belonged to the previous tenancy. The reset-onboarding
	CLI clears the same two fields through ``settings_reset.CONNECTION``."""
	settings.db_set(GEN_FIELD, 0)
	settings.db_set(HANDLE_FIELD, "")
