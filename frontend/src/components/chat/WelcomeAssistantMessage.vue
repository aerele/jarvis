<!--
  The first-chat introduction: a static, assistant-styled welcome bubble.

  PRESENTATION ONLY, and the rationale is a data contract, not a preference.
  A persisted assistant row here would (a) un-hide every abandoned empty chat in
  the sidebar, which lists only conversations that have messages, (b) change
  File Box's status derivation, which reads message rows, (c) defeat empty-row
  reuse (create_or_focus_empty) and the empty-conversation reaper, both of which
  key on "zero messages", and (d) put text the model never produced into the
  turn/pump state. So this component renders inside ChatView's existing
  `showWelcome` branch and writes nothing: no Jarvis Chat Message, no
  conversation, no openclaw session, no tokens.

  Consequences of that honesty, all deliberate:
    - no timestamp and no model badge: nothing generated this, so there is
      nothing to stamp it with;
    - no typewriter/streaming animation: it must not impersonate a live turn;
    - no activity/tool row: no tools ran;
    - it is a labelled REGION, not a live region — a screen reader meets it as
      page content on arrival, not as an update being announced.

  Visually it is the same shell as a real assistant message (Message.vue's
  variant="row"): avatar column and 14px/1.6 body, with NO visible name line -
  chat's assistant passes no `sender`, so its rows draw none either, and the
  avatar is the identity. The body rules are copied rather than imported because
  Vue scopes styles per component and Message.vue's are not exported.
-->
<template>
	<section class="jv-wam" data-presentation-only="true" :aria-label="regionLabel">
		<div class="jv-wam-avatar">
			<!-- Persona-consistent mark, mirroring PersonaPill (the only other place
			     the app draws a persona): the default renders the brand mark, which
			     is itself whitelabel-aware (a tenant logo lands HERE); Jara renders
			     her star orb. `persona` arrives already reconciled with the brand by
			     lib/homeIntro.homeIntroPersona, so a whitelabelled workspace can
			     never show Jara's orb where its own logo belongs. -->
			<JarvisMark v-if="persona !== 'Jara'" :size="28" :radius="7" />
			<span v-else class="jv-wam-orb jara" aria-hidden="true">
				<svg viewBox="0 0 24 24" fill="#fff">
					<path d="M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z" />
				</svg>
			</span>
		</div>
		<div class="jv-wam-col">
			<!-- Identity is the avatar, exactly as a real assistant turn renders it
			     (ChatView passes no `sender` to Message.vue, so no name line is drawn
			     there either) - a bold name here would make the introduction look
			     like a different kind of message than every reply that follows.
			     The heading survives visually hidden: it restores the landmark that
			     replacing the hero <h1> removed, so screen-reader heading navigation
			     still lands on this region. Same text as the section's aria-label. -->
			<h2 class="jv-wam-sr">{{ regionLabel }}</h2>
			<div class="jv-wam-body">
				<p>Hi {{ greetingName }} — I'm {{ speaker }}, your AI teammate inside your ERP.</p>
				<p>
					I only see the records your Frappe permissions allow, and by default I propose
					a change and ask you to confirm before it's applied — destructive actions
					always ask.
				</p>
				<p>
					Drop invoices, statements or spreadsheets into
					<strong>File Box</strong> and I'll turn them into drafts you review, and I can
					build dashboards that read your live data.
				</p>
				<p>What would you like to work on?</p>
			</div>
		</div>
	</section>
</template>

<script setup>
import { computed, onMounted } from "vue";
import JarvisMark from "@/components/JarvisMark.vue";

const props = defineProps({
	// Who the bubble is from — the tenant brand or the user's persona, already
	// resolved by lib/homeIntro.homeIntroSpeaker so this component holds no
	// branding/persona precedence logic of its own.
	speaker: { type: String, required: true },
	// "Jarvis" | "Jara", already reconciled with the tenant brand and gated by
	// the persona kill switch (lib/homeIntro.homeIntroPersona). Drives the avatar
	// only; the name comes from `speaker`, which the same resolver pair decides.
	persona: { type: String, default: "Jarvis" },
	firstName: { type: String, default: "" },
});

// A blank/absent first name (a fresh User with no full name) must not render
// "Hi  — I'm …".
const greetingName = computed(() => (props.firstName || "").trim() || "there");
// One string, two consumers (the section's accessible name and the hidden
// heading) so they can never drift apart.
const regionLabel = computed(() => `Welcome message from ${props.speaker}`);

// Best-effort seen-ack, fired once when the bubble actually reaches the DOM.
// The parent owns the request; a failure there is swallowed and simply means
// the introduction may appear again. It must never gate the composer.
const emit = defineEmits(["seen"]);
onMounted(() => emit("seen"));
</script>

<style scoped>
.jv-wam {
	display: flex;
	gap: 12px;
	text-align: left;
	margin: 0 0 26px;
}
.jv-wam-avatar {
	flex: none;
	margin-top: 2px;
}
.jv-wam-col {
	flex: 1;
	min-width: 0;
}
/* Visually hidden, still in the accessibility tree (same recipe as ChatView's
   .jv-sr live region). Not `display: none` / `visibility: hidden`, which would
   take the heading out of the tree along with the pixels. */
.jv-wam-sr {
	position: absolute;
	width: 1px;
	height: 1px;
	margin: -1px;
	padding: 0;
	overflow: hidden;
	clip: rect(0 0 0 0);
	clip-path: inset(50%);
	white-space: nowrap;
	border: 0;
}
.jv-wam-body {
	font-size: 14px;
	line-height: 1.6;
	color: var(--text);
	overflow-wrap: anywhere;
}
.jv-wam-body p {
	margin: 0 0 10px;
}
.jv-wam-body p:last-child {
	margin-bottom: 0;
}
.jv-wam-body strong {
	font-weight: 600;
}

/* Jara's mark, same geometry/gradient as PersonaPill's orb so the two surfaces
   cannot drift. Theme-invariant by construction: a fixed gradient with a white
   glyph reads identically in light and dark, exactly like .jv-mark. */
.jv-wam-orb {
	display: grid;
	place-items: center;
	width: 28px;
	height: 28px;
	border-radius: 7px;
}
.jv-wam-orb.jara {
	background: radial-gradient(
			circle at 34% 30%,
			rgba(255, 255, 255, 0.55),
			rgba(255, 255, 255, 0) 60%
		),
		linear-gradient(140deg, #9d7cea, #6846e3);
}
.jv-wam-orb svg {
	width: 55%;
	height: 55%;
	filter: drop-shadow(0 1px 1px rgba(0, 0, 0, 0.22));
}

@media (max-width: 640px) {
	.jv-wam {
		margin-bottom: 20px;
	}
}
</style>
