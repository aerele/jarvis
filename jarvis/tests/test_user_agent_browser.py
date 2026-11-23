"""The web browser option of user agents: switch, sites, cap, payload and readiness."""

from __future__ import annotations

import frappe

from jarvis.chat import user_agent_sync, user_agents, user_agents_api
from jarvis.tests._user_agent_helpers import DOCTYPE, OTHER, OWNER, UserAgentCase, _as


class BrowserCase(UserAgentCase):
	def setUp(self):
		super().setUp()
		self.switch(True)

	def switch(self, on: bool) -> None:
		frappe.db.set_single_value("Jarvis Settings", "enable_user_agent_browser", 1 if on else 0)

	def browser_agent(self, owner=OWNER, **values):
		values.setdefault("allow_browser", 1)
		values.setdefault("browser_sites", "example.com")
		return self.make_agent(owner, **values)


class TestBrowserSites(BrowserCase):
	def test_pasted_sites_are_normalised(self):
		sites, bad = user_agents.normalise_sites(
			"https://Portal.GST.gov.in/login?x=1\n  www.Example.com:8080 \n\nexample.com/\nportal.gst.gov.in\n"
		)
		self.assertIsNone(bad)
		self.assertEqual(sites, ["portal.gst.gov.in", "www.example.com", "example.com"])

	def test_a_line_that_is_not_a_host_is_named(self):
		for line in ("localhost", "*.example.com", "under_score.com", "-lead.com", "a" * 250 + ".com"):
			with self.subTest(line=line[:20]):
				self.assertEqual(user_agents.normalise_sites(f"ok.com\n{line}")[1], line)

	def test_ip_addresses_and_local_names_are_not_public_sites(self):
		for line in (
			"192.168.1.10",
			"http://10.0.0.1:8080/admin",
			"[::1]",
			"http://[2001:db8::1]:443/",
			"2001:db8::1",
			"foo.local",
			"a.b.cluster.local",
			"localhost",
			"LOCALHOST:3000",
			"files.internal",
			"nas.lan",
			"box.home",
			"ad.corp",
			"api.svc",
		):
			with self.subTest(line=line):
				self.assertEqual(user_agents.normalise_sites(line), ([], line))
				self.assertEqual(user_agents.site_problem(line), user_agents.SITE_NOT_PUBLIC)

	def test_an_all_numeric_dotted_name_is_refused(self):
		for line in ("1.2.3.4.5", "123.456", "example.123"):
			with self.subTest(line=line):
				self.assertEqual(user_agents.normalise_sites(line), ([], line))
				self.assertIsNotNone(user_agents.site_problem(line))

	def test_punycode_and_multi_label_hosts_pass(self):
		for line in (
			"xn--80ak6aa92e.com",
			"xn--bcher-kva.example",
			"example.xn--h2brj9c",
			"portal.gst.gov.in",
			"a.b.example.co.uk",
		):
			with self.subTest(line=line):
				self.assertEqual(user_agents.normalise_sites(line), ([line], None))
				self.assertIsNone(user_agents.site_problem(line))

	def test_a_non_public_line_gets_its_own_message(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.browser_agent(browser_sites="example.com\n10.0.0.5")
		self.assertIn("10.0.0.5", str(ctx.exception))
		self.assertIn(
			"is not a public website. List public sites only, for example example.com.", str(ctx.exception)
		)

	def test_the_saved_row_holds_the_normalised_list(self):
		row = self.browser_agent(browser_sites="HTTPS://A.example.com/x\na.example.com\nb.example.org:443")
		self.assertEqual(row.browser_sites, "a.example.com\nb.example.org")

	def test_listed_mode_needs_one_to_thirty_sites(self):
		with self.assertRaises(frappe.ValidationError):
			self.browser_agent(agent_name="None", browser_sites="")
		with self.assertRaises(frappe.ValidationError):
			self.browser_agent(
				agent_name="Too many", browser_sites="\n".join(f"s{i}.example.com" for i in range(31))
			)
		self.browser_agent(
			agent_name="Thirty", browser_sites="\n".join(f"s{i}.example.com" for i in range(30))
		)

	def test_a_bad_line_is_refused_with_the_line_in_the_message(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.browser_agent(browser_sites="example.com\nnot a site")
		self.assertIn("not a site", str(ctx.exception))

	def test_public_mode_needs_no_sites(self):
		row = self.browser_agent(browser_mode="Any public site", browser_sites="")
		self.assertEqual(user_agents.browser_view(row), {"enabled": True, "mode": "public", "sites": []})


class TestBrowserSwitchAndCap(BrowserCase):
	def test_turning_the_browser_on_with_the_switch_off_is_refused(self):
		self.switch(False)
		with self.assertRaises(frappe.PermissionError):
			self.browser_agent()
		row = self.make_agent()
		with _as(OWNER), self.assertRaises(frappe.PermissionError):
			user_agents_api.update_user_agent(row.name, allow_browser=1, browser_sites=["example.com"])

	def test_the_switch_needs_user_agents_on_too(self):
		self.flag(False)
		self.assertFalse(user_agents.browser_switch_on())

	def test_at_most_three_enabled_browser_agents_per_site(self):
		self.browser_agent(agent_name="One")
		self.browser_agent(agent_name="Two")
		self.browser_agent(OTHER, agent_name="Three")
		with self.assertRaises(frappe.ValidationError) as ctx:
			self.browser_agent(OTHER, agent_name="Four")
		self.assertIn("Only 3 agents per workspace can have a browser", str(ctx.exception))
		# A switched-off agent does not hold a slot.
		off = self.browser_agent(agent_name="Off", enabled=0)
		with _as(OWNER), self.assertRaises(frappe.ValidationError):
			user_agents_api.set_user_agent_enabled(off.name, 1)

	def test_list_reports_the_switch_and_the_cap(self):
		self.browser_agent(OTHER)
		with _as(OWNER):
			out = user_agents_api.list_user_agents()
		self.assertIs(out["browser_enabled"], True)
		self.assertEqual(out["max_browser_agents"], 3)
		self.assertEqual(out["browser_agents_in_use"], 1)
		self.switch(False)
		with _as(OWNER):
			self.assertIs(user_agents_api.list_user_agents()["browser_enabled"], False)


class TestBrowserApiAndPayload(BrowserCase):
	def test_create_and_update_take_the_browser_fields(self):
		with _as(OWNER):
			made = user_agents_api.create_user_agent(
				"Portal reader",
				"Read the GST portal.",
				tools=["get_list"],
				allow_browser=1,
				browser_mode="listed",
				browser_sites="https://portal.gst.gov.in/\nexample.com",
			)
			self.assertEqual(
				made["browser"],
				{"enabled": True, "mode": "listed", "sites": ["portal.gst.gov.in", "example.com"]},
			)
			public = user_agents_api.update_user_agent(made["name"], browser_mode="public")
			self.assertEqual(public["browser"]["mode"], "public")
			listed = user_agents_api.update_user_agent(
				made["name"], browser_mode="listed", browser_sites='["a.example.com"]'
			)
			self.assertEqual(listed["browser"]["sites"], ["a.example.com"])
			with self.assertRaises(frappe.ValidationError):
				user_agents_api.update_user_agent(made["name"], browser_mode="anywhere")

	def test_payload_carries_browser_only_when_on(self):
		listed = self.browser_agent(agent_name="Listed", browser_sites="example.com\nportal.gst.gov.in")
		public = self.browser_agent(agent_name="Public", browser_mode="Any public site", browser_sites="")
		plain = self.make_agent(agent_name="Plain")
		by_id = {e["id"]: e for e in user_agent_sync.build_payload()}
		self.assertEqual(
			by_id[listed.agent_id]["browser"],
			{"mode": "allowlist", "sites": ["example.com", "portal.gst.gov.in"]},
		)
		self.assertEqual(by_id[public.agent_id]["browser"], {"mode": "public", "sites": []})
		self.assertNotIn("browser", by_id[plain.agent_id])
		self.assertIn("web browser", by_id[listed.agent_id]["body"])
		self.assertNotIn("web browser", by_id[plain.agent_id]["body"])
		self.switch(False)
		for entry in user_agent_sync.build_payload():
			self.assertNotIn("browser", entry)

	def test_a_browser_edit_makes_the_agent_wait_for_its_sync(self):
		row = self.browser_agent()
		with _as(OWNER):
			self.assertTrue(user_agents_api.get_user_agent(row.name)["ready"])
			edited = user_agents_api.update_user_agent(row.name, browser_sites="other.example.com")
		self.assertEqual(edited["sync"]["status"], "Pending")
		self.assertFalse(edited["ready"])
		with _as(OWNER), self.assertRaises(user_agents.UserAgentUnavailableError):
			user_agents_api.start_user_agent_chat(row.name)
		self.sync.assert_called()

	def test_flipping_the_switch_queues_a_push_for_browser_agents(self):
		browser = self.browser_agent()
		plain = self.make_agent(agent_name="Plain")
		self.sync.reset_mock()
		self.switch(False)
		user_agent_sync.on_browser_switch_changed()
		self.sync.assert_called_once()
		self.assertEqual(frappe.db.get_value(DOCTYPE, browser.name, "sync_status"), "Pending")
		self.assertEqual(frappe.db.get_value(DOCTYPE, plain.name, "sync_status"), "Synced")


class TestBrowserCodes(BrowserCase):
	def test_the_browser_codes_and_their_classes(self):
		expected = {
			"browser_limit": (
				False,
				"Only 3 agents per workspace can have a browser. Turn it off on another agent first.",
			),
			"browser_image_unconfigured": (
				False,
				"Web browsing is not set up on this workspace yet. Turn off the browser option, or ask support to set it up.",
			),
			"browser_guard_missing": (
				False,
				"Jarvis could not set up this agent on your workspace. Contact support and share this code: browser_guard_missing.",
			),
			"browser_start_failed": (True, "The agent's browser did not start. Use Retry sync in a minute."),
		}
		for code, (retry, sentence) in expected.items():
			with self.subTest(code=code):
				self.assertIs(user_agent_sync.is_retryable(code), retry)
				self.assertEqual(user_agent_sync.sentence_for(code), sentence)
