"""PP-4 regression: a delegate-AUTHORED dashboard (``jarvis__save_agent_dashboard``)
must carry the SAME shadow enforcement as the server-generated fallback.

The panel finding: the authored dashboard WINS the precedence in
``record_delegate_run`` (richer artifact), but ``save_agent_dashboard`` created it
with NO ``owner_override`` and never routed the authored HTML through the shadow
gate. On a SHADOW installation that left the primary dashboard owner-owned (visible
on the general owner surface) and free to render an outward clean/compliant
attestation — exactly what PP-4 forbids.

These tests drive the tool exactly as the plugin path would (session_key resolved
from context to the run) and assert:

  * shadow → the authored dashboard is re-homed to the named reviewer (owner surface
    cannot read it; the reviewer can), the preview banner is stamped in-body, and a
    PP-1 strong verb the author emitted is neutralised;
  * live (control) → unchanged: the authored dashboard stays owner-owned with no
    preview banner.

Run:
  bench --site patterntest.localhost run-tests --app jarvis \
    --module jarvis.tests.test_platform_shadow_dashboard
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import dashboard_permissions
from jarvis.tools import _agent_run_ctx
from jarvis.tools.save_agent_dashboard import save_agent_dashboard

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
DASHBOARD = "Jarvis Dashboard"

SLUG = "platform-shadow-dash-agent"
TOKEN = "tok_sd_1"

# Authored HTML carrying BOTH an outward compliant claim and a PP-1 strong verb —
# the shadow gate must neutralise the verb and stamp the preview banner.
AUTHORED_HTML = (
	"<!doctype html><html><head><title>Close</title></head>"
	"<body><h1>Fully compliant close</h1>"
	"<p>This run recovered 5,000 and prevented a misstatement.</p></body></html>"
)


def _mk_user(email: str) -> str:
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		)
		u.insert(ignore_permissions=True)
		u.add_roles("Jarvis User")
	return email


def _mk_listing() -> str:
	if not frappe.db.exists(LISTING, SLUG):
		frappe.get_doc(
			{
				"doctype": LISTING,
				"agent_slug": SLUG,
				"title": "Platform Shadow Dashboard Agent",
				"rule_tokens": json.dumps([TOKEN]),
				"doctypes_required": json.dumps([]),
			}
		).insert(ignore_permissions=True)
	return SLUG


def _mk_installation(owner: str, reviewer: str, activation_state: str) -> object:
	# A new installation always activates in shadow (controller-enforced). For the
	# LIVE fixture, flip the flag directly on the row (bypassing the promotion path)
	# so this test exercises only the dashboard-ownership behaviour.
	doc = frappe.get_doc(
		{
			"doctype": INSTALLATION,
			"agent": SLUG,
			"run_as_user": owner,
			"reviewer": reviewer,
			"activation_state": "shadow",
		}
	)
	doc.owner = owner
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	frappe.db.set_value(INSTALLATION, doc.name, "owner", owner, update_modified=False)
	if activation_state == "live":
		frappe.db.set_value(INSTALLATION, doc.name, "activation_state", "live", update_modified=False)
	return frappe.get_doc(INSTALLATION, doc.name)


def _mk_run(owner: str, inst, session_key: str) -> str:
	doc = frappe.get_doc(
		{
			"doctype": RUN,
			"agent": SLUG,
			"installation": inst.name,
			"trigger": "manual",
			"status": "running",
			"started_at": frappe.utils.now(),
			"session_key": session_key,
		}
	)
	doc.owner = owner
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	frappe.db.set_value(RUN, doc.name, "owner", owner, update_modified=False)
	return doc.name


def _wipe():
	for dt in (RUN, DASHBOARD, INSTALLATION):
		filt = {"agent": SLUG} if dt in (RUN, INSTALLATION) else {"dashboard_title": ["like", "%"]}
		if dt == DASHBOARD:
			# Only the dashboards this suite's runs created (owned by our two users).
			names = frappe.get_all(
				DASHBOARD,
				filters={"owner": ["in", ["sd-owner@example.com", "sd-reviewer@example.com"]]},
				pluck="name",
				ignore_permissions=True,
			)
		else:
			names = frappe.get_all(dt, filters=filt, pluck="name", ignore_permissions=True)
		for n in names:
			frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
	frappe.db.commit()


class TestPP4ShadowAuthoredDashboard(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("sd-owner@example.com")
		cls.reviewer = _mk_user("sd-reviewer@example.com")
		_mk_listing()
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe()

	def tearDown(self):
		frappe.set_user("Administrator")
		_agent_run_ctx.clear_session_key()
		_wipe()

	def _call_save(self, owner, session_key, **kwargs):
		frappe.set_user(owner)
		_agent_run_ctx.set_session_key(session_key)
		try:
			return save_agent_dashboard(**kwargs)
		finally:
			_agent_run_ctx.clear_session_key()
			frappe.set_user("Administrator")

	# ------------------------------------------------------------------ #
	# shadow: re-home to reviewer + preview banner + strong-verb neutralise
	# ------------------------------------------------------------------ #
	def test_shadow_authored_dashboard_rehomed_and_gated(self):
		inst = _mk_installation(self.owner, self.reviewer, "shadow")
		sk = "agent:shadow-dash:" + frappe.generate_hash(length=8)
		run = _mk_run(self.owner, inst, sk)

		out = self._call_save(self.owner, sk, html=AUTHORED_HTML, title="Close Summary")
		dash = out["dashboard"]
		self.assertTrue(dash)
		self.assertEqual(frappe.db.get_value(RUN, run, "dashboard"), dash)

		d = frappe.get_doc(DASHBOARD, dash)
		# (1) re-homed to the named reviewer — NOT the general owner surface.
		self.assertEqual(d.owner, self.reviewer)
		self.assertEqual(d.target_user, self.reviewer)

		# read path: the general owner cannot read it; the reviewer can.
		self.assertFalse(dashboard_permissions.can_read_dashboard(d, self.owner))
		self.assertTrue(dashboard_permissions.can_read_dashboard(d, self.reviewer))

		# (2) the in-body preview banner is stamped, screenshot-safe.
		self.assertIn("Preview (shadow)", d.html)
		self.assertIn('data-result-state="shadow"', d.html)
		# the banner sits INSIDE the body (after <body>), not as detachable chrome.
		self.assertLess(d.html.lower().find("<body"), d.html.find("Preview (shadow)"))

		# (2b) the authored strong verbs are neutralised (never confirmed_outcome).
		self.assertNotIn("recovered 5,000", d.html)
		self.assertNotIn("prevented a misstatement", d.html)

	# ------------------------------------------------------------------ #
	# live control: authored dashboard unchanged (owner-owned, no banner)
	# ------------------------------------------------------------------ #
	def test_live_authored_dashboard_owner_owned_no_banner(self):
		inst = _mk_installation(self.owner, self.reviewer, "live")
		sk = "agent:live-dash:" + frappe.generate_hash(length=8)
		run = _mk_run(self.owner, inst, sk)

		out = self._call_save(self.owner, sk, html=AUTHORED_HTML, title="Close Summary")
		dash = out["dashboard"]
		self.assertEqual(frappe.db.get_value(RUN, run, "dashboard"), dash)

		d = frappe.get_doc(DASHBOARD, dash)
		# live: stays on the owner surface (visibility owner == installer owner).
		self.assertEqual(d.owner, self.owner)
		self.assertEqual(d.target_user, self.owner)
		self.assertTrue(dashboard_permissions.can_read_dashboard(d, self.owner))
		# no shadow preview banner — the authored artifact is presented as-is.
		self.assertNotIn("Preview (shadow)", d.html)
		self.assertIn("Fully compliant close", d.html)
