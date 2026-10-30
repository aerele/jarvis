// Keep in step with frontend/src/lib/holidayReport.js (the desktop original); only the reporter's
// import differs, because @ is this app's own src here.
// The holiday scene's failures are reported at most once per page session per design and
// kind, whichever file hits them (the art not loading, the scene throwing while it plays):
// one set of keys, so a broken scene cannot flood the error log with the same line.
import { report as reportError } from "@shared/lib/errorReporter";
import { DESIGNS } from "@jsshared/holiday/answer.mjs";

const reported = new Set();

/** Report `payload` unless `key` was already reported in this session. */
export function reportOnce(key, payload, report = reportError) {
	if (reported.has(key)) return;
	reported.add(key);
	report(payload);
}

// A load failure carries no stack: it opens with the browser's own "Failed to fetch dynamically
// imported module", which the error reporter drops as an already-handled stale chunk, so a
// stack would make the report vanish. The design in error_code is what support needs.
const KINDS = {
	load: {
		error_class: "HolidaySceneLoadFailed",
		message: "The holiday scene art could not be loaded",
		withStack: false,
	},
	play: {
		error_class: "HolidaySceneFailed",
		message: "The holiday scene failed while playing",
		withStack: true,
	},
};

/**
 * Report a scene failure with the design that failed in error_code ("holiday_scene:christmas",
 * from the closed set of designs) and, for a failure while playing, the thrown error's stack.
 * Once per design and kind.
 */
export function reportSceneFailure(kind, design, error, report = reportError) {
	const known = DESIGNS.includes(design) ? design : "unknown";
	const { withStack, ...fixed } = KINDS[kind];
	reportOnce(
		`${kind}:${known}`,
		{
			...fixed,
			error_code: `holiday_scene:${known}`,
			stack: withStack && error && error.stack ? String(error.stack) : "",
		},
		report
	);
}

/** Test-only: forget what was reported, so each spec starts from a fresh page. */
export function _resetReportedForTests() {
	reported.clear();
}
