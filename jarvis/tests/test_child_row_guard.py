"""A standalone write to a CHILD ROW is judged by the row's parent.

The write-risk guard classified a write by its doctype, so ``update_doc`` /
``delete_doc`` on an existing row of a child table (Has Role, DocPerm, DocField,
Block Module, Webhook Header, Notification Recipient, Portal User) was "ordinary":
it ran with no card in auto mode, "confirm all", an armed macro and an approved
skill run, and parked a card with no risk line in a plain chat. The row's parent
(User, DocType, Webhook, Notification, Customer) is structure or sensitive, so the
row is now refused on every route, naming the parent, where the change gets the
parent's own card (sensitive) or refusal (structure).

Real documents throughout: a throwaway user, a scratch ``custom=1`` DocType, a
disabled Webhook and Notification, a Customer and a Contact. Everything is removed
in ``tearDownClass``; every attempt is rolled back, so a failing run (the unfixed
code executes these writes) leaves nothing changed either. Starting a turn is
stubbed as in ``test_panel_failure._Hermetic``.

SERIAL ONLY: ``setUpClass`` commits its fixtures (users, a DocType, records), so
this module must not run on a shared local site at the same time as another run.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import _failure_kind, api
from jarvis.exceptions import SensitiveWriteRefusedError, StructureRefusedError, WriteRefusedError
from jarvis.permissions import ensure_jarvis_user_role
from jarvis.tests._conv_helpers import CONV, _make_conv
from jarvis.tools import _write_risk as wr

PENDING = "Jarvis Pending Action"
MSG = "Jarvis Chat Message"
TURN = "Jarvis Chat Turn"
AGENT_WRITE = "Jarvis Agent Write"
PREFIX = "jarvis-crg-"
SM = PREFIX + "sm@example.com"  # the acting user: a System Manager
TGT = PREFIX + "tgt@example.com"  # the user whose rows are aimed at
SCRATCH = "CRG Scratch"  # not "Jarvis ...": a custom DocType standing for any DocType
CODES = {"sensitive": "sensitive_refused", "structure": "structure_refused"}


def _user(email: str, roles: tuple) -> None:
	if frappe.db.exists("User", email):
		return
	prev = frappe.flags.in_import  # the flag Frappe's user-creation throttle honours
	frappe.flags.in_import = True
	try:
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Crg",
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
		doc.add_roles(*roles)
	finally:
		frappe.flags.in_import = prev


def _forget(doctype: str, name: str) -> None:
	"""Remove the "Deleted" comment Frappe's ``delete_doc`` writes for every record it
	deletes (``frappe.model.delete_doc.insert_feed``: no reference name, the record is
	named in the subject). Exactly this record's, never a wider delete."""
	frappe.db.delete(
		"Comment",
		{"comment_type": "Deleted", "reference_doctype": doctype, "subject": f"{doctype} {name}"},
	)


def _contacts_of(email: str) -> list[str]:
	"""The contacts Frappe made for a user (they can be deleted with the user, or with
	a Customer the user is a portal user of)."""
	names = frappe.get_all("Contact", or_filters={"email_id": email, "user": email}, pluck="name")
	names += frappe.get_all("Contact Email", filters={"email_id": email}, pluck="parent")
	return sorted(set(names))


def _drop_user(email: str, contacts: list[str]) -> None:
	"""The user and everything Frappe keeps beside one: its rows, the contact made
	for it, its defaults and the records of its deletion."""
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	for doctype in ("Has Role", "Block Module", "DefaultValue"):
		frappe.db.delete(doctype, {"parent": email})
	for contact in contacts:  # deleting the user may already have deleted its contact
		frappe.delete_doc("Contact", contact, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.db.delete("Deleted Document", {"deleted_doctype": "Contact", "deleted_name": contact})
		_forget("Contact", contact)
	frappe.db.delete("Notification Settings", {"name": email})
	frappe.db.delete("Version", {"ref_doctype": "User", "docname": email})
	frappe.db.delete("Deleted Document", {"deleted_name": email})
	_forget("User", email)
	_forget("Notification Settings", email)
	frappe.clear_cache(user=email)


def _drop_scratch() -> None:
	frappe.delete_doc_if_exists("DocType", SCRATCH, force=True)
	frappe.db.delete("Deleted Document", {"deleted_doctype": "DocType", "deleted_name": SCRATCH})
	frappe.db.delete("Version", {"ref_doctype": "DocType", "docname": SCRATCH})
	_forget("DocType", SCRATCH)
	frappe.db.sql_ddl(f"DROP TABLE IF EXISTS `tab{SCRATCH}`")
	frappe.clear_cache(doctype=SCRATCH)


def _drop(doctype: str, filters: dict) -> None:
	for name in frappe.get_all(doctype, filters=filters, pluck="name"):
		frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		frappe.db.delete("Version", {"ref_doctype": doctype, "docname": name})
		frappe.db.delete("Deleted Document", {"deleted_doctype": doctype, "deleted_name": name})
		_forget(doctype, name)


def _row(doctype: str, parent: str, parenttype: str) -> str:
	return frappe.db.get_value(doctype, {"parent": parent, "parenttype": parenttype}, "name")


class _Case(frappe._dict):
	"""One guarded row: what is aimed at it and the parent its refusal must name."""


class _Base(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._remove_fixtures()
		ensure_jarvis_user_role()
		# Sales Master Manager: the acting user may edit the Customer of the last case.
		sales = [r for r in ("Sales Master Manager",) if frappe.db.exists("Role", r)]
		_user(SM, ("System Manager", "Jarvis User", *sales))
		_user(TGT, ("Jarvis User",))
		target = frappe.get_doc("User", TGT)
		target.append("block_modules", {"module": "Core"})
		target.save(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "DocType",
				"name": SCRATCH,
				"module": "Custom",
				"custom": 1,
				"autoname": "hash",
				"fields": [{"label": "Title", "fieldname": "title", "fieldtype": "Data"}],
				"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1}],
			}
		).insert(ignore_permissions=True)
		webhook = frappe.get_doc(
			{
				"doctype": "Webhook",
				"__newname": PREFIX + "webhook",
				"webhook_doctype": "ToDo",
				"webhook_docevent": "after_insert",
				"request_url": "https://example.invalid/crg",
				"enabled": 0,
				"webhook_headers": [{"key": "X-Crg", "value": "1"}],
			}
		).insert(ignore_permissions=True)
		notification = frappe.get_doc(
			{
				"doctype": "Notification",
				"name": PREFIX + "notification",
				"subject": "crg",
				"document_type": "ToDo",
				"event": "New",
				"channel": "System Notification",
				"enabled": 0,
				"message": "crg",
				"recipients": [{"receiver_by_role": "System Manager"}],
			}
		).insert(ignore_permissions=True)
		contact = frappe.get_doc(
			{
				"doctype": "Contact",
				"first_name": PREFIX + "contact",
				"phone_nos": [{"phone": "+91 11111 11111", "is_primary_phone": 1}],
			}
		).insert(ignore_permissions=True)
		cls.contact = contact.name
		cls.phone_row = contact.phone_nos[0].name
		cls.cases = [
			_Case(dt="Has Role", parent=("User", TGT), changes={"role": "System Manager"}, risk="sensitive"),
			_Case(
				dt="DocPerm",
				parent=("DocType", SCRATCH),
				changes={"role": "All", "delete": 1},
				risk="structure",
			),
			_Case(
				dt="DocField",
				parent=("DocType", SCRATCH),
				changes={"permlevel": 3, "hidden": 1},
				risk="structure",
			),
			_Case(dt="Block Module", parent=("User", TGT), changes={"module": "Desk"}, risk="sensitive"),
			_Case(
				dt="Webhook Header",
				parent=("Webhook", webhook.name),
				changes={"key": "Authorization", "value": "Bearer crg"},
				risk="sensitive",
			),
			_Case(
				dt="Notification Recipient",
				parent=("Notification", notification.name),
				changes={"receiver_by_role": "All"},
				risk="sensitive",
			),
		]
		customer = cls._customer()
		if customer:
			cls.cases.append(
				_Case(dt="Portal User", parent=("Customer", customer), changes={"user": SM}, risk="sensitive")
			)
		# A Table MultiSelect row (an OAuth Client's allowed roles).
		oauth = frappe.get_doc(
			{
				"doctype": "OAuth Client",
				"app_name": PREFIX + "oauth",
				"redirect_uris": "https://example.invalid/crg",
				"default_redirect_uri": "https://example.invalid/crg",
				"allowed_roles": [{"role": "System Manager"}],
			}
		).insert(ignore_permissions=True)
		cls.cases.append(
			_Case(
				dt="OAuth Client Role",
				parent=("OAuth Client", oauth.name),
				changes={"role": "All"},
				risk="sensitive",
			)
		)
		cls.role_cases = cls._role_rows_of_ordinary_parents()
		cls.all_cases = [*cls.cases, *cls.role_cases]
		for case in cls.all_cases:
			case.row = _row(case.dt, case.parent[1], case.parent[0])
			assert case.row, case.dt
			field = frappe.db.get_value(case.dt, case.row, "parentfield")
			case.label = frappe.get_meta(case.parent[0]).get_field(field).label
		frappe.db.commit()

	@classmethod
	def _customer(cls) -> str | None:
		group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
		if not (group and territory):
			return None
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": PREFIX + "customer",
				"customer_group": group,
				"territory": territory,
				"portal_users": [{"user": TGT}],
			}
		).insert(ignore_permissions=True)
		return customer.name

	@classmethod
	def _role_rows_of_ordinary_parents(cls) -> list:
		"""Has Role rows under parents the guard does not classify: a Page (written
		straight to the table: a Page is only made in developer mode), a Dashboard
		Chart and a Workspace."""
		page = frappe.get_doc(
			{"doctype": "Page", "name": PREFIX + "page", "page_name": PREFIX + "page", "module": "Core"}
		)
		page.db_insert()
		frappe.get_doc(
			{
				"doctype": "Has Role",
				"name": PREFIX + "page-role",
				"role": "System Manager",
				"parent": page.name,
				"parenttype": "Page",
				"parentfield": "roles",
				"idx": 1,
			}
		).db_insert()
		chart = frappe.get_doc(
			{
				"doctype": "Dashboard Chart",
				"chart_name": PREFIX + "chart",
				"chart_type": "Count",
				"document_type": "ToDo",
				"based_on": "creation",
				"group_by_type": "Count",
				"filters_json": "[]",
				"type": "Bar",
				"roles": [{"role": "System Manager"}],
			}
		).insert(ignore_permissions=True)
		workspace = frappe.get_doc(
			{
				"doctype": "Workspace",
				"name": PREFIX + "workspace",
				"label": PREFIX + "workspace",
				"title": PREFIX + "workspace",
				"public": 1,
				"module": "Core",
				"content": "[]",
				"roles": [{"role": "System Manager"}],
			}
		).insert(ignore_permissions=True)
		parents = (("Page", page.name), ("Dashboard Chart", chart.name), ("Workspace", workspace.name))
		return [_Case(dt="Has Role", parent=p, changes={"role": "Guest"}, risk="sensitive") for p in parents]

	@classmethod
	def _remove_fixtures(cls):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		# Read before anything is deleted: a contact can go with the Customer.
		contacts = {email: _contacts_of(email) for email in (SM, TGT)}
		for owner in (SM, TGT):
			convs = frappe.get_all(CONV, filters={"owner": owner}, pluck="name")
			if convs:
				frappe.db.delete(PENDING, {"conversation": ["in", convs]})
				frappe.db.delete(MSG, {"conversation": ["in", convs]})
				frappe.db.delete(TURN, {"conversation": ["in", convs]})
				frappe.db.delete(CONV, {"name": ["in", convs]})
		frappe.db.delete(AGENT_WRITE, {"actor": ["in", (SM, TGT)]})
		frappe.db.delete("Jarvis Custom Skill", {"skill_name": ["like", PREFIX + "%"]})
		_drop("Customer", {"customer_name": PREFIX + "customer"})
		_drop("Contact", {"first_name": PREFIX + "contact"})
		_drop("Webhook", {"name": PREFIX + "webhook"})
		_drop("OAuth Client", {"app_name": PREFIX + "oauth"})
		_drop("Dashboard Chart", {"name": PREFIX + "chart"})
		_drop("Workspace", {"name": PREFIX + "workspace"})
		frappe.db.delete("Has Role", {"parent": ["like", PREFIX + "%"]})
		frappe.db.delete("Page", {"name": PREFIX + "page"})
		_drop("Notification", {"name": PREFIX + "notification"})
		for doctype in ("Has Role", "DocField", "Contact Phone"):
			frappe.db.delete(doctype, {"name": ["like", PREFIX + "%"]})
		_drop_scratch()
		_drop_user(TGT, contacts[TGT])
		_drop_user(SM, contacts[SM])
		# Whatever those deletes recorded (a user's contact goes with its Customer).
		frappe.db.delete("Deleted Document", {"data": ["like", f"%{PREFIX}%"]})
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		cls._remove_fixtures()
		super().tearDownClass()

	def setUp(self):
		for target in ("jarvis.chat.pump.ensure_pump", "jarvis.chat.pump.lpush_wake"):
			stub = patch(target, return_value={})
			stub.start()
			self.addCleanup(stub.stop)
		legacy = patch("jarvis.chat.api._dispatch_turn", return_value=None)
		legacy.start()
		self.addCleanup(legacy.stop)
		self._published = []
		frappe.set_user(SM)

	def tearDown(self):
		frappe.db.rollback()
		wr.take_refusal()
		frappe.set_user("Administrator")

	# ---- helpers ---------------------------------------------------------- #
	def _conv(self, **flags) -> str:
		conv = _make_conv(SM)
		if flags:
			frappe.db.set_value(CONV, conv, flags, update_modified=False)
			frappe.db.commit()
		return conv

	def _modes(self) -> dict[str, str]:
		"""A chat of every mode: plain, auto mode, an armed macro (skip_confirmation),
		"confirm all" (request_autorun) and an approved skill run."""
		from jarvis.tests.test_skill_approve_and_run import _make_skill

		skill = _make_skill(SM, armed=True, name=PREFIX + frappe.generate_hash(length=6))
		now = frappe.utils.now()
		return {
			"plain chat": self._conv(),
			"auto mode": self._conv(auto_mode=1, auto_mode_at=now),
			"armed macro": self._conv(skip_confirmation=1),
			"confirm all": self._conv(request_autorun=1, request_autorun_at=now),
			"skill run": self._conv(skill_autorun=1, skill_autorun_at=now, skill_autorun_skill=skill),
		}

	def _run(self, tool, args, conv=None):
		with patch("jarvis.chat.events.publish_to_user", side_effect=lambda u, p: self._published.append(p)):
			return api._run_tool(tool, args, conversation=conv)

	def _stored(self, case) -> dict | None:
		return frappe.db.get_value(case.dt, case.row, "*", as_dict=True)

	def _update(self, case) -> dict:
		return {"doctype": case.dt, "name": case.row, "changes": dict(case.changes)}

	def _delete(self, case) -> dict:
		return {"doctype": case.dt, "name": case.row}

	def _client_save(self, case) -> dict:
		"""``frappe.client.save`` of the row as a client would send it: whole."""
		doc = {k: v for k, v in self._stored(case).items() if v is not None}
		doc.update({"doctype": case.dt, "modified": str(doc["modified"]), "creation": str(doc["creation"])})
		doc.update(case.changes)
		return {"method": "frappe.client.save", "args": {"doc": frappe.as_json(doc)}}

	def _calls(self, case):
		return (("update_doc", self._update(case)), ("delete_doc", self._delete(case)))

	def _refused_rows(self, tool: str, doctype: str) -> int:
		return frappe.db.count(AGENT_WRITE, {"tool": tool, "ref_doctype": doctype, "outcome": "refused"})

	def _assert_refused(self, result, case, where=""):
		"""Refused with the parent's code, naming the parent record and its table."""
		self.assertIsInstance(result, dict, where)
		self.assertFalse(result.get("ok"), (where, result))
		error = result["error"]
		self.assertEqual(error["code"], CODES[case.risk], (where, error))
		message = error["message"]
		self.assertIn(f"Change this through its record: {case.parent[0]} {case.parent[1]}", message, where)
		self.assertIn(f"field {case.label}", message, where)
		self.assertEqual(frappe.utils.strip_html(message).strip(), message, "nothing strip_html would eat")
		self.assertNotIn(chr(0x2014), message + (error.get("hint") or ""), "no em-dash")
		self.assertEqual(error.get("doctype"), case.dt, where)
		self.assertEqual(error.get("kind"), _failure_kind.NOT_FIXABLE, f"{where}: error.kind")
		self.assertEqual(_failure_kind.envelope_kind(result), _failure_kind.NOT_FIXABLE, where)

	def _attempt(self, case, where, fn):
		"""Run one attempt, hand back its result, and prove the row is as it was. The
		attempt is rolled back either way, so a write the unfixed code lets through
		never reaches the next case."""
		before = self._stored(case)
		try:
			result = fn()
			after = self._stored(case)
		finally:
			frappe.db.rollback()
			wr.take_refusal()
		self.assertEqual(after, before, f"{where}: {case.dt} row changed")
		return result


class TestClassifiedByTheStoredParent(_Base):
	def test_update_and_delete_are_child_row_writes(self):
		for case in self.all_cases:
			for tool, args in self._calls(case):
				self.assertEqual(wr.risk_of(tool, args), "child_row", (case.dt, tool))
				with self.assertRaises(WriteRefusedError, msg=(case.dt, tool)):
					wr.check(tool, args, guarded=True)
				self.assertEqual(wr.allow_entries(tool, args), [], "no card can admit it")
			batch = {"doctype": case.dt, "updates": [{"name": case.row, "changes": dict(case.changes)}]}
			self.assertEqual(wr.risk_of("update_doc", batch), "child_row", case.dt)
			names = {"doctype": case.dt, "names": [case.row]}
			self.assertEqual(wr.risk_of("delete_doc", names), "child_row", case.dt)

	def test_the_callers_parent_is_never_believed(self):
		case = self.cases[0]
		args = self._update(case)
		args["changes"].update({"parenttype": "Contact", "parent": self.contact, "parentfield": "phone_nos"})
		self.assertEqual(wr.risk_of("update_doc", args), "child_row")
		args = {**self._update(case), "parenttype": "Contact", "parent": self.contact}
		self.assertEqual(wr.risk_of("update_doc", args), "child_row")

	def test_a_doctype_in_another_letter_case(self):
		case = self.cases[0]
		args = {**self._update(case), "doctype": case.dt.lower()}
		self.assertEqual(wr.risk_of("update_doc", args), "child_row")

	def test_an_ordinary_row_and_a_missing_row_are_not_classified(self):
		args = {"doctype": "Contact Phone", "name": self.phone_row, "changes": {"phone": "+91 22222 22222"}}
		self.assertIsNone(wr.risk_of("update_doc", args))
		self.assertIsNone(wr.risk_of("delete_doc", {"doctype": "Contact Phone", "name": self.phone_row}))
		# A row that does not exist: the tool answers with its own "not found".
		missing = {"doctype": "Has Role", "name": PREFIX + "missing", "changes": {"role": "All"}}
		self.assertIsNone(wr.risk_of("update_doc", missing))

	def test_the_stored_parent_decides_for_a_child_used_under_both_kinds(self):
		"""OAuth Scope sits under Connected App (sensitive) and Token Cache (ordinary)."""
		args = {"doctype": "OAuth Scope", "name": "x", "changes": {"scope": "all"}}
		with patch.object(wr, "_stored_parent", return_value=("Token Cache", "x", "scopes")):
			self.assertIsNone(wr.risk_of("update_doc", args), "an ordinary parent: unchanged")
			self.assertIsNone(wr.doc_risk(frappe._dict(doctype="OAuth Scope", name="x"), "before_validate"))
		with patch.object(wr, "_stored_parent", return_value=("Connected App", "x", "scopes")):
			self.assertEqual(wr.risk_of("update_doc", args), "child_row")
		# An ordinary row being moved under a User is a User's row.
		moved = frappe.get_doc("Contact Phone", self.phone_row)
		self.assertIsNone(wr.doc_risk(moved, "before_validate"))
		moved.update({"parenttype": "User", "parent": TGT, "parentfield": "roles"})
		self.assertEqual(wr.doc_risk(moved, "before_validate"), "child_row")

	def test_a_role_list_is_refused_under_every_parent(self):
		"""A child table that is nothing but a list of roles grants access wherever it
		sits: Has Role under a Page, a Dashboard Chart or a Workspace is refused like
		Has Role under a User. Tables that merely mention a role are not role lists."""
		for table in ("Has Role", "OAuth Client Role", "Jarvis Custom Skill Allowed Role"):
			self.assertTrue(wr._role_list(table), table)
		for table in ("Contact Phone", "Notification Recipient", "Portal User", "Jarvis Custom Skill Share"):
			self.assertFalse(wr._role_list(table), table)
		for case in self.role_cases:
			for tool, args in self._calls(case):
				self.assertEqual(wr.risk_of(tool, args), "child_row", (case.parent, tool))
			self.assertEqual(wr.doc_risk(frappe.get_doc(case.dt, case.row), "on_trash"), "child_row")
			with self.assertRaises(SensitiveWriteRefusedError) as raised:
				wr.check("update_doc", self._update(case))
			self.assertIn("grants access", str(raised.exception))
		# Owner decision R2-18: the parent's own update is sensitive when it sets the
		# role list, and ordinary when it does not.
		chart = self.role_cases[1].parent[1]
		args = {"doctype": "Dashboard Chart", "name": chart, "changes": {"roles": [{"role": "Guest"}]}}
		self.assertEqual(wr.risk_of("update_doc", args), "sensitive")
		args = {"doctype": "Dashboard Chart", "name": chart, "changes": {"roles": []}}
		self.assertEqual(wr.risk_of("update_doc", args), "sensitive", "emptying it opens the chart")
		args = {"doctype": "Dashboard Chart", "name": chart, "changes": {"color": "#ff0000"}}
		self.assertIsNone(wr.risk_of("update_doc", args), "anything else on it is ordinary")
		self.assertEqual(wr.risk_class("Dashboard Chart"), "access")
		self.assertEqual(wr.risk_class("ToDo"), "")
		self.assertEqual(wr._role_tables("Dashboard Chart"), ["roles"])
		self.assertEqual(wr._role_tables("ToDo"), [])
		self.assertEqual(wr._role_tables("Has Role"), [], "a child table has none of its own")
		self.assertEqual(wr._role_tables("No Such Form"), [])
		new = {"doctype": "Dashboard Chart", "values": {"chart_name": "x", "roles": [{"role": "Guest"}]}}
		self.assertEqual(wr.risk_of("create_doc", new), "sensitive")
		new["values"]["roles"] = []
		self.assertIsNone(wr.risk_of("create_doc", new), "a create that lists no role grants nothing")

	def test_a_role_list_set_through_its_parent_parks_a_card_in_every_mode(self):
		"""R2-18. The row is refused on its own (#1841); its parent's update ran with
		no card in the uncarded modes. Now it parks the access card everywhere."""
		chart = self.role_cases[1].parent[1]
		kept = frappe.get_all(
			"Has Role", filters={"parent": chart, "parenttype": "Dashboard Chart"}, pluck="name"
		)
		roles = [*({"name": n} for n in kept), {"role": "Guest"}]
		args = {"doctype": "Dashboard Chart", "name": chart, "changes": {"roles": roles}}
		for mode, conv in self._modes().items():
			result = self._run("update_doc", args, conv)
			self.assertTrue(result["ok"], (mode, result))
			self.assertEqual(result["data"]["status"], "pending_confirmation", mode)
			card = frappe.parse_json(frappe.db.get_value(PENDING, {"conversation": conv}, "card"))
			self.assertEqual(card.get("risk_line"), wr.RISK_LINES["access"], mode)
			frappe.db.rollback()
		self.assertEqual(
			frappe.db.count("Has Role", {"parent": chart, "parenttype": "Dashboard Chart"}), len(kept)
		)
		# And the card, confirmed, writes exactly that list (auto mode's card).
		from jarvis.chat import pending_actions as pa

		conv = self._conv(auto_mode=1, auto_mode_at=frappe.utils.now())
		self.assertEqual(self._run("update_doc", args, conv)["data"]["status"], "pending_confirmation")
		done = pa.execute(frappe.db.get_value(PENDING, {"conversation": conv}, "name"), conversation=conv)
		self.assertTrue(done["ok"], done)
		stored = frappe.get_all(
			"Has Role",
			filters={"parent": chart, "parenttype": "Dashboard Chart"},
			pluck="role",
			order_by="idx",
		)
		self.assertEqual(stored[-1], "Guest")
		self.assertEqual(len(stored), len(kept) + 1)
		frappe.db.delete("Has Role", {"parent": chart, "parenttype": "Dashboard Chart", "role": "Guest"})
		frappe.db.commit()

	def test_the_save_itself_sees_a_changed_role_list(self):
		"""The same rule at the save, for the routes that never pass the argument
		check (``frappe.client`` through run_method)."""
		chart = frappe.get_doc("Dashboard Chart", self.role_cases[1].parent[1])
		chart.get_doc_before_save = lambda: frappe.get_doc("Dashboard Chart", chart.name)
		self.assertIsNone(wr.doc_risk(chart, "before_validate"), "unchanged roles")
		chart.append("roles", {"role": "Guest"})
		self.assertEqual(wr.doc_risk(chart, "before_validate"), "sensitive")
		self.assertIsNone(wr.doc_risk(chart, "on_trash"))
		chart.set("roles", [])
		self.assertEqual(wr.doc_risk(chart, "before_validate"), "sensitive", "emptied")
		fresh = frappe.new_doc("Dashboard Chart")
		self.assertIsNone(wr.doc_risk(fresh, "before_validate"), "a new record with no role")
		fresh.append("roles", {"role": "Guest"})
		self.assertEqual(wr.doc_risk(fresh, "before_validate"), "sensitive")

	def test_a_role_list_of_jarviss_own_configuration(self):
		"""Refused when asked for by row (it parked a card before); the ORM guard still
		leaves Jarvis's own configuration to Jarvis's own tools. Creating one, and a
		skill's share row, are classified as before."""
		table = "Jarvis Custom Skill Allowed Role"
		with patch.object(wr, "_stored_parent", return_value=("Jarvis Custom Skill", "s", "allowed_roles")):
			args = {"doctype": table, "name": "x", "changes": {"role": "All"}}
			self.assertEqual(wr.risk_of("update_doc", args), "child_row")
			self.assertEqual(wr.risk_of("delete_doc", {"doctype": table, "name": "x"}), "child_row")
			self.assertIsNone(wr.doc_risk(frappe._dict(doctype=table, name="x"), "before_validate"))
		self.assertEqual(wr.risk_of("create_doc", {"doctype": table, "values": {"role": "All"}}), "sensitive")
		share = {"doctype": "Jarvis Custom Skill Share", "name": "x", "changes": {"user": TGT}}
		self.assertEqual(wr.risk_of("update_doc", share), "sensitive")

	def test_an_unknown_doctype_is_never_an_error_here(self):
		"""Two spaces, a name that is no doctype, nothing at all: not classified and
		nothing raised or left in the message log; the tool gives its own answer."""
		logged = len(frappe.get_message_log())
		for doctype in ("Has  Role", "Nope Zz", "   ", "", None, 5, ["Has Role"]):
			for tool, args in (
				("update_doc", {"doctype": doctype, "name": "x", "changes": {"role": "All"}}),
				("update_doc", {"doctype": doctype, "updates": [{"name": "x", "changes": {"a": 1}}]}),
				("delete_doc", {"doctype": doctype, "name": "x"}),
			):
				self.assertIsNone(wr.risk_of(tool, args), (doctype, tool))
				self.assertIsNone(wr.check(tool, args, guarded=True), (doctype, tool))
			if isinstance(doctype, str):
				row = frappe._dict(doctype=doctype, name="x")
				self.assertIsNone(wr._doc_child_row(row), doctype)
				with wr.guard_scope([]):
					wr.guard_doc_event(row, "before_validate")
					wr._guard_delete(doctype, "x", None)
		self.assertEqual(len(frappe.get_message_log()), logged)

	def test_an_orphan_row(self):
		"""No stored parent: refused when the child table can sit under a structure or
		sensitive parent at all (DocField only ever does; Has Role may), unchanged
		when it never does (Contact Phone)."""
		frappe.set_user("Administrator")
		for doctype, values, expected in (
			("DocField", {"fieldname": "crg_orphan", "fieldtype": "Data"}, "child_row"),
			("Has Role", {"role": "All"}, "child_row"),
			("Contact Phone", {"phone": "+91 33333 33333"}, None),
		):
			row = frappe.get_doc({"doctype": doctype, "name": PREFIX + "orphan", **values})
			row.db_insert()
			args = {"doctype": doctype, "name": row.name, "changes": {"idx": 1}}
			self.assertEqual(wr.risk_of("update_doc", args), expected, doctype)
			self.assertEqual(wr.risk_of("delete_doc", {"doctype": doctype, "name": row.name}), expected)
			if expected:
				with self.assertRaises(WriteRefusedError) as raised:
					wr.check("update_doc", args)
				self.assertIn("no parent record", str(raised.exception))

	def test_a_row_of_jarviss_own_configuration(self):
		"""Jarvis's own configuration is sensitive in the argument layer only: so are
		its rows (the ORM guard leaves both to Jarvis's own tools)."""
		stored = ("Jarvis Macro", "crg-macro", "steps")
		args = {"doctype": "Jarvis Macro Step", "name": "x", "changes": {"idx": 2}}
		with patch.object(wr, "_stored_parent", return_value=stored):
			self.assertEqual(wr.risk_of("update_doc", args), "child_row")
			row = frappe._dict(doctype="Jarvis Macro Step", name="x")
			self.assertIsNone(wr.doc_risk(row, "before_validate"))

	def test_a_conditionally_sensitive_parent_counts_whole(self):
		"""A Report's roles and filters, a Web Form's fields: whatever the record's
		type or content, its rows go through the record."""
		for parent, field, table in (
			("Report", "roles", "Has Role"),
			("Report", "filters", "Report Filter"),
			("Web Form", "web_form_fields", "Web Form Field"),
			("Auto Repeat", "repeat_on_days", "Auto Repeat Day"),
		):
			with patch.object(wr, "_stored_parent", return_value=(parent, "x", field)):
				args = {"doctype": table, "name": "x", "changes": {"idx": 2}}
				self.assertEqual(wr.risk_of("update_doc", args), "child_row", (parent, table))
				row = frappe._dict(doctype=table, name="x")
				self.assertEqual(wr.doc_risk(row, "before_validate"), "child_row", (parent, table))

	def test_only_the_sensitive_table_of_a_field_sensitive_parent(self):
		with patch.object(wr, "_stored_parent", return_value=("Customer", "c", "portal_users")):
			self.assertEqual(wr.risk_of("delete_doc", {"doctype": "Portal User", "name": "x"}), "child_row")
		with patch.object(wr, "_stored_parent", return_value=("Customer", "c", "sales_team")):
			self.assertIsNone(wr.risk_of("delete_doc", {"doctype": "Sales Team", "name": "x"}))


class TestWhatTheRefusalSays(_Base):
	"""What the model is told to do instead depends on what the PARENT gets: a card,
	the Desk, or (a parent the guard does not classify) nothing at all, in which case
	the refusal must not send the model to a write that would run uncarded."""

	def _refusal(self, dt, kind, parenttype, parent, field, grant=False):
		return wr.child_row_refusal(wr._ChildRow(dt, "row-1", kind, parenttype, parent, field, grant=grant))

	def test_under_a_sensitive_parent_it_points_at_the_parents_card(self):
		e = self._refusal("Has Role", "sensitive", "User", TGT, "roles")
		self.assertIsInstance(e, SensitiveWriteRefusedError)
		self.assertIn("Use update_doc on User with its roles rows as they should be", str(e))
		self.assertIn("so the user gets one confirmation card showing the whole change.", str(e))
		self.assertIn("each kept or changed row with its name from get_doc", str(e), "what the tool asks for")
		self.assertIn("update that record's roles table", e.hint)

	def test_under_a_structure_parent_it_points_at_desk_only(self):
		e = self._refusal("DocField", "structure", "DocType", SCRATCH, "fields")
		self.assertIsInstance(e, StructureRefusedError)
		self.assertIn("it is set up in Desk, not from chat", str(e))
		self.assertEqual(e.desk_path, wr.desk_path("DocType", SCRATCH))
		for text in (str(e), e.hint):
			self.assertNotIn("update_doc", text)
			self.assertNotIn("confirmation card", text)

	def test_under_a_parent_with_a_role_list_it_points_at_the_parents_card(self):
		"""Review of aerele/jarvis#1841 said the refusal of a Page's, Dashboard
		Chart's or Workspace's role row must not promise a card the parent would not
		get. Owner decision R2-18 gives the parent that card, so the refusal now
		sends the model there, as under a User."""
		for case in self.role_cases:
			with self.assertRaises(SensitiveWriteRefusedError) as raised:
				wr.check("update_doc", self._update(case))
			e = raised.exception
			record = f"{case.parent[0]} {case.parent[1]}"
			self.assertIn("grants access (this changes who can see or edit)", str(e), record)
			if case.parent[0] == "Page":
				# Frappe lets only the Administrator save a Page: no card is promised
				# that nobody else could confirm.
				self.assertIn(f"tell the user to change it in Desk on {record}", str(e))
				self.assertNotIn("update_doc", str(e) + e.hint, record)
				continue
			self.assertIn("update_doc", str(e) + e.hint, record)
			self.assertIn("confirmation card", str(e) + e.hint, record)
			self.assertTrue(
				wr._parent_gets_a_card(wr._target_child_row(wr._Target(case.dt, "update", case.row)))
			)

	def test_a_role_row_is_about_access_whatever_its_parent_runs(self):
		"""A Report counts whole as a conditional (code) doctype, but a role row of it
		decides who may open it: the reason is the access one."""
		script = frappe.db.get_value("Report", {"report_type": "Script Report"}, "name")
		builder = frappe.db.get_value("Report", {"report_type": "Report Builder"}, "name")
		for report in (script, builder):
			e = self._refusal("Has Role", "sensitive", "Report", report, "roles")
			self.assertIn("grants access (this changes who can see or edit)", str(e), report)
			self.assertNotIn("runs code", str(e), report)
		# A Script Report's update is carded (it is sensitive). A Report Builder
		# report's is carded too when it sets the role list (R2-18), and only then.
		for name in (script, builder):
			self.assertIn(
				"confirmation card", str(self._refusal("Has Role", "sensitive", "Report", name, "roles"))
			)
		args = {"doctype": "Report", "name": builder, "changes": {"roles": []}}
		self.assertEqual(wr.risk_of("update_doc", args), "sensitive", "who may open the report")
		args = {"doctype": "Report", "name": builder, "changes": {"disabled": 1}}
		self.assertIsNone(wr.risk_of("update_doc", args), "the rest of a Report Builder report is ordinary")
		self.assertEqual(
			wr.risk_line("update_doc", args | {"changes": {"roles": []}}), wr.RISK_LINES["access"]
		)
		# A NEW Report Builder report takes its form's roles from Frappe itself
		# (``Report.before_insert``): that is not a role list the caller set, so
		# creating one stays an ordinary write, at the arguments and at the save.
		values = {
			"report_name": PREFIX + "new builder",
			"ref_doctype": "ToDo",
			"report_type": "Report Builder",
			"is_standard": "No",
		}
		self.assertIsNone(wr.risk_of("create_doc", {"doctype": "Report", "values": values}))
		fresh = frappe.get_doc({"doctype": "Report", **values})
		fresh.set("__islocal", True)  # as ``insert`` marks it before ``before_insert`` runs
		fresh.set_doctype_roles()
		self.assertTrue(fresh.roles, "Frappe filled them in")
		self.assertIsNone(wr.doc_risk(fresh, "before_validate"), "its own default roles")
		fresh.append("roles", {"role": "Guest"})
		self.assertEqual(wr.doc_risk(fresh, "before_validate"), "sensitive", "a list of the caller's")
		conv = self._conv(auto_mode=1, auto_mode_at=frappe.utils.now())
		made = self._run("create_doc", {"doctype": "Report", "values": values}, conv)
		self.addCleanup(
			lambda: (frappe.db.delete("Report", {"name": values["report_name"]}), frappe.db.commit())
		)
		self.addCleanup(
			frappe.db.delete, "Has Role", {"parent": values["report_name"], "parenttype": "Report"}
		)
		self.assertTrue(made.get("ok"), made)
		self.assertNotEqual((made.get("data") or {}).get("status"), "pending_confirmation", "no card")
		self.assertTrue(frappe.db.exists("Report", values["report_name"]), "created as before")
		doc = frappe.get_doc("Report", builder)
		doc.get_doc_before_save = lambda: frappe.get_doc("Report", builder)
		self.assertIsNone(wr.doc_risk(doc, "before_validate"))
		doc.append("roles", {"role": "Guest"})
		self.assertEqual(wr.doc_risk(doc, "before_validate"), "sensitive", "the same at the save")
		# A row that is not a role list keeps the parent's own reason.
		e = self._refusal("Report Filter", "sensitive", "Report", script, "filters")
		self.assertIn("runs code", str(e))

	def test_one_audit_label_in_both_layers(self):
		"""A child-row refusal is logged as ``child_row`` whether the arguments or the
		save itself were refused, with what its parent is beside it."""
		case = self.cases[0]
		for layer in ("arguments", "save"):
			with patch.object(wr, "log_line") as logged:
				if layer == "arguments":
					self._run("update_doc", self._update(case), self._conv())
				else:
					frappe.set_user("Administrator")
					with wr.guard_scope([]), self.assertRaises(WriteRefusedError):
						frappe.get_doc(case.dt, case.row).save(ignore_permissions=True)
			frappe.db.rollback()
			wr.take_refusal()
			refused = [c for c in logged.call_args_list if c.args[3] == "refused"]
			self.assertEqual(len(refused), 1, layer)
			self.assertEqual(refused[0].args[:2], ("child_row", case.dt), layer)
			self.assertEqual(refused[0].kwargs.get("parent_kind"), case.risk, layer)
			self.assertEqual(refused[0].kwargs.get("under"), f"{case.parent[0]} {case.parent[1]}", layer)


class TestRefusedOnEveryRoute(_Base):
	def test_update_and_delete_refused_in_every_mode(self):
		for mode, conv in self._modes().items():
			for case in self.all_cases:
				for tool, args in self._calls(case):
					where = f"{mode} / {tool} {case.dt}"
					audited = self._refused_rows(tool, case.dt)

					def attempt(tool=tool, args=args, conv=conv, case=case, audited=audited, where=where):
						result = self._run(tool, args, conv)
						self.assertEqual(self._refused_rows(tool, case.dt), audited + 1, where)
						return result

					result = self._attempt(case, where, attempt)
					self._assert_refused(result, case, where)
					self.assertFalse(frappe.db.exists(PENDING, {"conversation": conv}), f"{where}: a card")

	def test_file_box_chat(self):
		conv = self._conv(file_box=1)
		for case in self.all_cases:
			for tool, args in self._calls(case):
				where = f"file box / {tool} {case.dt}"
				result = self._attempt(case, where, lambda t=tool, a=args: self._run(t, a, conv))
				self._assert_refused(result, case, where)

	def test_dispatch_confirmed_refuses_a_confirmed_and_an_uncarded_call(self):
		"""A card parked before this rule, and every uncarded run, end here."""
		for case in self.all_cases:
			for tool, args in self._calls(case):
				for flags in ({"allow_risky": True}, {"uncarded": True}, {}):
					where = f"dispatch_confirmed {flags} / {tool} {case.dt}"
					audited = self._refused_rows(tool, case.dt)

					def attempt(tool=tool, args=args, flags=flags, case=case, audited=audited, where=where):
						with patch("jarvis.api.dispatch") as dispatched:
							result = api.dispatch_confirmed(tool, args, **flags)
						dispatched.assert_not_called()  # refused before the tool, not only at the save
						self.assertEqual(self._refused_rows(tool, case.dt), audited + 1, where)
						return result

					self._assert_refused(self._attempt(case, where, attempt), case, where)

	def test_batches_refused(self):
		conv = self._conv(auto_mode=1)
		for case in self.all_cases:
			updates = {"doctype": case.dt, "updates": [{"name": case.row, "changes": dict(case.changes)}]}
			names = {"doctype": case.dt, "names": [case.row]}
			for tool, args in (("update_doc", updates), ("delete_doc", names)):
				where = f"batch {tool} {case.dt}"
				result = self._attempt(case, where, lambda t=tool, a=args: self._run(t, a, conv))
				self._assert_refused(result, case, where)

	def test_draft_panel_refuses(self):
		from jarvis.chat.actions_api import apply_action

		conv = self._conv()
		for case in self.all_cases:
			action = {
				"verb": "update",
				"doctype": case.dt,
				"name": case.row,
				"values": dict(case.changes),
				"conversation": conv,
			}
			where = f"draft panel {case.dt}"
			result = self._attempt(case, where, lambda a=action: apply_action(frappe.as_json(a)))
			self._assert_refused(result, case, where)

	def test_approval_board_edit_refuses_before_its_dry_run(self):
		from jarvis.chat import approvals_api

		for case in self.all_cases:
			args = self._update(case)
			items = [{"op": "update", "doctype": case.dt, "name": case.row, "values": args["changes"]}]
			with patch("jarvis.api._run_preview") as preview:
				result = approvals_api._edit_dry_run("update_doc", args, items)
			preview.assert_not_called()
			self.assertIsNotNone(result, case.dt)
			self.assertIn(f"{case.parent[0]} {case.parent[1]}", result["error"]["message"], case.dt)

	def test_file_box_sheet_and_pending_action_routes(self):
		from jarvis.chat import actions_api, held_sheets
		from jarvis.chat.pending_actions import _execute

		conv = self._conv(file_box=1)
		for case in self.all_cases:
			args = self._update(case)
			sheet = self._attempt(
				case, "sheet", lambda a=args: held_sheets.file_questions("update_doc", a, conv)
			)
			self._assert_refused(sheet, case, f"sheet {case.dt}")
			row = frappe._dict(tool="update_doc", exec_user=SM, kind="file_box_held", name="crg-row")
			held, _interfered, crash = self._attempt(
				case, "held", lambda r=row, a=args: _execute._dispatch(r, a, user=SM)
			)
			self.assertFalse(crash)
			self._assert_refused(held, case, f"pending action {case.dt}")
			record = {"tool": "delete_doc", "args": self._delete(case), "exec_user": SM}
			legacy = self._attempt(
				case, "legacy", lambda r=record: actions_api._dispatch_call(r, "crg-token", "", "crg crash")
			)
			self._assert_refused(legacy, case, f"legacy token {case.dt}")

	def test_frappe_client_through_run_method(self):
		"""``frappe.client.save`` saves the row itself; ``set_value`` and ``delete``
		write through the parent (Frappe's own child-table handling), which the guard
		already judged: all refused when nobody was shown a card."""
		for case in self.cases:
			calls = {
				"save": self._client_save(case),
				"set_value": {
					"method": "frappe.client.set_value",
					"args": {"doctype": case.dt, "name": case.row, "fieldname": dict(case.changes)},
				},
				"delete": {"method": "frappe.client.delete", "args": {"doctype": case.dt, "name": case.row}},
				"bulk_update": {
					"method": "frappe.client.bulk_update",
					"args": {
						"docs": frappe.as_json([{"doctype": case.dt, "docname": case.row, **case.changes}])
					},
				},
			}
			for label, call in calls.items():
				where = f"run_method {label} {case.dt}"
				result = self._attempt(
					case, where, lambda c=call: api.dispatch_confirmed("run_method", c, uncarded=True)
				)
				if label == "bulk_update":
					# Frappe collects each row's failure and answers ok: the row is unchanged.
					continue
				self.assertFalse(result.get("ok"), (where, result))
				self.assertIn(result["error"]["code"], (*wr.REFUSAL_CODES, "brake_refused"), where)
			# The row saved on its own is refused even after a human confirmed the call
			# (the card showed a method, not the parent's table).
			where = f"confirmed run_method save {case.dt}"
			result = self._attempt(
				case,
				where,
				lambda c=calls["save"]: api.dispatch_confirmed("run_method", c, allow_risky=True),
			)
			self._assert_refused(result, case, where)

	def test_a_role_row_saved_through_frappe_client(self):
		"""The row of an ordinary parent, saved on its own through run_method."""
		for case in self.role_cases:
			for flags in ({"uncarded": True}, {"allow_risky": True}):
				where = f"run_method save {flags} {case.parent[0]}"
				result = self._attempt(
					case,
					where,
					lambda f=flags, c=case: api.dispatch_confirmed("run_method", self._client_save(c), **f),
				)
				self._assert_refused(result, case, where)

	def test_run_method_in_an_auto_mode_chat(self):
		conv = self._conv(auto_mode=1, auto_mode_at=frappe.utils.now())
		case = self.cases[0]
		call = self._client_save(case)
		audited = self._refused_rows("run_method", case.dt)

		def attempt():
			result = self._run("run_method", call, conv)
			self.assertEqual(self._refused_rows("run_method", case.dt), audited + 1, "filed as refused")
			return result

		self._assert_refused(self._attempt(case, "auto mode run_method", attempt), case)

	def test_a_new_row_saved_as_a_root_document(self):
		"""``frappe.client.save`` of a row with no name inserts it under the parent it
		claims: a new System Manager role for a user, with no card. As Administrator:
		Frappe's own permission check already stops a System Manager here."""
		frappe.set_user("Administrator")
		doc = {
			"doctype": "Has Role",
			"role": "System Manager",
			"parent": TGT,
			"parenttype": "User",
			"parentfield": "roles",
		}
		call = {"method": "frappe.client.save", "args": {"doc": frappe.as_json(doc)}}
		granted = {"parent": TGT, "parenttype": "User", "role": "System Manager"}
		for flags in ({"uncarded": True}, {"allow_risky": True}):
			try:
				result = api.dispatch_confirmed("run_method", call, **flags)
				self.assertFalse(frappe.db.exists("Has Role", granted), flags)
			finally:
				frappe.db.rollback()
			self.assertEqual((result.get("ok"), result["error"]["code"]), (False, "sensitive_refused"), flags)
			self.assertIn(f"Change this through its record: User {TGT}", result["error"]["message"])

	def test_a_confirmed_run_method_that_writes_through_the_parent(self):
		"""R2-13, unchanged: ``frappe.client.set_value`` on a row loads and saves the
		PARENT. The user confirmed that call, so a sensitive parent's save is admitted;
		a structure parent's never is."""
		for case in self.cases:
			call = {
				"method": "frappe.client.set_value",
				"args": {"doctype": case.dt, "name": case.row, "fieldname": dict(case.changes)},
			}
			try:
				result = api.dispatch_confirmed("run_method", call, allow_risky=True)
				if case.risk == "structure":
					self.assertEqual(result["error"]["code"], "structure_refused", case.dt)
				elif case.dt in ("Webhook Header", "Notification Recipient"):
					self.assertTrue(result["ok"], (case.dt, result))
			finally:
				frappe.db.rollback()


class TestTheSaveItself(_Base):
	"""The ORM guard: a row saved, db_set or deleted as the tool call's ROOT write."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")

	def _error(self, case):
		return StructureRefusedError if case.risk == "structure" else SensitiveWriteRefusedError

	def test_root_save_db_set_and_delete_refused(self):
		for case in self.all_cases:
			field, value = next(iter(case.changes.items()))
			for allow in ([], wr.allow_entries("run_method", {})):
				with wr.guard_scope(allow):
					doc = frappe.get_doc(case.dt, case.row)
					doc.set(field, value)
					with self.assertRaises(self._error(case), msg=f"save {case.dt}"):
						doc.save(ignore_permissions=True)
					with self.assertRaises(self._error(case), msg=f"db_set {case.dt}"):
						frappe.get_doc(case.dt, case.row).db_set(field, value)
					with self.assertRaises(self._error(case), msg=f"delete {case.dt}"):
						frappe.delete_doc(case.dt, case.row, force=True, ignore_permissions=True)
					with self.assertRaises(self._error(case), msg=f"hookless delete {case.dt}"):
						frappe.delete_doc(
							case.dt, case.row, force=True, ignore_permissions=True, ignore_on_trash=True
						)
				frappe.db.rollback()
				self.assertIsNotNone(wr.take_refusal())

	def test_a_spoofed_parent_on_the_document_is_not_believed(self):
		case = self.cases[0]
		with wr.guard_scope([]):
			doc = frappe.get_doc(case.dt, case.row)
			doc.update({"parenttype": "Contact", "parent": self.contact, "parentfield": "phone_nos"})
			with self.assertRaises(SensitiveWriteRefusedError):
				doc.save(ignore_permissions=True)

	def test_refused_in_a_dry_run_too(self):
		"""A sensitive document's dry run builds its card; a row's never does."""
		from jarvis.tools._preview_sandbox import preview_sandbox

		case = self.cases[0]
		with wr.guard_scope(), self.assertRaises(SensitiveWriteRefusedError), preview_sandbox():
			frappe.get_doc(case.dt, case.row).db_set("role", "System Manager")

	def test_the_parents_own_save_carries_its_rows(self):
		"""Nested: the confirmed card named the User, and its rows save with it."""
		with wr.guard_scope(wr.allow_entries("update_doc", {"doctype": "User", "name": TGT, "changes": {}})):
			user = frappe.get_doc("User", TGT)
			user.append("roles", {"role": "Guest"})
			user.append("block_modules", {"module": "Desk"})
			user.save(ignore_permissions=True)
			self.assertIn("Guest", [r.role for r in frappe.get_doc("User", TGT).roles])

	def test_a_row_saved_by_another_documents_save_is_nested(self):
		from jarvis.tests.test_write_guard import _local_hook

		_local_hook(self, "ToDo", "on_update", "jarvis.tests.test_child_row_guard.crg_touch_row")
		case = self.cases[0]
		crg_touch_row.row = (case.dt, case.row)
		with wr.guard_scope([]):
			frappe.get_doc({"doctype": "ToDo", "description": PREFIX + "nested"}).insert()
		self.assertEqual(frappe.db.get_value(case.dt, case.row, "idx"), 7)

	def test_outside_a_tool_call_the_guard_is_inert(self):
		"""Desk, REST and the scheduler save and delete rows as before."""
		for case in self.all_cases:
			field, value = next(iter(case.changes.items()))
			doc = frappe.get_doc(case.dt, case.row)
			doc.set(field, value)
			doc.save(ignore_permissions=True)
			self.assertEqual(frappe.db.get_value(case.dt, case.row, field), value, case.dt)
			frappe.delete_doc(case.dt, case.row, force=True, ignore_permissions=True)
			self.assertFalse(frappe.db.exists(case.dt, case.row), case.dt)
			frappe.db.rollback()

	def test_an_ordinary_row_saves_as_the_root_write(self):
		with wr.guard_scope([]):
			doc = frappe.get_doc("Contact Phone", self.phone_row)
			doc.phone = "+91 44444 44444"
			doc.save(ignore_permissions=True)
			doc.db_set("phone", "+91 55555 55555")
			frappe.delete_doc("Contact Phone", self.phone_row, force=True, ignore_permissions=True)
		self.assertIsNone(wr.take_refusal())


def crg_touch_row(doc, method=None):
	"""A doc_events handler standing for a controller that saves a row itself."""
	row = frappe.get_doc(*crg_touch_row.row)
	row.idx = 7
	row.save(ignore_permissions=True)


class TestUnchanged(_Base):
	"""What must behave exactly as before."""

	def _phone(self):
		return frappe.db.get_value("Contact Phone", self.phone_row, "phone")

	def test_an_ordinary_child_row_in_every_mode(self):
		"""A Contact's phone row: a plain chat parks an ordinary card (no risk line),
		the uncarded modes update it at once, and a delete parks a card in all of them."""
		new = "+91 22222 22222"
		update = {"doctype": "Contact Phone", "name": self.phone_row, "changes": {"phone": new}}
		delete = {"doctype": "Contact Phone", "name": self.phone_row}
		self.assertEqual(wr.risk_line("update_doc", update), "")
		for mode, conv in self._modes().items():
			try:
				result = self._run("update_doc", update, conv)
				self.assertTrue(result["ok"], (mode, result))
				if mode == "plain chat":
					self.assertEqual(result["data"]["status"], "pending_confirmation", mode)
					self.assertNotEqual(self._phone(), new, mode)
					card = frappe.db.get_value(PENDING, {"conversation": conv}, "card")
					self.assertNotIn("risk_line", frappe.parse_json(card or "{}"), "an ordinary card")
				else:
					self.assertNotEqual(result["data"].get("status"), "pending_confirmation", mode)
					self.assertEqual(self._phone(), new, f"{mode}: updated with no card")
			finally:
				frappe.db.rollback()
			try:
				frappe.db.delete(PENDING, {"conversation": conv})
				result = self._run("delete_doc", delete, conv)
				self.assertTrue(result["ok"], (mode, result))
				self.assertEqual(result["data"]["status"], "pending_confirmation", f"{mode}: delete parks")
				self.assertTrue(frappe.db.exists("Contact Phone", self.phone_row))
			finally:
				frappe.db.rollback()

	def test_an_ordinary_child_row_through_dispatch_confirmed(self):
		new = "+91 66666 66666"
		update = {"doctype": "Contact Phone", "name": self.phone_row, "changes": {"phone": new}}
		for flags in ({"allow_risky": True}, {"uncarded": True}):
			result = api.dispatch_confirmed("update_doc", update, **flags)
			self.assertTrue(result["ok"], (flags, result))
			self.assertEqual(self._phone(), new)
			frappe.db.rollback()
		result = api.dispatch_confirmed(
			"delete_doc", {"doctype": "Contact Phone", "name": self.phone_row}, allow_risky=True
		)
		self.assertTrue(result["ok"], result)
		self.assertFalse(frappe.db.exists("Contact Phone", self.phone_row))

	def test_a_user_update_that_changes_roles_still_parks_the_sensitive_card(self):
		roles = [{"name": self.cases[0].row}, {"role": "Guest"}]
		args = {"doctype": "User", "name": TGT, "changes": {"roles": roles}}
		self.assertEqual(wr.risk_of("update_doc", args), "sensitive")
		for mode, conv in self._modes().items():
			result = self._run("update_doc", args, conv)
			self.assertTrue(result["ok"], (mode, result))
			self.assertEqual(result["data"]["status"], "pending_confirmation", mode)
			card = frappe.parse_json(frappe.db.get_value(PENDING, {"conversation": conv}, "card"))
			self.assertEqual(card.get("risk_line"), wr.RISK_LINES["access"], mode)
			frappe.db.rollback()

	def test_a_parent_update_of_a_structure_or_field_sensitive_doctype(self):
		conv = self._conv(auto_mode=1, auto_mode_at=frappe.utils.now())
		fields = [{"name": c.row, "hidden": 1} for c in self.cases if c.dt == "DocField"]
		result = self._run(
			"update_doc", {"doctype": "DocType", "name": SCRATCH, "changes": {"fields": fields}}, conv
		)
		self.assertEqual(result["error"]["code"], "structure_refused", result)
		self.assertIn("set up in Desk", result["error"]["message"])
		portal = [c for c in self.cases if c.dt == "Portal User"]
		if portal:
			args = {
				"doctype": "Customer",
				"name": portal[0].parent[1],
				"changes": {"portal_users": [{"name": portal[0].row}, {"user": SM}]},
			}
			result = self._run("update_doc", args, conv)
			self.assertEqual(result["data"]["status"], "pending_confirmation", result)

	def test_an_employee_field_rule_is_untouched(self):
		args = {"doctype": "Employee", "name": "x", "changes": {"user_id": TGT}}
		self.assertEqual(wr.risk_of("update_doc", args), "sensitive")
		self.assertIsNone(
			wr.risk_of("update_doc", {"doctype": "ToDo", "name": "x", "changes": {"status": "Closed"}})
		)
