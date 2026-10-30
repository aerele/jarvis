// Loads the worn-item art (jarvis/public/js/shared/holiday/worn.mjs) the first time a mark
// needs it. A literal import path, never a template string, so the bundler can see it and
// name the chunk `holiday-*` (see chunkFileNames in vite.config.js), which keeps it out of
// the service worker's precache. The retry and report rules are in holidayWornLoader.js.
import { report } from "@shared/lib/errorReporter";
import { createWornLoader } from "./holidayWornLoader";

const worn = createWornLoader({
	load: () => import("@jsshared/holiday/worn.mjs"),
	report,
});

export const loadWorn = worn.load;
export const loadedWorn = worn.loaded;
