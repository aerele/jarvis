// The Business screen's "your notes" count (#747). The count comes from
// getBusinessStatus, not from the list, so a delete has to move it too: the list lost
// the note while the counter still said 1 until a reload. One less, never below 0.
export function statusAfterDelete(status) {
	if (!status) return status;
	const count = Number(status.my_notes) || 0;
	return { ...status, my_notes: Math.max(0, count - 1) };
}
