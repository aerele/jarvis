// Where focus goes when a row's full form opens and closes in the record draft card.
// Rows are found by their stable `__key` (draftTables.newRowKey), never by position:
// a row removed above an open one moves it up a place, and focus must still return
// to the toggle the user opened. Only a user's own toggle click moves focus; rows the
// card opens by itself (applyAutoOpen, "Open changed rows") set `__open` and nothing else.
import { nextTick } from "vue";

export const rowFormId = (tableIndex, key) => `jv-rowform-${tableIndex}-${key}`;
export const rowToggleId = (tableIndex, key) => `jv-rowtoggle-${tableIndex}-${key}`;

// Open the row (if closed) and move focus to its form section once it is in the DOM (the
// same watch + nextTick shape as the artifact panel's focus). The summary's row numbers.
export function showRowForm(tableIndex, row) {
	row.__open = true;
	nextTick(() => document.getElementById(rowFormId(tableIndex, row.__key))?.focus());
}

// The toggle click: flip the row, and on open move focus into it.
export function toggleRowForm(tableIndex, row) {
	if (row.__open) row.__open = false;
	else showRowForm(tableIndex, row);
}

// Done and Escape: close the form and give focus back to that row's toggle, as
// closeCreateMenu gives focus back to its button.
export function closeRowForm(tableIndex, row) {
	row.__open = false;
	nextTick(() => document.getElementById(rowToggleId(tableIndex, row.__key))?.focus());
}
