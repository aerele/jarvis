"""The cross-repo tool-name contract: generator + loader for ``tool-names.json``.

``tool-names.json`` (this module's sibling) is the single artifact that pins the
Jarvis tool name set. It is GENERATED FROM THE BACKEND REGISTRY, because the
backend is what can actually serve a call: a plugin descriptor with no Python
implementation is a tool the model can invoke and the bench can only answer with
``ToolNotFoundError``. That was the JF-001 defect — ``compute_materiality`` and
``run_scrutiny`` outlived their implementations in the plugin's descriptor table
while ``record_agent_run`` / ``save_agent_dashboard`` existed on the bench with
no descriptor at all, so the delegates could not discover their own writeback
tools. Two hand-edited name lists in two repos, no test between them.

Each repo now tests itself against this artifact:

  * jarvis — ``jarvis/tests/test_tool_contract.py``: the registry equals
    ``tools`` + ``backend_only``, exactly, in both directions;
  * jarvis-openclaw-plugin — ``tests/tool-contract.test.ts``: its descriptor
    table and ``openclaw.plugin.json`` contracts equal ``tools``, exactly.

UPDATE FLOW (mirrored in ``jarvis/tools/README.md`` → "Adding a tool"):

  1. edit ``registry.py::_TOOL_NAMES`` (and drop in the tool module);
  2. regenerate, from the bench root::

       env/bin/python -m jarvis.tools._tool_contract --write

     until you do, ``test_tool_contract`` fails — the jarvis side cannot drift;
  3. copy the regenerated ``tool-names.json`` VERBATIM to the plugin repo at
     ``contracts/tool-names.json`` (same bytes, same ``digest``);
  4. the plugin's contract test now fails until ``src/tool-defs.ts``,
     ``src/schemas.ts`` and ``openclaw.plugin.json`` carry exactly those names.

No CI job can see both repos (the plugin repo is private; jarvis CI holds no
cross-repo token), so step 3 is a human copy and only step 4 catches a stale
one — on the plugin PR, before it ships. ``digest`` is what makes the copy
checkable by eye: two copies with the same digest carry the same name set, and a
copy that was hand-edited rather than regenerated fails its own repo's test.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

CONTRACT_PATH = os.path.join(os.path.dirname(__file__), "tool-names.json")
CONTRACT_VERSION = 1

# Registry tools that deliberately carry NO plugin descriptor, each with the
# reason it is excluded. This is the ONLY sanctioned asymmetry between the two
# name sets; everything else must match exactly, in both directions. An entry
# that is no longer in the registry is a hard error (see ``build_contract``), and
# an entry that has been given a descriptor after all fails the PLUGIN's contract
# test — so neither half of a resolved exception can be forgotten.
BACKEND_ONLY: dict[str, str] = {
	"create_docs": (
		"Deprecated compat shim for already-shipped plugin bundles that still advertise "
		"jarvis__create_docs; bulk create is create_doc(docs=[...]) now. Re-adding a descriptor "
		"would resurrect the retired surface. Delete the module (and this entry) once no "
		"deployed bundle references it."
	),
	"export_document": (
		"Implemented by jarvis #337 but never given a plugin descriptor, so no agent can call it "
		"today. Exposing it is a product decision (it writes a File and needs a jarvis-persona "
		"TOOLS.md row), not a drift fix — recorded here so the gap is explicit while every OTHER "
		"drift still fails CI. Remove this entry in the change that adds the descriptor."
	),
}

_COMMENT: tuple[str, ...] = (
	"GENERATED FILE — do not hand-edit. Single source of truth for the Jarvis tool name set.",
	"Home: jarvis repo, jarvis/tools/tool-names.json, generated from jarvis/tools/registry.py.",
	"Vendored copy: jarvis-openclaw-plugin, contracts/tool-names.json — byte-identical.",
	"",
	"Update flow:",
	"  1. change registry.py::_TOOL_NAMES (+ the tool module);",
	"  2. regenerate: env/bin/python -m jarvis.tools._tool_contract --write",
	"     (jarvis test_tool_contract fails until this file matches the registry);",
	"  3. copy this file VERBATIM into the plugin repo at contracts/tool-names.json;",
	"  4. the plugin's tests/tool-contract.test.ts fails until src/tool-defs.ts,",
	"     src/schemas.ts and openclaw.plugin.json carry exactly these names.",
	"",
	"'tools' = the names the plugin MUST expose, one descriptor each, as jarvis__<name>.",
	"'backend_only' = registry tools deliberately without a descriptor, and why.",
	"'digest' covers the two name lists only (not this comment, not the reasons); the two",
	"repo copies are compared by it, because no CI job can see both repos.",
)


def _digest(tools, backend_only) -> str:
	"""sha256 over the two name lists, in a form both Python and TypeScript can
	recompute byte-for-byte (one ``<kind>:<name>\\n`` line per entry, sorted)."""
	payload = "".join(f"tool:{name}\n" for name in sorted(tools))
	payload += "".join(f"backend_only:{name}\n" for name in sorted(backend_only))
	return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_contract() -> dict:
	"""The contract document the registry implies, right now."""
	from jarvis.tools.registry import list_tools

	registry = set(list_tools())
	stale = sorted(name for name in BACKEND_ONLY if name not in registry)
	if stale:
		raise ValueError(
			f"BACKEND_ONLY names no longer in the registry: {stale}. Drop them from "
			"jarvis/tools/_tool_contract.py and regenerate tool-names.json."
		)
	unreasoned = sorted(name for name, why in BACKEND_ONLY.items() if not (why or "").strip())
	if unreasoned:
		raise ValueError(f"BACKEND_ONLY entries need a reason: {unreasoned}")

	tools = sorted(registry - set(BACKEND_ONLY))
	backend_only = {name: BACKEND_ONLY[name] for name in sorted(BACKEND_ONLY)}
	return {
		"$comment": list(_COMMENT),
		"contract_version": CONTRACT_VERSION,
		"digest": _digest(tools, backend_only),
		"tools": tools,
		"backend_only": backend_only,
	}


def render(document: dict) -> str:
	"""The exact bytes of the checked-in artifact (and of the plugin's copy)."""
	return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def load_contract(path: str = CONTRACT_PATH) -> dict:
	with open(path, encoding="utf-8") as fh:
		return json.load(fh)


def write_contract(path: str = CONTRACT_PATH) -> str:
	rendered = render(build_contract())
	with open(path, "w", encoding="utf-8") as fh:
		fh.write(rendered)
	return rendered


def main(argv: list[str] | None = None) -> int:
	"""``--write`` regenerates the artifact; no argument checks it (exit 1 on drift)."""
	args = sys.argv[1:] if argv is None else argv
	if "--write" in args:
		write_contract()
		print(f"wrote {CONTRACT_PATH}")
		return 0
	with open(CONTRACT_PATH, encoding="utf-8") as fh:
		current = fh.read()
	if current == render(build_contract()):
		return 0
	print(
		f"{CONTRACT_PATH} is out of date — regenerate with "
		"`env/bin/python -m jarvis.tools._tool_contract --write`",
		file=sys.stderr,
	)
	return 1


if __name__ == "__main__":
	raise SystemExit(main())
