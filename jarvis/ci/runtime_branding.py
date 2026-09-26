"""Scan an app checkout against an externally supplied, versioned branding policy."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path, PurePosixPath


class PolicyError(ValueError):
	pass


def policy_digest(document):
	data = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
	return hashlib.sha256(data.encode()).hexdigest()


def _relative_path(value):
	return (
		isinstance(value, str)
		and bool(value)
		and not value.startswith("/")
		and "\\" not in value
		and all(part not in ("", ".", "..") for part in value.split("/"))
		and not any(char in value for char in "*?[]\n\r\0")
	)


def load_policy(raw, expected_digest=None):
	try:
		if not raw or len(raw.encode()) > 48_000:
			raise ValueError
		doc = json.loads(raw)
		if set(doc) != {"version", "terms", "allowed_lines", "allowed_paths", "minimum_files"}:
			raise ValueError
		if type(doc["version"]) is not int or doc["version"] != 1:
			raise ValueError
		terms = doc["terms"]
		if not isinstance(terms, list) or not terms:
			raise ValueError
		if any(not isinstance(term, str) or not term.strip() or "\n" in term for term in terms):
			raise ValueError
		if not isinstance(doc["allowed_lines"], dict) or not isinstance(doc["minimum_files"], dict):
			raise ValueError
		if not doc["minimum_files"]:
			raise ValueError
		for path, lines in doc["allowed_lines"].items():
			if not _relative_path(path) or not isinstance(lines, list) or not lines:
				raise ValueError
			if any(not isinstance(line, str) or "\n" in line or "\r" in line for line in lines):
				raise ValueError
		for path, floor in doc["minimum_files"].items():
			if not _relative_path(path) or type(floor) is not int or floor < 1:
				raise ValueError
		if not isinstance(doc["allowed_paths"], list):
			raise ValueError
		if any(not _relative_path(path) for path in doc["allowed_paths"]):
			raise ValueError
		if expected_digest is not None and policy_digest(doc) != expected_digest:
			raise PolicyError("Branding policy checksum mismatch.")
		return doc
	except (ValueError, TypeError, AttributeError) as exc:
		if isinstance(exc, PolicyError):
			raise
		raise PolicyError("Branding policy is missing or malformed.") from None


def scan(root, policy):
	root = Path(root).resolve()
	checkout = subprocess.check_output(
		["git", "-C", str(root), "rev-parse", "--show-toplevel"], text=True
	).strip()
	if Path(checkout).resolve() != root:
		raise PolicyError("Scan root must be the checkout root.")
	paths = (
		subprocess.check_output(
			["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
		)
		.decode()
		.split("\0")
	)
	pattern = re.compile("|".join(re.escape(term) for term in policy["terms"]), re.IGNORECASE)
	allowances = {path: Counter(lines) for path, lines in policy["allowed_lines"].items()}
	allowed_paths = set(policy["allowed_paths"])
	scanned = dict.fromkeys(policy["minimum_files"], 0)
	errors = []
	for name in sorted(set(filter(None, paths))):
		path = root / name
		if not _relative_path(name) or path.is_symlink():
			errors.append(f"{name}: unsupported source path")
			continue
		try:
			raw = path.read_bytes()
		except FileNotFoundError:
			continue  # A locally deleted tracked file is not part of the working tree.
		if pattern.search(name):
			if name in allowed_paths:
				allowed_paths.remove(name)
			else:
				errors.append(f"{name}: forbidden branding in path")
		if b"\0" in raw:
			continue
		try:
			content = raw.decode("utf-8")
		except UnicodeDecodeError:
			continue
		for base in scanned:
			if PurePosixPath(base) in PurePosixPath(name).parents:
				scanned[base] += 1
		for number, line in enumerate(content.splitlines(), 1):
			if not pattern.search(line):
				continue
			remaining = allowances.get(name, Counter())
			if remaining[line] > 0:
				remaining[line] -= 1
			else:
				errors.append(f"{name}:{number}: forbidden branding outside exact policy allowance")
	for name, lines in allowances.items():
		if any(lines.values()):
			errors.append(f"{name}: stale line allowance; update the Admin-owned policy")
	for name in sorted(allowed_paths):
		errors.append(f"{name}: stale path allowance; update the Admin-owned policy")
	for base, floor in policy["minimum_files"].items():
		if scanned[base] < floor:
			errors.append(f"{base}: only {scanned[base]} text files scanned; minimum is {floor}")
	return errors


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--root", type=Path, default=Path.cwd())
	source = parser.add_mutually_exclusive_group(required=True)
	source.add_argument("--policy", type=Path)
	source.add_argument("--policy-env")
	parser.add_argument("--expected-policy-sha256")
	args = parser.parse_args(argv)
	try:
		raw = args.policy.read_text() if args.policy else os.environ.get(args.policy_env)
		policy = load_policy(raw, args.expected_policy_sha256)
		errors = scan(args.root, policy)
	except (PolicyError, OSError, subprocess.CalledProcessError, UnicodeError) as exc:
		print(f"Branding check failed: {exc}", file=sys.stderr)
		return 1
	if errors:
		print("\n".join(errors), file=sys.stderr)
		return 1
	print(f"Branding policy {policy_digest(policy)} passed.")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
