"""Every whitelisted endpoint on a migrated surface still ANSWERS.

This exists because of a defect the whole filter suite missed. A guard meant for
one wiki function was pasted into five, and four of them have no ``filters``
parameter — so they raised ``NameError`` on the very first line of real work.
``get_wiki_caps`` is called when the Wiki tab mounts, so the tab could not load
at all; pages could not be opened or created; the promotion flow was dead. All
139 tests were green, because they cover the filter CONTRACT exhaustively and
the endpoints around it not at all.

The class of bug is "edits that read correctly at every call site and only
binding-resolution or execution exposes". The cheap, complete answer is to call
every endpoint on every migrated surface once, with no filter arguments, and
assert it returns rather than raises. It does not check WHAT they return — the
contract suites do that — only that a migration did not break the surface it
migrated.

Deliberately parameterless: a migration touches the filter arguments, so the
call that proves nothing was collaterally broken is the one that passes none.
"""

from __future__ import annotations

import contextlib
import inspect
import unittest

import frappe

from jarvis.chat import list_registry

USER_SMOKE = "lfs-user@example.com"


def _ensure_user(email: str) -> str:
	from jarvis.permissions import ensure_jarvis_user_role

	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "smoke",
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		)
		u.flags.ignore_permissions = True
		u.insert()
		frappe.db.commit()
	doc = frappe.get_doc("User", email)
	roles = set(frappe.get_roles(email))
	for role in ("Jarvis User", "System Manager"):
		if role not in roles:
			doc.add_roles(role)
	frappe.db.commit()
	frappe.clear_cache(user=email)
	return email


def setUpModule() -> None:
	frappe.set_user("Administrator")
	_ensure_user(USER_SMOKE)


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


#: The modules behind every MIGRATED surface. Derived from the registry so a
#: later wave cannot migrate a surface and quietly skip this net.
def _modules_under_test() -> set[str]:
	mods = set()
	for view in list_registry.filterable_views():
		for dotted in view.endpoints:
			mods.add(dotted.rsplit(".", 1)[0])
	return mods


#: Endpoints that legitimately need arguments to do anything, or that MUTATE.
#: A smoke call must not create documents or fire side effects, and "it needs a
#: slug" is not the failure this is hunting. Everything else must survive a bare
#: call — including raising a *clean* PermissionError/ValidationError, which is
#: an answer; only an unexpected error (NameError, TypeError, AttributeError) is
#: the bug.
_ALWAYS_SKIP = {"frappe", "cint", "cstr", "flt", "getdate", "now_datetime", "add_to_date"}


def _whitelisted(module) -> list:
	out = []
	for name, fn in vars(module).items():
		if name.startswith("_") or name in _ALWAYS_SKIP:
			continue
		# Frappe records whitelisted functions in a global SET (frappe.whitelisted),
		# not as an attribute on the function — and it registers the innermost
		# function, so a decorated endpoint is matched through __wrapped__.
		if not callable(fn):
			continue
		candidates = {fn}
		inner = getattr(fn, "__wrapped__", None)
		while inner is not None:
			candidates.add(inner)
			inner = getattr(inner, "__wrapped__", None)
		if not (candidates & frappe.whitelisted):
			continue
		if getattr(fn, "__module__", "") != module.__name__:
			continue  # imported into this module, tested where it lives
		out.append((name, fn))
	return sorted(out)


#: The one thing a smoke call must never do.
_MUTATING = ("create_", "update_", "delete_", "save_", "apply_", "request_", "approve_",
             "reject_", "archive_", "restore_", "run_", "set_", "promote_", "generate_",
             "sync_", "rebuild_", "bulk_", "reset_", "clear_", "record_", "confirm_",
             "start_", "stop_", "cancel_", "retry_", "publish_", "install_", "toggle_")


class TestMigratedSurfacesStillAnswer(unittest.TestCase):
	"""A bare call to every read endpoint of every migrated surface."""

	#: These are the errors that mean "a code change broke this endpoint", as
	#: opposed to "this endpoint declined the request", which is a real answer.
	BROKEN = (NameError, TypeError, AttributeError, ImportError, IndentationError)

	def test_every_read_endpoint_on_a_migrated_surface_can_be_called(self):
		import importlib

		checked = 0
		for dotted in sorted(_modules_under_test()):
			module = importlib.import_module(dotted)
			for name, fn in _whitelisted(module):
				if name.startswith(_MUTATING):
					continue
				sig = inspect.signature(fn)
				# Only the endpoints that can be called with nothing at all —
				# a required argument is not what this net is for.
				if any(
					p.default is inspect.Parameter.empty
					and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
					for p in sig.parameters.values()
				):
					continue
				checked += 1
				with self.subTest(endpoint=f"{dotted}.{name}"), _as(USER_SMOKE):
					try:
						fn()
					except self.BROKEN as e:
						self.fail(
							f"{dotted}.{name}() raised {type(e).__name__}: {e}\n"
							f"A bare call to a migrated surface's endpoint must not blow up — "
							f"this is the collateral a filter migration causes."
						)
					except Exception:
						# A clean refusal (permission, validation, a missing
						# record) is an ANSWER. Only the list above is the bug.
						pass
		self.assertGreater(checked, 10, "the smoke net collapsed — it is checking almost nothing")

	def test_the_wiki_surface_specifically_still_mounts(self):
		"""The regression that motivated this file, named explicitly.

		``get_wiki_caps`` is what the Wiki tab calls on mount; if it raises, the
		tab renders nothing at all, and no filter test would notice.
		"""
		from jarvis.chat import wiki

		with _as(USER_SMOKE):
			caps = wiki.get_wiki_caps()
			self.assertIsInstance(caps, dict)

			pages = wiki.list_wiki_pages_page(page_length=5)
			self.assertIn("rows", pages)

	def test_a_bare_call_to_each_wiki_endpoint_that_takes_no_filters(self):
		"""Each of the four functions the pasted guard actually broke."""
		from jarvis.chat import wiki

		with _as(USER_SMOKE):
			for fn, kwargs in (
				(wiki.get_wiki_caps, {}),
				(wiki.get_wiki_page, {"slug": "lfs-does-not-exist"}),
				(wiki.create_wiki_page, {"title": "", "page_type": "Process"}),
				(wiki.request_wiki_promotion, {"page": "lfs-does-not-exist", "to_scope": "Org"}),
			):
				with self.subTest(endpoint=fn.__name__):
					try:
						fn(**kwargs)
					except self.BROKEN as e:
						self.fail(f"{fn.__name__} raised {type(e).__name__}: {e}")
					except Exception:
						pass  # a clean refusal is fine; a NameError is not
