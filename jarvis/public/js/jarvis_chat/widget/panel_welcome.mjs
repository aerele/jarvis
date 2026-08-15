// The welcome state: a greeting and a few starting points, so an empty panel
// suggests what to do instead of showing a blank box.
//
// Ported from the side-chat design board, minus its visual identity — the
// board's purple accent and decorative animation are off-language per
// design.md 2.2 and 1.3, so only the copy and the layout idea carry over.
//
// Pure: the greeting is a function of the hour, and the prompts are a function
// of what the user is looking at, which makes both unit-testable.

export function greetingFor(hour) {
  const h = Number.isFinite(hour) ? hour : 12;
  if (h < 12) return "Good morning";
  if (h < 17) return "Good afternoon";
  return "Good evening";
}

// `name` is a display name; take the first word so "Good evening, Kavin" reads
// like a person talking rather than a database row.
export function greetingLine(hour, name) {
  const g = greetingFor(hour);
  const first = String(name || "")
    .trim()
    .split(/\s+/)[0];
  return first ? `${g}, ${first}` : g;
}

// Starting points, scoped to whatever the panel can see. On a record the
// generic "which sales orders are overdue" prompt is a worse suggestion than
// one about the record already on screen.
export function suggestionsFor(ctx) {
  const label = ctx && typeof ctx === "object" ? ctx : null;

  if (label && label.doctype && label.name) {
    return [
      {
        title: "Explain this record",
        prompt: "Summarise this record and flag anything unusual.",
      },
      {
        title: "Find what is linked",
        prompt: "What documents are linked to this one?",
      },
      {
        title: "Suggest next steps",
        prompt: "What should I do next with this record?",
      },
    ];
  }

  if (label && (label.doctype || label.report_name)) {
    return [
      {
        title: "Analyse data",
        prompt: "Which of these need my attention first?",
      },
      { title: "Summarise", prompt: "Summarise what I am looking at." },
      {
        title: "Find outliers",
        prompt: "Is anything here unusual or out of place?",
      },
    ];
  }

  return [
    {
      title: "Analyse data",
      prompt: "Which sales orders are overdue this month?",
    },
    { title: "Take an action", prompt: "Draft a document for me to review." },
    { title: "Search records", prompt: "Search for a customer or contact." },
  ];
}
