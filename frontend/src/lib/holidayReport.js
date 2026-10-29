// The holiday scene's failures are reported at most once per page session, whichever file
// hits them (the art not loading, the scene throwing while it plays): one set of keys, so
// a broken scene cannot flood the error log with the same line.
import { report as reportError } from "@/lib/errorReporter";

const reported = new Set();

/** Report `payload` unless `key` was already reported in this session. */
export function reportOnce(key, payload, report = reportError) {
	if (reported.has(key)) return;
	reported.add(key);
	report(payload);
}

/** Test-only: forget what was reported, so each spec starts from a fresh page. */
export function _resetReportedForTests() {
	reported.clear();
}
