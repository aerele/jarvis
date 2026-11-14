// Document Intake wrappers (File Box "Intake" view). These endpoints live in their own
// module, like approvals.js and paymentCalendar.js; the page imports "@/api/intake".
import { call } from "frappe-ui";

// {rows, total, has_more, start, page_length}. `scope` is "mine" or "all" (managers).
export const intakeListPage = (p = {}) =>
	call("jarvis.chat.filebox.list_intake_page", {
		search: p.search || "",
		filters: JSON.stringify(p.filters || {}),
		scope: p.scope || "mine",
		start: p.start || 0,
		page_length: p.page_length || 20,
	});

// Run a Failed or Skipped file again -> {ok, status: "Queued"}.
export const intakeRetry = (activity) => call("jarvis.chat.filebox.retry_intake", { activity });
