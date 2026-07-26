"""The jarvis half of the cross-repo tool-name contract (JF-001).

Fails on any drift between ``jarvis/tools/registry.py`` and the checked-in
``jarvis/tools/tool-names.json``. The plugin repo's ``tests/tool-contract.test.ts``
holds the other half against a verbatim copy of the same artifact, so a tool that
exists on only one side can no longer ship: the model would advertise a call the
bench answers with ToolNotFoundError, or a delegate could not discover a tool the
bench implements.
"""

import os
import tempfile
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from jarvis.tools import _tool_contract as tc
from jarvis.tools.registry import list_tools

_REGENERATE = "regenerate with `env/bin/python -m jarvis.tools._tool_contract --write`"


class TestToolContract(FrappeTestCase):
	def setUp(self):
		self.document = tc.load_contract()

	def test_checked_in_artifact_is_the_registry_regenerated(self):
		with open(tc.CONTRACT_PATH, encoding="utf-8") as fh:
			on_disk = fh.read()
		self.assertEqual(
			on_disk,
			tc.render(tc.build_contract()),
			f"tool-names.json does not match the registry — {_REGENERATE}, then copy the file "
			"into jarvis-openclaw-plugin at contracts/tool-names.json",
		)

	def test_registry_equals_the_artifact_in_both_directions(self):
		registry = set(list_tools())
		declared = set(self.document["tools"]) | set(self.document["backend_only"])
		self.assertEqual(
			registry,
			declared,
			f"registry vs tool-names.json drift: only-in-registry={sorted(registry - declared)}, "
			f"only-in-artifact={sorted(declared - registry)} — {_REGENERATE}",
		)

	def test_backend_only_entries_are_live_disjoint_and_reasoned(self):
		registry = set(list_tools())
		exposed = set(self.document["tools"])
		for name, reason in self.document["backend_only"].items():
			self.assertIn(name, registry, f"backend_only lists {name!r}, which no longer exists")
			self.assertNotIn(name, exposed, f"{name!r} is both exposed and backend_only")
			self.assertTrue(
				(reason or "").strip(),
				f"backend_only[{name!r}] must say WHY the plugin has no descriptor for it",
			)

	def test_digest_matches_the_artifact_it_ships_with(self):
		# Recomputed from the FILE's own lists, not from the registry: this is what
		# catches a hand-edited name list, and it is the value the plugin repo's copy
		# is compared against by eye (no CI job can see both repos).
		self.assertEqual(
			self.document["digest"],
			tc._digest(self.document["tools"], self.document["backend_only"]),
			f"tool-names.json was edited without regenerating — {_REGENERATE}",
		)

	def test_check_mode_passes_and_write_is_byte_identical(self):
		self.assertEqual(tc.main([]), 0)
		with tempfile.TemporaryDirectory() as tmp:
			path = os.path.join(tmp, "tool-names.json")
			written = tc.write_contract(path)
			with open(tc.CONTRACT_PATH, encoding="utf-8") as fh:
				self.assertEqual(written, fh.read())
			self.assertEqual(tc.load_contract(path), self.document)

	def test_generation_fails_closed_on_a_stale_exception(self):
		with patch.dict(tc.BACKEND_ONLY, {"retired_tool": "gone"}, clear=False):
			with self.assertRaises(ValueError):
				tc.build_contract()

	def test_generation_fails_closed_on_an_unreasoned_exception(self):
		with patch.dict(tc.BACKEND_ONLY, {"create_docs": "  "}, clear=False):
			with self.assertRaises(ValueError):
				tc.build_contract()


class TestDelegateWritebackAudit(FrappeTestCase):
	"""save_agent_dashboard writes a Jarvis Dashboard document from a detached
	delegate turn; like record_agent_run beside it, that write must be AUDITED
	(in _WRITE_TOOLS) and must never demand a confirmation card nobody can
	click (not in _GATED_WRITES). Wave-B review follow-up: the descriptor
	shipped without the audit entry, so the insert ran unaudited."""

	def test_save_agent_dashboard_is_write_but_not_gated(self):
		from jarvis import api

		self.assertIn("save_agent_dashboard", api._WRITE_TOOLS)
		self.assertNotIn("save_agent_dashboard", api._GATED_WRITES)

	def test_record_agent_run_is_write_but_not_gated(self):
		from jarvis import api

		self.assertIn("record_agent_run", api._WRITE_TOOLS)
		self.assertNotIn("record_agent_run", api._GATED_WRITES)


class TestRegistryCapabilityContract(FrappeTestCase):
	"""The vendored delegate registry's capability contract is PINNED.

	The bench snapshots each run's tools_allow/nature from
	jarvis/agents/registry.json while fleet delivers the container's allow-list
	from the private store manifests that file was exported from. They are only
	the same authority while (slug -> nature, tools_allow) matches EXACTLY —
	Wave-A adversarial P2-2: nothing gated this, so a re-vendor lag would
	silently split bench authorization from container capability.

	The digest is SEMANTIC (canonical JSON of {slug: {nature, sorted
	tools_allow}}), so formatting/descriptions may move freely. The SAME literal
	is pinned in the private agents repo
	(lib/check_registry_capability_contract.py); its docstring holds the paired
	update flow. Changing one side without the other fails that repo's validate
	or this test."""

	PINNED_CAPABILITY_DIGEST = "da27633e88f1bf4b623464af6aa83f99f3a78cf6882895f1ad7a675e56ca77b4"

	def test_vendored_registry_capability_digest_is_pinned(self):
		import hashlib
		import json

		import frappe

		path = frappe.get_app_path("jarvis", "agents", "registry.json")
		with open(path, encoding="utf-8") as fh:
			doc = json.load(fh)
		contract = {}
		for entry in doc["agents"]:
			slug = entry.get("agent_slug") or entry.get("slug")
			self.assertTrue(slug, "registry entry with no slug")
			self.assertNotIn(slug, contract, f"duplicate slug {slug}")
			contract[slug] = {
				"nature": entry.get("nature"),
				"tools_allow": sorted(entry.get("tools_allow") or []),
			}
		canonical = json.dumps(contract, sort_keys=True, separators=(",", ":"))
		got = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
		self.assertEqual(
			got,
			self.PINNED_CAPABILITY_DIGEST,
			"the vendored registry's capability contract moved — re-vendor from the "
			"agents repo and update BOTH pinned digests in the same change set "
			"(see lib/check_registry_capability_contract.py there)",
		)
