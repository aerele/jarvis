// Macros-page API additions (DESIGN-V3 §8.4). `src/api.js` is frozen - new
// endpoints get thin wrappers in per-feature modules under src/api/.
import { call } from "frappe-ui";

// §8.3 - bulk delete of own macros (each row's run history goes first, server
// side). Not-owned rows are skipped with per-row reasons.
// -> { deleted: int, skipped: [{name, reason}] }
export const deleteMacrosBulk = (names) =>
	call("jarvis.chat.macros_api.delete_macros_bulk", {
		names: JSON.stringify(Array.from(names || [])),
	});
