// Maps the Desk route to the `context` payload that jarvis.chat.api.send_message
// forwards to turn_handler._prepend_doc_context, which turns it into a leading
// "[Viewing: ...]" line on the prompt. The stored message is untouched, so the
// transcript still reads as the user typed it.
//
// Pure on purpose: the panel calls this on every route change and before every
// send, and this mapping is the part of that path worth unit testing.

// Frappe route shapes this recognizes:
//   ["Form", doctype, name]        -> a single record
//   ["List", doctype, ...]         -> a list view
//   ["query-report", reportName]   -> a report (filters passed in via opts)
// Anything else (workspaces, dashboards, settings) yields null, and the panel
// then behaves as a plain chat with no context chip.
export function contextFromRoute(route, opts = {}) {
  if (!Array.isArray(route) || route.length === 0) return null;
  const head = route[0];
  const a = route[1];
  const b = route[2];

  // A brand-new unsaved doc routes to ["Form", doctype] with no name segment.
  // There is no record to point the agent at, so send nothing rather than
  // inventing "new-sales-invoice-1".
  if (head === "Form" && a && b) return { doctype: a, name: b };
  if (head === "List" && a) return { doctype: a };
  if (head === "query-report" && a) {
    const f = opts.filters;
    const hasFilters = f && typeof f === "object" && Object.keys(f).length > 0;
    return hasFilters ? { report_name: a, filters: f } : { report_name: a };
  }
  return null;
}

// Human-readable form for the panel's context chip. Mirrors the wording
// _prepend_doc_context uses, so the chip and the prompt agree about what the
// agent is looking at.
export function contextLabel(ctx) {
  if (!ctx || typeof ctx !== "object") return "";
  if (ctx.report_name) return `${ctx.report_name} report`;
  if (ctx.doctype && ctx.name) return `${ctx.doctype} ${ctx.name}`;
  if (ctx.doctype) return `the ${ctx.doctype} list`;
  return "";
}
