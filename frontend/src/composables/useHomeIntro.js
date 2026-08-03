// The chat-home introduction's boot/latch/ack transition, extracted from
// ChatView.vue so it can be behaviour-tested without mounting the ~12k-line
// view. It owns ONLY the intro state seam — nothing else about ChatView.
//
// What it does NOT change (the architecture is approved and preserved):
//   - the bubble is presentation only (WelcomeAssistantMessage.vue); this
//     composable persists nothing but the best-effort server "seen" ack;
//   - it renders strictly inside ChatView's existing `showWelcome` branch, so
//     an empty conversation stays empty and a conversation with messages (a
//     proactive one included) never draws it;
//   - the ack is fire-and-forget and must never gate the composer.
//
// Dependencies are passed as refs/getters so the composable is pure w.r.t.
// ChatView and testable with plain refs:
//   showWelcome    Ref<boolean>   — the empty-state branch is active
//   booting        Ref<boolean>   — initial conversation load in flight
//   visibleCount   () => number   — visibleMessages.length
//   ui             Ref<object>    — the get_chat_ui_settings boot payload
//   isWhitelabeled boolean        — @/branding compound flag (name OR logo)
//   agentName      string         — @/branding tenant brand name
//   getPersona     () => string   — the user's preferred persona (store)
//   markSeen       (v) => Promise — api.markHomeIntroSeen (injected for tests)
//   emitTelemetry  (event, data)  — bounded UI telemetry sink (optional)
import { ref, computed, watch } from "vue";

import {
	homeIntroDue,
	homeIntroPersona as resolveHomeIntroPersona,
	homeIntroSpeaker,
} from "@/lib/homeIntro";

// Coarse, privacy-bounded buckets for time-from-display-to-first-prompt. A
// bucket label ships, never a raw duration, so no individual session timing is
// reconstructable from the telemetry.
export function timeToPromptBucket(ms) {
	if (!Number.isFinite(ms) || ms < 0) return "unknown";
	if (ms < 5000) return "0-5s";
	if (ms < 15000) return "5-15s";
	if (ms < 60000) return "15-60s";
	if (ms < 300000) return "1-5m";
	return "5m+";
}

export function useHomeIntro({
	showWelcome,
	booting,
	visibleCount,
	ui,
	isWhitelabeled,
	agentName,
	getPersona,
	markSeen,
	emitTelemetry,
} = {}) {
	const homeIntroPending = ref(false);
	const homeIntroVersion = ref(0);
	// Session-global latches: the ack fires once, and time-to-first-prompt is
	// noted once, per ChatView lifetime (a full page bootstrap resets them).
	let _acked = false;
	let _displayedAt = 0;
	let _firstPromptNoted = false;

	const showHomeIntro = computed(() => showWelcome.value && homeIntroPending.value);

	// Persona drives the avatar and (on an unbranded workspace) the name. Both
	// the whitelabel and kill-switch rules live in one resolver so the mark and
	// the name are decided from the same predicate — a logo-only tenant gets its
	// own logo, never Jara's orb beside a brand name.
	const homeIntroPersona = computed(() =>
		resolveHomeIntroPersona({
			isWhitelabeled,
			personaEnabled: ui && ui.value ? ui.value.persona_enabled : undefined,
			persona: typeof getPersona === "function" ? getPersona() : undefined,
		})
	);
	const homeIntroSpeakerName = computed(() =>
		homeIntroSpeaker({ agentName, isWhitelabeled, persona: homeIntroPersona.value })
	);

	// Decided ONCE from the boot payload (both numbers land in `ui` before
	// booting flips false, so the bubble can never flash in or out).
	function initFromBoot() {
		const u = (ui && ui.value) || {};
		homeIntroVersion.value = Number(u.home_intro_version) || 0;
		homeIntroPending.value = homeIntroDue(u);
	}

	function _tel(event, payload) {
		if (typeof emitTelemetry !== "function") return;
		try {
			emitTelemetry(event, payload || {});
		} catch (e) {
			/* telemetry must never affect chat */
		}
	}

	// The intro retiring because a real message appeared on the empty home IS
	// the first accepted prompt for this session. Guarded by _displayedAt so
	// opening an existing chat (intro never displayed, _displayedAt still 0)
	// records nothing — and a genuinely-new user, who is the intro's audience,
	// has no prior chats to open, so the confound population is essentially
	// empty.
	function _noteFirstPrompt() {
		if (_firstPromptNoted || !_displayedAt) return;
		_firstPromptNoted = true;
		_tel("first_prompt", {
			version: homeIntroVersion.value,
			bucket: timeToPromptBucket(Date.now() - _displayedAt),
		});
	}

	// `!booting`: the boot restore of the last conversation must NOT count as
	// "the user has moved on" — otherwise an existing user (seen version 0, or a
	// later version bump) would retire the introduction before ever reaching the
	// empty home it renders on, and would never see it at all.
	watch(
		() => visibleCount(),
		(n) => {
			if (booting.value || n <= 0) return;
			if (homeIntroPending.value) {
				_noteFirstPrompt();
				homeIntroPending.value = false;
			}
		}
	);

	// Best-effort seen-ack + the "displayed" telemetry, fired once when the
	// bubble actually reaches the DOM. A failed ack only means the introduction
	// may appear again on a later page load; it must never stand between the
	// user and the composer, so the request is fire-and-forget.
	function ackHomeIntro() {
		if (_acked) return;
		_acked = true;
		_displayedAt = Date.now();
		_tel("displayed", { version: homeIntroVersion.value });
		if (typeof markSeen === "function") {
			try {
				Promise.resolve(markSeen(homeIntroVersion.value)).catch(() => {});
			} catch (e) {
				/* a synchronous throw from the transport must not reach the UI */
			}
		}
	}

	// A welcome suggestion card was chosen. Category token only — never the
	// prompt text or any user content.
	function noteSuggestionSelected(category) {
		_tel("suggestion_selected", { category: String(category || "").slice(0, 40) });
	}

	return {
		homeIntroPending,
		homeIntroVersion,
		showHomeIntro,
		homeIntroPersona,
		homeIntroSpeakerName,
		initFromBoot,
		ackHomeIntro,
		noteSuggestionSelected,
	};
}
