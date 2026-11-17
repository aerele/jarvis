"""Guards the logo Jarvis shows on the Desk home tile and in the About dialog.

No database access. Runs as a plain unit test:
  bench --site <site> run-tests --module jarvis.tests.test_apps_screen
"""

import json
import unittest
from pathlib import Path

from jarvis import hooks

APP_ROOT = Path(__file__).resolve().parents[1]
DESKTOP_ICON = APP_ROOT / "desktop_icon" / "jarvis.json"


class TestAppsScreenEntry(unittest.TestCase):
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
