// Loads a holiday scene's art (jarvis/public/js/shared/holiday/scenes/<design>.mjs) the
// first time a welcome screen may play it. A static map of literal import paths, never a
// template string, so the bundler can see every scene and name its chunk `holiday-*`
// (see chunkFileNames in vite.config.js). A design with no entry here has no scene.
//
// Like the worn art (lib/holidayWorn.js) the scene is optional decoration, so a failed
// import (a stale chunk after a deploy, an offline tab) gives null: no scene, the cards
// show, nothing reloads and nothing throws. isSceneLoading() is true while an import is
// pending, so the router's preload-error guard leaves Safari's file-less message alone.
// The first failure of the session is reported with a fixed text (the error reporter
// drops chunk-load messages as already handled).
import { _resetReportedForTests, reportOnce } from "@/lib/holidayReport";

export const SCENE_LOADERS = {
	diwali: () => import("@jsshared/holiday/scenes/diwali.mjs"),
};

export const SCENE_RETRY_MS = 60 * 1000;

const loaded = new Map(); // design -> module, kept for the life of the page
const pending = new Map(); // design -> import in flight
const retryAt = new Map(); // design -> no new attempt before this time (ms since epoch)

/** Does this design have a scene in this build? Own keys only: "toString" has none. */
export function hasScene(design) {
	return typeof design === "string" && Object.hasOwn(SCENE_LOADERS, design);
}

export function isSceneLoading() {
	return pending.size > 0;
}

/** Resolves to the scene module, or null when there is none or it cannot load. Never rejects. */
export function loadScene(design, { now = Date.now, loaders = SCENE_LOADERS } = {}) {
	if (!Object.hasOwn(loaders, design)) return Promise.resolve(null);
	if (loaded.has(design)) return Promise.resolve(loaded.get(design));
	if (pending.has(design)) return pending.get(design);
	if (now() < (retryAt.get(design) || 0)) return Promise.resolve(null);
	// Registered before the import can settle, so isSceneLoading() is already true when
	// Vite dispatches its preload error for this very import.
	const attempt = Promise.resolve()
		.then(loaders[design])
		.then((mod) => {
			if (!mod || typeof mod.play !== "function") throw new Error("not a scene");
			loaded.set(design, mod);
			return mod;
		})
		.catch(() => {
			retryAt.set(design, now() + SCENE_RETRY_MS);
			reportOnce("load", {
				error_class: "HolidaySceneLoadFailed",
				error_code: "holiday_scene",
				message: "The holiday scene art could not be loaded",
			});
			return null;
		})
		.finally(() => {
			pending.delete(design);
		});
	pending.set(design, attempt);
	return attempt;
}

/** Test-only: forget everything, so each spec starts from a fresh page. */
export function _resetForTests() {
	loaded.clear();
	pending.clear();
	retryAt.clear();
	_resetReportedForTests();
}
