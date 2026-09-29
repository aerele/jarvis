"""Ops guard: the pending-action / held-write / File Box modules title every Error Log
``jarvis.<area>.<event>``, so ``api_errors.is_jarvis_error`` forwards it to the admin
Errors feed."""

from __future__ import annotations

import ast
import glob
import os
import tempfile

from frappe.tests.utils import FrappeTestCase

import jarvis

_CHAT = os.path.join(os.path.dirname(jarvis.__file__), "chat")
_GLOBS = ("pending_actions/*.py", "held_*.py", "filebox.py", "pending_confirm*.py")
# Titles these modules carried before the pending-action work (moved verbatim).
_PRE_EXISTING = {
	"pending_confirm: confirmation summary build failed",
	"pending_confirm: park failed; token not stored",
}


def _title(call: ast.Call, consts: dict) -> str | None:
	node = next((k.value for k in call.keywords if k.arg == "title"), None)
	if node is None and len(call.args) >= 2:
		node = call.args[1]
	if isinstance(node, ast.Name):
		return consts.get(node.id)
	if isinstance(node, ast.JoinedStr) and node.values:
		node = node.values[0]
	if isinstance(node, ast.Constant) and isinstance(node.value, str):
		return node.value
	return None


def bare_titles(path: str) -> list[str]:
	with open(path) as f:
		tree = ast.parse(f.read())
	consts = {
		t.id: n.value.value
		for n in tree.body
		if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
		for t in n.targets
		if isinstance(t, ast.Name)
	}
	bad = []
	for n in ast.walk(tree):
		if not isinstance(n, ast.Call):
			continue
		name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None)
		if name != "log_error":
			continue
		title = _title(n, consts)
		if title not in _PRE_EXISTING and not (title or "").startswith("jarvis."):
			bad.append(f"{os.path.basename(path)}:{n.lineno} {title!r}")
	return bad


class TestErrorLogTitles(FrappeTestCase):
	def test_every_new_error_log_is_jarvis_titled(self):
		paths = sorted({p for g in _GLOBS for p in glob.glob(os.path.join(_CHAT, g))})
		self.assertGreater(len(paths), 10)
		self.assertEqual([b for p in paths for b in bare_titles(p)], [])

	def test_guard_flags_a_bare_title(self):
		src = (
			"import frappe\nT = 'jarvis.x.ok'\n"
			"frappe.log_error(title=T)\nfrappe.log_error(title=f'jarvis.x.{1}')\n"
			"frappe.log_error(title='file_box send failed')\nfrappe.log_error('msg')\n"
		)
		with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
			f.write(src)
		self.addCleanup(os.unlink, f.name)
		self.assertEqual(
			[b.split(" ", 1)[1] for b in bare_titles(f.name)], ["'file_box send failed'", "None"]
		)
