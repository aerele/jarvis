"""R5-J9 + R5-J11(c) — delegate write capabilities and pack-derived ceiling counting.

R5-P0-04 (operator ``writes[]`` are runtime capabilities): a ``jarvis__create_doc`` /
``jarvis__update_doc`` call arriving over a DELEGATE run session is bounded by the
agent's declared ``writes[]`` contract BEFORE any Frappe permission check —

  * an AUDITOR (empty ``writes``) is refused every write outright;
  * an OPERATOR may create/update ONLY a doctype in ``writes[]``, ONLY a draft
    (``docstatus == 0``), and updates ONLY a row it owns (its run-as identity);
  * a NON-delegate caller (standard chat / macro / test — no bound Run) is left
    completely untouched.

JF-017: the contract the gate reads is the run's LAUNCH SNAPSHOT, so the fixtures
here stamp one exactly as ``_launch_audit`` does. The snapshot-vs-live-listing
behaviour itself is covered in ``test_platform_capability_contract``.

R5-P1-02 (``_verify_reviewer_two_pack_capacity`` slug fallback): the reviewer
two-pack activation-ceiling gate now counts DISTINCT non-empty canonical
``rule_pack`` values and never infers a pack from the agent slug, so two agents in
one pack (or two agents with no declared pack) no longer masquerade as two packs.

Also covers ``agent_catalog.sync_agent_listings`` storing the registry ``writes[]``
and ``rule_pack`` (tolerant of a registry that predates the fields).

Run:
  bench --site patterntest.localhost run-tests --app jarvis \
    --module jarvis.tests.test_platform_write_caps
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_catalog, agents_api
from jarvis.exceptions import PermissionDeniedError
from jarvis.tools import _agent_run_ctx, _delegate_capability
from jarvis.tools.create_doc import create_doc
from jarvis.tools.update_doc import update_doc

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"

OP_SLUG = "wc-operator"
AUD_SLUG = "wc-auditor"
WRITES = [{"doctype": "ToDo", "mode": "draft"}]  # the operator's declared contract


def _mk_user(email: str, roles=("System Manager",)) -> str:
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
			}
		)
		u.flags.ignore_permissions = True
		u.insert(ignore_permissions=True)
	existing = {r.role for r in frappe.get_doc("User", email).roles}
	for role in roles:
		if role not in existing:
			frappe.get_doc("User", email).add_roles(role)
	return email


def _mk_listing(slug: str, nature: str, writes, rule_pack: str = "") -> None:
	if frappe.db.exists(LISTING, slug):
		frappe.delete_doc(LISTING, slug, force=True, ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": LISTING,
			"agent_slug": slug,
			"title": f"WriteCaps {slug}",
			"nature": nature,
			"delivery": "delegate",
			"status": "Published",
			"rule_tokens": json.dumps([]),
			"doctypes_required": json.dumps([w["doctype"] for w in (writes or [])]),
			"writes": json.dumps(writes or []),
			"rule_pack": rule_pack,
		}
	).insert(ignore_permissions=True)


def _mk_install_and_run(slug: str, run_as: str, session_key: str, legacy: bool = False) -> str:
	"""A running Jarvis Agent Run bound to ``session_key`` (the delegate identity
	the write tools resolve), carrying the JF-017 capability snapshot ``_launch_audit``
	stamps. ``legacy=True`` instead marks it a pre-JF-017 run (no snapshot), whose
	write caps resolve against the LIVE listing. Returns the run name."""
	inst = frappe.get_doc(
		{
			"doctype": INSTALLATION,
			"agent": slug,
			"run_as_user": run_as,
			"activation_state": "shadow",
		}
	)
	inst.owner = run_as
	inst.flags.ignore_permissions = True
	inst.insert(ignore_permissions=True)
	if legacy:
		contract = {"capability_contract": "legacy"}
	else:
		contract = _delegate_capability.contract_for_launch(frappe.get_doc(LISTING, slug))
	run = frappe.get_doc(
		{
			"doctype": RUN,
			"agent": slug,
			"installation": inst.name,
			"trigger": "manual",
			"status": "running",
			"started_at": frappe.utils.now(),
			"session_key": session_key,
			**contract,
		}
	)
	run.owner = run_as
	run.flags.ignore_permissions = True
	run.insert(ignore_permissions=True)
	return run.name


def _mk_todo(owner: str, description: str = "wc todo") -> str:
	doc = frappe.get_doc({"doctype": "ToDo", "description": description})
	doc.owner = owner
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	frappe.db.set_value("ToDo", doc.name, "owner", owner, update_modified=False)
	return doc.name


def _wipe():
	for slug in (OP_SLUG, AUD_SLUG):
		for dt in (RUN, INSTALLATION):
			for n in frappe.get_all(dt, filters={"agent": slug}, pluck="name", ignore_permissions=True):
				frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
		if frappe.db.exists(LISTING, slug):
			frappe.delete_doc(LISTING, slug, force=True, ignore_permissions=True)


# --------------------------------------------------------------------------- #
# R5-J9 — delegate write-capability enforcement
# --------------------------------------------------------------------------- #
class TestDelegateWriteCaps(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.run_as = _mk_user("wc-runas@example.com")
		cls.other = _mk_user("wc-other@example.com")
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe()
		_mk_listing(OP_SLUG, "Operator", WRITES, rule_pack="pack-wc-a")
		_mk_listing(AUD_SLUG, "Auditor", [], rule_pack="")
		self.op_key = f"agent:agent-{OP_SLUG}:wc-op-run"
		self.aud_key = f"agent:agent-{AUD_SLUG}:wc-aud-run"
		_mk_install_and_run(OP_SLUG, self.run_as, self.op_key)
		_mk_install_and_run(AUD_SLUG, self.run_as, self.aud_key)

	def tearDown(self):
		_agent_run_ctx.clear_session_key()
		frappe.set_user("Administrator")
		_wipe()

	def _as_delegate(self, session_key: str):
		"""Enter the delegate dispatch context: the run-as identity + its session_key
		stashed exactly as the plugin dispatcher (api.py) would."""
		frappe.set_user(self.run_as)
		_agent_run_ctx.set_session_key(session_key)

	# --- the four adversarial cases ---------------------------------------- #
	def test_operator_out_of_contract_doctype_refused(self):
		"""An operator whose contract is ToDo-only cannot create/update a doctype it
		never declared (the R5-P0-04 cross-doctype mutation)."""
		self._as_delegate(self.op_key)
		with self.assertRaises(PermissionDeniedError) as ctx:
			create_doc("Note", {"title": "x"})
		self.assertIn("declared write contract", str(ctx.exception))
		# update of an out-of-contract doctype is refused before it even loads the doc
		with self.assertRaises(PermissionDeniedError):
			update_doc("Note", "whatever", {"title": "y"})

	def test_operator_submitted_doc_refused(self):
		"""An operator may only touch DRAFTS — a submitted (docstatus != 0) row of a
		DECLARED doctype is still refused."""
		frappe.set_user("Administrator")
		name = _mk_todo(self.run_as)
		frappe.db.set_value("ToDo", name, "docstatus", 1)  # simulate a submitted row
		self._as_delegate(self.op_key)
		with self.assertRaises(PermissionDeniedError) as ctx:
			update_doc("ToDo", name, {"description": "edit"})
		self.assertIn("DRAFT", str(ctx.exception).upper())

	def test_operator_foreign_owned_doc_refused(self):
		"""An operator may update only rows OWNED by its run-as identity — a draft of a
		declared doctype owned by another identity is refused."""
		frappe.set_user("Administrator")
		name = _mk_todo(self.other)  # owned by someone else
		self._as_delegate(self.op_key)
		with self.assertRaises(PermissionDeniedError) as ctx:
			update_doc("ToDo", name, {"description": "edit"})
		self.assertIn("owned by another identity", str(ctx.exception))

	def test_auditor_create_refused_outright(self):
		"""An auditor (null/empty writes) is refused every write — it holds no write
		capability regardless of the target doctype."""
		self._as_delegate(self.aud_key)
		with self.assertRaises(PermissionDeniedError) as ctx:
			create_doc("ToDo", {"description": "x"})
		self.assertIn("no declared write capability", str(ctx.exception))

	# --- the positive delegate paths (the gate must not over-block) -------- #
	def test_operator_creates_declared_doctype(self):
		self._as_delegate(self.op_key)
		res = create_doc("ToDo", {"description": "operator draft"})
		self.assertEqual(res["doctype"], "ToDo")
		self.assertTrue(res["name"])

	def test_operator_updates_own_declared_draft(self):
		frappe.set_user("Administrator")
		name = _mk_todo(self.run_as)
		self._as_delegate(self.op_key)
		res = update_doc("ToDo", name, {"description": "edited by operator"})
		self.assertEqual(res["name"], name)
		self.assertEqual(frappe.db.get_value("ToDo", name, "description"), "edited by operator")

	# --- non-delegate regression ------------------------------------------- #
	def test_non_delegate_session_untouched(self):
		"""No session_key in context -> not a delegate -> the write proceeds under the
		ordinary Frappe permission engine only (no capability gate)."""
		frappe.set_user(self.run_as)
		_agent_run_ctx.clear_session_key()
		res = create_doc("Note", {"title": "ordinary note"})  # not a delegate: any perm-ok doctype
		self.assertEqual(res["doctype"], "Note")

	def test_unbound_session_key_is_not_a_delegate(self):
		"""A session_key with NO bound Run is not a delegate — the write tools leave it
		untouched (a normal chat session_key never matches Run.session_key)."""
		frappe.set_user(self.run_as)
		_agent_run_ctx.set_session_key("chat-session-with-no-agent-run")
		res = create_doc("Note", {"title": "chat note"})
		self.assertEqual(res["doctype"], "Note")


# --------------------------------------------------------------------------- #
# R5-J11(c) — pack-derived ceiling counting (no slug fallback)
# --------------------------------------------------------------------------- #
class TestPackDerivedCeiling(FrappeTestCase):
	CUSTOMER = "wc-pack-owner@example.com"
	REVIEWER = "wc-pack-reviewer@example.com"
	A = "wc-pack-a"
	B = "wc-pack-b"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_mk_user(cls.CUSTOMER)
		_mk_user(cls.REVIEWER)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		for slug in (self.A, self.B):
			for n in frappe.get_all(
				INSTALLATION, filters={"agent": slug}, pluck="name", ignore_permissions=True
			):
				frappe.delete_doc(INSTALLATION, n, force=True, ignore_permissions=True)
			if frappe.db.exists(LISTING, slug):
				frappe.delete_doc(LISTING, slug, force=True, ignore_permissions=True)

	def _install(self, slug: str, rule_pack: str) -> None:
		_mk_listing(slug, "Auditor", [], rule_pack=rule_pack)
		doc = frappe.get_doc(
			{
				"doctype": INSTALLATION,
				"agent": slug,
				"run_as_user": self.CUSTOMER,
				"reviewer": self.REVIEWER,
				"activation_state": "shadow",
			}
		)
		doc.owner = self.CUSTOMER
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		frappe.db.set_value(INSTALLATION, doc.name, "owner", self.CUSTOMER, update_modified=False)

	def test_two_distinct_packs_qualify(self):
		self._install(self.A, "pack-one")
		self._install(self.B, "pack-two")
		self.assertEqual(agents_api._verify_reviewer_two_pack_capacity(self.CUSTOMER), self.REVIEWER)

	def test_two_agents_same_pack_do_not_qualify(self):
		self._install(self.A, "pack-one")
		self._install(self.B, "pack-one")  # same canonical pack
		with self.assertRaises(frappe.ValidationError):
			agents_api._verify_reviewer_two_pack_capacity(self.CUSTOMER)

	def test_empty_pack_ids_never_inferred_from_slug(self):
		"""The R5-P1-02 regression: two DISTINCT agent slugs with NO canonical pack must
		NOT count as two packs (the slug fallback is gone)."""
		self._install(self.A, "")
		self._install(self.B, "")
		with self.assertRaises(frappe.ValidationError):
			agents_api._verify_reviewer_two_pack_capacity(self.CUSTOMER)


# --------------------------------------------------------------------------- #
# sync stores writes[] + rule_pack (tolerant of a registry that predates them)
# --------------------------------------------------------------------------- #
class TestSyncStoresWritesAndPack(FrappeTestCase):
	SYNC_OP = "wc-sync-operator"
	SYNC_PLAIN = "wc-sync-plain"

	def tearDown(self):
		frappe.set_user("Administrator")
		for slug in (self.SYNC_OP, self.SYNC_PLAIN):
			if frappe.db.exists(LISTING, slug):
				frappe.delete_doc(LISTING, slug, force=True, ignore_permissions=True)
		# Restore the real listings the synthetic-registry sync round-tripped.
		agent_catalog.sync_agent_listings()
		frappe.db.commit()

	def test_sync_stores_writes_and_pack_and_tolerates_absence(self):
		"""A synthetic registry (superset of the real one, so no real listing is
		spuriously deprecated) proves the sync STORES writes[]/rule_pack when present
		and stores empty when absent (an older registry lacking the fields)."""
		real = agent_catalog._load_registry()
		synth = {
			"publisher": real.get("publisher"),
			"agents": list(real.get("agents") or [])
			+ [
				{
					"agent_slug": self.SYNC_OP,
					"title": "Sync Operator",
					"nature": "operator",
					"writes": [{"doctype": "Material Request", "mode": "draft"}],
					"rule_pack": "pack-sync-x",
					"doctypes_required": ["Material Request"],
				},
				{
					# predates writes/rule_pack entirely -> must be tolerated (empty)
					"agent_slug": self.SYNC_PLAIN,
					"title": "Sync Plain",
					"nature": "auditor",
				},
			],
		}
		with patch.object(agent_catalog, "_load_registry", return_value=synth):
			agent_catalog.sync_agent_listings()

		op = frappe.db.get_value(LISTING, self.SYNC_OP, ["writes", "rule_pack"], as_dict=True)
		self.assertEqual(json.loads(op.writes), [{"doctype": "Material Request", "mode": "draft"}])
		self.assertEqual(op.rule_pack, "pack-sync-x")

		plain = frappe.db.get_value(LISTING, self.SYNC_PLAIN, ["writes", "rule_pack"], as_dict=True)
		self.assertEqual(json.loads(plain.writes), [])  # tolerant: absent -> empty list
		self.assertIn(plain.rule_pack, (None, ""))  # tolerant: absent -> empty
