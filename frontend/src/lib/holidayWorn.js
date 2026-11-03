// Loads the worn-item art (jarvis/public/js/shared/holiday/worn.mjs) the first time a
// mark needs it. A literal import path, never a template string, so the bundler can
// see it and name the chunk `holiday-*` (see chunkFileNames in vite.config.js).
//
// The art is optional decoration, so a failed import (a stale chunk after a deploy, an
// offline tab) gives null: the mark simply wears nothing. It must never reload the page
// or throw into the app. Three things keep it that way:
//   - isHolidayLoading() is true while the import is pending. Safari's preload-error
//     message has no file name, so the router cannot recognise the chunk from the
//     message alone; it leaves the event alone while this is set.
//   - after a failure nothing retries for a minute, so a dead chunk is not requested on
//     every mount.
//   - the first failure of the session is reported (fixed text, no browser message,
//     because the error reporter drops chunk-load messages as already handled).
import { report as reportError } from "@/lib/errorReporter";

export const WORN_RETRY_MS = 60 * 1000;

let loaded = null; // the module, once it has loaded; kept for the life of the page
let pending = null; // the import in flight
let retryAt = 0; // no new attempt before this time (ms since epoch)
let reported = false;

export function isHolidayLoading() {
	return pending !== null;
}

/** The art if it has loaded already, else null. Lets a new mark draw it at once. */
export function loadedWorn() {
	return loaded;
}

/** Resolves to the art module, or null when it cannot be loaded. Never rejects. */
export function loadWorn({
	now = Date.now,
	load = () => import("@jsshared/holiday/worn.mjs"),
} = {}) {
	if (loaded) return Promise.resolve(loaded);
	if (pending) return pending;
	if (now() < retryAt) return Promise.resolve(null);
	// `pending` is set before the import can settle, so isHolidayLoading() is already true
	// when Vite dispatches its preload error for this very import.
	const attempt = Promise.resolve()
		.then(load)
		.then((mod) => {
			mod.installWornCss();
			loaded = mod;
			return mod;
		})
		.catch(() => {
			retryAt = now() + WORN_RETRY_MS;
			if (!reported) {
				reported = true;
				reportError({
					error_class: "HolidayArtLoadFailed",
					error_code: "holiday_art",
					message: "The holiday worn-item art could not be loaded",
				});
			}
			return null;
		})
		.finally(() => {
			pending = null;
		});
	pending = attempt;
	return attempt;
}

/** Test-only: forget everything, so each spec starts from a fresh page. */
export function _resetForTests() {
	loaded = null;
	pending = null;
	retryAt = 0;
	reported = false;
}
