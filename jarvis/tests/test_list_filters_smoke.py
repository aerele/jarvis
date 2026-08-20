"""Did a filter migration break the surface it migrated?

This exists because of a defect the whole filter suite missed. A guard meant for
one wiki function was pasted into five, and four of them have no ``filters``
parameter — so they raised ``NameError`` on the very first line of real work.
``get_wiki_caps`` is called when the Wiki tab mounts, so the tab could not load
at all; pages could not be opened or created; the promotion flow was dead. All
139 tests were green, because they cover the filter CONTRACT exhaustively and
the endpoints around it not at all.

The class of bug is "an edit that reads correctly at every call site, and only
binding-resolution or execution exposes". TWO complementary nets, because
neither alone closes it:

**What each one actually covers — stated precisely, because an inflated
coverage claim is itself the failure mode here.** A future reviewer will trust
these sentences instead of re-deriving them; this branch has already lost time
twice to a claim that read as broader than it was.

1. :class:`TestMigratedSurfacesStillAnswer` — DYNAMIC. Calls, of the ~66
   whitelisted endpoints on migrated surfaces, only the ~20 that take no
   required argument and do not mutate; plus four regressions named explicitly
   because the paste hit them. It necessarily skips everything needing an
   argument and everything with side effects, so it is NOT a complete sweep. Its
   value is that it exercises the real call path, so it catches breakage that no
   static reading would — a bad default, an import-time failure, a decorator
   applied in the wrong order.

2. :class:`TestNoUnboundNamesOnMigratedSurfaces` — STATIC. Reads the source, so
   it covers ALL of them regardless of arity or side effects, which is the half
   the dynamic net structurally cannot reach. It only finds ONE shape of bug: a
   name loaded with nothing binding it. That is exactly the shape of this
   incident, and it is the one that would have caught the same paste in
   ``save_wiki_page`` — same file, same surface, same commit — where a smoke
   test must not go, because saving a page is a mutation.

Neither checks WHAT an endpoint returns; the contract suites do that.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import pathlib
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
	# Frappe records whitelisted functions in a global collection
	# (frappe.whitelisted), not as an attribute on the function, and it
	# registers the innermost function, so a decorated endpoint is matched
	# through __wrapped__. That collection is a set on Frappe 16 but a plain
	# list on Frappe 15, so coerce to a set once (it is stable for this scan)
	# before intersecting per candidate.
	whitelisted = set(frappe.whitelisted)
	for name, fn in vars(module).items():
		if name.startswith("_") or name in _ALWAYS_SKIP:
			continue
		if not callable(fn):
			continue
		candidates = {fn}
		inner = getattr(fn, "__wrapped__", None)
		while inner is not None:
			candidates.add(inner)
			inner = getattr(inner, "__wrapped__", None)
		if not (candidates & whitelisted):
			continue
		if getattr(fn, "__module__", "") != module.__name__:
			continue  # imported into this module, tested where it lives
		out.append((name, fn))
	return sorted(out)


#: The one thing a smoke call must never do.
_MUTATING = (
	"create_",
	"update_",
	"delete_",
	"save_",
	"apply_",
	"request_",
	"approve_",
	"reject_",
	"archive_",
	"restore_",
	"run_",
	"set_",
	"promote_",
	"generate_",
	"sync_",
	"rebuild_",
	"bulk_",
	"reset_",
	"clear_",
	"record_",
	"confirm_",
	"start_",
	"stop_",
	"cancel_",
	"retry_",
	"publish_",
	"install_",
	"toggle_",
)


class TestMigratedSurfacesStillAnswer(unittest.TestCase):
	"""A bare call to the endpoints that CAN be called bare.

	Not a sweep of the surface: of ~66 whitelisted endpoints it reaches ~20 (the
	parameterless, non-mutating reads) plus four named regressions. See the
	module docstring for why that is the honest boundary, and
	:class:`TestNoUnboundNamesOnMigratedSurfaces` for the half that covers the
	rest.
	"""

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


# --------------------------------------------------------------------------- #
# The other half: a STATIC pass, covering what a call-based net structurally
# cannot.
# --------------------------------------------------------------------------- #
def _bound_names(fn: ast.AST) -> set[str]:
	"""Every name a function body binds, erring towards MORE bound, never fewer.

	Python function scope is flat, so a name bound anywhere in the body is bound
	throughout it. Under-reporting a binder here would produce a FALSE ALARM, so
	every binding form is covered — parameters, assignment and its augmented and
	annotated forms, tuple/star unpacking, `for` targets, `with ... as`,
	`except ... as`, walrus, comprehension targets, imports, `global`/`nonlocal`,
	nested defs and classes, lambda parameters, and match captures.
	"""
	bound: set[str] = set()

	def add_target(node: ast.AST) -> None:
		for sub in ast.walk(node):
			if isinstance(sub, ast.Name):
				bound.add(sub.id)

	def add_args(args: ast.arguments) -> None:
		for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
			bound.add(a.arg)
		if args.vararg:
			bound.add(args.vararg.arg)
		if args.kwarg:
			bound.add(args.kwarg.arg)

	if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
		add_args(fn.args)

	for node in ast.walk(fn):
		if isinstance(node, ast.Assign):
			for t in node.targets:
				add_target(t)
		elif isinstance(node, ast.AugAssign | ast.AnnAssign):
			add_target(node.target)
		elif isinstance(node, ast.For | ast.AsyncFor):
			add_target(node.target)
		elif isinstance(node, ast.comprehension):
			add_target(node.target)
		elif isinstance(node, ast.NamedExpr):
			add_target(node.target)
		elif isinstance(node, ast.withitem):
			if node.optional_vars is not None:
				add_target(node.optional_vars)
		elif isinstance(node, ast.ExceptHandler):
			if node.name:
				bound.add(node.name)
		elif isinstance(node, ast.Import | ast.ImportFrom):
			for alias in node.names:
				bound.add((alias.asname or alias.name).split(".")[0])
		elif isinstance(node, ast.Global | ast.Nonlocal):
			bound.update(node.names)
		elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
			bound.add(node.name)
			if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
				add_args(node.args)
		elif isinstance(node, ast.Lambda):
			add_args(node.args)
		elif isinstance(node, ast.MatchAs | ast.MatchStar):
			if node.name:
				bound.add(node.name)
		elif isinstance(node, ast.MatchMapping):
			if node.rest:
				bound.add(node.rest)
	return bound


def _module_level_names(tree: ast.Module) -> set[str]:
	names: set[str] = set()
	for node in tree.body:
		if isinstance(node, ast.Assign):
			for t in node.targets:
				for sub in ast.walk(t):
					if isinstance(sub, ast.Name):
						names.add(sub.id)
		elif isinstance(node, ast.AugAssign | ast.AnnAssign):
			for sub in ast.walk(node.target):
				if isinstance(sub, ast.Name):
					names.add(sub.id)
		elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
			names.add(node.name)
		elif isinstance(node, ast.Import | ast.ImportFrom):
			for alias in node.names:
				names.add((alias.asname or alias.name).split(".")[0])
		elif isinstance(node, ast.Try):
			for sub in ast.walk(node):
				if isinstance(sub, ast.Import | ast.ImportFrom):
					for alias in sub.names:
						names.add((alias.asname or alias.name).split(".")[0])
	return names


def _scan_unbound(modules: list[str]) -> list[str]:
	"""Every `Name` load with nothing binding it, across whole modules."""
	import builtins
	import importlib

	builtin_names = set(dir(builtins))
	findings: list[str] = []
	for dotted in modules:
		path = pathlib.Path(importlib.import_module(dotted).__file__)
		tree = ast.parse(path.read_text())
		module_names = _module_level_names(tree)
		for fn in tree.body:
			if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
				continue
			available = _bound_names(fn) | module_names | builtin_names
			for node in ast.walk(fn):
				if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)):
					continue
				if node.id in available:
					continue
				findings.append(f"{path.name}:{fn.name}() line {node.lineno} -> {node.id}")
	return findings


class TestNoUnboundNamesOnMigratedSurfaces(unittest.TestCase):
	"""A name loaded with nothing binding it — the shape of the wiki P0.

	This is the half the call-based net cannot reach. Of the 66 whitelisted
	endpoints on migrated surfaces the smoke net calls 20: it necessarily skips
	everything that needs an argument and everything that mutates. The paste that
	broke this branch landed in four functions; had it also landed in
	``save_wiki_page`` — same file, same surface, same commit — no call-based test
	would have seen it, because saving a wiki page is a mutation a smoke test must
	not perform.

	Reading the source needs no database, no session and no side effects, so it
	covers ALL of them regardless of arity or mutation.
	"""

	def test_no_function_on_a_migrated_surface_loads_an_unbound_name(self):
		modules = sorted(_modules_under_test())
		self.assertTrue(modules, "no migrated surfaces — the static pass has no subject")
		# Scanned in a helper so the failing frame holds the FINDINGS and not the
		# several-thousand-name scratch sets — a wall of locals is the difference
		# between a failure someone acts on and one they scroll past.
		findings = _scan_unbound(modules)
		self.assertEqual(
			findings,
			[],
			"a name is loaded with nothing binding it — this endpoint raises NameError "
			"the moment it is called:\n  " + "\n  ".join(findings),
		)
