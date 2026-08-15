"""Recurring business-note greeting (server side).

Jarvis nudges a System User to share Business voice notes by surfacing an
in-chat banner card on every third genuinely-new chat, until the user either
records a Business note or explicitly dismisses. This module is a pure reader
plus a counter bump; ALL gating lives here (never at the call site). No
conversation is created and no Redis state is used.

- ``Dismissed`` state (the permanent "Don't ask again")  -> never show.
- any Business note the user already wrote               -> never show.
- operator flag ``voice_features_enabled`` off           -> never show.
- otherwise show when the new-chat count is a positive multiple of 3.

Durability (the one hard rule): the new-chat counter and the permanent
"Don't ask again" flag both live in the ``Jarvis User Settings`` DocType (a
real DB row), so ``bench clear-cache`` / Redis eviction can never resurrect a
greeting the user killed nor reset the cadence. The cadence itself is the
deferral: "Maybe later" pins the current count to
``business_greeting_hidden_at_count`` (so refreshes stay quiet for the rest of
that tick), and the card naturally returns on the next multiple-of-three chat.
"""

import frappe
from frappe.utils import cint

from jarvis.chat.usage import get_or_create_user_settings
from jarvis.chat.voice_notes_api import _require_system_user

PREF = "Jarvis User Settings"
NOTE = "Jarvis Voice Note"
_SETTINGS = "Jarvis Settings"


def _stt_enabled() -> bool:
	"""Whether speech-to-text is configured (mirrors get_business_status)."""
	try:
		from jarvis.chat import voice

		return bool(voice.stt_config())
	except Exception:
		return False


def _voice_features_enabled() -> bool:
	"""Operator toggle, NULL=ON (the ``_flag_on`` idiom, voice_facts.py:673):
	Single defaults are not backfilled on migrate, so an unset row must read
	ON. Probes tabSingles directly because get_single_value coerces an unset
	Check to 0, which is indistinguishable from operator-off."""
	try:
		row = frappe.db.sql(
			"select value from tabSingles where doctype=%s and field=%s",
			(_SETTINGS, "voice_features_enabled"),
		)
	except Exception:
		return True
	if not row:
		return True
	return bool(cint(row[0][0]))


def _get_pref(user: str) -> frappe._dict | None:
	"""Return the user's greeting state row (doc name == user, autoname
	field:user) or None when never written."""
	return (
		frappe.db.get_value(
			PREF,
			{"user": user},
			[
				"business_greeting_state",
				"business_greeting_chat_count",
				"business_greeting_hidden_at_count",
			],
			as_dict=True,
		)
		or None
	)


def _upsert_pref(user: str, **values: object) -> None:
	"""Insert-or-update the user's Jarvis User Settings row (ignore_permissions:
	backend-managed greeting state, written on the user's own behalf)."""
	get_or_create_user_settings(user)
	frappe.db.set_value(PREF, {"user": user}, values, update_modified=True)


@frappe.whitelist()
def maybe_greet() -> dict:
	"""Report whether the business-note greeting card should show right now.

	Pure reader: no writes, no commits, no locks. Always uses
	``frappe.session.user`` (never accepts a user argument, so one user can't
	trigger a greeting for another). The card surfaces when the user's new-chat
	count is a positive multiple of 3, unless the greeting was dismissed, the
	user already wrote a Business note, or voice features are operator-off.
	Returns ``{show_card, stt_enabled}``.
	"""
	_require_system_user()
	user = frappe.session.user
	stt_enabled = _stt_enabled()
	quiet = {"show_card": False, "stt_enabled": stt_enabled}

	pref = _get_pref(user)
	state = (pref.business_greeting_state or "") if pref else ""

	# Permanent opt-out, or the user already has a Business note in any status
	# (New/Processed/Archived) so there is nothing left to offer.
	if state == "Dismissed":
		return quiet
	if frappe.db.exists(NOTE, {"owner": user, "context_type": "Business"}):
		return quiet
	if not _voice_features_enabled():
		return quiet

	count = cint(pref.business_greeting_chat_count) if pref else 0
	hidden_at = cint(pref.business_greeting_hidden_at_count) if pref else 0
	# "Maybe later" pins the count it was clicked at: the card stays hidden for
	# the REST of that cadence tick (refreshes included) and returns on the next
	# multiple-of-three chat, when count has moved past hidden_at.
	show_card = count > 0 and count % 3 == 0 and count != hidden_at
	return {"show_card": show_card, "stt_enabled": stt_enabled}


@frappe.whitelist()
def hide_greeting() -> dict:
	"""Maybe later / the card's X: hide the card for the rest of the current
	cadence tick. Records the current chat count on the durable preference row,
	so a page refresh cannot re-show a card the user just closed; the cadence
	brings it back on the next multiple-of-three chat. Idempotent."""
	_require_system_user()
	user = frappe.session.user
	pref = _get_pref(user)
	count = cint(pref.business_greeting_chat_count) if pref else 0
	_upsert_pref(user, business_greeting_hidden_at_count=count)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist()
def dismiss_greeting() -> dict:
	"""Don't ask again: mute the greeting permanently. The Dismissed flag lives
	ONLY in the DocType (never Redis), so a cache eviction can never resurrect a
	greeting the user explicitly killed. Idempotent."""
	_require_system_user()
	user = frappe.session.user
	_upsert_pref(user, business_greeting_state="Dismissed")
	frappe.db.commit()
	return {"ok": True}


def increment_new_chat_count(user: str) -> None:
	"""Bump the per-user new-chat counter that drives the every-third-new-chat
	greeting cadence. Atomic UPDATE so two tabs racing "New Chat" can't lose an
	increment the way a read-modify-write would.

	The ONLY caller is ``create_or_focus_empty``'s create-fallback branch, which
	runs exactly once per genuinely-new interactive chat. ``create_conversation``
	is deliberately NOT hooked directly: ``filebox.py`` calls it for unattended
	File Box drops, which must never count toward the greeting cadence.
	"""
	get_or_create_user_settings(user)
	frappe.db.sql(
		"UPDATE `tabJarvis User Settings` "
		"SET business_greeting_chat_count = business_greeting_chat_count + 1 "
		"WHERE user = %s",
		(user,),
	)
