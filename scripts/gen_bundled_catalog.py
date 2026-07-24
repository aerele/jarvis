"""Regenerate jarvis/_model_catalog.py from the admin seed.

The bundled catalog is the offline fallback for admin's Jarvis LLM Provider
catalog. It MUST mirror the seed exactly: get_model_catalog() returns it on any
failure, and CI always runs with admin unreachable, so these values ARE the
values every test sees. A hand-maintained copy would drift on its first edit.

Run from the bench root after any change to PROVIDER_SEED:

    python apps/jarvis/scripts/gen_bundled_catalog.py \
        --seed apps/jarvis_admin_v2/fleet/provider_catalog.py \
        --out  apps/jarvis/jarvis/_model_catalog.py
"""

import argparse
import importlib.util
import pprint

HEADER_LINES = [
	'"""Bundled fallback for the admin-owned LLM provider + model catalog.',
	"",
	"GENERATED FILE. Do not edit by hand. Regenerate with:",
	"    python apps/jarvis/scripts/gen_bundled_catalog.py",
	"",
	"Returned whenever admin is unreachable or the Redis cache is cold, which",
	"includes every CI run. It therefore MIRRORS jarvis_admin_v2's PROVIDER_SEED",
	"exactly rather than trimming it: a shorter list here silently shrinks the",
	"catalog during an outage and breaks the tests that assert full model lists.",
	"NO secrets: ids, labels and display flags only.",
	'"""',
	"",
	"from __future__ import annotations",
	"",
	"BUNDLED_MODEL_CATALOG: list[dict] = ",
]


def load_seed(path):
	spec = importlib.util.spec_from_file_location("_seed", path)
	mod = importlib.util.module_from_spec(spec)
	try:
		spec.loader.exec_module(mod)
	except ImportError:
		# provider_catalog imports frappe at module scope; re-read the literal.
		import ast

		tree = ast.parse(open(path).read())
		for node in tree.body:
			if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "PROVIDER_SEED":
				return ast.literal_eval(node.value)
			if isinstance(node, ast.Assign) and any(
				getattr(t, "id", "") == "PROVIDER_SEED" for t in node.targets
			):
				return ast.literal_eval(node.value)
		raise SystemExit("PROVIDER_SEED not found")
	return mod.PROVIDER_SEED


def to_wire(entry):
	"""Shape one seed entry exactly like get_provider_catalog's response."""
	return {
		"provider_id": entry["provider_id"],
		"catalog_id": entry.get("catalog_id") or entry["provider_id"],
		"label": entry["label"],
		"subscription_label": entry.get("subscription_label") or entry["label"],
		"default_base_url": entry.get("default_base_url") or "",
		"renderer_id": entry.get("renderer_id") or "",
		"auth_profile_id": entry.get("auth_profile_id") or "",
		"supports_api_key": bool(entry.get("supports_api_key", 1)),
		"supports_subscription": bool(entry.get("supports_subscription", 0)),
		"needs_base_url": bool(entry.get("needs_base_url", 0)),
		"is_local": bool(entry.get("is_local", 0)),
		"models": [
			{
				"model_id": m["model_id"],
				"label": m.get("label") or m["model_id"],
				"tier": m["tier"],
				"is_default": bool(m.get("is_default", 0)),
				"sort_order": m.get("sort_order", 0),
			}
			for m in sorted(entry["models"], key=lambda m: (m.get("sort_order", 0), m["model_id"]))
		],
	}


if __name__ == "__main__":
	ap = argparse.ArgumentParser()
	ap.add_argument("--seed", required=True)
	ap.add_argument("--out", required=True)
	a = ap.parse_args()
	data = [to_wire(e) for e in load_seed(a.seed)]
	with open(a.out, "w") as fh:
		fh.write("\n".join(HEADER_LINES) + pprint.pformat(data, width=100, sort_dicts=False) + "\n")
	print(f"wrote {len(data)} providers to {a.out}")
