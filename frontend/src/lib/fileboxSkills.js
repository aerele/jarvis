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

// The picker's options: "None" (system default only) + each accessible skill to
// apply ALONGSIDE the system's chosen skill. The OCR / Data Entry skill is the
// system's own base, so it is not offered as an extra to layer on.
export function skillOptions(customSkills) {
	return [
		{ label: "None", value: "" },
		...(customSkills || []).map((s) => ({ label: skillLabel(s), value: s.skill_name })),
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
