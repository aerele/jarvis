"""Jarvis Custom Skill DocType controller.

One row per customer-authored skill. Each row renders to a SKILL.md that is
pushed into the customer's openclaw container (under ``openclaw_state/
custom_skills/custom-<slug>/SKILL.md``) and loaded ALONGSIDE the shared
read-only persona skills. Rows are owned by the Frappe user who created them
(``if_owner`` permission); the push itself is bench-global (a Jarvis bench maps
to one customer / one container) and is triggered explicitly via
``jarvis.chat.custom_skills_api.apply_custom_skills``.

All the user-facing validation lives here so it runs on every insert/save
regardless of whether the write came from the SPA, the Desk, or a test.
"""

import re

import frappe
from frappe import _
from frappe.model.document import Document

# A skill_name is a bare slug the customer authors (e.g. "invoicing"). It is
# lowercased and must be hyphen-separated alphanumerics. Everywhere it reaches
# openclaw it is prefixed with ``custom-`` (see chat/custom_skills.py) so it can
# never collide with a shared persona skill name.
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MIN_SLUG_LEN = 3
MAX_SLUG_LEN = 40
MAX_DESC_LEN = 500
MAX_INSTR_LEN = 20000
MAX_SKILLS_PER_OWNER = 25
RESERVED_PREFIX = "custom-"
# Bare-slug prefix reserved for the learning engine's compiled ``learned-<domain>``
# rows. A normal author is blocked from minting a ``learned-selling`` etc. slug so
# they cannot masquerade as a compiled learned skill; only the compiler (which
# sets ``frappe.flags.jarvis_pattern_engine``) or Administrator may author it.
LEARNED_PREFIX = "learned-"

# Scope ladder (security review PART 2 TASK 10), mirroring Jarvis Wiki Page.
SCOPES = ("User", "Role", "Org")

SKILL_DOCTYPE = "Jarvis Custom Skill"


def _clear_personal_clause_cache(owner: str | None) -> None:
	"""personal_skill_clause (chat/custom_skills.py) caches a per-user count of
	enabled Personal rows for 300s; drop it on any row change so a skill saved
	mid-conversation is announced on the owner's next turn."""
	try:
		from jarvis.chat.custom_skills import personal_skills_cache_key

		frappe.cache().delete_value(personal_skills_cache_key(owner or frappe.session.user))
	except Exception:
		pass


def _managed_flag_privileged(user: str | None = None) -> bool:
	"""True for writes allowed to touch the engine-owned ``managed_by_learning``
	flag: the compiler (which sets ``frappe.flags.jarvis_pattern_engine``),
	Administrator, or a System Manager. Everyone else is a normal author."""
	if frappe.flags.jarvis_pattern_engine:
		return True
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	return "System Manager" in frappe.get_roles(user)


def user_can_use_skill(skill, user: str | None = None, user_roles: list[str] | None = None) -> bool:
	"""Scope-ladder visibility rule (security review PART 2 TASK 13; the single
	rule every read path — generic REST via the ORM hook, the SPA list/get, the
	plugin find/get tools — now agrees on).

	A user may see/invoke a skill iff they are the owner, are in ``shared_with``,
	or the skill's scope admits them:

	  * ``User``  — owner only (private).
	  * ``Role``  — holders of ``target_role`` (or, for compiler-managed rows /
	    legacy multi-role narrowing, an ``allowed_roles`` intersection).
	  * ``Org``   — everyone, UNLESS ``allowed_roles`` is set (managed learned
	    rows + legacy "Org narrowed by roles"), in which case role-match.

	System Manager (and Administrator) always passes. ``skill`` may be a Document
	or any dict-like row; child tables absent from a ``frappe.get_all`` row are
	fetched by parent name. Instruction-level enforcement — see TASK 11 for why
	role-restricted bodies must ALSO be kept off the shared container push.
	"""
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	if user_roles is None:
		user_roles = frappe.get_roles(user)
	if "System Manager" in user_roles:
		return True
	if (skill.get("owner") or "") == user:
		return True
	if user in _child_values(skill, "shared_with", "user", "Jarvis Custom Skill Share"):
		return True
	# NULL/empty scope == Org (pre-migration rows); "Personal" is the pre-ladder
	# spelling of User (safe until the migration backfills the DB).
	scope = (skill.get("scope") or "Org").strip() or "Org"
	if scope == "Personal":
		scope = "User"
	roles = set(user_roles)
	if scope == "User":
		# Private: owner/shared/SM already handled above.
		return False
	allowed = set(_child_values(skill, "allowed_roles", "role", "Jarvis Custom Skill Allowed Role"))
	if scope == "Role":
		target_role = (skill.get("target_role") or "").strip()
		if target_role and target_role in roles:
			return True
		return bool(allowed & roles)
	# Org (or legacy empty): everyone unless narrowed by allowed_roles.
	if not allowed:
		return True
	return bool(allowed & roles)


def _child_values(skill, fieldname: str, value_field: str, child_doctype: str) -> list[str]:
	rows = skill.get(fieldname)
	if rows is None:
		# Row without child tables loaded: fall back to a parent lookup.
		parent = skill.get("name")
		if not parent:
			return []
		return frappe.get_all(
			child_doctype,
			filters={"parenttype": SKILL_DOCTYPE, "parent": parent},
			pluck=value_field,
		)
	values = []
	for row in rows:
		value = (
			row
			if isinstance(row, str)
			else (row.get(value_field) if hasattr(row, "get") else getattr(row, value_field, None))
		)
		if value:
			values.append(value)
	return values


class JarvisCustomSkill(Document):
	def validate(self):
		self._validate_slug()
		self._validate_scope()
		self._guard_new_scope()
		self._guard_scope_change()
		self._guard_content_change()
		self._validate_lengths()
		self._validate_unique_per_owner()
		self._validate_owner_cap()
		self._guard_managed_flag()

	def on_update(self):
		_clear_personal_clause_cache(self.owner)

	def on_trash(self):
		_clear_personal_clause_cache(self.owner)

	def _validate_scope(self):
		# Scope ladder {User, Role, Org} (security review PART 2 TASK 10). NEW rows
		# default to User (private-first); pre-migration rows carry legacy scopes,
		# so a blank scope on an EXISTING row reads as Org (its historical meaning)
		# while a blank scope on a fresh insert defaults to User. "Personal" is the
		# pre-ladder spelling of User.
		raw = (self.scope or "").strip()
		if raw == "Personal":
			raw = "User"
		if not raw:
			raw = "User" if self.is_new() else "Org"
		self.scope = raw
		if self.scope not in SCOPES:
			frappe.throw(_("Scope must be one of {0}.").format(", ".join(SCOPES)))
		if self.scope == "Role":
			self.target_role = (self.target_role or "").strip() or None
			if not self.target_role:
				frappe.throw(_("Role-scope skills need a Target Role."))
			# allowed_roles stays meaningful for compiler-managed rows only; an
			# authored Role skill's audience is target_role.
		elif self.scope == "User":
			# A private skill has no role audience — clear both so a stray
			# allowed_roles/target_role can never leak it to role-holders (TASK 13
			# U1: no silent Personal+roles no-op).
			self.target_role = None
			if not frappe.flags.jarvis_pattern_engine:
				self.set("allowed_roles", [])
		else:  # Org
			self.target_role = None

	def _scope_change_authorized(self) -> bool:
		"""Who may WIDEN a skill's scope (mint or promote it to Role/Org): the
		learning compiler (engine flag), Administrator, or a skill reviewer
		(Jarvis Skill Reviewer / Jarvis Admin / System Manager — TASK 15 set).
		A normal owner may only ever create/keep a User-scope skill; widening is
		reviewer-gated (the promotion workflow), never a self-service field flip."""
		if frappe.flags.jarvis_pattern_engine:
			return True
		from jarvis.permissions import is_skill_reviewer

		return is_skill_reviewer(frappe.session.user)

	def _guard_new_scope(self):
		"""Block a non-reviewer from CREATING a skill directly at Role/Org scope
		(the [E1]/[O1] self-service org-escalation the review flagged) — closes the
		generic-REST insert + the create_custom_skill tool's scope arg at the
		controller, so it holds no matter which door the insert comes through.
		New skills default to User; Role/Org creation needs the reviewer set."""
		if not self.is_new():
			return
		if self.scope in ("Role", "Org") and not self._scope_change_authorized():
			frappe.throw(
				_(
					"New skills are private (User scope); promoting to Role or Org "
					"needs a reviewer's approval."
				),
				frappe.PermissionError,
			)

	def _guard_scope_change(self):
		"""Only a reviewer (or the compiler) may re-scope or re-target an EXISTING
		skill — anything else would let an owner widen their own skill bench-wide
		via a save path (SPA, frappe.client.set_value, a raw controller write).
		Runs regardless of ignore_permissions, exactly like the Wiki page guard
		(jarvis_wiki_page._guard_scope_change), because the reviewer promotion
		writes save with ignore_permissions=True."""
		if self.is_new():
			return
		prev = frappe.db.get_value(self.doctype, self.name, ["scope", "target_role"], as_dict=True)
		if not prev:
			return
		prev_scope = (prev.scope or "").strip() or "Org"
		if self.scope == prev_scope and (self.target_role or None) == (prev.target_role or None):
			return
		if self._scope_change_authorized():
			return
		# Owner-initiated NARROWING (strictly fewer viewers down the User<Role<Org
		# ladder) is always safe — it reduces exposure — so the owner may
		# self-demote without a reviewer (e.g. un-publish an Org skill back to
		# private). WIDENING and lateral Role re-targeting stay reviewer-gated.
		rank = {"User": 0, "Role": 1, "Org": 2}
		is_owner = (self.owner or "") == frappe.session.user
		if is_owner and rank.get(self.scope, 2) < rank.get(prev_scope, 2):
			return
		frappe.throw(
			_(
				"Only a reviewer can widen the scope or change the audience of an "
				"existing skill. Request a promotion instead."
			),
			frappe.PermissionError,
		)

	def _guard_content_change(self):
		"""Only a reviewer (or the compiler) may change the CONTENT of an existing
		Role/Org skill — the instructions, description or user-invocable flag the
		shared audience runs. Without this, a promotion could be approved against a
		reviewed body and then have its instructions rewritten afterwards (or a hidden
		tail slipped in) with no second review — the four-eyes bypass Codex flagged
		(CDX-SP-1). Siblings _guard_new_scope / _guard_scope_change bind the SCOPE to
		review; this binds the CONTENT. Runs regardless of ignore_permissions (like
		the scope guard) because the reviewer promotion / insight-apply writes save
		with ignore_permissions=True. A private (User) skill is unguarded — its owner
		edits it freely; only widening to Role/Org needs a reviewer, and the guard
		then locks that reviewed content."""
		if self.is_new():
			return
		if self.scope not in ("Role", "Org"):
			return
		if self._scope_change_authorized():
			return
		prev = frappe.db.get_value(
			self.doctype, self.name, ["instructions", "description", "user_invocable"], as_dict=True
		)
		if not prev:
			return
		changed = (
			(self.instructions or "") != (prev.instructions or "")
			or (self.description or "") != (prev.description or "")
			or int(self.user_invocable or 0) != int(prev.user_invocable or 0)
		)
		if not changed:
			return
		frappe.throw(
			_(
				"Only a reviewer can change the content of a shared (Role/Org) skill. "
				"Narrow it back to private to edit it, then request a fresh promotion."
			),
			frappe.PermissionError,
		)

	def _validate_slug(self):
		self.skill_name = (self.skill_name or "").strip().lower()
		if not self.skill_name:
			frappe.throw(_("Skill name is required."))
		if self.skill_name.startswith(RESERVED_PREFIX):
			frappe.throw(
				_("Skill name must not start with '{0}' (added automatically).").format(RESERVED_PREFIX)
			)
		# ``learned-`` is engine-only: the compiler authors ``learned-<domain>``
		# rows as Administrator under the engine flag; block every other author so
		# a customer cannot forge a skill that looks compiler-managed.
		if self.skill_name.startswith(LEARNED_PREFIX) and not (
			frappe.flags.jarvis_pattern_engine or frappe.session.user == "Administrator"
		):
			frappe.throw(
				_(
					"Skill name must not start with '{0}' (reserved for skills the learning engine manages)."
				).format(LEARNED_PREFIX)
			)
		if not (MIN_SLUG_LEN <= len(self.skill_name) <= MAX_SLUG_LEN):
			frappe.throw(_("Skill name must be {0}-{1} characters.").format(MIN_SLUG_LEN, MAX_SLUG_LEN))
		if not SLUG_RE.fullmatch(self.skill_name):
			frappe.throw(
				_(
					"Skill name may only contain lowercase letters, digits and "
					"single hyphens (e.g. 'monthly-close')."
				)
			)

	def _validate_lengths(self):
		self.description = (self.description or "").strip()
		self.instructions = (self.instructions or "").strip()
		if not self.description:
			frappe.throw(_("Description is required."))
		if not self.instructions:
			frappe.throw(_("Instructions are required."))
		if len(self.description) > MAX_DESC_LEN:
			frappe.throw(_("Description must be at most {0} characters.").format(MAX_DESC_LEN))
		if len(self.instructions) > MAX_INSTR_LEN:
			frappe.throw(_("Instructions must be at most {0} characters.").format(MAX_INSTR_LEN))

	def _validate_unique_per_owner(self):
		# Frappe's field-level ``unique`` is global, not per-owner; enforce
		# (owner, skill_name) uniqueness here so two customers can both have a
		# skill named "invoicing".
		owner = self.owner or frappe.session.user
		# R2-SP-3a: a reviewer-approved promotion materializes the shared copy under
		# the SYSTEM identity (MANAGED_OWNER), but frappe's insert forces ``owner`` to
		# the acting reviewer's session before this validate runs, then the endpoint
		# reassigns it. Validate uniqueness against the FINAL system owner instead, so
		# (a) the check catches a real duplicate Administrator-owned shared slug BEFORE
		# commit (never insert-then-reassign into a collision), and (b) a reviewer who
		# happens to own a private skill of the same slug does not spuriously block a
		# legitimate promotion.
		if frappe.flags.jarvis_promotion_materialize:
			from jarvis.chat.custom_skills import MANAGED_OWNER

			owner = MANAGED_OWNER
		clash = frappe.db.exists(
			"Jarvis Custom Skill",
			{
				"owner": owner,
				"skill_name": self.skill_name,
				"name": ["!=", self.name or ""],
			},
		)
		if clash:
			frappe.throw(_("You already have a skill named '{0}'.").format(self.skill_name))

	def _validate_owner_cap(self):
		if not self.is_new():
			return
		# The reviewer-approved promotion materializes the shared Role/Org copy under
		# a system owner (see custom_skills_api.decide_skill_promotion); that copy is
		# not user hoarding, and the org-wide push budget (MAX_SKILLS_PER_PUSH) already
		# bounds the shared catalog, so the per-owner cap must not block a legitimate
		# approval once the system identity accumulates enough promoted skills.
		if frappe.flags.jarvis_promotion_materialize:
			return
		owner = self.owner or frappe.session.user
		count = frappe.db.count("Jarvis Custom Skill", {"owner": owner})
		if count >= MAX_SKILLS_PER_OWNER:
			frappe.throw(_("You can have at most {0} custom skills.").format(MAX_SKILLS_PER_OWNER))

	def _guard_managed_flag(self):
		"""``managed_by_learning`` is engine-owned (plan section 6.6 security).

		Without this guard a non-privileged owner could flip the flag to 1 on
		their OWN row via ``frappe.client.set_value`` / REST (the field carries no
		permlevel), and ``learned_skill_clause`` would then inject that rogue skill
		into EVERY user's chat turn. Only the compiler (engine flag), Administrator
		or a System Manager may set or change it; a normal author is frozen at its
		stored value (0 for their own rows)."""
		if _managed_flag_privileged():
			return
		new_val = 1 if self.managed_by_learning else 0
		before = self.get_doc_before_save()
		old_val = (1 if before.managed_by_learning else 0) if before is not None else 0
		if new_val or new_val != old_val:
			frappe.throw(
				_("Only the learning engine can set 'Managed by Learning'."),
				frappe.PermissionError,
			)
