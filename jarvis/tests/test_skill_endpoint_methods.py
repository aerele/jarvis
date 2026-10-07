"""The skills, learned-rules and app-learning endpoints answer POST only, and so do
the document side-panel writers (comments, assignments, shares, likes, attachments).

Every whitelisted function in the three modules is covered by walking Frappe's own
registry, so an endpoint added later without ``methods=["POST"]`` fails here."""

import frappe
from frappe.handler import is_valid_http_method
from frappe.tests.utils import FrappeTestCase
from frappe.utils import set_request

from jarvis.chat import app_learning_api, custom_skills_api, docmeta_api, learned_api

_MISSING = object()

POST_ONLY_MODULES = (custom_skills_api, learned_api, app_learning_api)

# ``get_docmeta`` only reads; the rest of the side panel changes a document.
DOCMETA_WRITERS = (
	"add_comment",
	"update_comment",
	"delete_comment",
	"toggle_assignment",
	"toggle_share",
	"toggle_like",
	"delete_attachment",
)


def _endpoints(module):
	"""The whitelisted functions ``module`` defines, by name."""
	found = {}
	for fn in frappe.whitelisted:
		if getattr(fn, "__module__", None) == module.__name__:
			found[fn.__name__] = fn
	return found


class TestSkillEndpointsArePostOnly(FrappeTestCase):
	def setUp(self):
		super().setUp()
		prev = getattr(frappe.local, "request", _MISSING)
		self.addCleanup(self._restore_request, prev)

	@staticmethod
	def _restore_request(prev):
		if prev is _MISSING:
			if hasattr(frappe.local, "request"):
				del frappe.local.request
		else:
			frappe.local.request = prev

	def _assert_post_only(self, module, name, fn):
		path = f"/api/method/{module.__name__}.{name}"
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[fn], ["POST"])
		set_request(method="GET", path=path)
		with self.assertRaises(frappe.PermissionError):
			is_valid_http_method(fn)
		set_request(method="POST", path=path)
		is_valid_http_method(fn)  # no raise

	def test_every_endpoint_of_the_three_modules_is_post_only(self):
		for module in POST_ONLY_MODULES:
			endpoints = _endpoints(module)
			# A module that registers nothing means the walk above stopped seeing it.
			self.assertTrue(endpoints, module.__name__)
			for name, fn in sorted(endpoints.items()):
				with self.subTest(endpoint=f"{module.__name__}.{name}"):
					self._assert_post_only(module, name, fn)

	def test_the_walk_finds_the_known_endpoints(self):
		# Pins the walk itself: if Frappe stopped registering by ``__module__`` the test
		# above would pass on an empty or partial list.
		self.assertLessEqual(
			{"list_custom_skills", "delete_custom_skill", "share_custom_skill", "apply_custom_skills"},
			set(_endpoints(custom_skills_api)),
		)
		self.assertLessEqual(
			{"approve_learned_pattern", "apply_learned_skills", "set_learning_settings"},
			set(_endpoints(learned_api)),
		)
		self.assertLessEqual({"schedule_app_learning"}, set(_endpoints(app_learning_api)))

	def test_document_side_panel_writers_are_post_only(self):
		endpoints = _endpoints(docmeta_api)
		for name in DOCMETA_WRITERS:
			with self.subTest(endpoint=name):
				self.assertIn(name, endpoints)
				self._assert_post_only(docmeta_api, name, endpoints[name])
