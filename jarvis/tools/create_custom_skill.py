"""Save a new skill (Jarvis Custom Skill row) from chat.

The agent captures a procedure the user just walked it through and saves it
as a skill owned by the session user. The skill is ALWAYS created private
(scope=User): a bench-wide (Org) or role-shared (Role) skill can only be minted
by a reviewer through the promotion workflow, never by the agent in one call
(security review PART 2 TASK 10 — this tool used to accept scope="Org"
directly, letting the agent mint a bench-wide skill with no reviewer gate). A
private skill is reached via jarvis__find_skills / jarvis__get_skill and is
never pushed to the shared container catalog. This tool NEVER triggers a
push/apply itself — Apply restarts the container and stays a deliberate human
action.

Confirmation-gated in jarvis/api.py (_GATED_WRITES): the model path parks
the call behind a human confirm card.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools.find_skills import _require_system_user

SKILL = "Jarvis Custom Skill"


def create_custom_skill(
	skill_name: str,
	description: str,
	instructions: str,
	scope: str = "User",
	user_invocable: int = 1,
) -> dict:
	"""Insert a PRIVATE (User-scope) skill row as the session user; the DocType
	controller enforces the slug grammar, length caps, per-owner uniqueness/cap,
	the reserved ``custom-``/``learned-`` prefixes and the scope guard. The
	``scope`` argument is capped at User — a request for Role/Org is honored as a
	private skill (promotion is reviewer-gated). Returns ``{name, skill_name,
	scope, note}``."""
	_require_system_user()

	# Cap at User regardless of what the model asked for: widening is
	# reviewer-only (the controller would reject a non-reviewer's Role/Org
	# create anyway; capping here keeps the agent path from 403-ing on itself).
	requested = (scope or "User").strip().capitalize()

	ui = user_invocable
	if isinstance(ui, str):
		ui = ui.strip().lower() in ("1", "true", "yes", "on")

	doc = frappe.get_doc(
		{
			"doctype": SKILL,
			"skill_name": skill_name,
			"description": description,
			"instructions": instructions,
			"scope": "User",
			"user_invocable": 1 if ui else 0,
			"enabled": 1,
		}
	)
	try:
		doc.insert()
	except (frappe.ValidationError, frappe.DuplicateEntryError) as e:
		# Controller frappe.throw()s carry clean user-facing messages; re-raise
		# as the tool-envelope type so direct callers and the dispatch wrapper
		# classify them identically.
		raise InvalidArgumentError(str(e) or type(e).__name__)

	note = (
		"Saved as a private skill: only you can use it and it is never pushed "
		"to the shared catalog; recall it with jarvis__find_skills / "
		"jarvis__get_skill."
	)
	if requested in ("Org", "Role"):
		note += (
			" Making it visible to your role or the whole org needs a reviewer "
			"to approve a promotion request."
		)
	return {
		"name": doc.name,
		"skill_name": doc.skill_name,
		"scope": doc.scope or "User",
		"note": note,
	}
