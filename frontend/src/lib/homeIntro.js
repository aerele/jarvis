// The chat-home introduction (WelcomeAssistantMessage.vue): the two decisions
// that must be testable without mounting ChatView.vue, kept as pure functions.
//
// The bubble is PRESENTATION ONLY. It is not a Jarvis Chat Message, it creates
// no conversation and no session, and it renders strictly inside the existing
// `showWelcome` branch — so an empty conversation stays empty (create_or_focus_
// empty still reuses it, the reaper still collects it, the first send is still
// seq=1) and a conversation that HAS messages (a proactive one included) can
// never draw it.

const DEFAULT_NAME = "Jarvis";

/**
 * Is the introduction due for this user, per the chat UI settings bootstrap?
 *
 * Both numbers come from the server (`get_chat_ui_settings`): `home_intro_
 * version` is what the SPA should render, `home_intro_seen_version` is what
 * this user has already acknowledged. The server OMITS the seen key when it
 * could not read it, and an older backend sends neither — either way the
 * comparison yields false and the ordinary compact hero renders. That is the
 * deliberate fail-quiet direction: repeating the introduction is a nuisance,
 * but re-lecturing an established user because a read blipped is worse.
 *
 * @param {{home_intro_version?: number, home_intro_seen_version?: number}|null|undefined} ui
 */
export function homeIntroDue(ui) {
	if (!ui) return false;
	const current = Number(ui.home_intro_version);
	const seen = Number(ui.home_intro_seen_version);
	if (!Number.isFinite(current) || !Number.isFinite(seen)) return false;
	return current > seen;
}

/**
 * Which persona the bubble RENDERS AS — the avatar, and (through
 * homeIntroSpeaker) the name.
 *
 * `isWhitelabeled` is @/branding's compound flag: true when the tenant has set
 * a custom agent name OR uploaded a logo. Either one makes the brand the
 * identity, so the persona is forced back to the default and JarvisMark draws
 * the tenant's own logo. Without this, a logo-only tenant whose user prefers
 * Jara would get Jara's purple orb where their logo belongs, and (via
 * homeIntroSpeaker, which already defers to the brand) a name that disagrees
 * with the mark beside it. Name and avatar are decided from the same predicate
 * here so they cannot drift apart.
 *
 * `personaEnabled` is the Jarvis Settings kill switch as reported by the boot
 * payload: off means turns answer in the default voice, so the bubble must not
 * present itself as Jara either. `undefined` (an older backend that does not
 * send the key) reads as ON, matching the rest of the SPA.
 *
 * @param {{isWhitelabeled?: boolean, personaEnabled?: boolean, persona?: string}} arg
 */
export function homeIntroPersona({ isWhitelabeled, personaEnabled, persona } = {}) {
	if (isWhitelabeled) return DEFAULT_NAME;
	if (personaEnabled === false) return DEFAULT_NAME;
	return persona === "Jara" ? "Jara" : DEFAULT_NAME;
}

/**
 * Who the bubble is FROM.
 *
 * Two independent identities meet here and the precedence matters:
 *   - the tenant BRAND (`agentName` from @/branding, backed by Jarvis
 *     Settings.agent_name). It is what the agent calls itself in every turn
 *     (turn_handler._assistant_name_clause), so on a whitelabelled workspace it
 *     wins outright — the welcome must never introduce itself as something the
 *     rest of the product never says.
 *   - the per-user PERSONA (Jarvis | Jara), a voice, not a product name. On an
 *     unbranded workspace `agentName` IS "Jarvis", so deferring to the persona
 *     there simply greets a Jara user as Jara — which is the whole point of the
 *     preference.
 *
 * `persona` is expected to be homeIntroPersona()'s output, which has already
 * applied the whitelabel and kill-switch rules; the `isWhitelabeled` check is
 * kept here too so the two can never disagree even if a caller passes a raw
 * preference.
 *
 * @param {{agentName?: string, isWhitelabeled?: boolean, persona?: string}} arg
 */
export function homeIntroSpeaker({ agentName, isWhitelabeled, persona } = {}) {
	const brand = String(agentName || "").trim() || DEFAULT_NAME;
	if (isWhitelabeled) return brand;
	return persona === "Jara" ? "Jara" : brand;
}
