// Drop-time skill-pin helpers for the File Box picker + badge. Pure functions so
// the option/label logic (Auto + OCR + provenance-labelled custom skills, and the
// slug->label mapping the badge reads) is unit-testable without mounting the page.

// The persona fallback skill (no Jarvis Custom Skill row); the backend accepts it
// by literal match. Keep in sync with jarvis/chat/filebox.py OCR_DATA_ENTRY.
export const OCR_DATA_ENTRY = "ocr-data-entry";

// A custom-skill row's picker label, with provenance for anything not the user's own.
export function skillLabel(s) {
	if (s.mine) return s.skill_name;
	if (s.shared_by) return `${s.skill_name} · shared by ${s.shared_by}`;
	if (s.via_role) return `${s.skill_name} · via ${s.via_role}`;
	return `${s.skill_name} · shared`;
}

// The picker's options: "Automatic" (File Box picks the matching skill) + each
// accessible skill File Box may use ("Use in File Box" on) to pin instead.
// The OCR / Data Entry skill is the system's own base, so it is not offered as an
// extra to layer on.
export function skillOptions(customSkills) {
	return [
		{ label: "Automatic", value: "" },
		...(customSkills || [])
			.filter((s) => s.use_in_file_box)
			.map((s) => ({ label: skillLabel(s), value: s.skill_name })),
	];
}

// Slug -> human label for the list badge; a pin whose skill was since removed
// still reads clearly rather than showing a bare slug.
export function pinnedLabel(slug, customSkills) {
	if (!slug) return "";
	if (slug === OCR_DATA_ENTRY) return "OCR / Data Entry";
	const s = (customSkills || []).find((x) => x.skill_name === slug);
	return s ? s.skill_name : `${slug} (removed)`;
}

// The Skills list chip for a skill File Box follows to a fixed document type.
export function createsChip(row) {
	return row && row.use_in_file_box && row.file_box_creates
		? `File Box → ${row.file_box_creates}`
		: "";
}

// The skill editor shows the server's "File Box creates" refusal (the doctype's
// FileBoxCreatesError) on that field; any other error stays a toast.
export function isCreatesError(e) {
	return !!e && e.exc_type === "FileBoxCreatesError";
}

// A promotion request's File Box snapshot as the reviewer's badge ("" when the
// server sent none): approval publishes both fields.
export function promoFileBox(p) {
	if (!p || p.use_in_file_box_snapshot == null) return "";
	const creates = p.file_box_creates_snapshot;
	if (p.use_in_file_box_snapshot) return creates ? `File Box → ${creates}` : "Used in File Box";
	return "Not used in File Box" + (creates ? ` · creates ${creates}` : "");
}
