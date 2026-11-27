// The composer's "Use a skill" pick (ComposerPlusMenu) rides on the message as the same
// "/skill_name" token typing "/" inserts. The server finds it with the pattern below
// (jarvis.chat.custom_skills._INVOKE_RE: at the start or after whitespace), checks the
// user may use that skill, and tells the agent to load it for the turn.
const TOKEN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

function escapeRe(s) {
	return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// True when `text` already invokes `slug` (typed by hand, or a resend of a message that
// carries it), so the token is never added twice.
export function hasSkillToken(text, slug) {
	if (!slug) return false;
	return new RegExp(`(?:^|\\s)/${escapeRe(slug)}(?=$|\\s|[.,;:!?)])`).test(text || "");
}

// `text` with the skill's token in front: "/slug question", or just "/slug" when the
// user wrote nothing else (the skill alone is the request). A slug the server could not
// resolve (not a token) leaves the text unchanged.
export function withSkillToken(text, slug) {
	const body = (text || "").trim();
	if (!slug || !TOKEN.test(slug) || hasSkillToken(body, slug)) return body;
	return body ? `/${slug} ${body}` : `/${slug}`;
}
