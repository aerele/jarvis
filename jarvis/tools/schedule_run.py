"""Schedule work to run once, at a set time: a one-time macro.

"Remind me on Monday to review this", "check these comments next week and tell me",
"run my month-end macro once on the 1st at 8": the chat had no way to set work for
later. This tool does it with what the Macros page already runs, a macro on a
one-time schedule (``schedule_frequency = "once"``): it fires once as the user, the
schedule switches itself off, and the user gets a notification when it finishes.

Two forms:

- ``prompt``: a new macro with that one step, named ``title`` (else from the prompt).
- ``macro``: one of the user's existing macros, run once at ``run_at``. Only a macro
  with no schedule on, not armed and not on hold: replacing a recurring schedule
  would silently end it, and an armed macro's schedule changes on its own page.

Gated, and a brake: it parks a confirmation card in every mode (an armed macro or
an approved skill never schedules work unasked), and a File Box run refuses it.
The macro is never armed here, so its own writes still ask when it runs.
"""

from __future__ import annotations

import frappe
from frappe.utils import get_datetime, now_datetime

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError

MACRO = "Jarvis Macro"
_TITLE_WORDS = 8


def schedule_run(
	run_at: str,
	prompt: str | None = None,
	title: str | None = None,
	macro: str | None = None,
) -> dict:
	"""Schedule ``prompt`` (a new one-time macro) or ``macro`` (an existing one) to
	run once at ``run_at`` (site time, "YYYY-MM-DD HH:MM").

	Returns ``{macro, macro_name, run_at, link, note}``."""
	from jarvis.chat.macros_api import _schedule_block_reason

	prompt = (prompt or "").strip()
	if bool(prompt) == bool(macro):
		raise InvalidArgumentError("pass exactly one of prompt (new work) or macro (an existing macro)")
	slot = _parse_run_at(run_at)
	user = frappe.session.user
	blocked = _schedule_block_reason(user)
	if blocked:
		raise PermissionDeniedError(blocked)
	doc = _schedule_existing(macro, slot) if macro else _schedule_new(prompt, title, slot, user)
	return {
		"macro": doc.name,
		"macro_name": doc.macro_name,
		"run_at": str(doc.next_run_at or slot),
		"link": f"/jarvis/macros/{doc.name}",
		"note": (
			"Scheduled to run once at run_at (site time). It runs as the user, the schedule "
			"switches itself off afterwards, and the user gets a notification when it finishes. "
			"Its steps that change records still ask for confirmation when it runs."
		),
	}


def _parse_run_at(run_at) -> object:
	if not run_at:
		raise InvalidArgumentError("run_at is required (site time, YYYY-MM-DD HH:MM)")
	try:
		slot = get_datetime(run_at)
	except Exception:
		raise InvalidArgumentError(f"run_at must be a date and time, got {run_at!r}") from None
	if slot is None:
		raise InvalidArgumentError(f"run_at must be a date and time, got {run_at!r}")
	slot = slot.replace(second=0, microsecond=0)
	if slot <= now_datetime():
		raise InvalidArgumentError(
			f"run_at {slot:%Y-%m-%d %H:%M} has already passed on the site clock; pick a time in the future."
		)
	return slot


def _schedule_fields(slot) -> dict:
	return {
		"schedule_enabled": 1,
		"schedule_frequency": "once",
		"schedule_date": slot.date(),
		"schedule_time": slot.strftime("%H:%M:00"),
	}


def _schedule_new(prompt: str, title: str | None, slot, user: str):
	from jarvis.jarvis.doctype.jarvis_macro.jarvis_macro import (
		MAX_MACROS_PER_OWNER,
		MAX_NAME_LEN,
		MAX_PROMPT_LEN,
	)

	if len(prompt) > MAX_PROMPT_LEN:
		raise InvalidArgumentError(f"prompt must be at most {MAX_PROMPT_LEN} characters")
	if frappe.db.count(MACRO, {"owner": user}) >= MAX_MACROS_PER_OWNER:
		raise InvalidArgumentError(
			f"The user already has {MAX_MACROS_PER_OWNER} macros, the most allowed. One-time "
			"macros that already ran still count: they can delete the ones they no longer need "
			"on the Macros page, then ask again."
		)
	doc = frappe.get_doc(
		{
			"doctype": MACRO,
			"macro_name": _unique_name(_title(title, prompt, MAX_NAME_LEN), user, MAX_NAME_LEN),
			"description": f"Scheduled from chat to run once on {slot:%Y-%m-%d at %H:%M}.",
			"enabled": 1,
			"stop_on_error": 1,
			"skip_confirmation": 0,
			**_schedule_fields(slot),
			"steps": [{"prompt": prompt}],
		}
	)
	doc.insert()
	return doc


def _title(title: str | None, prompt: str, limit: int) -> str:
	text = " ".join((title or "").split())
	if not text:
		words = prompt.split()
		text = " ".join(words[:_TITLE_WORDS]) + (" ..." if len(words) > _TITLE_WORDS else "")
	return text[:limit].rstrip()


def _unique_name(base: str, user: str, limit: int) -> str:
	"""``base``, or ``base (2)``, ``base (3)`` ...: macro names are unique per owner."""
	name, n = base, 1
	while frappe.db.exists(MACRO, {"owner": user, "macro_name": name}):
		n += 1
		suffix = f" ({n})"
		name = base[: limit - len(suffix)].rstrip() + suffix
	return name


def _schedule_existing(macro: str, slot):
	from jarvis.chat.macros import is_held

	user = frappe.session.user
	name = frappe.db.get_value(MACRO, {"name": macro, "owner": user}, "name") or frappe.db.get_value(
		MACRO, {"macro_name": macro, "owner": user}, "name"
	)
	if not name:
		raise InvalidArgumentError(f"no macro of yours named {macro!r}")
	doc = frappe.get_doc(MACRO, name)
	doc.check_permission("write")
	where = "set its schedule on the Macros page instead"
	if is_held(doc):
		raise InvalidArgumentError(f"{doc.macro_name} is on hold by an admin, so it cannot be scheduled.")
	if not frappe.utils.cint(doc.enabled):
		raise InvalidArgumentError(
			f"{doc.macro_name} is switched off (a draft), so a scheduled run would be skipped: "
			"switch it on on the Macros page, then ask again."
		)
	if frappe.utils.cint(doc.skip_confirmation):
		raise InvalidArgumentError(f"{doc.macro_name} is armed (it skips confirmation): {where}.")
	if frappe.utils.cint(doc.schedule_enabled):
		raise InvalidArgumentError(
			f"{doc.macro_name} already has a schedule; running it once would replace it: {where}."
		)
	doc.update(_schedule_fields(slot))
	doc.save()
	return doc
