"""Interim A access governance for the Agents Marketplace (jarvis#1062).

The inversion these cover: an agent listing used to be reachable by everyone
until an admin restricted it, and is now reachable by nobody until an admin
allows it - by ROLE, by NAMED USER, or both. That is a security default, so the
tests here are mostly about what does NOT happen: a plain user with no grant
cannot see, install or run an agent, and an admin still can.

Grouped by the thing that would break if the change were wrong:

  * TestDenyByDefault      - the inversion itself, at all four gates.
  * TestAllowedUsersPath   - a grant by NAME, with no role behind it.
  * TestSetAgentAccess     - the admin endpoint's validation and atomicity.
  * TestGrandfatherPatch   - nobody who worked before the upgrade stops working.
  * TestPushRoster         - the container roster follows the grants.
  * TestActivationAuthority- a named reviewer can no longer self sign-off.
  * TestAdminInstallControl- a tenant admin can operate another owner's install.
"""

import json
import unittest

import frappe
import frappe.permissions

from jarvis.chat import agent_catalog, agents_api
from jarvis.patches import v2_18_agent_access_grandfather as grandfather
from jarvis.tests._agent_access import allow_listing_for, clear_listing_access

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
ALLOWED_USER = "Jarvis Agent Allowed User"

SLUG = "access-gov-auditor"
ROLE_GRANTED = "Jarvis Access Gov Role"
REVIEWER_ROLE = "Jarvis Skill Reviewer"


def _ensure_role(name: str) -> str:
	if not frappe.db.exists("Role", name):
		doc = frappe.get_doc({"doctype": "Role", "role_name": name, "desk_access": 1})
		doc.flags.ignore_permissions = True
		doc.insert()
	return name


def _ensure_user(email: str, roles=("Jarvis User",)) -> str:
	from jarvis.permissions import ensure_jarvis_admin_role, ensure_jarvis_user_role

	ensure_jarvis_user_role()
	ensure_jarvis_admin_role()
	if not frappe.db.exists("User", email):
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
	# Unconditional: on a shared bench the user may survive from an earlier run
	# WITHOUT the roles this module needs, and an exists-guard would skip them.
	held = set(frappe.get_roles(email))
	missing = [r for r in roles if r not in held]
	if missing:
		frappe.get_doc("User", email).add_roles(*missing)
	frappe.clear_cache(user=email)
	return email


def _mk_listing() -> str:
	if not frappe.db.exists(LISTING, SLUG):
		frappe.get_doc(
			{
				"doctype": LISTING,
				"agent_slug": SLUG,
				"title": "Access Governance Auditor",
				"description": "fixture",
				"nature": "Auditor",
				"status": "Published",
				"delivery": "delegate",
				"version": "1.0.0",
				"rule_pack": f"pack-{SLUG}",
				# Explicitly dependency-free: this module is about ACCESS, and an
				# installability refusal arriving first would mask every assertion.
				"min_apps": json.dumps([]),
				"doctypes_required": json.dumps([]),
				"rule_tokens": json.dumps([]),
			}
		).insert(ignore_permissions=True)
	frappe.db.set_value(LISTING, SLUG, {"status": "Published", "nature": "Auditor"}, update_modified=False)
	return SLUG


class AccessGovernanceCase(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_ensure_role(ROLE_GRANTED)
		cls.plain = _ensure_user("agov-plain@example.com")
		cls.named = _ensure_user("agov-named@example.com")
		cls.roled = _ensure_user("agov-roled@example.com", ("Jarvis User", ROLE_GRANTED))
		cls.admin = _ensure_user("agov-admin@example.com", ("Jarvis User", "Jarvis Admin"))
		cls.reviewer = _ensure_user("agov-reviewer@example.com", ("Jarvis User", REVIEWER_ROLE))
		# Reviewing is a job, not a seat on the chat surface: this fixture holds the
		# reviewing role and NOTHING else, which is the shape promote/demote must
		# admit (see _require_activation_authority).
		cls.reviewer_only = _ensure_user("agov-reviewer-only@example.com", (REVIEWER_ROLE,))
		_mk_listing()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		# The listing is bench-global state: leg 1 of build_agent_push_payload ships
		# every GRANTED Published listing, so leaving this one behind (granted by a
		# test that did not clean up) puts an unexpected slug in other modules'
		# roster assertions. Shared-site hygiene, not politeness.
		frappe.set_user("Administrator")
		for name in frappe.get_all(INSTALLATION, filters={"agent": SLUG}, pluck="name"):
			frappe.delete_doc(INSTALLATION, name, force=True, ignore_permissions=True)
		clear_listing_access(SLUG)
		if frappe.db.exists(LISTING, SLUG):
			frappe.delete_doc(LISTING, SLUG, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		self._wipe()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._wipe()

	def _wipe(self):
		for name in frappe.get_all(INSTALLATION, filters={"agent": SLUG}, pluck="name"):
			frappe.delete_doc(INSTALLATION, name, force=True, ignore_permissions=True)
		clear_listing_access(SLUG)
		frappe.db.commit()

	def _as(self, user):
		frappe.set_user(user)
		self.addCleanup(frappe.set_user, "Administrator")

	def _row(self, user):
		"""This agent's row in ``user``'s catalog, or None if hidden from them."""
		frappe.set_user(user)
		try:
			return next((r for r in agents_api.list_agents() if r["name"] == SLUG), None)
		finally:
			frappe.set_user("Administrator")


# --------------------------------------------------------------------------- #
# G1 - the inversion
# --------------------------------------------------------------------------- #
class TestDenyByDefault(AccessGovernanceCase):
	def test_closed_listing_is_hidden_from_a_plain_user(self):
		self.assertIsNone(
			self._row(self.plain),
			"a listing with no allowed roles and no allowed users must not appear in a plain user's catalog",
		)

	def test_closed_listing_cannot_be_read_by_slug(self):
		# Hiding the row is not enough: guessing the slug must not be a way round it.
		self._as(self.plain)
		with self.assertRaises(frappe.PermissionError):
			agents_api.get_agent(SLUG)

	def test_closed_listing_cannot_be_installed(self):
		self._as(self.plain)
		with self.assertRaises(frappe.PermissionError):
			agents_api.install_agent(SLUG)

	def test_closed_listing_cannot_be_run(self):
		# The run gate reads the INSTALLATION's run-as user, so this needs a row that
		# already exists - the state a tenant lands in if access is revoked after
		# install, and the state the grandfather patch exists to prevent on upgrade.
		inst = _mk_install(self.plain)
		frappe.db.set_value(INSTALLATION, inst, "enabled", 1, update_modified=False)
		frappe.db.commit()
		self._as(self.plain)
		with self.assertRaises(frappe.PermissionError):
			agents_api.run_agent_now(inst)

	def test_admin_is_unaffected_by_the_closed_default(self):
		row = self._row(self.admin)
		self.assertIsNotNone(row, "a Jarvis Admin must still see a closed listing")
		self.assertEqual(row["allowed"], 1)
		self.assertTrue(agents_api._user_allowed_for_agent(SLUG, self.admin))

	def test_a_role_grant_opens_it_for_holders_only(self):
		allow_listing_for(SLUG, roles=[ROLE_GRANTED])
		self.assertTrue(agents_api._user_allowed_for_agent(SLUG, self.roled))
		self.assertFalse(agents_api._user_allowed_for_agent(SLUG, self.plain))
		self.assertIsNotNone(self._row(self.roled))
		self.assertIsNone(self._row(self.plain))

	def test_display_flag_and_server_gate_agree(self):
		"""The catalog's ``allowed`` flag and the gate come from one predicate.

		They used to be two hand-copied boolean expressions, which is how a user
		gets an enabled Install button that 403s."""
		allow_listing_for(SLUG, user=self.named)
		for user in (self.plain, self.named, self.roled, self.admin):
			row = self._row(user)
			flag = bool(row and row["allowed"])
			self.assertEqual(
				flag,
				agents_api._user_allowed_for_agent(SLUG, user),
				f"catalog flag and server gate disagree for {user}",
			)


# --------------------------------------------------------------------------- #
# G1 - the named-user half
# --------------------------------------------------------------------------- #
class TestAllowedUsersPath(AccessGovernanceCase):
	def test_named_user_is_allowed_with_no_role_grant_at_all(self):
		allow_listing_for(SLUG, user=self.named)
		self.assertTrue(agents_api._user_allowed_for_agent(SLUG, self.named))
		# and nobody else rides in on it
		self.assertFalse(agents_api._user_allowed_for_agent(SLUG, self.plain))
		self.assertFalse(agents_api._user_allowed_for_agent(SLUG, self.roled))

	def test_named_user_can_install_and_the_row_carries_no_roster(self):
		allow_listing_for(SLUG, user=self.named)
		self._as(self.named)
		res = agents_api.install_agent(SLUG)
		self.assertTrue(res["ok"])
		detail = agents_api.get_agent(SLUG)
		self.assertEqual(detail["allowed"], 1)
		# WHO ELSE has access is admin information.
		self.assertNotIn("allowed_users", detail)
		self.assertNotIn("allowed_roles", detail)

	def test_admin_detail_carries_both_lists(self):
		allow_listing_for(SLUG, user=self.named, roles=[ROLE_GRANTED])
		self._as(self.admin)
		detail = agents_api.get_agent(SLUG)
		self.assertEqual(detail["allowed_roles"], [ROLE_GRANTED])
		self.assertEqual(detail["allowed_users"], [self.named])


# --------------------------------------------------------------------------- #
# G3 - the admin endpoint
# --------------------------------------------------------------------------- #
class TestSetAgentAccess(AccessGovernanceCase):
	def test_replaces_both_tables_in_one_call(self):
		allow_listing_for(SLUG, user=self.named, roles=[ROLE_GRANTED])
		self._as(self.admin)
		res = agents_api.set_agent_access(SLUG, roles=["Jarvis User"], users=[self.plain])
		self.assertEqual(res["allowed_roles"], ["Jarvis User"])
		self.assertEqual(res["allowed_users"], [self.plain])
		self.assertFalse(res["applied"])
		# The previous grants are GONE, not merged - moving somebody from a role to a
		# named grant must not leave them holding both.
		frappe.set_user("Administrator")
		detail = agents_api.get_agent(SLUG)
		self.assertEqual(detail["allowed_roles"], ["Jarvis User"])
		self.assertEqual(detail["allowed_users"], [self.plain])

	def test_empty_pair_closes_rather_than_opens(self):
		allow_listing_for(SLUG, roles=[ROLE_GRANTED])
		self._as(self.admin)
		agents_api.set_agent_access(SLUG, roles=[], users=[])
		frappe.set_user("Administrator")
		self.assertFalse(agents_api._user_allowed_for_agent(SLUG, self.roled))
		self.assertFalse(agents_api._user_allowed_for_agent(SLUG, self.plain))

	def test_requires_an_admin(self):
		self._as(self.plain)
		with self.assertRaises(frappe.PermissionError):
			agents_api.set_agent_access(SLUG, roles=[ROLE_GRANTED])

	def test_rejects_non_grantable_roles(self):
		self._as(self.admin)
		for role in ("Administrator", "Guest", "All"):
			with self.assertRaises(frappe.ValidationError):
				agents_api.set_agent_access(SLUG, roles=[role])

	def test_rejects_unknown_and_system_users(self):
		self._as(self.admin)
		with self.assertRaises(frappe.ValidationError):
			agents_api.set_agent_access(SLUG, users=["nobody-at-all@example.com"])
		for user in ("Administrator", "Guest"):
			with self.assertRaises(frappe.ValidationError):
				agents_api.set_agent_access(SLUG, users=[user])

	def test_rejects_a_disabled_user(self):
		"""Disabling is how an offboarded person is revoked.

		Recording one as allowed would write a grant that outlives the offboarding,
		so the endpoint refuses it at the point of entry."""
		frappe.db.set_value("User", self.named, "enabled", 0, update_modified=False)
		frappe.db.commit()
		try:
			self._as(self.admin)
			with self.assertRaises(frappe.ValidationError):
				agents_api.set_agent_access(SLUG, users=[self.named])
		finally:
			frappe.set_user("Administrator")
			frappe.db.set_value("User", self.named, "enabled", 1, update_modified=False)
			frappe.db.commit()

	def test_accepts_json_strings_from_the_http_layer(self):
		# Over HTTP both lists arrive as JSON strings, not lists.
		self._as(self.admin)
		res = agents_api.set_agent_access(SLUG, roles=f'["{ROLE_GRANTED}"]', users=f'["{self.named}"]')
		self.assertEqual(res["allowed_roles"], [ROLE_GRANTED])
		self.assertEqual(res["allowed_users"], [self.named])

	def test_set_agent_roles_shim_preserves_named_grants(self):
		"""The old endpoint sets the ROLE half only.

		A cached older SPA build calling it must not silently revoke a named grant
		made through the new Access editor."""
		allow_listing_for(SLUG, user=self.named)
		self._as(self.admin)
		res = agents_api.set_agent_roles(SLUG, [ROLE_GRANTED])
		self.assertEqual(res["allowed_roles"], [ROLE_GRANTED])
		frappe.set_user("Administrator")
		self.assertTrue(agents_api._user_allowed_for_agent(SLUG, self.named))

	def test_search_users_is_admin_only_and_excludes_system_identities(self):
		self._as(self.plain)
		with self.assertRaises(frappe.PermissionError):
			agents_api.search_users("a")
		frappe.set_user(self.admin)
		rows = agents_api.search_users("agov")
		names = {r["name"] for r in rows}
		self.assertIn(self.plain, names)
		self.assertNotIn("Administrator", names)
		self.assertNotIn("Guest", names)
		self.assertLessEqual(len(rows), 20)


# --------------------------------------------------------------------------- #
# G2 - the upgrade path
# --------------------------------------------------------------------------- #
class TestGrandfatherPatch(AccessGovernanceCase):
	def test_existing_install_keeps_working_after_the_inversion(self):
		# Install while allowed, then simulate the upgrade: the listing's grants are
		# gone (every shipped listing declares default_allowed_roles: []).
		allow_listing_for(SLUG, user=self.named)
		inst = _mk_install(self.named)
		clear_listing_access(SLUG)
		self.assertFalse(
			agents_api._user_allowed_for_agent(SLUG, self.named),
			"precondition: the upgrade closes the listing",
		)

		grandfather.grandfather_existing_installs()

		allowed = frappe.get_all(
			ALLOWED_USER,
			filters={"parenttype": LISTING, "parentfield": "allowed_users", "parent": SLUG},
			pluck="user",
		)
		self.assertIn(self.named, allowed)
		self.assertTrue(agents_api._user_allowed_for_agent(SLUG, self.named))
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "agent"), SLUG)

	def test_grandfathers_the_run_as_user_too_not_only_the_owner(self):
		"""The run-as user is the identity every dispatch gate litigates.

		After an admin re-targets an install it is frequently NOT the owner, and
		grandfathering the owner alone would leave the scheduled run refused."""
		inst = _mk_install(self.named)
		frappe.db.set_value(INSTALLATION, inst, "run_as_user", self.plain, update_modified=False)
		frappe.db.commit()

		grandfather.grandfather_existing_installs()

		self.assertTrue(agents_api._user_allowed_for_agent(SLUG, self.named))
		self.assertTrue(agents_api._user_allowed_for_agent(SLUG, self.plain))

	def test_is_idempotent(self):
		_mk_install(self.named)
		first = grandfather.grandfather_existing_installs()["added"]
		second = grandfather.grandfather_existing_installs()["added"]
		self.assertGreater(first, 0)
		self.assertEqual(second, 0, "re-running the patch must add nothing")

	def test_skips_an_identity_already_allowed_by_role(self):
		"""A role grant is the stronger, more durable statement.

		Pinning the person by name as well would silently outlive the role being
		revoked - the migration would have widened access, not preserved it."""
		_mk_install(self.roled)
		allow_listing_for(SLUG, roles=[ROLE_GRANTED])

		grandfather.grandfather_existing_installs()

		allowed = frappe.get_all(
			ALLOWED_USER,
			filters={"parenttype": LISTING, "parentfield": "allowed_users", "parent": SLUG},
			pluck="user",
		)
		self.assertNotIn(self.roled, allowed)

	def test_never_grants_administrator(self):
		inst = _mk_install(self.named)
		frappe.db.set_value(INSTALLATION, inst, "owner", "Administrator", update_modified=False)
		frappe.db.commit()

		grandfather.grandfather_existing_installs()

		allowed = frappe.get_all(
			ALLOWED_USER,
			filters={"parenttype": LISTING, "parentfield": "allowed_users", "parent": SLUG},
			pluck="user",
		)
		self.assertNotIn("Administrator", allowed)


# --------------------------------------------------------------------------- #
# G4 - the container roster
# --------------------------------------------------------------------------- #
class TestPushRoster(AccessGovernanceCase):
	def _slugs(self):
		return [e["slug"] for e in agent_catalog.build_agent_push_payload()]

	def test_an_allowed_listing_ships_with_zero_installations(self):
		"""The point of the whole design: an admin allows and applies ONCE, then
		allowed users self-install without each install restarting the workspace."""
		self.assertNotIn(f"agent-{SLUG}", self._slugs())
		allow_listing_for(SLUG, roles=[ROLE_GRANTED])
		self.assertIn(f"agent-{SLUG}", self._slugs())

	def test_a_named_user_grant_alone_also_ships_it(self):
		allow_listing_for(SLUG, user=self.named)
		self.assertIn(f"agent-{SLUG}", self._slugs())

	def test_an_unallowed_uninstalled_listing_does_not_ship(self):
		self.assertNotIn(f"agent-{SLUG}", self._slugs())

	def test_an_enabled_install_of_a_closed_listing_does_not_ship(self):
		"""The roster admits exactly what dispatch admits.

		An enabled install on a CLOSED listing is refused by run_agent_now and by
		_sweep_one, so advertising its delegate would seat a roster entry the bench
		will never honour - the #457 mismatch. The upgrade is protected by the
		grandfather PATCH (which runs during migrate, before anything can apply),
		not by a looser gate here - see the test below."""
		inst = _mk_install(self.named)
		frappe.db.set_value(INSTALLATION, inst, "enabled", 1, update_modified=False)
		frappe.db.commit()
		self.assertFalse(agents_api._user_allowed_for_agent(SLUG, self.named))
		self.assertNotIn(f"agent-{SLUG}", self._slugs())

	def test_a_grandfathered_install_ships(self):
		"""The upgrade path, end to end: install, close the listing (what the
		inversion does to every shipped agent), run the patch, and the customer's
		agent is back in the roster."""
		allow_listing_for(SLUG, user=self.named)
		inst = _mk_install(self.named)
		frappe.db.set_value(INSTALLATION, inst, "enabled", 1, update_modified=False)
		clear_listing_access(SLUG)
		self.assertNotIn(f"agent-{SLUG}", self._slugs())

		grandfather.grandfather_existing_installs()

		self.assertTrue(agents_api._user_allowed_for_agent(SLUG, self.named))
		self.assertIn(f"agent-{SLUG}", self._slugs())

	def test_a_restricted_listing_still_excludes_a_blocked_run_as_user(self):
		"""#457 must not regress: the legacy carve-out is for the EMPTY pair only.

		Once an admin has said who may use an agent, an enabled install whose
		run-as user is not among them stays out of the roster."""
		inst = _mk_install(self.named)
		frappe.db.set_value(INSTALLATION, inst, "enabled", 1, update_modified=False)
		allow_listing_for(SLUG, roles=[ROLE_GRANTED])  # self.named does not hold it
		frappe.db.commit()
		# The blocked install contributes nothing: asserted on the per-OWNER build,
		# which skips the allowed-listing leg.
		self.assertEqual([e["slug"] for e in agent_catalog.build_agent_push_payload(owner=self.named)], [])
		# The grant itself is real, though - the listing ships on its own account,
		# so holders of ROLE_GRANTED can install and run it. Leg 1 and leg 2 answer
		# different questions and both matter.
		self.assertIn(f"agent-{SLUG}", self._slugs())

	def test_only_published_listings_ship(self):
		allow_listing_for(SLUG, roles=[ROLE_GRANTED])
		frappe.db.set_value(LISTING, SLUG, "status", "Coming Soon", update_modified=False)
		frappe.db.commit()
		try:
			self.assertNotIn(f"agent-{SLUG}", self._slugs())
		finally:
			frappe.db.set_value(LISTING, SLUG, "status", "Published", update_modified=False)
			frappe.db.commit()

	def test_the_two_legs_dedupe_to_one_entry(self):
		inst = _mk_install(self.named)
		frappe.db.set_value(INSTALLATION, inst, "enabled", 1, update_modified=False)
		allow_listing_for(SLUG, user=self.named)
		frappe.db.commit()
		self.assertEqual(self._slugs().count(f"agent-{SLUG}"), 1)


# --------------------------------------------------------------------------- #
# G6 - who may sign off
# --------------------------------------------------------------------------- #
class TestActivationAuthority(AccessGovernanceCase):
	def test_the_named_reviewer_alone_cannot_promote(self):
		"""install_agent stamps reviewer = the installer.

		So "the named reviewer may promote" made every installer their own
		approver, which is exactly the rubber stamp the shadow period exists to
		prevent."""
		inst = _mk_install(self.named, reviewer=self.named)
		self._as(self.named)
		with self.assertRaises(frappe.PermissionError):
			agents_api.promote_installation(inst, justification="looks fine to me")

	def test_a_skill_reviewer_can_promote(self):
		inst = _mk_install(self.named, reviewer=self.named)
		self._as(self.reviewer)
		res = agents_api.promote_installation(inst, justification="reviewed 10 samples")
		self.assertTrue(res["ok"])
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "activation_state"), "live")
		# WHO signed off is still recorded - only the authority to do it changed.
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "promoted_by"), self.reviewer)

	def test_a_reviewer_without_jarvis_user_can_still_promote(self):
		"""Promote is gated on the reviewer set ALONE, like apply_agents.

		Stacking a Jarvis User check on top would lock out exactly the person the
		Jarvis Skill Reviewer role exists for. Asserted with a user who deliberately
		holds no other Jarvis role."""
		self.assertNotIn("Jarvis User", frappe.get_roles(self.reviewer_only))
		inst = _mk_install(self.named, reviewer=self.named)
		self._as(self.reviewer_only)
		res = agents_api.promote_installation(inst, justification="reviewed, clean")
		self.assertTrue(res["ok"])
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "activation_state"), "live")
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "promoted_by"), self.reviewer_only)

	def test_a_reviewer_without_jarvis_user_can_still_demote(self):
		inst = _mk_install(self.named, reviewer=self.named)
		frappe.set_user(self.reviewer_only)
		agents_api.promote_installation(inst)
		res = agents_api.demote_installation(inst, reason="false positives")
		frappe.set_user("Administrator")
		self.assertTrue(res["ok"])
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "activation_state"), "shadow")

	def test_the_named_reviewer_alone_cannot_demote(self):
		inst = _mk_install(self.named, reviewer=self.named)
		frappe.set_user(self.reviewer)
		agents_api.promote_installation(inst)
		self._as(self.named)
		with self.assertRaises(frappe.PermissionError):
			agents_api.demote_installation(inst, reason="changed my mind")


# --------------------------------------------------------------------------- #
# G7 - a tenant admin can operate other owners' installs
# --------------------------------------------------------------------------- #
class TestAdminInstallControl(AccessGovernanceCase):
	def test_jarvis_admin_can_disable_another_owners_install(self):
		"""Jarvis Admin held READ ONLY on Jarvis Agent Installation, so a tenant
		admin could see a runaway install and do nothing about it."""
		# Preconditions, asserted rather than assumed: this test failed once because
		# the DocPerm row had not reached the DB, and the bare PermissionError said
		# nothing about which of the two halves was missing.
		self.assertIn("Jarvis Admin", frappe.get_roles(self.admin))
		self.assertNotIn("System Manager", frappe.get_roles(self.admin))  # must not pass via SM
		self.assertEqual(
			frappe.permissions.get_role_permissions(INSTALLATION, user=self.admin).get("write"),
			1,
			"Jarvis Admin has no non-owner write on Jarvis Agent Installation - the "
			"DocPerm row in the DocType JSON has not reached this database",
		)
		inst = _mk_install(self.named)
		frappe.db.set_value(INSTALLATION, inst, "enabled", 1, update_modified=False)
		frappe.db.commit()

		self._as(self.admin)
		res = agents_api.set_enabled(inst, 0)
		self.assertTrue(res["ok"])
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "enabled"), 0)

	def test_jarvis_admin_can_stop_another_owners_run(self):
		inst = _mk_install(self.named)
		run = frappe.get_doc(
			{
				"doctype": "Jarvis Agent Run",
				"agent": SLUG,
				"installation": inst,
				"status": "running",
				"started_at": frappe.utils.now(),
			}
		)
		run.flags.ignore_permissions = True
		run.insert(ignore_permissions=True)
		frappe.db.set_value("Jarvis Agent Run", run.name, "owner", self.named, update_modified=False)
		frappe.db.commit()
		self.addCleanup(_delete_run, run.name)

		self._as(self.admin)
		res = agents_api.stop_agent_run(run.name)
		self.assertTrue(res["ok"])
		self.assertEqual(res["status"], "stopped")

	def test_a_plain_non_owner_still_cannot(self):
		"""The widened DocPerm is for ADMINS - it must not have opened the row to
		every Jarvis User, whose grant is still if_owner."""
		inst = _mk_install(self.named)
		self._as(self.plain)
		with self.assertRaises(frappe.PermissionError):
			agents_api.set_enabled(inst, 0)


def _delete_run(name: str) -> None:
	frappe.set_user("Administrator")
	if frappe.db.exists("Jarvis Agent Run", name):
		frappe.delete_doc("Jarvis Agent Run", name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _mk_install(owner: str, reviewer: str | None = None) -> str:
	"""An installation row for ``owner``, created directly.

	Not via ``install_agent``: these tests need a row to exist in access states
	that endpoint would refuse to create, which is the whole point of the
	grandfather and revoked-after-install cases."""
	doc = frappe.get_doc(
		{
			"doctype": INSTALLATION,
			"agent": SLUG,
			"enabled": 0,
			"run_as_user": owner,
			"activation_state": "shadow",
			"reviewer": reviewer or owner,
			"installed_version": frappe.db.get_value(LISTING, SLUG, "version"),
			"installed_at": frappe.utils.now(),
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	frappe.db.set_value(INSTALLATION, doc.name, "owner", owner, update_modified=False)
	frappe.db.commit()
	return doc.name
