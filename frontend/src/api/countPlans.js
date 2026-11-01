// Human physical-count entry. These calls are separate from the generic agent
// configuration API because they create submitted observation records through a
// dedicated, permission-checked endpoint.
import { call } from "frappe-ui";

const CC = "jarvis.chat.cycle_count_entry.";

export const listCountPlans = ({ start = 0, pageLength = 20 } = {}) =>
	call(CC + "list_count_plans", { start, page_length: pageLength });

export const getCountPlan = (request, entryKey = "") =>
	call(CC + "get_count_plan", {
		request,
		...(entryKey ? { entry_key: entryKey } : {}),
	});

export const recordCounts = (request, countedAt, entries, entryKey) =>
	call(CC + "record_counts", {
		request,
		counted_at: countedAt,
		entries: JSON.stringify(entries),
		entry_key: entryKey,
	});
