// Per-chat auto mode (#581): what the composer's Auto toggle shows.
//
// Auto mode is chosen with a chat's FIRST message and can never be turned off,
// so the toggle has three shapes: hidden (the chat already has messages and is
// not in auto mode, so there is nothing to choose), an editable pre-send toggle
// (`armed` is the local choice), and a locked "on" once the server says the chat
// is in auto mode. The server value always wins over the local `armed` flag.
//
// Pure and import-free so the SPA and its tests share one definition.
export function autoModeView({ messageCount, convAutoMode, armed } = {}) {
	if (convAutoMode) return { visible: true, on: true, locked: true };
	if (!messageCount) return { visible: true, on: !!armed, locked: false };
	return { visible: false, on: false, locked: false };
}

// Exact copy (plan Global Constraints); shared by the composer and Settings.
export const AUTO_MODE_COPY = {
	tipOff: "Auto mode: apply changes without asking in this chat",
	tipArmed: "Auto mode is on for this chat. Click to turn it off before you send.",
	tipLocked: "Auto mode is on for this chat. Deletes, cancels and amends still ask.",
	label: "Auto",
	dialogTitle: "Turn on auto mode?",
	dialogMessage:
		"Jarvis will apply changes without asking first. Deletes, cancels and amends still ask. Once a chat starts in auto mode, it stays in auto mode.",
	// Shown on its own line, highlighted.
	dialogWarning: "Not recommended if you're new to ERPNext or Jarvis.",
	dialogConfirm: "Turn on auto mode",
	dialogCancel: "Cancel",
	settingsTitle: "Start new chats in auto mode",
	settingsHelp: "Jarvis applies changes without asking. Deletes, cancels and amends still ask.",
};
