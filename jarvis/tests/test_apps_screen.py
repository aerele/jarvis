"""Guards how Jarvis is listed on the Desk home, the /apps screen and About.

No database access. Runs as a plain unit test:
  bench --site <site> run-tests --module jarvis.tests.test_apps_screen
"""

import json
import unittest
from pathlib import Path

from frappe.apps import is_desk_apps

from jarvis import hooks
from jarvis.permissions import JARVIS_ACCESS_ROLES

APP_ROOT = Path(__file__).resolve().parents[1]
DESKTOP_ICON = APP_ROOT / "desktop_icon" / "jarvis.json"
CHAT_PAGE = APP_ROOT / "jarvis" / "page" / "jarvis_chat" / "jarvis_chat.json"


class TestAppsScreenEntry(unittest.TestCase):
	def test_route_keeps_login_landing_on_the_desk(self):
		"""Frappe sends users to /apps after login once any listed app has a
		route outside /desk. A route of /jarvis here would move every Jarvis
		user's landing page, so the entry must stay a Desk route."""
		self.assertTrue(
			is_desk_apps(hooks.add_to_apps_screen),
			"add_to_apps_screen route left /desk; this changes where users land after login.",
		)

	def test_everyone_who_sees_the_entry_can_open_its_route(self):
		"""The entry shows for JARVIS_ACCESS_ROLES but routes through a Desk
		page with its own role list. A role missing there would see Jarvis on
		/apps and get a no-permission page when clicking it."""
		page_roles = {row["role"] for row in json.loads(CHAT_PAGE.read_text())["roles"]}
		self.assertEqual(set(JARVIS_ACCESS_ROLES) - page_roles, set())

	def test_logo_is_a_file_the_site_serves(self):
		"""Both the hook and the Desk tile point at one committed file under
		public/. A renamed or unpublished logo would show a broken image."""
		logo = hooks.add_to_apps_screen[0]["logo"]
		self.assertEqual(json.loads(DESKTOP_ICON.read_text())["logo_url"], logo)

		prefix = "/assets/jarvis/"
		self.assertTrue(logo.startswith(prefix), logo)
		self.assertTrue((APP_ROOT / "public" / logo.removeprefix(prefix)).is_file(), logo)


if __name__ == "__main__":
	unittest.main()
