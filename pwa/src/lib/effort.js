// Effort, the PWA's name for the thinking level. Kept free of Vue so node:test
// runs it without npm deps (CI's PWA lib suite).

// The UI names effort in plain words; the backend wants low/medium/high
// (jarvis.chat.api._ALLOWED_THINKING). One map, so the wire value can never
// drift from the label. Balanced is the default, so it sends no level and the
// agent default (medium) applies, as on the web app.
export const EFFORT = [
	{ value: "Fast", thinking: "low", hint: "Quick replies" },
	{ value: "Balanced", thinking: "", hint: "Default" },
	{ value: "Thorough", thinking: "high", hint: "Deep reasoning" },
];

export const thinkingOf = (effort) => EFFORT.find((e) => e.value === effort)?.thinking ?? "";

// get_chat_ui_settings lists the levels; an empty list means no choice to offer.
export const effortOffered = (ui) =>
	Array.isArray(ui?.thinking_levels) && ui.thinking_levels.length > 0;

// The level a new chat sends. Settings still loading (or failed) keep the pick.
export const sendThinking = (ui, effort) => (ui && !effortOffered(ui) ? "" : thinkingOf(effort));
