"""The skills, learned-rules and app-learning endpoints that change something answer
POST only, and so do the document side-panel writers (comments, assignments, shares,
likes, attachments). Endpoints that only read keep answering GET.

Every whitelisted function of the three modules is found in Frappe's own registry and
must be either POST-only or named in ``READ_ONLY`` below, so an endpoint added later
has to be classified here. A function named as read-only is also checked for the
calls that would make it a writer."""

import inspect
import re

import frappe
from frappe.handler import is_valid_http_method
from frappe.tests.utils import FrappeTestCase
from frappe.utils import set_request

from jarvis.chat import app_learning_api, custom_skills_api, docmeta_api, learned_api

_MISSING = object()

# Endpoints that only read. Everything else in these modules is POST-only: it saves,
# deletes, queues work or calls a model.
READ_ONLY = {
	custom_skills_api: {
		"list_custom_skills",
		"list_custom_skills_page",
		"get_custom_skill",
		"file_box_doctypes",
		"list_shareable_users",
		"get_skill_shares",
		"list_skill_promotion_requests",
		"preflight_skill_promotion",
		"my_skill_promotion",
		"promotable_target_roles",
		"get_custom_skills_sync_status",
	},
	learned_api: {
		"list_learned_patterns_page",
		"get_learned_pattern",
		"get_learned_apply_status",
		"pending_learned_count",
		"get_learning_settings",
		"get_learning_status",
		"get_review_access",
		"list_promotion_requests_page",
		"go_to_chat_context",
	},
	app_learning_api: {"list_custom_apps", "get_app_learning_overview", "list_app_learning_runs_page"},
}

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

# What a function that only reads does not call.
_WRITES = re.compile(
	r"\.insert\(|\.save\(|\.delete\(|\.submit\(|\.cancel\(|\.db_set\(|delete_doc\(|"
	r"set_value\(|set_single_value\(|db\.commit\(|enqueue\w*\(|publish_realtime\("
)


def _endpoints(module):
	"""The whitelisted functions ``module`` defines, by name."""
	found = {}
	for fn in frappe.whitelisted:
		if getattr(fn, "__module__", None) == module.__name__:
			found[fn.__name__] = fn
	return found


class TestSkillEndpointMethods(FrappeTestCase):
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

	def test_every_endpoint_that_is_not_read_only_is_post_only(self):
		for module, readers in READ_ONLY.items():
			endpoints = _endpoints(module)
			# A module that registers nothing means the walk above stopped seeing it.
			self.assertTrue(endpoints, module.__name__)
			for name, fn in sorted(endpoints.items()):
				if name in readers:
					continue
				with self.subTest(endpoint=f"{module.__name__}.{name}"):
					self._assert_post_only(module, name, fn)

	def test_every_read_only_name_is_a_real_endpoint_that_answers_get(self):
		for module, readers in READ_ONLY.items():
			endpoints = _endpoints(module)
			for name in sorted(readers):
				with self.subTest(endpoint=f"{module.__name__}.{name}"):
					self.assertIn(name, endpoints)  # a renamed or removed reader fails here
					for method in ("GET", "POST"):
						set_request(method=method, path=f"/api/method/{module.__name__}.{name}")
						is_valid_http_method(endpoints[name])  # no raise

	def test_a_read_only_endpoint_does_not_write(self):
		# Shallow on purpose: it reads the function's own source, not what it calls. It
		# catches the plain mistake, a writer filed as a reader.
		for module, readers in READ_ONLY.items():
			for name in sorted(readers):
				with self.subTest(endpoint=f"{module.__name__}.{name}"):
					source = inspect.getsource(inspect.unwrap(getattr(module, name)))
					self.assertIsNone(_WRITES.search(source), _WRITES.search(source))

	def test_the_walk_finds_the_known_endpoints(self):
		# Pins the walk itself: if Frappe stopped registering by ``__module__`` the tests
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
