"""Post-reply chat feedback: a thumbs up/down (with an optional note on a down)
on an assistant reply, forwarded to the admin fleet dashboard.

Direct-send, no local table: this bench keeps NO copy. ``submit_feedback``
derives every piece of metadata server-side (tenant is derived on the admin side
from the authenticated principal), then forwards it best-effort. A failed forward
is dropped silently - feedback is low-stakes, and a blocked tap is not acceptable.
The reply text is never sent; only the rating, refs, and a bounded optional note.

Only the caller's OWN assistant messages can be rated (ownership via the same
gate the rest of the chat surface uses, ``chat.api._get_owned_conversation``).

The same direct-send, no-local-table rule governs the once-per-session popup
("How did this session go?") at the bottom of this module: only the popup's
own metadata (``Jarvis Conversation.session_feedback_asked_at``, and the
``turn_count`` that triggers it) is kept on the bench - the response itself
lives only on the admin side.

The periodic "business pulse" survey (last section) follows the same rule and
keeps exactly two fields, both on ``Jarvis User Settings``:
``pulse_last_period_key`` and ``pulse_offer_count``. There is no cron behind
it - the fleet scheduler is paused, so a scheduled survey would silently never
run. Instead it reuses the lazy period-key rollover the per-user token cap
already ships (``jarvis.chat.usage.current_period_key``, jarvis#1165): the
current monthly bucket is compared against the stored key on chat open, and a
mismatch IS the rollover.
"""

from __future__ import annotations

import re

import frappe

from jarvis.chat.api import _get_owned_conversation
from jarvis.permissions import require_jarvis_access

MSG = "Jarvis Chat Message"
CONV = "Jarvis Conversation"
USER_SETTINGS = "Jarvis User Settings"

_RATINGS = {"up", "down"}
_MAX_NOTE = 1000
#: The agent session UUID lives as the last segment of a composite session_key
#: like "agent:main:dashboard:<uuid>". Empty when absent/malformed - the rating
#: still records; only the admin-side deep-link is best-effort.
_SESSION_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")

#: Turns a conversation must have settled before the once-per-session popup is
#: due. Hardcoded for v1 (spec "Settings"): there is no precedent for admin
#: pushing a setting down to bench Jarvis Settings, so a configurable field would
#: be a surface with no way to drive it.
SESSION_FEEDBACK_TURN_THRESHOLD = 10
#: The five preset reactions the popup offers, worst to best. Mirrored by the
#: admin ingest's own allowlist (never trust the bench) and by the SPA dialog.
_SESSION_CHIP_VALUES = {"Great", "Good", "Okay", "Not great", "Frustrating"}
#: The reactions that reveal the optional "what went wrong?" line. "Okay" counts:
#: the spec's rule is "Okay or worse", not just the bottom two.
_SESSION_LOW_CHIPS = {"Okay", "Not great", "Frustrating"}


@frappe.whitelist()
def submit_feedback(message_id: str, rating: str, note: str | None = None) -> dict:
	"""Record a thumbs up/down on one assistant reply and forward it to admin.

	Args:
		message_id: the Jarvis Chat Message name of the assistant reply.
		rating: "up" or "down".
		note: optional free text, kept only on a "down".

	The rating commits on the first call (thumbs tap); a later call carrying the
	note folds onto the same admin row (upsert). Returns ``{"ok": True}`` even when
	the forward fails - the tap must never surface an error.
	"""
	require_jarvis_access()
	rating = (rating or "").strip()
	if rating not in _RATINGS:
		frappe.throw("rating must be 'up' or 'down'", frappe.ValidationError)

	# Raw db read bypasses field permlevel; we only need these four columns.
	msg = frappe.db.get_value(
		MSG, message_id, ["conversation", "role", "model", "reply_duration_ms"], as_dict=True
	)
	if not msg:
		frappe.throw("message not found", frappe.DoesNotExistError)
	if msg.role != "assistant":
		frappe.throw("can only rate assistant replies", frappe.ValidationError)

	# Ownership: the canonical chat gate (raises PermissionError for another user's
	# conversation, DoesNotExistError if it vanished). session_key is permlevel-1;
	# read it via db.get_value to bypass that, matching how the worker touches it.
	_get_owned_conversation(msg.conversation)
	session_key = frappe.db.get_value(CONV, msg.conversation, "session_key")

	payload = {
		"rating": rating,
		"message_ref": message_id,
		"conversation_ref": msg.conversation,
		"session_id": _session_uuid(session_key),
		"model": msg.model or "",
		"user_ref": frappe.session.user,
		"reply_duration_ms": msg.reply_duration_ms or 0,
		"note": (note or "").strip()[:_MAX_NOTE] if rating == "down" else "",
	}
	_forward(payload)
	return {"ok": True}


def _session_uuid(session_key: str | None) -> str:
	"""Bare agent session UUID from a composite session_key, or "" when absent."""
	tail = (session_key or "").rsplit(":", 1)[-1]
	return tail if _SESSION_UUID_RE.match(tail) else ""


def _forward(payload: dict) -> None:
	"""Best-effort forward to admin. NEVER raises to the caller: a lost rating is
	acceptable (low-stakes), a blocked or errored tap is not."""
	try:
		from jarvis import admin_client

		admin_client.push_chat_feedback(payload)
	except Exception:
		frappe.log_error(title="chat feedback forward failed")


# --------------------------------------------------------------------------- #
# Once-per-session popup ("How did this session go?")
# --------------------------------------------------------------------------- #


@frappe.whitelist()
def session_feedback_status(conversation: str) -> dict:
	"""Whether the once-per-session popup is due for this conversation now.

	Costs ONE indexed row read (the ownership gate's own load) and no scan:
	``turn_count`` is a maintained counter bumped per settled turn by
	``jarvis.chat.settlement._bump_turn_count``, never a live ``COUNT(*)`` - this
	is polled after every assistant reply, so a scan here would be the most
	expensive query the feature adds.

	Not due for a conversation the user did not start: a File Box drop
	(``file_box``) or an agent-opened run log (``agent_initiated`` - a macro run,
	a macro merge, an app-learning run, a scheduled audit, a proactive message,
	an act-on-a-finding chat). Not due either once the popup has been answered or
	skipped here - ``session_feedback_asked_at`` is final, so Skip means "never
	again in this conversation" and survives a reload or a second tab.
	"""
	require_jarvis_access()
	# The canonical ownership gate; it already loads the row, so read the four
	# fields off the doc it returns rather than issuing a second query.
	conv = _get_owned_conversation(conversation)
	if conv.file_box or conv.agent_initiated:
		return {"due": False}
	due = int(conv.turn_count or 0) >= SESSION_FEEDBACK_TURN_THRESHOLD and not conv.session_feedback_asked_at
	return {"due": bool(due)}


@frappe.whitelist(methods=["POST"])
def submit_session_feedback(
	conversation: str, chip_value: str | None = None, note: str | None = None
) -> dict:
	"""Record the once-per-session popup's answer, or a Skip (``chip_value`` unset).

	The response is recorded AT MOST ONCE per conversation. Claiming the popup and
	stamping ``session_feedback_asked_at`` is a single compare-and-set (see
	``_claim_session_feedback``) that is committed before the admin forward, so two
	tabs - or a double-click that beats the dialog's own disable - cannot both
	forward: the loser returns ``recorded: False`` and forwards nothing, and the
	winner never holds a row lock across the network call. A Skip claims the popup exactly the
	same way and is never forwarded to admin (there is no response to store); a
	real reaction is forwarded best-effort. The optional note is kept only on
	"Okay" or worse - the popup only reveals the field there, and a note typed
	before switching to a positive reaction must not ride along.

	A malformed reaction is rejected BEFORE the claim: the one-shot popup is the
	user's only chance to answer in this conversation, so a broken client must
	not silently burn it.

	Returns ``{"ok": True, "recorded": <bool>}`` even when the forward fails: like
	the thumbs tap, a lost response is acceptable and a blocked popup is not.
	"""
	require_jarvis_access()
	conv = _get_owned_conversation(conversation)
	if chip_value and chip_value not in _SESSION_CHIP_VALUES:
		frappe.throw("chip_value is not one of the preset reactions", frappe.ValidationError)

	# Short-circuit on the already-loaded doc first (the common repeat: a reload or
	# a second tab opened later), then settle a genuine race with the CAS.
	if conv.session_feedback_asked_at or not _claim_session_feedback(conversation):
		return {"ok": True, "recorded": False}
	# Durability-before-external-call (the ONE sanctioned reason for an explicit
	# commit here, and the same thing api.create_conversation does): make the claim
	# durable and RELEASE the conversation row lock BEFORE the admin HTTPS forward
	# below. Frappe would otherwise not commit until the end of the request, holding
	# that row lock for the whole admin round-trip - up to admin_client's timeout
	# when admin is unreachable. Anything touching this conversation meanwhile would
	# block on it: settlement's turn-count bump (delaying a run:end), and
	# admission.accept_or_queue's _lock_conversation - which waits while holding the
	# SITE-WIDE shard lock, turning one slow popup into a shard-wide send stall.
	frappe.db.commit()
	if not chip_value:
		return {"ok": True, "recorded": False}

	payload = {
		"kind": "Session",
		"session_ref": conversation,
		"chip_value": chip_value,
		"user_ref": frappe.session.user,
		"note": (note or "").strip()[:_MAX_NOTE] if chip_value in _SESSION_LOW_CHIPS else "",
	}
	_forward_session(payload)
	return {"ok": True, "recorded": True}


def _claim_session_feedback(conversation: str) -> bool:
	"""Stamp ``session_feedback_asked_at`` and return True iff THIS call claimed
	the one-shot popup.

	A conditional UPDATE rather than ``db.set_value``, because a read-then-write
	leaves a window in which two tabs (or a double-click that beats the dialog's
	disable) both decide the popup is unanswered and both forward. The admin side
	upserts on ``(tenant, kind, session_ref)``, so the second push would silently
	OVERWRITE the user's real first answer rather than duplicate it - a lost
	response, not just a noisy one. ``session_feedback_asked_at IS NULL`` in the
	WHERE makes the claim atomic: exactly one caller sees rowcount 1.

	``modified`` is deliberately left alone (the ``update_modified=False``
	equivalent): this is server-set popup metadata, not a user edit of the
	conversation, and must not reorder the sidebar."""
	frappe.db.sql(
		"""UPDATE `tabJarvis Conversation` SET session_feedback_asked_at=%(now)s
		WHERE name=%(c)s AND session_feedback_asked_at IS NULL""",
		{"now": frappe.utils.now(), "c": conversation},
	)
	# Rowcount is read BEFORE any commit (a commit can reset the cursor) - the same
	# discipline jarvis.chat.turn_state._run_cas and admission._run_cas follow.
	cursor = getattr(frappe.db, "_cursor", None)
	return bool(cursor and int(cursor.rowcount) == 1)


def _forward_session(payload: dict) -> None:
	"""Best-effort forward to admin, same contract as ``_forward`` above: a lost
	response is acceptable, a blocked popup is not. Named verbatim in
	``admin_client.push_session_feedback``'s docstring - keep the two in step."""
	try:
		from jarvis import admin_client

		admin_client.push_session_feedback(payload)
	except Exception:
		frappe.log_error(title="session feedback forward failed")


# --------------------------------------------------------------------------- #
# Periodic "business pulse" survey
# --------------------------------------------------------------------------- #

#: Cadence of the pulse survey, spelled the way ``usage.current_period_key``
#: expects it (it shares ``limit_period``'s vocabulary). Hardcoded for v1, for
#: the same reason as SESSION_FEEDBACK_TURN_THRESHOLD above.
PULSE_SURVEY_CADENCE = "Monthly"
#: Offers per period before the survey goes quiet. "Maybe later" is not
#: recorded anywhere: it simply lets the next chat open offer again, three
#: times in all, rather than nagging for the rest of the month. Answering
#: stores this value outright - see ``_store_pulse_state``.
PULSE_MAX_OFFERS = 3
#: The window "used this period" is measured over. Monthly cadence -> the last
#: 30 days (spec: "monthly -> last 30 days"), a rolling window rather than the
#: calendar bucket, so a survey offered on the 2nd still has something to show.
_PULSE_WINDOW_DAYS = 30
#: Bound on the free-text "is Jarvis solving your business use case?" answer.
_MAX_USE_CASE = 1000


def _pulse_state_columns_present() -> bool:
	"""False while the app runs ahead of ``bench migrate`` (a dev-server reload,
	a worker restarted early). Reading or writing a column that is not there yet
	would 500 the chat open, where "never due" is the right fallback: the survey
	is not worth an error, and it comes back on its own after the migrate.

	Same guard, and same reasoning, as ``usage.period_select_fields``; Frappe
	caches the table's column list, so this is not a per-call DESCRIBE."""
	try:
		return bool(frappe.db.has_column(USER_SETTINGS, "pulse_last_period_key"))
	except Exception:
		return False


def _pulse_window_start():
	"""Start of the rolling usage window the survey asks about."""
	return frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-_PULSE_WINDOW_DAYS)


def _pulse_period_label(now=None) -> str:
	"""Badge text for the dialog, e.g. "This month, Sep 2026"."""
	now = now or frappe.utils.now_datetime()
	return now.strftime("This month, %b %Y")


def _turns_since(user: str, since) -> int:
	"""Total settled turns across this user's OWN conversations last active
	since ``since``.

	A real SUM of the maintained ``turn_count`` counter, not a proxy like "has a
	conversation": a user who opened chats but never sent anything has not used
	Jarvis this period and must not be surveyed. ``file_box`` and
	``agent_initiated`` conversations contribute 0 by construction (the counter
	never advances on them - see ``settlement._bump_turn_count``), so they are
	excluded without needing a filter.

	One query, scoped to one user, and NOT on a hot path: it is reached only
	after the period-key gate says an offer is still possible, so once the user
	has any turns at all it runs at most ``PULSE_MAX_OFFERS`` times per period.
	``owner`` carries no index on this table (Frappe indexes only ``modified``
	by default), so this is a per-tenant table scan - acceptable at that rate,
	and precisely why it must stay behind the cheap gate."""
	from frappe.query_builder.functions import Sum

	conv = frappe.qb.DocType(CONV)
	rows = (
		frappe.qb.from_(conv)
		.select(Sum(conv.turn_count).as_("total"))
		.where((conv.owner == user) & (conv.last_active_at >= since))
	).run(as_dict=True)
	return int((rows[0].total if rows else 0) or 0)


def _store_pulse_state(settings_name: str, period_key: str, offer_count: int) -> None:
	"""Stamp the period key and the offer count on the user's settings row.
	The ANSWER path's write: unconditional, because an answer always wins over
	whatever the offer count was. Offers themselves go through
	``_claim_pulse_offer`` below, which is conditional.

	``update_modified=False``: server-owned app state (permlevel 1), not a user
	edit of their settings.

	Callers pass ``PULSE_MAX_OFFERS`` to mean "quiet for the rest of this
	period". Two states share that value deliberately - "answered" and "offered
	three times" - because the outcome is identical and the spec allots the
	survey exactly two fields."""
	if not _pulse_state_columns_present():
		return
	frappe.db.set_value(
		USER_SETTINGS,
		settings_name,
		{"pulse_last_period_key": period_key, "pulse_offer_count": offer_count},
		update_modified=False,
	)


def _claim_pulse_offer(settings_name: str, period_key: str, seen_count: int, same_period: bool) -> bool:
	"""Advance the offer count to ``seen_count + 1`` and return True iff THIS
	call claimed the offer, i.e. the row still read exactly as the caller saw it.

	A compare-and-set rather than ``_store_pulse_state``, for the same reason
	``_claim_session_feedback`` is one. ``pulse_context`` reads the count, then
	runs the comparatively slow feature-usage sweep, then writes. In that window
	the user can ANSWER the survey in another tab (``submit_pulse_feedback``
	stores ``PULSE_MAX_OFFERS``), and a blind ``count + 1`` write afterwards
	would drag the answered marker back DOWN to 2 and re-offer a survey the user
	already filled in. The same window lets two chat opens racing on one
	settings row both read 0 and both burn an offer. With the WHERE clause
	pinned to what was read, exactly one writer sees rowcount 1; the others
	report "not due" and burn nothing.

	Two predicates, not one NULL-safe expression, because the two branches
	compare different things:
	  * same period - the key matches AND the count is still what we read;
	  * rollover (or a row created moments ago by ``get_or_create_user_settings``,
	    whose key is NULL and count 0) - the key is anything but the current one.
	    The count is NOT compared here: last period's total is irrelevant, and
	    the reset to 1 is the whole point.

	Raw SQL is the standing-rule exception the session claim already takes:
	``db.set_value`` cannot express a conditional write, and the rowcount is
	read BEFORE any commit (a commit can reset the cursor), the discipline
	``turn_state._run_cas`` and ``admission._run_cas`` follow. ``modified`` is
	left alone: server-owned state, not a user edit."""
	if same_period:
		frappe.db.sql(
			"""UPDATE `tabJarvis User Settings`
			SET pulse_offer_count=%(next)s
			WHERE name=%(n)s AND pulse_last_period_key=%(k)s AND pulse_offer_count=%(seen)s""",
			{"n": settings_name, "k": period_key, "seen": seen_count, "next": seen_count + 1},
		)
	else:
		frappe.db.sql(
			"""UPDATE `tabJarvis User Settings`
			SET pulse_last_period_key=%(k)s, pulse_offer_count=1
			WHERE name=%(n)s AND (pulse_last_period_key IS NULL OR pulse_last_period_key != %(k)s)""",
			{"n": settings_name, "k": period_key},
		)
	cursor = getattr(frappe.db, "_cursor", None)
	return bool(cursor and int(cursor.rowcount) == 1)


@frappe.whitelist(methods=["POST"])
def pulse_context() -> dict:
	"""Whether the periodic business-pulse survey is due for the current user
	right now and, if so, the period label and the dynamic feature list.

	POST-only despite the getter-shaped name, and that is load-bearing: this
	endpoint WRITES (it claims the offer below), and Frappe commits only for
	unsafe methods - reached over GET the claim would be rolled back silently
	and the anti-nag cap would never advance. Same reason, and the same
	annotation, as ``release_notice.check`` and ``maintenance_notice.check``.

	Called on every chat open, so it is ordered cheapest-gate-first and returns
	``{"due": False}`` almost always. The comparatively expensive feature-usage
	sweep (7 sources, two of them bounded scans) runs ONLY once the period key
	and the turn gate have both said an offer is really happening - it must
	never be computed speculatively (spec: performance review).

	Returning ``due`` also CLAIMS the offer: the count is incremented here, not
	when the user answers, because "Maybe later" and a closed tab look the same
	to the server and both have to count against the cap."""
	require_jarvis_access()
	if not _pulse_state_columns_present():
		return {"due": False}
	from jarvis.chat.usage import current_period_key

	user = frappe.session.user
	current_key = current_period_key(PULSE_SURVEY_CADENCE)
	# READ, never ``get_doc``: the settings doc carries a child table
	# (``user_model_usage``) and this runs on every chat open, so the deciding
	# path is three columns off one row - the same shape ``greeting._get_pref``
	# uses for its own proactive-card state. ``get_or_create_user_settings``
	# stays for the one path that genuinely needs a row: claiming an offer.
	row = frappe.db.get_value(
		USER_SETTINGS,
		{"user": user},
		["name", "pulse_last_period_key", "pulse_offer_count"],
		as_dict=True,
	)
	same_period = bool(row) and row.pulse_last_period_key == current_key
	# A key from an earlier period IS the rollover: the count restarts at 0
	# rather than carrying last month's exhausted total forward.
	offer_count = int(row.pulse_offer_count or 0) if same_period else 0
	if offer_count >= PULSE_MAX_OFFERS:
		return {"due": False}
	since = _pulse_window_start()
	if _turns_since(user, since) < 1:
		return {"due": False}
	from jarvis.chat.feature_usage import get_used_features

	# May be empty, and the survey is still due: the stars and the open question
	# are worth asking regardless. Only the chip question hides (spec: "hidden
	# if empty"), which the dialog decides from this list.
	features = get_used_features(user, since)
	# A user chatting before their settings row exists is normal (the row is
	# created lazily by whichever surface needs it first), and an offer has to be
	# recorded somewhere, so this is where the create belongs.
	if row:
		settings_name = row.name
	else:
		from jarvis.chat.usage import get_or_create_user_settings

		settings_name = get_or_create_user_settings(user).name
	# Conditional on the row still reading as it did above: the sweep just ran
	# is exactly the window in which an answer in another tab, or a second chat
	# open, can have moved it. Losing the race means "not due", never a
	# re-offer of an answered survey (see _claim_pulse_offer).
	if not _claim_pulse_offer(settings_name, current_key, offer_count, same_period):
		return {"due": False}
	return {
		"due": True,
		"period_label": _pulse_period_label(),
		"period_key": current_key,
		"features_offered": _feature_keys(list(features)),
	}


def _feature_keys(value) -> list:
	"""The tracked feature keys in ``value``, in ``feature_usage.FEATURES``
	order.

	An allowlist filter of the same shape as ``_SESSION_CHIP_VALUES`` above,
	not just a length bound: the submitted lists come from the client, so only
	keys this bench can actually report survive. That drops junk and duplicates
	and caps the list at 7 by construction. The JSON string form is accepted
	too - ``frappe.client`` passes list arguments as JSON over HTTP."""
	from jarvis.chat.feature_usage import FEATURES

	if isinstance(value, str):
		try:
			value = frappe.parse_json(value)
		except Exception:
			return []
	if not isinstance(value, list):
		return []
	picked = {item for item in value if isinstance(item, str)}
	return [feature for feature in FEATURES if feature in picked]


@frappe.whitelist(methods=["POST"])
def submit_pulse_feedback(
	stars: int,
	features_offered: list | str,
	features_selected: list | str,
	use_case_text: str = "",
	note: str | None = None,
) -> dict:
	"""Record one business-pulse response: forward it to admin, then silence the
	survey for the rest of this period.

	POST-only for the same reason as ``pulse_context`` above: it writes, and a
	GET would be rolled back - here that would forward the answer to admin and
	then re-offer the survey on the next chat open.

	The full set that was OFFERED rides along with the subset the user picked as
	most used, so the admin dashboard can tell "used it but did not pick it as
	top" from "never offered because unused" (spec: Dynamic feature list).

	A malformed rating is rejected BEFORE anything is written or sent, so a
	broken client cannot burn the user's survey for the month on a value that
	was never a real answer.

	Forward first, THEN write: ``frappe.db.set_value`` holds the settings row
	lock until the request commits, and that row is the one every turn's usage
	accounting also updates. Writing first would hold that lock across the admin
	HTTPS round-trip - up to ``admin_client``'s timeout when admin is
	unreachable - and stall this user's own chat, the same hazard
	``submit_session_feedback`` commits early to avoid. The write still happens
	when the forward fails: the user answered, and asking them again is worse
	than losing one low-stakes response (the trade the thumbs tap already
	makes)."""
	require_jarvis_access()
	try:
		stars = int(stars)
	except (TypeError, ValueError):
		# Over HTTP every argument arrives as a string, so a broken client sends
		# a non-numeric rating as easily as an out-of-range one; both are the
		# same 417 to the caller, never a 500.
		frappe.throw("stars must be between 1 and 5", frappe.ValidationError)
	if not 1 <= stars <= 5:
		frappe.throw("stars must be between 1 and 5", frappe.ValidationError)
	from jarvis.chat.usage import current_period_key, get_or_create_user_settings

	current_key = current_period_key(PULSE_SURVEY_CADENCE)
	payload = {
		"kind": "Pulse",
		"period_key": current_key,
		"stars": stars,
		"features_offered": _feature_keys(features_offered),
		"features_selected": _feature_keys(features_selected),
		"use_case_text": (use_case_text or "").strip()[:_MAX_USE_CASE],
		"user_ref": frappe.session.user,
		"note": (note or "").strip()[:_MAX_NOTE],
	}
	_forward_pulse(payload)
	# Resolved AFTER the forward for the same reason the write is: on a user
	# whose settings row does not exist yet this INSERTs one, and an uncommitted
	# insert holds its own row lock just as an update does.
	row = get_or_create_user_settings(frappe.session.user)
	_store_pulse_state(row.name, current_key, PULSE_MAX_OFFERS)
	return {"ok": True}


def _forward_pulse(payload: dict) -> None:
	"""Best-effort forward to admin, same contract as ``_forward`` and
	``_forward_session`` above: a lost response is acceptable, a blocked dialog
	is not. Named verbatim in ``admin_client.push_pulse_feedback``'s docstring -
	keep the two in step."""
	try:
		from jarvis import admin_client

		admin_client.push_pulse_feedback(payload)
	except Exception:
		frappe.log_error(title="pulse feedback forward failed")
