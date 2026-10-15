// The words the Skills Admin tab and its dialogs use for a row. Plain words: the
// server's names for these (scope Org, "armed") never reach the screen.

export const WRITES_HELP = "The assistant may write without asking you to confirm each time.";
// Why an admin's own skill shows fewer actions.
export const OWN_HELP = "You cannot hold or release your own skill.";

export const isCompany = (row) => row.scope === "Org";
export const isShared = (row) => row.scope === "Role" || row.scope === "Org";

export const enabledLabel = (on) => (on ? "On" : "Off");

// The badge: who can use the skill.
export function audienceLabel(row) {
	if (row.scope === "Org") return "Everyone";
	if (row.scope === "Role") return row.target_role ? `Role: ${row.target_role}` : "Role";
	return "Private";
}

// The Audience lane's text: a private skill says who it is shared with.
export function audienceDetail(row) {
	if (row.scope === "Org") return "Everyone on this site";
	if (row.scope === "Role") {
		return row.target_role ? `Everyone with the role ${row.target_role}` : "A role";
	}
	const n = Number(row.shared_with_count) || 0;
	return n ? `Shared with ${n} ${n === 1 ? "person" : "people"}` : "Not shared";
}

// "Asha Rao (asha@example.com)", or the address alone when that is all there is.
export function ownerText(row) {
	const full = row.owner_name;
	return full && full !== row.owner ? `${full} (${row.owner})` : row.owner;
}

// "Asha Rao", or the address.
export const ownerName = (row) => row.owner_name || row.owner;

export const skillLabel = (row) => row.skill_name || row.name;

// "3 macros use it (1 of them writes without asking).", or "No macro uses it."
export function macroUse(used) {
	const total = Number((used || {}).total) || 0;
	if (!total) return "No macro uses it.";
	const writes = Number(used.armed) || 0;
	return (
		`${total} macro${total === 1 ? " uses" : "s use"} it` +
		(writes ? ` (${writes} of them write${writes === 1 ? "s" : ""} without asking).` : ".")
	);
}

// What an act is called in the History table. The server names the verb; one this
// list does not know is shown as sent.
const VERBS = {
	hold: "Put on hold",
	release: "Released",
	delete: "Deleted",
};
export const verbText = (verb) => VERBS[String(verb || "").toLowerCase()] || String(verb || "");
