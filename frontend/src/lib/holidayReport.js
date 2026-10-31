// The holiday scene's failures are reported at most once per page session per design and
// kind, whichever file hits them (the art not loading, the scene throwing while it plays):
// one set of keys, so a broken scene cannot flood the error log with the same line.
import { report as reportError } from "@/lib/errorReporter";
import { DESIGNS } from "@jsshared/holiday/answer.mjs";

const reported = new Set();

/** Report `payload` unless `key` was already reported in this session. */
export function reportOnce(key, payload, report = reportError) {
	if (reported.has(key)) return;
	reported.add(key);
	report(payload);
}

const KINDS = {
	load: {
		error_class: "HolidaySceneLoadFailed",
		message: "The holiday scene art could not be loaded",
	},
	play: { error_class: "HolidaySceneFailed", message: "The holiday scene failed while playing" },
};

/**
 * Report a scene failure with the design that failed in error_code ("holiday_scene:christmas",
 * from the closed set of designs) and the thrown error's stack. Once per design and kind.
 */
export function reportSceneFailure(kind, design, error, report = reportError) {
	const known = DESIGNS.includes(design) ? design : "unknown";
	reportOnce(
		`${kind}:${known}`,
		{
			...KINDS[kind],
			error_code: `holiday_scene:${known}`,
			stack: error && error.stack ? String(error.stack) : "",
		},
		report
	);
}

/** Test-only: forget what was reported, so each spec starts from a fresh page. */
export function _resetReportedForTests() {
	reported.clear();
}
