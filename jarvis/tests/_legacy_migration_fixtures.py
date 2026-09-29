"""Load independently maintained legacy-schema inputs for upgrade integration tests.

This module is test-only. Customer migration code never reads these inputs.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

EXPECTED_SHA256 = "2c111a0706e96a13665b7e740a637d53474e19cda7ae4eb2fe9753d182345626"
VALUE_ENV = "JARVIS_LEGACY_MIGRATION_FIXTURES"
FILE_ENV = "JARVIS_LEGACY_MIGRATION_FIXTURES_FILE"


def digest(document):
	return hashlib.sha256(
		json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
	).hexdigest()


def validate(raw, expected_sha256=EXPECTED_SHA256):
	try:
		if not isinstance(raw, str) or not raw or len(raw.encode()) > 16_384:
			raise ValueError
		doc = json.loads(raw)
		if (
			set(doc) != {"version", "watermark", "capture_provider", "settings"}
			or type(doc["version"]) is not int
		):
			raise ValueError
		if doc["version"] != 1:
			raise ValueError
		for name in ("watermark", "capture_provider"):
			entry = doc[name]
			if not isinstance(entry, dict) or set(entry) != {"legacy_column", "patch"}:
				raise ValueError
			if not isinstance(entry["legacy_column"], str) or not re.fullmatch(
				r"[a-z][a-z0-9_]{0,63}", entry["legacy_column"]
			):
				raise ValueError
			if entry["legacy_column"] in {"agent_seq_watermark", "agent_provider"}:
				raise ValueError
			if not isinstance(entry["patch"], str) or not re.fullmatch(
				r"jarvis\.patches\.[a-z][a-z0-9_]*\.execute", entry["patch"]
			):
				raise ValueError
		settings = doc["settings"]
		if not isinstance(settings, dict) or set(settings) != {"legacy_patch", "patch", "renames"}:
			raise ValueError
		for key, suffix in (("legacy_patch", ""), ("patch", r"\.execute")):
			if not isinstance(settings[key], str) or not re.fullmatch(
				r"jarvis\.patches\.[a-z][a-z0-9_]*" + suffix, settings[key]
			):
				raise ValueError
		renames = settings["renames"]
		targets = {
			"jarvis_admin_url",
			"jarvis_admin_api_key",
			"agent_url",
			"agent_token",
			"agent_compose_dir",
			"agent_config_path",
			"agent_llm_key_path",
		}
		if not isinstance(renames, dict) or len(renames) != 7:
			raise ValueError
		if set(renames.values()) != targets or set(renames) & targets:
			raise ValueError
		if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) for key in renames):
			raise ValueError
		if digest(doc) != expected_sha256:
			raise ValueError
		return doc
	except (ValueError, TypeError, KeyError):
		raise ValueError(
			"Legacy migration fixtures are missing, invalid, or differ from the reviewed checksum."
		) from None


def load(fixture_file=None):
	if fixture_file is not None:
		return validate(Path(fixture_file).read_text())
	raw, filename = os.environ.get(VALUE_ENV), os.environ.get(FILE_ENV)
	if raw and filename:
		raise ValueError("Supply either the fixture value or fixture file, not both.")
	if filename:
		return validate(Path(filename).read_text())
	if not raw:
		raise ValueError(f"Upgrade tests require the Admin-owned fixture: set {VALUE_ENV} or {FILE_ENV}.")
	return validate(raw)


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--fixture-file", type=Path)
	args = parser.parse_args()
	try:
		doc = load(args.fixture_file)
	except (OSError, ValueError) as exc:
		print(str(exc), file=sys.stderr)
		return 1
	print(f"Legacy migration fixtures {digest(doc)} validated.")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
