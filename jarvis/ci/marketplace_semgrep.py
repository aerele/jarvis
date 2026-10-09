"""Gate a Semgrep JSON report the way the Frappe Marketplace audit scores it.

The audit (frappe/press ``marketplace_app_audit``) gives "Fail" for a Critical or Major
finding and "Needs Improvement" for a Minor one, and yanks the release on a blocking
one. Findings of internal-only rules count too. This fails on all of them; only Info
findings pass."""

import argparse
import json
import sys

# frappe/press SEMGREP_TO_AUDIT_SEVERITY
SEVERITY = {
	"CRITICAL": "Critical",
	"ERROR": "Critical",
	"HIGH": "Major",
	"WARNING": "Minor",
	"MEDIUM": "Minor",
	"LOW": "Info",
	"INFO": "Info",
}
FAILING = frozenset(("Critical", "Major", "Minor"))


def audit_severity(result: dict) -> str:
	return SEVERITY.get(str(result.get("extra", {}).get("severity", "INFO")).upper(), "Info")


def is_blocking(result: dict) -> bool:
	return (result.get("extra", {}).get("metadata") or {}).get("is_blocking") is True


def fails_audit(result: dict) -> bool:
	return is_blocking(result) or audit_severity(result) in FAILING


def _escape(value: str, *, prop: bool = False) -> str:
	value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
	return value.replace(":", "%3A").replace(",", "%2C") if prop else value


def annotation(result: dict) -> str:
	rule = result["check_id"].rsplit(".", 1)[-1]
	kind = f"{'blocking, ' if is_blocking(result) else ''}{audit_severity(result)}"
	message = " ".join(result.get("extra", {}).get("message", "").split())
	return (
		f"::error file={_escape(result['path'], prop=True)},line={result['start']['line']},"
		f"title={_escape(f'{rule} ({kind})', prop=True)}::{_escape(message)}"
	)


def main(argv=None) -> int:
	argparse.ArgumentParser(
		description=__doc__, epilog="Reads `semgrep scan --json` output on stdin."
	).parse_args(argv)
	report = json.load(sys.stdin)
	results = report.get("results", [])
	failing = [r for r in results if fails_audit(r)]
	for result in failing:
		print(annotation(result))
	for error in report.get("errors", []):
		print(f"::warning title=semgrep::{_escape(str(error.get('message') or error.get('type'))[:500])}")
	print(
		f"{len(failing)} finding(s) fail the Marketplace audit; "
		f"{len(results) - len(failing)} info (reported by the audit, not gated)."
	)
	return 1 if failing else 0


if __name__ == "__main__":
	sys.exit(main())
