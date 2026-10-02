// Macros-page API additions (DESIGN-V3 §8.4). `src/api.js` is frozen - new
// endpoints get thin wrappers in per-feature modules under src/api/.
import { call } from "frappe-ui";

// §8.3 - bulk delete of own macros (each row's live runs are stopped and its run
// history goes first, server side). A row that was not deleted is skipped with its
// reason: `reason` is a code, `message` the sentence to show, `title` the macro's
// own name. `stopped_runs` counts the runs stopped, skipped macros included.
// -> { deleted: int, skipped: [{name, title, reason, message}], stopped_runs: int }
export const deleteMacrosBulk = (names) =>
	call("jarvis.chat.macros_api.delete_macros_bulk", {
		names: JSON.stringify(Array.from(names || [])),
	});
