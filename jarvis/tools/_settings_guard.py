"""The two guarded settings writes (owner decision R2-10; plan rev 4 items 6-7):
an update of CRM Settings that leaves the Frappe CRM data sync on, and an update
of Domain Settings, each only through a confirmation card with no trial run. The
lock, the schema diff and the wiring are shared (``_guarded_structure``); this
module holds what each save does, its park checks, its card lines and its
clean-up.

CRM Settings (erpnext/crm/doctype/crm_settings/crm_settings.py, the same on ERPNext
15 and 16). ``on_update`` -> ``custom_fields_for_frappe_crm_data_sync`` (:64-75):
whenever the saved document has ``enable_frappe_crm_data_synchronization`` on, on
EVERY save and not only the one that turns it on, ``create_custom_fields`` runs
for the static ``get_frappe_crm_custom_fields`` (:77-98: ``crm_deal`` on Quotation
and on Customer). That inserts the missing field rows, then for each form
``frappe.db.updatedb`` commits and runs ``ALTER TABLE`` (frappe custom_field.py
``create_custom_fields``, 15 :301-367, 16 :322-388). A failed ALTER leaves field
rows without a column on a form every salesperson uses. With the switch OFF the
save changes no structure: that update is ordinary sensitive configuration
(``_write_risk``) and never reaches this module. The switch also opens a door:
it lets the allowed users create customers, prospects, contacts and addresses
here from a Frappe CRM site (crm/frappe_crm_api.py ``validate_frappe_crm_sync``).

Domain Settings (frappe/core/doctype/domain_settings/domain_settings.py and
core/doctype/domain/domain.py, the same on Frappe 15 and 16). Every save runs
``setup_domain`` for each active domain (roles enabled and given to the person
saving, property setters, ``set_value`` saves, ``create_custom_fields``, then the
domain's own ``on_setup`` code: domain.py 15 :24-41, 16 :26-43) and
``restrict_roles_and_modules`` for every domain an app declares
(domain_settings.py :43-73): the modules and roles of each are tied to it, and for
a domain that is NOT active its roles are removed from every user and disabled,
and its fields deleted. Stock frappe / erpnext / hrms declare no domains, so on
most sites the save changes the list and nothing else.

Refused, with the Desk path: a site where any declared domain carries ``on_setup``
(its own code), ``set_value`` or ``properties`` (writes to other documents the
card cannot show), and a save that would delete a domain's fields (deleting a
Custom Field is never done from chat). Otherwise the card names the roles and
modules turned off, how many people lose each role, and the roles the person
confirming is given; the ``Has Role`` rows the save deletes are kept on the
confirmation row, so they can be put back.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

import frappe
from frappe.utils import cint, get_datetime, strip_html

from jarvis.exceptions import InvalidFieldValueError, PermissionDeniedError, StructureRefusedError
from jarvis.tools import _custom_field_guard as cfg
from jarvis.tools import _guarded_structure as gs
from jarvis.tools._guarded_structure import CleanUp

CRM = "CRM Settings"
DOMAIN = "Domain Settings"
RISK_CRM = "crm_settings_sync"
RISK_DOMAIN = "domain_settings"
SYNC = "enable_frappe_crm_data_synchronization"
_DESK_ONLY = "Do not retry it with another tool; tell the user where to do it."
# Role rows the save may remove and still be undone from the confirmation row.
HAS_ROLE_MAX = 5000
# What a declared domain may carry and still be switched from chat.
_REFUSED_DOMAIN_KEYS = {
	"on_setup": "runs its own setup code",
	"set_value": "writes values into other documents",
	"properties": "changes field properties on other forms",
}


def _refuse(doctype: str, why: str) -> StructureRefusedError:
	from jarvis.tools._write_risk import desk_path

	return StructureRefusedError(f"{why} {_DESK_ONLY}", doctype=doctype, desk_path=desk_path(doctype))


# --------------------------------------------------------------------------- #
# A settings document (a Single), read and put back raw
# --------------------------------------------------------------------------- #
def _child_tables(doctype: str) -> list[tuple[str, str]]:
	return [(df.fieldname, df.options) for df in frappe.get_meta(doctype).get_table_fields()]


def _modified(doctype: str):
	"""The settings document's own timestamp, from the table (never a cache)."""
	rows = frappe.db.sql(
		"SELECT value FROM `tabSingles` WHERE doctype=%(d)s AND field='modified'", {"d": doctype}
	)
	return rows[0][0] if rows else None


def _before(doctype: str) -> dict:
	"""The settings document as stored: its values and the rows of its tables."""
	values = dict(frappe.db.sql("SELECT field, value FROM `tabSingles` WHERE doctype=%(d)s", {"d": doctype}))
	children = {
		child: frappe.get_all(child, filters={"parent": doctype, "parenttype": doctype}, fields=["*"])
		for _fieldname, child in _child_tables(doctype)
	}
	return frappe.parse_json(frappe.as_json({"values": values, "children": children}))


def _restore(doctype: str, before: dict) -> None:
	singles = frappe.qb.DocType("Singles")
	frappe.db.delete("Singles", {"doctype": doctype})
	for key, value in (before.get("values") or {}).items():
		frappe.qb.into(singles).columns("doctype", "field", "value").insert(doctype, key, value).run()
	for _fieldname, child in _child_tables(doctype):
		frappe.db.delete(child, {"parent": doctype, "parenttype": doctype})
		for row in (before.get("children") or {}).get(child) or []:
			gs.write_row(child, row)
	frappe.clear_document_cache(doctype, doctype)
	frappe.clear_cache(doctype=doctype)


def _settings_state(doctype: str, undo: dict) -> str:
	"""Where the settings document stands against what this confirmation wrote:
	``untouched`` (nothing of it committed), ``ours`` (still exactly its write) or
	``changed`` (someone saved it again since: never overwritten)."""
	before = undo.get("before") if isinstance(undo.get("before"), dict) else None
	written = undo.get("written") if isinstance(undo.get("written"), dict) else None
	if before is None or not written:
		return "untouched"
	now = _modified(doctype)
	stamp = (before.get("values") or {}).get("modified")
	if str(now or "") == str(stamp or "") or (now and stamp and get_datetime(now) == get_datetime(stamp)):
		return "untouched"
	if now and get_datetime(now) == get_datetime(written.get("modified")):
		return "ours"
	return "changed"


def _extra_keys(row: dict, key: str) -> list[str]:
	"""What a one-value row carries beside its value and the keys Frappe owns."""
	from jarvis.tools._child_rows import _ROW_SYSTEM_KEYS

	return sorted(
		str(k) for k in row if k != key and k not in _ROW_SYSTEM_KEYS and not str(k).startswith("__")
	)


def _generated_rows(fields_by_doctype) -> dict[str, list[dict]]:
	"""``create_custom_fields``' argument as form -> the fields it names, each as the
	app wrote it (a fieldname taken from the label where it gives none)."""
	out: dict[str, list[dict]] = {}
	for doctypes, fields in (fields_by_doctype or {}).items():
		fields = [fields] if isinstance(fields, dict) else list(fields or [])
		for doctype in [doctypes] if isinstance(doctypes, str) else list(doctypes):
			for df in fields:
				row = dict(df)
				if not row.get("fieldname") and row.get("label"):
					row["fieldname"] = frappe.scrub(row["label"])
				out.setdefault(doctype, []).append(row)
	return out


def _inserted(row: dict) -> dict:
	"""A missing field as ``create_custom_field`` inserts it (custom_field.py
	15 :280-298, 16 :301-319)."""
	return {"permlevel": 0, "fieldtype": "Data", "hidden": 0, "is_system_generated": 1, **row}


def _rewrite(doctype: str, row: dict) -> dict | None:
	"""What ``create_custom_fields`` does to a field that already exists under the
	name it wants: it writes the app's own values over it and saves it, whoever made
	the field (custom_field.py 15 :347-357, 16 :368-378). None when nothing differs
	(Frappe then leaves the field alone)."""
	stored = frappe.db.get_value(cfg.CF, {"dt": doctype, "fieldname": row["fieldname"]}, "*", as_dict=True)
	changes = {k: [stored.get(k), v] for k, v in row.items() if stored.get(k) != v}
	if not changes:
		return None
	return {"name": stored.name, "dt": doctype, "fieldname": row["fieldname"], "changes": changes}


def _check_fields(owner: str, rows_by_doctype: dict[str, list[dict]], rewrites: list) -> dict[str, list[str]]:
	"""Pre-check every form the save adds fields to (plan rev 4 item 6): the form
	exists, and its table takes exactly the missing fields' columns and nothing
	else. Returns form -> the fieldnames that will be added, and appends to
	``rewrites`` every existing field the save will write over (``_rewrite``).
	Nothing a caller can correct is wrong here, so every refusal carries the Desk
	path."""
	adds: dict[str, list[str]] = {}
	for doctype, rows in rows_by_doctype.items():
		if frappe.db.get_value("DocType", doctype, "name") != doctype:
			raise _refuse(owner, f"This would add fields to {doctype}, which is not a form on this site.")
		existing = set(
			frappe.get_all(
				cfg.CF,
				filters={"dt": doctype, "fieldname": ["in", [r["fieldname"] for r in rows]]},
				pluck="fieldname",
			)
		)
		new = [_inserted(r) for r in rows if r["fieldname"] not in existing]
		if new and not frappe.has_permission(cfg.CF, "create"):
			raise PermissionDeniedError(
				f"Saving this adds the field {', '.join(r['fieldname'] for r in new)} to {doctype}, which "
				"needs permission to create Custom Fields (usually the System Manager role)."
			)
		rewritten = [r for r in (_rewrite(doctype, r) for r in rows if r["fieldname"] in existing) if r]
		rewrites.extend(rewritten)
		try:
			added = cfg.check_generated_fields(doctype, new, [r for r in rows if r["fieldname"] in existing])
		except StructureRefusedError:
			raise _refuse(
				owner,
				f"{doctype}'s table has other structure changes waiting, so a setting that adds fields to "
				"it is not saved from chat. An administrator brings the table in line first (bench "
				"migrate applies pending structure changes).",
			) from None
		except InvalidFieldValueError as e:
			raise _refuse(owner, f"This setting cannot add its field to {doctype}: {e}") from None
		if added:
			adds[doctype] = added
	return adds


def _fields_line(adds: dict[str, list[str]]) -> str:
	parts = [f"{', '.join(names)} to {doctype}" for doctype, names in sorted(adds.items())]
	several = sum(len(names) for names in adds.values()) != 1
	return f"It adds the field{'s' if several else ''} {'; '.join(parts)}."


def _rewrite_lines(rewrites: list[dict]) -> list[str]:
	"""Said on the card: an existing field the save writes over, and what changes."""
	lines = []
	for r in rewrites:
		changes = "; ".join(f"{key}: {old} to {new}" for key, (old, new) in r["changes"].items())
		lines.append(f"It also rewrites the existing field {r['fieldname']} on {r['dt']} ({changes}).")
	return lines


def _rewritten_before(rewrites: list[dict]) -> dict:
	"""The fields the save will write over, as they are now: what a clean-up puts
	back."""
	rows = {r["name"]: frappe.db.get_value(cfg.CF, r["name"], "*", as_dict=True) for r in rewrites}
	return frappe.parse_json(frappe.as_json({name: row for name, row in rows.items() if row}))


def _restore_rewritten(undo: dict, said: list[str]) -> tuple[bool, bool]:
	"""Put back each existing field the save wrote over, exactly as it was, only
	when it still carries the timestamp of THAT save (``undo["rewrote"]``, stamped
	by ``_guarded_structure.stamp_nested``); ``(clean, changed)``. A field someone
	changed again is left, and the outcome is never called clean."""
	clean, changed = True, False
	stamps = {r.get("name"): r.get("modified") for r in undo.get("rewrote") or [] if isinstance(r, dict)}
	for name, before in (undo.get("rewritten") or {}).items():
		modified = frappe.db.get_value(cfg.CF, name, "modified")
		where = f"The field {before.get('fieldname')} on {before.get('dt')}"
		if not modified:
			said.append(f"{where} no longer exists; it was not restored.")
			clean = False
		elif get_datetime(modified) == get_datetime(before.get("modified")):
			continue
		elif name in stamps and get_datetime(modified) == get_datetime(stamps[name]):
			gs.write_row(cfg.CF, before, name=name)
			frappe.clear_cache(doctype=before.get("dt"))
			said.append(f"{where} was put back as it was.")
			changed = True
		else:
			said.append(f"{where} was changed again in the meantime; it was left as it is.")
			clean = False
	return clean, changed


def _adds_announced(adds: dict[str, list[str]], card_line: str) -> bool:
	"""Whether the card named every field this save will add (``card_holds``)."""
	return all(f"{', '.join(names)} to {doctype}" in card_line for doctype, names in adds.items())


def _clean_made(undo: dict, said: list[str]) -> tuple[bool, bool]:
	"""Clean up the fields the save made; ``(clean, changed)``."""
	clean, changed = True, False
	for note in cfg.clean_up_generated(undo.get("made")):
		said.append(note.text)
		clean, changed = clean and note.clean, changed or note.changed
	return clean, changed


# --------------------------------------------------------------------------- #
# CRM Settings with the data sync on
# --------------------------------------------------------------------------- #
def crm_sync_fields() -> dict:
	"""The fields ERPNext's data sync adds, from its own static list."""
	from erpnext.crm.doctype.crm_settings.crm_settings import CRMSettings

	return CRMSettings.get_frappe_crm_custom_fields()


@dataclass
class _CrmPlan:
	call: dict
	doc: dict
	rows: dict = field(default_factory=dict)  # form -> the sync's field rows
	adds: dict = field(default_factory=dict)  # form -> fieldnames that will be added
	rewrites: list = field(default_factory=list)  # existing fields the sync writes over
	turns_on: bool = False
	users_change: bool = False


class CrmSettingsSync:
	risk = RISK_CRM

	def __init__(self, args: dict):
		self.name = args.get("name")
		changes = args.get("changes")
		self.given = dict(changes) if isinstance(changes, dict) else {}
		self._plan_cache: _CrmPlan | None = None

	@property
	def lock_doctype(self) -> str:
		return CRM

	@property
	def lock_doctypes(self) -> list[str]:
		"""The settings document and every form the sync adds fields to."""
		try:
			return [CRM, *_generated_rows(crm_sync_fields())]
		except Exception:
			return [CRM]

	def check(self) -> _CrmPlan:
		with cfg._own_messages():
			self._plan_cache = self._analyse()
		return self._plan_cache

	def _plan(self) -> _CrmPlan:
		return self._plan_cache or self.check()

	def _values(self) -> dict:
		"""The changes in stored form (the write tools' own value check, then the
		casts of the save), with every allowed user named exactly."""
		from jarvis.tools._field_values import check_values

		meta = frappe.get_meta(CRM)
		unknown = sorted(str(k) for k in self.given if not meta.has_field(k))
		if unknown:
			raise InvalidFieldValueError(
				f"CRM Settings has no setting named {', '.join(unknown)}. Leave it out or use the right name."
			)
		out = {}
		for key, value in check_values(CRM, dict(self.given)).items():
			df = meta.get_field(key)
			if df.fieldtype in ("Table", "Table MultiSelect"):
				out[key] = self._users(value)
			elif df.fieldtype in ("Check", "Int"):
				out[key] = cint(value)
			elif value is None or isinstance(value, str):
				out[key] = (value or "").strip() or None
			elif isinstance(value, bool) or not isinstance(value, int | float):
				raise InvalidFieldValueError(f"The CRM setting {key} must be one plain value.")
			else:
				out[key] = str(value)
		return out

	def _users(self, rows) -> list[dict]:
		if rows is None:
			return []
		if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
			raise InvalidFieldValueError("allowed_users must be a list of rows, each naming a user.")
		users = []
		for row in rows:
			extra = _extra_keys(row, "user")
			if extra:
				raise InvalidFieldValueError(
					f"An allowed user row only names a user; leave out {', '.join(extra)}."
				)
			user = row.get("user")
			found = (
				frappe.db.get_value("User", user, "name") if isinstance(user, str) and user.strip() else None
			)
			if not found or found != user:
				raise InvalidFieldValueError(
					f"allowed_users: there is no user {user!r}. Use the user's exact id (their email)."
				)
			if found in users:
				raise InvalidFieldValueError(f"allowed_users lists {found} more than once.")
			users.append(found)
		return [{"user": u} for u in users]

	def _analyse(self) -> _CrmPlan:
		if not frappe.has_permission(CRM, "write"):
			raise PermissionDeniedError("You do not have permission to change CRM Settings.")
		if self.name not in (None, "", CRM):
			raise InvalidFieldValueError(f"CRM Settings is one document, named {CRM!r}.")
		if not self.given:
			raise InvalidFieldValueError("Say what to change in CRM Settings: changes is empty.")
		call = self._values()
		stored = frappe.get_doc(CRM)  # a document of its own: nothing cached is touched
		was_on = bool(cint(stored.get(SYNC)))
		before_users = sorted(r.user for r in stored.get("allowed_users") or [])
		probe = frappe.get_doc(CRM)
		probe.update(copy.deepcopy(call))  # Frappe writes into the row dicts it is given
		plan = _CrmPlan(call=call, doc=probe.as_dict())
		plan.turns_on = bool(cint(probe.get(SYNC))) and not was_on
		plan.users_change = sorted(r.user for r in probe.get("allowed_users") or []) != before_users
		# Frappe's own rules, which would otherwise fail the save at Confirm.
		try:
			probe.validate_enable_opportunity_creation_from_contact_us()
			probe.validate_allowed_users()
		except frappe.ValidationError as e:
			raise InvalidFieldValueError(
				strip_html(str(e)).strip() or "CRM Settings refused the change."
			) from None
		if cint(probe.get(SYNC)):
			plan.rows = _generated_rows(crm_sync_fields())
			plan.adds = _check_fields(CRM, plan.rows, plan.rewrites)
		return plan

	def canonical_args(self) -> dict:
		"""The call as it is carded, sealed and run. Called after ``check``."""
		return {"doctype": CRM, "name": CRM, "changes": dict(self._plan().call)}

	def lead_line(self) -> str | None:
		return None if self._plan().adds else "Confirming changes CRM settings for every user."

	def card_holds(self, card_line: str) -> bool:
		"""Under the lock, before the claim: the card named every field the save
		will add (one may have been removed since the park)."""
		return _adds_announced(self._plan().adds, card_line)

	def risk_lines(self) -> list[str]:
		from jarvis.tools._write_risk import RISK_LINES

		plan = self._plan()
		lines = []
		if plan.adds:
			lines.append(_fields_line(plan.adds).replace("It adds", "The Frappe CRM data sync adds"))
		elif plan.rows:
			lines.append(
				"The Frappe CRM data sync is on; its fields are already on "
				f"{' and '.join(sorted(plan.rows))}, so none is added."
			)
		lines.extend(_rewrite_lines(plan.rewrites))
		if plan.turns_on or plan.users_change:
			lines.append(
				"With the data sync on, the allowed users can create customers, prospects, contacts and "
				"addresses here from a Frappe CRM site."
			)
			lines.append(RISK_LINES["access"])
		lines.append(RISK_LINES["settings"])
		return lines

	def snapshot(self) -> dict:
		"""The settings as they are now, and the one default their save rewrites."""
		out = {"risk": RISK_CRM, "before": None, "default": None, "rewritten": {}}
		try:
			out["before"] = _before(CRM)
			out["default"] = frappe.db.get_default("campaign_naming_by")
			with cfg._own_messages():
				out["rewritten"] = _rewritten_before(self._analyse().rewrites)
		except Exception:
			pass
		return out

	@staticmethod
	def clean_up(undo: dict) -> CleanUp:
		"""Put CRM Settings back as it was (only when it still carries this
		confirmation's own timestamp), and remove each field the sync made whose
		column was never added."""
		state = _settings_state(CRM, undo)
		said = []
		clean, changed = _clean_made(undo, said)
		same, restored = _restore_rewritten(undo, said)
		clean, changed = clean and same, changed or restored
		if state == "ours":
			_restore(CRM, undo["before"])
			if undo.get("default") is not None:
				frappe.db.set_default("campaign_naming_by", undo["default"])
			said.insert(0, "CRM Settings was put back as it was.")
			changed = True
		elif state == "changed":
			said.insert(0, "CRM Settings was changed again in the meantime; it was left as it is.")
			clean = False
		if not said:
			return CleanUp("Nothing was changed.")
		return CleanUp(" ".join(said), clean=clean, changed=changed)


# --------------------------------------------------------------------------- #
# Domain Settings
# --------------------------------------------------------------------------- #
def declared_domains() -> dict[str, dict]:
	"""Every domain an installed app declares (the ``domains`` hook), with its data,
	read the way Frappe's own save reads it."""
	return {
		name: dict(frappe.get_domain_data(name) or {}) for name in list(frappe.get_hooks("domains") or {})
	}


def _listed_as(noun: str, names: list) -> str:
	""" "the role A" / "the roles A, B"."""
	return f"the {noun}{'' if len(names) == 1 else 's'} {', '.join(names)}"


def _listed(value) -> list:
	return list(value) if isinstance(value, list | tuple | set) else ([value] if value else [])


@dataclass
class _DomainPlan:
	call: dict
	active: list
	roles_off: list = field(default_factory=list)
	roles_on: list = field(default_factory=list)
	modules_off: list = field(default_factory=list)
	modules_on: list = field(default_factory=list)
	holders: int = 0  # Has Role rows the save removes
	adds: dict = field(default_factory=dict)
	rewrites: list = field(default_factory=list)
	declared: bool = False


class DomainSettingsUpdate:
	risk = RISK_DOMAIN
	# The role rows the save removed stay on the confirmation row after a successful
	# confirm, for the operator's recovery aid (``_guarded_structure.undo_confirmation``).
	KEEP_UNDO = True

	def __init__(self, args: dict):
		self.name = args.get("name")
		changes = args.get("changes")
		self.given = dict(changes) if isinstance(changes, dict) else {}
		self._plan_cache: _DomainPlan | None = None

	@property
	def lock_doctype(self) -> str:
		return DOMAIN

	@property
	def lock_doctypes(self) -> list[str]:
		"""The settings document and every form a declared domain adds fields to."""
		forms: set[str] = set()
		try:
			for data in declared_domains().values():
				forms.update(_generated_rows(data.get("custom_fields")))
		except Exception:
			pass
		return [DOMAIN, *sorted(forms)]

	def check(self) -> _DomainPlan:
		with cfg._own_messages():
			self._plan_cache = self._analyse()
		return self._plan_cache

	def _plan(self) -> _DomainPlan:
		return self._plan_cache or self.check()

	def _active(self) -> tuple[list[str], list[dict]]:
		"""The domains the save leaves active, and the table's rows in stored form:
		a domain that is already active keeps its row (by name), a new one is added."""
		unknown = sorted(str(k) for k in self.given if k != "active_domains")
		if unknown:
			raise InvalidFieldValueError(
				f"Domain Settings holds only the list active_domains; leave out {', '.join(unknown)}."
			)
		if "active_domains" not in self.given:
			raise InvalidFieldValueError("Say which domains are active: give the whole active_domains list.")
		rows = self.given.get("active_domains")
		rows = [] if rows is None else rows
		if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
			raise InvalidFieldValueError("active_domains must be a list of rows, each naming a domain.")
		stored = {
			r.domain: r.name
			for r in frappe.get_all(
				"Has Domain", filters={"parent": DOMAIN, "parenttype": DOMAIN}, fields=["name", "domain"]
			)
		}
		active, out = [], []
		for row in rows:
			extra = _extra_keys(row, "domain")
			if extra:
				raise InvalidFieldValueError(
					f"An active domain row only names a domain; leave out {', '.join(extra)}."
				)
			domain = row.get("domain")
			found = (
				frappe.db.get_value("Domain", domain, "name")
				if isinstance(domain, str) and domain.strip()
				else None
			)
			if not found or found != domain:
				raise InvalidFieldValueError(
					f"active_domains: there is no Domain named {domain!r}. Use the exact name of an existing Domain."
				)
			if found in active:
				raise InvalidFieldValueError(f"active_domains lists {found} more than once.")
			active.append(found)
		# ``update_doc`` keeps a table's rows by their name and refuses a list that
		# names none of the saved rows, so a domain already active keeps its row and
		# a new one takes over a row that is being dropped, before any row is added.
		spare = [name for domain, name in sorted(stored.items()) if domain not in active]
		for domain in active:
			name = stored.get(domain) or (spare.pop(0) if spare else None)
			out.append({"name": name, "domain": domain} if name else {"domain": domain})
		return active, out

	def _analyse(self) -> _DomainPlan:
		if not frappe.has_permission(DOMAIN, "write"):
			raise PermissionDeniedError(
				"You do not have permission to change Domain Settings (usually the System Manager role)."
			)
		if self.name not in (None, "", DOMAIN):
			raise InvalidFieldValueError(f"Domain Settings is one document, named {DOMAIN!r}.")
		active, rows = self._active()
		plan = _DomainPlan(call={"active_domains": rows}, active=active)
		declared = declared_domains()
		plan.declared = bool(declared)
		fields: dict[str, list[dict]] = {}
		tied: dict[str, str] = {}  # module -> the domain it ends up tied to
		# In the hook's own order, as Frappe walks it (``restrict_roles_and_modules``):
		# a module named by two domains ends up tied to the later one, and a role of
		# any domain that is not active is removed whatever another domain says.
		for domain, data in declared.items():
			for key, why in _REFUSED_DOMAIN_KEYS.items():
				if data.get(key):
					raise _refuse(
						DOMAIN,
						f"The domain {domain} {why} when Domain Settings is saved, which a confirmation card "
						"cannot show, so Domain Settings is changed in Desk on this site.",
					)
			on = domain in active
			(plan.roles_on if on else plan.roles_off).extend(_listed(data.get("restricted_roles")))
			tied.update(dict.fromkeys(_listed(data.get("modules")), domain))
			made = _generated_rows(data.get("custom_fields"))
			if on:
				for doctype, field_rows in made.items():
					fields.setdefault(doctype, []).extend(field_rows)
			elif any(
				frappe.db.exists(cfg.CF, {"dt": doctype, "fieldname": r.get("fieldname")})
				for doctype, field_rows in made.items()
				for r in field_rows
			):
				raise _refuse(
					DOMAIN,
					f"Saving Domain Settings with the domain {domain} not active deletes the fields that "
					"domain added, and a field is never deleted from chat.",
				)
		plan.roles_off = sorted(set(plan.roles_off))
		plan.roles_on = sorted(set(plan.roles_on) - set(plan.roles_off))
		plan.modules_on = sorted(m for m, domain in tied.items() if domain in active)
		plan.modules_off = sorted(m for m, domain in tied.items() if domain not in active)
		if plan.roles_off:
			plan.holders = frappe.db.count("Has Role", {"role": ["in", plan.roles_off]})
			if plan.holders > HAS_ROLE_MAX:
				raise _refuse(
					DOMAIN,
					f"This would remove the roles {', '.join(plan.roles_off)} from {plan.holders} people at "
					f"once. More than {HAS_ROLE_MAX} is not done from chat, so it is done in Desk.",
				)
		plan.adds = _check_fields(DOMAIN, fields, plan.rewrites)
		return plan

	def canonical_args(self) -> dict:
		"""The call as it is carded, sealed and run. Called after ``check``."""
		return {"doctype": DOMAIN, "name": DOMAIN, "changes": dict(self._plan().call)}

	@staticmethod
	def before_write(doc) -> None:
		"""As the confirmed save begins: drop Frappe's cached list of active domains.
		``restrict_roles_and_modules`` reads that list from the cache while the save
		runs (domain_settings.py :45, ``frappe.get_active_domains``) and the cache is
		only cleared when the save ends (:41), so a list cached before the save made
		Frappe treat the domain being switched ON as inactive: its roles taken from
		everyone and its fields deleted, the opposite of what the card said. Dropped
		here, the list is read again from the rows this save has just written."""
		frappe.cache.delete_value("active_domains")
		frappe.cache.delete_value("active_modules")

	def lead_line(self) -> str | None:
		return (
			None
			if self._plan().adds
			else "Confirming changes the active domains of this site for every user."
		)

	def card_holds(self, card_line: str) -> bool:
		"""As ``CrmSettingsSync.card_holds``."""
		return _adds_announced(self._plan().adds, card_line)

	def risk_lines(self) -> list[str]:
		from jarvis.tools._write_risk import RISK_LINES

		plan = self._plan()
		lines = []
		if plan.adds:
			lines.append(_fields_line(plan.adds))
		lines.extend(_rewrite_lines(plan.rewrites))
		if not plan.declared:
			lines.append(
				"No app on this site declares domains, so only the list of active domains changes: no "
				"role or module is switched."
			)
			return lines
		off = []
		if plan.roles_off:
			off.append(
				f"{_listed_as('role', plan.roles_off)} ({gs.counted(plan.holders, 'role assignment')} "
				f"{'is' if plan.holders == 1 else 'are'} removed)"
			)
		if plan.modules_off:
			off.append(_listed_as("module", plan.modules_off))
		if off:
			lines.append(f"It turns off for every user: {' and '.join(off)}.")
		on = []
		if plan.roles_on:
			on.append(f"{_listed_as('role', plan.roles_on)} (also given to you)")
		if plan.modules_on:
			on.append(_listed_as("module", plan.modules_on))
		if on:
			lines.append(f"It turns on: {' and '.join(on)}.")
		if off or on:
			lines.append(RISK_LINES["access"])
		return lines

	def snapshot(self) -> dict:
		"""What the save removes and rewrites, as it is now: the settings, every
		``Has Role`` row of a role it turns off, and the roles and modules it ties
		to a domain."""
		out = {"risk": RISK_DOMAIN, "before": None, "has_role": [], "roles": {}, "modules": {}, "held": []}
		try:
			with cfg._own_messages():
				plan = self._analyse()
			out["before"] = _before(DOMAIN)
			if plan.roles_off:
				out["has_role"] = frappe.parse_json(
					frappe.as_json(
						frappe.get_all("Has Role", filters={"role": ["in", plan.roles_off]}, fields=["*"])
					)
				)
			roles = [*plan.roles_off, *plan.roles_on]
			if roles:
				out["roles"] = {
					r.name: {"disabled": cint(r.disabled), "restrict_to_domain": r.restrict_to_domain}
					for r in frappe.get_all(
						"Role",
						filters={"name": ["in", roles]},
						fields=["name", "disabled", "restrict_to_domain"],
					)
				}
				out["held"] = frappe.get_all(
					"Has Role", filters={"role": ["in", plan.roles_on]}, pluck="name"
				)
			modules = [*plan.modules_off, *plan.modules_on]
			if modules:
				out["modules"] = dict(
					frappe.get_all(
						"Module Def",
						filters={"name": ["in", modules]},
						fields=["name", "restrict_to_domain"],
						as_list=True,
					)
				)
			out["roles_on"] = plan.roles_on
			out["rewritten"] = _rewritten_before(plan.rewrites)
		except Exception:
			pass
		return out

	@staticmethod
	def clean_up(undo: dict) -> CleanUp:
		"""Put Domain Settings back as it was (only when it still carries this
		confirmation's own timestamp): the list, the roles it removed from people,
		the roles and modules it switched, the roles it gave the person who
		confirmed; and remove each field it made whose column was never added."""
		state = _settings_state(DOMAIN, undo)
		said = []
		clean, changed = _clean_made(undo, said)
		same, restored = _restore_rewritten(undo, said)
		clean, changed = clean and same, changed or restored
		if state == "changed":
			said.insert(0, "Domain Settings was changed again in the meantime; it was left as it is.")
			return CleanUp(" ".join(said), clean=False, changed=changed)
		if state == "untouched":
			return CleanUp(" ".join(said) or "Nothing was changed.", clean=clean, changed=changed)
		_restore(DOMAIN, undo["before"])
		said.insert(0, "Domain Settings was put back as it was.")
		restored = 0
		for row in undo.get("has_role") or []:
			if not frappe.db.exists("Has Role", row.get("name")) and frappe.db.exists(
				row.get("parenttype"), row.get("parent")
			):
				gs.write_row("Has Role", row)
				restored += 1
		if restored:
			said.append(
				f"{gs.counted(restored, 'role assignment')} it removed {'was' if restored == 1 else 'were'} put back."
			)
		for role, was in (undo.get("roles") or {}).items():
			frappe.db.set_value("Role", role, was, update_modified=False)
		for module, domain in (undo.get("modules") or {}).items():
			frappe.db.set_value("Module Def", module, "restrict_to_domain", domain, update_modified=False)
		user = (undo.get("written") or {}).get("user")
		given = [
			name
			for name in frappe.get_all(
				"Has Role",
				filters={"parenttype": "User", "parent": user, "role": ["in", undo.get("roles_on") or [""]]},
				pluck="name",
			)
			if name not in (undo.get("held") or [])
		]
		if given:
			frappe.db.delete("Has Role", {"name": ["in", given]})
		frappe.clear_cache()
		return CleanUp(" ".join(said), clean=clean, changed=True)
