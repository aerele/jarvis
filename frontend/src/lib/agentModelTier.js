/**
 * Pure helpers behind the per-agent minimum-model UI (T6): tier badge colours,
 * plain-language requirement/state copy, and provider grouping for the model
 * picker. Kept dependency-free so AgentModelCard / AgentInstallDialog /
 * AgentModelPicker all read the exact same wording and every branch is
 * unit-testable without mounting a component.
 *
 * Mirrors jarvis.chat.agent_models.TIERS ("Standard", "Advanced", "Frontier" -
 * rank = index + 1) purely for display; the server is the only source of
 * truth for what is actually eligible.
 */

const TIER_THEME = { Standard: "gray", Advanced: "blue", Frontier: "green" };

export function tierTheme(tier) {
	return TIER_THEME[tier] || "gray";
}

// "Needs an Advanced model or better." / "No minimum model required."
export function requirementPhrase(tier) {
	if (!tier) return "No minimum model required.";
	const article = /^[aeiou]/i.test(tier) ? "an" : "a";
	return `Needs ${article} ${tier} model or better.`;
}

export function providerLabel(id) {
	const s = String(id || "").trim();
	if (!s) return "Other";
	return s.charAt(0).toUpperCase() + s.slice(1);
}

// [{provider, model, ...}] -> [{provider: "Anthropic", models: [...]}], best-
// first order preserved both across and within groups (the server already
// sorts the flat list best-first; this only partitions it).
export function groupByProvider(models) {
	const order = [];
	const byKey = new Map();
	for (const m of models || []) {
		const key = m && m.provider ? m.provider : "";
		if (!byKey.has(key)) {
			byKey.set(key, []);
			order.push(key);
		}
		byKey.get(key).push(m);
	}
	return order.map((key) => ({ provider: providerLabel(key), models: byKey.get(key) }));
}

// The MODEL CARD's flag-off guard: whether to render the "Model" section at
// all. `info` is a get_agent_model() response (or null while loading/absent).
// Off, or nothing has ever governed this agent (no requirement AND no row
// created yet - e.g. a fresh install of a no-min-model agent), renders
// NOTHING - the hard requirement that flag-off keeps today's UI byte-for-byte.
export function shouldShowModelCard(info) {
	return !!(info && info.enforced && (info.min_model || info.state));
}

// Whether the Install button should open the confirm dialog (vs installing
// straight away, today's behaviour) - flag on AND the listing declares a
// requirement.
export function shouldConfirmInstallModel(info) {
	return !!(info && info.enforced && info.min_model);
}

// "Pending apply" badge: the server already computes pending_apply as chosen
// != pushed for a pinned row: this is a pass-through, not a re-derivation, so
// the SPA never disagrees with what the next Apply would actually push.
export function isPendingApply(info) {
	return !!(info && info.pending_apply);
}

// catalog === "ok" AND an empty model list - "unknown" (unreadable catalog)
// is a DIFFERENT state (we don't yet know, so we must not claim none exist).
export function isNoneEligible(eligible) {
	return !!(eligible && eligible.catalog === "ok" && (eligible.models || []).length === 0);
}

export function isCatalogUnknown(eligible) {
	return !!(eligible && eligible.catalog === "unknown");
}

// The current-model label for the card/dialog header: the enriched choice's
// catalog label, falling back to the bare model id, falling back to a neutral
// phrase for `legacy`/no-row-yet (nothing tenant-pinned to name).
export function currentModelLabel(info) {
	const choice = info && info.choice;
	if (choice && (choice.label || choice.model)) return choice.label || choice.model;
	return "Your default model";
}

// State-banner copy (jarvis#T6): one canned sentence per state, distinct from
// whatever raw `note` the row carries (the backend's note is written for its
// own revalidation log, not this card's copy).
export function stateBannerText(info) {
	const state = info && info.state;
	if (state === "legacy") {
		return "Using your default model — choose one that meets this agent's requirement.";
	}
	if (state === "changed") {
		return `Your chosen model is no longer available, so this agent moved to ${currentModelLabel(
			info
		)}.`;
	}
	if (state === "needs_model") {
		return "This agent is paused: none of your models meet its requirement.";
	}
	return "";
}

export function stateBannerType(info) {
	const state = info && info.state;
	if (state === "needs_model") return "warning";
	if (state === "changed") return "warning";
	if (state === "legacy") return "info";
	return "info";
}

// The secondary "fleet_unresolved / leftover note" banner - only for the
// healthy pinned states (auto/chosen), where the state banner above has
// nothing to say but the row still carries something worth surfacing.
// legacy/changed/needs_model already have their own canned copy above, so
// this never fires for them.
export function shouldShowNoteBanner(info) {
	if (!info) return false;
	const state = info.state;
	if (state === "changed" || state === "needs_model" || state === "legacy") return false;
	return !!(info.fleet_unresolved || info.note);
}

// Plain-language, canApply-aware copy for that banner - NEVER the raw
// backend `note` verbatim. The backend writes that field for its own
// revalidation log (e.g. "Apply catalog changes to finish updating this
// agent", "Platform upgrade pending") and a plain user has no Apply control
// to act on, so this always reduces to one of two sentences instead of
// leaking backend wording a non-reviewer can't do anything with.
export function noteBannerText(info, canApply) {
	if (!shouldShowNoteBanner(info)) return "";
	return canApply
		? "This agent's model setup needs to be applied — apply catalog changes on the Agents page."
		: "An admin needs to apply the latest agent changes.";
}
