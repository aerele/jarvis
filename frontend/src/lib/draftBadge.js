// The badge in the header of a draft's Preview and of its edit form: one text per verb, so the two
// sheets of one draft say the same thing in the same case. The desktop keeps its long-standing words
// ("Draft - not saved", "Pending change"); the phone has no dash in its voice: "Draft, not saved".
// Pure, relative imports only (the phone's sheet and the shared Preview both import it).
export function draftBadgeText(isUpdate, surface = "desktop") {
	if (isUpdate) return "Pending change";
	return surface === "phone" ? "Draft, not saved" : "Draft - not saved";
}
