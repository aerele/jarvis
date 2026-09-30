// What the composer's auto-mode toggle shows (#581). Pure and import-free so it
// is unit-tested with node:test. A chat that already has auto mode always shows
// it, locked; otherwise the toggle is only offered while the chat is still empty
// (auto mode can only be chosen with a chat's first message).
export function autoModeView({ messageCount, convAutoMode, armed }) {
	if (convAutoMode) return { visible: true, on: true, locked: true };
	if (messageCount === 0) return { visible: true, on: !!armed, locked: false };
	return { visible: false, on: false, locked: false };
}
