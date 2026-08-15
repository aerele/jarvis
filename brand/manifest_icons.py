#!/usr/bin/env python3
"""Reproducible source for the PWA manifest icons.

The five PNGs in jarvis/public/manifest/ were made by hand with no source file.
This renders them from the same brand definition the rest of the pack uses.

By default it only reports. Replacing the live icons changes what every
customer sees in their browser tab and on their home screen, so writing them is
opt-in behind --write.

    python brand/manifest_icons.py             compare and report
    python brand/manifest_icons.py --write     overwrite the live icons
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

BRAND_DIR = Path(__file__).resolve().parent
MANIFEST_DIR = BRAND_DIR.parent / "jarvis" / "public" / "manifest"


def _load_generate():
	"""Load generate.py by path under a unique name.

	`import generate` would be resolved against sys.path, and "generate" is
	generic enough that another module of that name could shadow this one when
	the script runs inside a larger bench process. Binding by path removes that.
	"""
	spec = importlib.util.spec_from_file_location("jarvis_brand_generate", BRAND_DIR / "generate.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


brand = _load_generate()

# bleed=True skips the rounded corners. Android masks maskable icons itself, and
# iOS masks the apple-touch icon, so both must fill the full square. The two
# purpose="any" icons keep their rounded corners, matching what ships today and
# what pwa/vite.config.js declares.
ICONS = {
	"icon-192.png": (192, False),
	"icon-512.png": (512, False),
	"icon-192-maskable.png": (192, True),
	"icon-512-maskable.png": (512, True),
	"apple-touch-180.png": (180, True),
}


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"--write", action="store_true", help="overwrite the live icons instead of only reporting"
	)
	parser.add_argument(
		"--check",
		action="store_true",
		help="exit 1 if the live icons have drifted, for wiring into CI",
	)
	args = parser.parse_args()

	rendered = render_all()
	if args.write:
		for name, payload in rendered.items():
			(MANIFEST_DIR / name).write_bytes(payload)
		print(f"wrote {len(rendered)} icons to {MANIFEST_DIR}")
		return 0

	return report(rendered, fail_on_drift=args.check)


def render_all() -> dict[str, bytes]:
	renderer = brand.PngRenderer()
	return {name: brand._encode(renderer.tile(size, bleed=bleed)) for name, (size, bleed) in ICONS.items()}


def report(rendered: dict[str, bytes], fail_on_drift: bool = False) -> int:
	"""Name every live icon that differs from the current brand definition.

	Reports without failing by default. The live icons are known to have drifted,
	so a bare run must not go red and block anyone; --check is the opt-in gate
	for when someone wants CI to enforce this.
	"""
	differing = brand.stale_assets(rendered, root=MANIFEST_DIR)

	if not differing:
		print(f"all {len(rendered)} manifest icons match the brand definition")
		return 0

	print("These live icons do NOT match brand/generate.py:")
	for name in differing:
		print(f"  {name}")
	print("\nThe live icons predate the current --brand-1/--brand-2 in main.css.")
	print("Run with --write to regenerate them. That changes the tab and home-screen")
	print("icon for every customer, so treat it as its own decision.")
	return 1 if fail_on_drift else 0


if __name__ == "__main__":
	sys.exit(main())
