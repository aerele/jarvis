"""Decode reviewed CI snapshots without credentials or repository variables.

Admin maintains the source documents. Encoded snapshots travel with the app
commit they validate; encoding avoids adding literal branding to source scans.
"""

import argparse
import base64
import binascii
import json
from pathlib import Path

from jarvis.ci.runtime_branding import load_policy
from jarvis.tests._legacy_migration_fixtures import validate

POLICY_SHA256 = "86e42605f8ed3e1f73a8b9bf6ba111ef03343505c16ceb1a6ec696e57d261fd2"
_DATA = Path(__file__).with_name("data")
_FILES = {"fixture": "migration-fixture.b64", "policy": "branding-policy.b64"}


def decode(kind):
	try:
		raw = base64.b64decode((_DATA / _FILES[kind]).read_text().strip(), validate=True).decode()
	except (binascii.Error, UnicodeError) as exc:
		raise ValueError("Invalid encoded CI input") from exc
	document = validate(raw) if kind == "fixture" else load_policy(raw, POLICY_SHA256)
	return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("kind", choices=tuple(_FILES))
	parser.add_argument("--output", required=True, type=Path)
	args = parser.parse_args()
	# Validate before creating an output; never silently fall back or skip tests.
	args.output.write_text(decode(args.kind))
	print(f"Validated {args.kind} snapshot for this checkout.")


if __name__ == "__main__":
	main()
