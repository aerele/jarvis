<template>
	<!-- Intro product tour (onboarding step 1, chromeless: no step rail).
		 6 slides, TOUR order: Chat, Welcome, Skills, Macros, File Box, Agents
		 (Chat leads with the animated conversation; the mock sidebar nav
		 rendered inside each slide keeps its own SIDEBAR order: Chat, Skills,
		 Macros, File Box, Agents - see NAV_ORDER). Translated from the
		 approved preview (docs/superpowers/specs/onboarding-preview-v6.html);
		 self-contained, only depends on the app palette vars OnboardingView
		 already applies. -->
	<div class="tour">
		<div class="tour-stage">
			<!-- slide 1 · chat -->
			<div v-if="cur === 0" class="slide">
				<div class="slide-copy">
					<span class="eyebrow">Chat</span>
					<h2>Ask anything about your business.</h2>
					<p>
						“Which sales orders are overdue?” “Draft a follow-up to this lead.”
						{{ agentName }}
						pulls the answer straight from ERPNext and shows its work.
					</p>
				</div>
				<div class="mock">
					<div class="mock-bar">
						<i></i><i></i><i></i><span>Chat · Stock &amp; dispatch</span>
					</div>
					<div class="mock-body">
						<div class="m-side" v-html="sideHtml('Chat')"></div>
						<div class="m-main">
							<!-- looping animated conversation: chatStep (script) is a tiny
								 phase clock that toggles which bubbles exist via v-if; the
								 motion itself (typing reveal, caret blink, tool-dot pulse,
								 loop fade) is CSS. .chat-flow is pinned to the mock's fixed
								 content box and bottom-aligned, so new bubbles push older
								 ones past the top edge where overflow:hidden clips them -
								 same as a real scrolled-to-bottom chat - and the mock never
								 grows or jumps. -->
							<div class="chat-anim" :class="{ fading: chatFading }">
								<div class="chat-flow">
									<div class="cb tool" v-if="chatStep === 'tool'">
										<span class="g"></span>run_report · Sales Order + Bin
									</div>
									<div class="cb u" v-if="chatAtLeast('bubble1')">
										For the available stock, which customer orders can I
										dispatch now, who pays me on time, to keep up my cashflow
										in a good state? Analyse and list.
									</div>
									<div class="cb a cb-reply" v-if="chatAtLeast('reply1')">
										<p class="tbl-lead">
											3 orders can ship today, ₹2.4L total. Ranked by how
											fast each customer pays.
										</p>
										<div class="tbl">
											<div class="tbl-row">
												<b>Acme Industries · SO-0142</b>
												<span
													>₹1.1L · 120/120 ready · pays in 12d avg</span
												>
											</div>
											<div class="tbl-row">
												<b>Vertex Traders · SO-0138</b>
												<span
													>₹84,000 · 60/60 ready · pays in 18d avg</span
												>
											</div>
											<div class="tbl-row">
												<b>Sunrise Mills · SO-0151</b>
												<span
													>₹47,500 · 200/200 ready · pays in 9d avg</span
												>
											</div>
										</div>
									</div>
									<div class="cb u" v-if="chatAtLeast('bubble2')">
										Create pick list for first order.
									</div>
									<div
										class="cb a confirm-card"
										v-if="chatStep === 'confirm' || chatStep === 'approve'"
									>
										<div class="confirm-t">
											About to create a Pick List for SO-0142, Acme
											Industries.
										</div>
										<div class="confirm-btns">
											<span
												class="confirm-btn confirm-btn--ok"
												:class="{ pressed: chatStep === 'approve' }"
												>Approve</span
											>
											<span class="confirm-btn">Cancel</span>
										</div>
									</div>
									<div class="cb a cb-done" v-if="chatAtLeast('done')">
										<svg
											width="11"
											height="11"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="2.6"
											stroke-linecap="round"
											stroke-linejoin="round"
										>
											<path d="M20 6 9 17l-5-5" />
										</svg>
										Pick List PICK-0091 created in draft mode.
									</div>
								</div>
								<div class="composer">
									<template v-if="chatStep === 'type1' || chatStep === 'type2'">
										<span
											class="type-line"
											:class="chatStep === 'type1' ? 'type-q1' : 'type-q2'"
											>{{ chatStep === "type1" ? chatQ1 : chatQ2 }}</span
										><span class="type-caret" aria-hidden="true"></span>
									</template>
									<template v-else>Ask a follow-up…</template>
								</div>
							</div>
						</div>
					</div>
				</div>
			</div>

			<!-- slide 2 · welcome -->
			<div v-else-if="cur === 1" class="slide">
				<div class="slide-copy">
					<span class="eyebrow">Welcome</span>
					<h2>Harness AI agents inside your ERPNext.</h2>
					<p>
						{{ agentName }} is an AI teammate that lives in your ERP. Ask a question,
						hand off a task, or let it watch the books, all in plain language.
					</p>
					<ul class="pts">
						<li>
							<svg
								width="15"
								height="15"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="2.4"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path d="M20 6 9 17l-5-5" /></svg
							>Reads your real data, trusting the ledger over guesses
						</li>
						<li>
							<svg
								width="15"
								height="15"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="2.4"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path d="M20 6 9 17l-5-5" /></svg
							>Builds a personalized knowledge base of your business as you work
						</li>
						<li>
							<svg
								width="15"
								height="15"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="2.4"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path d="M20 6 9 17l-5-5" /></svg
							>Respects each user’s Frappe permissions, so everyone sees only what
							they’re allowed to
						</li>
						<li>
							<svg
								width="15"
								height="15"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="2.4"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path d="M20 6 9 17l-5-5" /></svg
							>{{ PERMISSION_TOUR_COPY }}
						</li>
					</ul>
				</div>
				<div class="mock">
					<div class="mock-bar">
						<i></i><i></i><i></i><span>{{ agentName }} · New chat</span>
					</div>
					<div class="mock-body">
						<div class="m-side" v-html="sideHtml('Chat')"></div>
						<div class="m-main">
							<div class="m-welcome">
								<!-- fill is #fff, not var(--surface): the chip is the brand gradient in
								     both themes, so a theme-flipping fill would put a dark star on it. -->
								<div class="m-welcome-mk">
									<svg width="15" height="15" viewBox="0 0 24 24" fill="#fff">
										<path
											d="M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z"
										/>
									</svg>
								</div>
								<div class="m-welcome-hi">Good morning, Priya</div>
								<div class="m-welcome-sub">
									Ask about your ERP data, run a workflow, or draft something.
								</div>
							</div>
							<div class="m-sugg-grid">
								<div class="m-sugg">
									<b class="mi mi-blue"
										><svg
											width="12"
											height="12"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="1.5"
											stroke-linecap="round"
											stroke-linejoin="round"
										>
											<path d="M3 3v18h18" />
											<path d="m19 9-5 5-4-4-3 3" /></svg
									></b>
									<div>
										<i>Analyse data</i><u>Which sales orders are overdue?</u>
									</div>
								</div>
								<div class="m-sugg">
									<b class="mi mi-green"
										><svg
											width="12"
											height="12"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="1.5"
											stroke-linecap="round"
											stroke-linejoin="round"
										>
											<path d="M12 5v14M5 12h14" /></svg
									></b>
									<div><i>Take an action</i><u>Create a new Sales Order</u></div>
								</div>
								<div class="m-sugg">
									<b class="mi mi-amber"
										><svg
											width="12"
											height="12"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="1.5"
											stroke-linecap="round"
											stroke-linejoin="round"
										>
											<circle cx="11" cy="11" r="8" />
											<path d="m21 21-4.35-4.35" /></svg
									></b>
									<div>
										<i>Search records</i><u>Find a customer or contact</u>
									</div>
								</div>
								<div class="m-sugg">
									<b class="mi mi-violet"
										><svg
											width="12"
											height="12"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="1.5"
											stroke-linecap="round"
											stroke-linejoin="round"
										>
											<path
												d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"
											/></svg
									></b>
									<div><i>Draft content</i><u>Follow-up email to a lead</u></div>
								</div>
							</div>
							<div class="composer">
								Ask {{ agentName }}… @ to mention a user, / for a doctype or tool
							</div>
						</div>
					</div>
				</div>
			</div>

			<!-- slide 3 · skills -->
			<div v-else-if="cur === 2" class="slide">
				<div class="slide-copy">
					<span class="eyebrow">Skills</span>
					<h2>It already knows Frappe &amp; ERPNext.</h2>
					<p>
						{{ agentName }} ships with deep skills for every core doctype, and you can
						create custom skills for your domain-specific workflows, so it works the
						way your team already does.
					</p>
				</div>
				<div class="mock">
					<div class="mock-bar"><i></i><i></i><i></i><span>Skills</span></div>
					<div class="mock-body">
						<div class="m-side" v-html="sideHtml('Skills')"></div>
						<div class="m-main">
							<div class="m-row">
								<span class="pill">core</span>
								<div class="t">Customer ledger lookup</div>
								<div class="meta"></div>
							</div>
							<div class="m-row">
								<span class="pill">sales</span>
								<div class="t">Sales order follow-up</div>
								<div class="meta"></div>
							</div>
							<div class="m-row">
								<span class="pill amber">custom</span>
								<div class="t">Invoice data entry</div>
								<div class="meta"></div>
							</div>
							<div class="m-row">
								<span class="pill amber">custom</span>
								<div class="t">GST reconciliation</div>
								<div class="meta"></div>
							</div>
							<div class="m-row m-row-dashed">
								<span class="m-row-cta"
									><svg
										width="11"
										height="11"
										viewBox="0 0 24 24"
										fill="none"
										stroke="currentColor"
										stroke-width="1.5"
										stroke-linecap="round"
										stroke-linejoin="round"
									>
										<path d="M12 5v14M5 12h14" />
									</svg>
									New skill</span
								>
							</div>
						</div>
					</div>
				</div>
			</div>

			<!-- slide 4 · macros -->
			<div v-else-if="cur === 3" class="slide">
				<div class="slide-copy">
					<span class="eyebrow">Macros</span>
					<h2>Turn a routine into one click.</h2>
					<p>
						Save any multi-step job like “month-end close” or “daily AR reminders” as a
						macro. Run it on demand or on a schedule, and watch every run.
					</p>
				</div>
				<div class="mock">
					<div class="mock-bar"><i></i><i></i><i></i><span>Macros · Runs</span></div>
					<div class="mock-body">
						<div class="m-side" v-html="sideHtml('Macros')"></div>
						<div class="m-main">
							<div class="m-row">
								<span class="pill amber">running</span>
								<div class="t">Month-end close</div>
								<div class="meta"></div>
							</div>
							<div class="m-row">
								<span class="pill">done</span>
								<div class="t">Daily AR reminders</div>
								<div class="meta"></div>
							</div>
							<div class="m-row">
								<span class="pill">done</span>
								<div class="t">Sync price list</div>
								<div class="meta"></div>
							</div>
							<div class="m-row">
								<span class="pill">done</span>
								<div class="t">Weekly sales digest</div>
								<div class="meta"></div>
							</div>
						</div>
					</div>
				</div>
			</div>

			<!-- slide 5 · file box -->
			<div v-else-if="cur === 4" class="slide">
				<div class="slide-copy">
					<span class="eyebrow">File Box</span>
					<h2>Drop files in, get clean entries out.</h2>
					<p>
						Upload invoices, bank statements or price lists. {{ agentName }} reads
						them, extracts the details, and drafts the entries in ERPNext. You just
						review and approve.
					</p>
				</div>
				<div class="mock">
					<div class="mock-bar"><i></i><i></i><i></i><span>File Box</span></div>
					<div class="mock-body">
						<div class="m-side" v-html="sideHtml('File Box')"></div>
						<div class="m-main">
							<div class="m-row">
								<span class="pill amber">reading</span
								><span class="fname">INV-ACME-0921.pdf</span>
								<div class="meta"></div>
							</div>
							<div class="m-row">
								<span class="pill">extracted</span
								><span class="fname">bank-stmt-jun.pdf</span>
								<div class="meta"></div>
							</div>
							<div class="m-row">
								<span class="pill">filed</span
								><span class="fname">price-list-q3.xlsx</span>
								<div class="meta"></div>
							</div>
							<div class="m-row m-row-dashed">
								<span class="m-row-cta"
									><svg
										width="11"
										height="11"
										viewBox="0 0 24 24"
										fill="none"
										stroke="currentColor"
										stroke-width="1.5"
										stroke-linecap="round"
										stroke-linejoin="round"
									>
										<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
										<path d="m17 8-5-5-5 5" />
										<path d="M12 3v12" />
									</svg>
									Drop a file or browse</span
								>
							</div>
						</div>
					</div>
				</div>
			</div>

			<!-- slide 6 · agents (final: the Onboard Jarvis CTA lives in the footer's
				 Next slot, so it sits bottom-right like every other slide's advance
				 button; Skip is hidden here). -->
			<div v-else class="slide">
				<div class="slide-copy">
					<span class="eyebrow">Agents</span>
					<h2>Put specialists to work in the background.</h2>
					<p>
						Install expert-built ERPNext agents, or build your own custom agents for
						your team’s workflows. They watch and surface findings before you ask.
					</p>
					<p class="final-call">
						<mark
							>Ready to see it on your data? Onboard {{ agentName }} and explore
							everything hands-on. It takes about two minutes.</mark
						>
					</p>
				</div>
				<div class="mock">
					<div class="mock-bar"><i></i><i></i><i></i><span>Agents</span></div>
					<div class="mock-body">
						<div class="m-side" v-html="sideHtml('Agents')"></div>
						<div class="m-main">
							<div class="m-grid">
								<div class="m-card">
									<div class="ico">
										<svg
											width="11"
											height="11"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="1.5"
											stroke-linecap="round"
											stroke-linejoin="round"
										>
											<circle cx="11" cy="11" r="8" />
											<path d="m21 21-4.35-4.35" />
										</svg>
									</div>
									<div class="nm">Close Auditor</div>
									<div class="ds">
										Read-only period-end integrity checks on your books
									</div>
									<span class="m-inst on">Installed</span>
								</div>
								<div class="m-card">
									<div class="ico">
										<svg
											width="11"
											height="11"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="1.5"
											stroke-linecap="round"
											stroke-linejoin="round"
										>
											<circle cx="12" cy="12" r="8" />
											<path
												d="M14.8 9A2 2 0 0 0 13 8h-2a2 2 0 0 0 0 4h2a2 2 0 0 1 0 4h-2a2 2 0 0 1-1.8-1"
											/>
											<path d="M12 6v2m0 8v2" />
										</svg>
									</div>
									<div class="nm">AR Follow-up</div>
									<div class="ds">
										Chases overdue receivables, drafts reminders
									</div>
									<span class="m-inst">Install</span>
								</div>
								<div class="m-card">
									<div class="ico">
										<svg
											width="11"
											height="11"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="1.5"
											stroke-linecap="round"
											stroke-linejoin="round"
										>
											<rect x="3" y="4" width="18" height="18" rx="2" />
											<path d="M16 2v4M8 2v4M3 10h18" />
										</svg>
									</div>
									<div class="nm">Month-end Close</div>
									<div class="ds">Runs your closing checklist on schedule</div>
									<span class="m-inst">Install</span>
								</div>
								<div class="m-card dashed">
									<div class="ico ico-plain">
										<svg
											width="11"
											height="11"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="1.5"
											stroke-linecap="round"
											stroke-linejoin="round"
										>
											<path d="M12 5v14M5 12h14" />
										</svg>
									</div>
									<div class="nm">Build custom</div>
									<div class="ds">An agent for your own workflow</div>
								</div>
							</div>
						</div>
					</div>
				</div>
			</div>
		</div>

		<div class="tour-foot">
			<div class="dots">
				<button
					v-for="i in SLIDE_COUNT"
					:key="i"
					:class="{ on: cur === i - 1 }"
					:aria-label="`Go to slide ${i}`"
					:aria-current="cur === i - 1 ? 'step' : undefined"
					@click="go(i - 1)"
				></button>
			</div>
			<div class="tour-nav">
				<button v-if="!isLast" class="skip" @click="$emit('skip')">Skip tour</button>
				<button
					class="btn btn--ghost btn--sm"
					:style="{ visibility: cur === 0 ? 'hidden' : 'visible' }"
					@click="step(-1)"
				>
					Back
				</button>
				<button v-if="!isLast" class="btn btn--primary btn--sm" @click="step(1)">
					Next
				</button>
				<button v-else class="btn btn--primary btn--sm" @click="$emit('finish')">
					Onboard {{ agentName }}
				</button>
			</div>
		</div>
	</div>
</template>

<script setup>
import { ref, computed, watch, onUnmounted } from "vue";
import { agentName } from "@/branding";
import { PERMISSION_TOUR_COPY } from "@/onboarding/permissionCopy";

// 'finish' = the final-slide CTA (or advancing past the last slide);
// 'skip' = the footer "Skip tour" link. Both land the wizard on the Plan step.
const emit = defineEmits(["finish", "skip"]);

const SLIDE_COUNT = 6;
const LAST = SLIDE_COUNT - 1;
const cur = ref(0);
const isLast = computed(() => cur.value === LAST);

function go(i) {
	cur.value = Math.max(0, Math.min(LAST, i));
}
function step(d) {
	if (cur.value === LAST && d > 0) {
		emit("finish");
		return;
	}
	go(cur.value + d);
}

// ---- chat slide (cur === 0): looping animated conversation ----
// A tiny phase clock steps through the exchange; every visual (typing
// reveal, caret blink, tool-dot pulse, bubble/card presence) is driven off
// the current step name and rendered as CSS, so this is a state machine,
// not a frame-by-frame animator. It only runs while the Chat slide is
// mounted, and is skipped entirely under prefers-reduced-motion, which
// instead shows the loop's settled last frame with no motion at all.
const CHAT_SEQUENCE = [
	"type1", // composer types the stock/dispatch question
	"bubble1", // question becomes a user bubble
	"tool", // brief tool-running chip
	"reply1", // assistant's ranked customer table
	"type2", // composer types the pick-list follow-up
	"bubble2", // follow-up becomes a user bubble
	"confirm", // confirm card offered
	"approve", // Approve shows a pressed state
	"done", // confirmation line
	"hold", // pause before the loop fades and restarts
];
const CHAT_DURATION_MS = {
	type1: 3200,
	bubble1: 450,
	tool: 900,
	reply1: 2600,
	type2: 1200,
	bubble2: 450,
	confirm: 1100,
	approve: 450,
	done: 2600,
	hold: 2400,
};
const CHAT_INDEX = Object.fromEntries(CHAT_SEQUENCE.map((s, i) => [s, i]));
const chatQ1 =
	"For the available stock, which customer orders can I dispatch now, who pays me on time, to keep up my cashflow in a good state? Analyse and list.";
const chatQ2 = "Create pick list for first order.";
const chatStep = ref(CHAT_SEQUENCE[CHAT_SEQUENCE.length - 1]);
const chatFading = ref(false);
function chatAtLeast(step) {
	return CHAT_INDEX[chatStep.value] >= CHAT_INDEX[step];
}

let chatTimer = null;
let chatIdx = 0;
function clearChatTimer() {
	if (chatTimer) {
		clearTimeout(chatTimer);
		chatTimer = null;
	}
}
function runChatStep() {
	const stepName = CHAT_SEQUENCE[chatIdx];
	chatStep.value = stepName;
	chatTimer = setTimeout(() => {
		chatIdx += 1;
		if (chatIdx >= CHAT_SEQUENCE.length) {
			// loop boundary: fade the whole exchange out, then reset to blank
			chatFading.value = true;
			chatTimer = setTimeout(() => {
				chatIdx = 0;
				chatFading.value = false;
				runChatStep();
			}, 500);
			return;
		}
		runChatStep();
	}, CHAT_DURATION_MS[stepName]);
}
function startChatAnim() {
	clearChatTimer();
	chatIdx = 0;
	chatFading.value = false;
	runChatStep();
}
function prefersReducedMotion() {
	return (
		typeof window !== "undefined" &&
		typeof window.matchMedia === "function" &&
		window.matchMedia("(prefers-reduced-motion: reduce)").matches
	);
}
// Slides use v-if, so leaving the Chat slide destroys its DOM - but the
// timer above is JS state on this component, not the DOM, so it needs its
// own stop here too, on every slide change and again on unmount.
watch(
	cur,
	(slide) => {
		if (slide === 0 && !prefersReducedMotion()) {
			startChatAnim();
		} else {
			clearChatTimer();
			chatStep.value = CHAT_SEQUENCE[CHAT_SEQUENCE.length - 1];
			chatFading.value = false;
		}
	},
	{ immediate: true }
);
onUnmounted(clearChatTimer);

// ---- mock sidebar: mirrors the REAL app sidebar (brand + user, New Chat,
// Search Chat, feather-icon nav, Recent chats), rendered from data exactly
// like the preview's NAV_ICONS renderer. Static trusted strings only.
const FI = (d, size = 11) =>
	`<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
const NAV_ICONS = {
	Chat: '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>',
	Skills: '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
	Macros: '<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>',
	"File Box":
		'<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
	Agents: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/>',
};
const NAV_ORDER = ["Chat", "Skills", "Macros", "File Box", "Agents"];

function sideHtml(active) {
	return (
		`<div class="m-brand"><span class="d"><svg width="10" height="10" viewBox="0 0 24 24" fill="#fff"><path d="M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z"/></svg></span><span class="col"><b>${agentName}</b><small>Administrator</small></span></div>` +
		`<div class="m-act">${FI('<path d="M12 5v14M5 12h14"/>')}New Chat</div>` +
		`<div class="m-act">${FI(
			'<circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>'
		)}Search Chat</div>` +
		NAV_ORDER.map(
			(n) => `<div class="m-nav${n === active ? " on" : ""}">${FI(NAV_ICONS[n])}${n}</div>`
		).join("") +
		'<div class="m-recent">Recent chats</div>' +
		'<div class="m-recent-item">Overdue sales orders</div>'
	);
}
</script>

<style scoped>
/* Tour panel height is standardized with the wizard steps (tour = the tallest,
   ~624px) so the dialog never jumps between steps; relaxed on mobile. */
.tour {
	position: relative;
	min-height: 604px;
	display: grid;
	grid-template-rows: 1fr auto;
	color: var(--text);
}
.tour-stage {
	position: relative;
	overflow: hidden;
	display: flex;
	flex-direction: column;
}
.slide {
	flex: 1;
	display: grid;
	padding: 40px 44px 8px;
	grid-template-columns: 1fr 1.15fr;
	gap: 36px;
	align-items: center;
	min-height: 472px;
	animation: jvTourFade 0.3s ease;
}
@keyframes jvTourFade {
	from {
		opacity: 0;
		transform: translateY(6px);
	}
	to {
		opacity: 1;
		transform: none;
	}
}
@media (prefers-reduced-motion: reduce) {
	.slide {
		animation: none;
	}
	.dots button {
		transition: none;
	}
	.btn {
		transition: none;
	}
	/* belt-and-braces: the chatStep clock in <script setup> already never
	   starts under this preference, so these never actually mount, but they
	   stay off here too in case that ever changes. */
	.chat-anim {
		transition: none;
	}
	.type-caret {
		animation: none;
		opacity: 1;
	}
	.cb.tool .g {
		animation: none;
	}
	.confirm-btn--ok {
		transition: none;
	}
}

/* keyboard focus */
button:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 2px;
}

/* ---- copy column ---- */
.slide-copy .eyebrow {
	display: inline-flex;
	align-items: center;
	gap: 7px;
	font-size: 12px;
	font-weight: 500;
	color: var(--text-2);
	background: var(--surface-2);
	border-radius: 99px;
	padding: 4px 11px;
	margin-bottom: 16px;
}
.slide-copy h2 {
	font-size: 30px;
	font-weight: 680;
	line-height: 1.12;
	letter-spacing: -0.02em;
	margin: 0 0 12px;
	text-wrap: balance;
}
.slide-copy p {
	font-size: 15px;
	line-height: 1.55;
	color: var(--text-2);
	margin: 0;
	max-width: 42ch;
}
.slide-copy .pts {
	list-style: none;
	margin: 18px 0 0;
	padding: 0;
	display: grid;
	gap: 9px;
}
.slide-copy .pts li {
	display: flex;
	gap: 9px;
	align-items: flex-start;
	font-size: 13.5px;
	color: var(--text-2);
}
.slide-copy .pts svg {
	color: var(--green);
	flex: none;
	margin-top: 1px;
}
/* highlighted closing invitation on the final slide */
.final-call {
	margin-top: 18px !important;
	font-size: 14.5px !important;
	line-height: 1.7 !important;
}
.final-call mark {
	background: var(--surface-2);
	color: var(--text);
	padding: 3px 7px;
	border-radius: 6px;
	-webkit-box-decoration-break: clone;
	box-decoration-break: clone;
	font-weight: 550;
}

/* ---- buttons (local to the tour; the wizard steps have their own) ---- */
.btn {
	display: inline-flex;
	align-items: center;
	justify-content: center;
	gap: 7px;
	height: 36px;
	padding: 0 16px;
	border-radius: 8px;
	border: 1px solid transparent;
	font-family: inherit;
	font-size: 13.5px;
	font-weight: 500;
	line-height: 1;
	cursor: pointer;
	white-space: nowrap;
	transition: background-color 0.15s ease, color 0.15s ease, border-color 0.15s ease;
}
.btn--primary {
	background: var(--text);
	border-color: var(--text);
	color: var(--surface);
}
.btn--primary:hover {
	background: var(--text-2);
	border-color: var(--text-2);
}
.btn--ghost {
	background: var(--surface-2);
	border-color: transparent;
	color: var(--text);
}
.btn--ghost:hover {
	background: var(--surface-3);
	color: var(--text);
}
.btn--sm {
	height: 32px;
	padding: 0 12px;
	font-size: 12.5px;
	border-radius: 8px;
}
.btn--lg {
	height: 38px;
	padding: 0 20px;
	font-size: 14px;
	border-radius: 8px;
}

/* ---- mock "device" framing each product surface ---- */
.mock {
	border: 1px solid var(--border);
	border-radius: 14px;
	background: var(--surface-1);
	overflow: hidden;
	box-shadow: 0 0 1px rgba(0, 0, 0, 0.2), 0 12px 24px -6px rgba(0, 0, 0, 0.08);
	aspect-ratio: 16/11;
}
.mock-bar {
	display: flex;
	align-items: center;
	gap: 6px;
	padding: 9px 12px;
	border-bottom: 1px solid var(--border);
	background: var(--surface);
}
.mock-bar i {
	width: 9px;
	height: 9px;
	border-radius: 50%;
	background: var(--border-2);
}
.mock-bar span {
	margin-left: 8px;
	font-size: 11px;
	color: var(--text-3);
}
.mock-body {
	display: flex;
	height: calc(100% - 39px);
}
.m-main {
	flex: 1;
	padding: 14px;
	overflow: hidden;
	position: relative;
}

/* the sidebar itself is injected via v-html → style through :deep() */
.m-side {
	width: 34%;
	max-width: 150px;
	background: var(--surface-1);
	border-right: 1px solid var(--border);
	padding: 9px 8px;
	display: flex;
	flex-direction: column;
	gap: 2px;
	overflow: hidden;
}
.m-side :deep(.m-brand) {
	display: flex;
	align-items: center;
	gap: 7px;
	padding: 2px 6px 8px;
}
.m-side :deep(.m-brand .d) {
	width: 18px;
	height: 18px;
	border-radius: 5px;
	background: var(--brand-grad);
	display: grid;
	place-items: center;
	flex: none;
}
.m-side :deep(.m-brand .col) {
	display: flex;
	flex-direction: column;
	line-height: 1.15;
	min-width: 0;
}
.m-side :deep(.m-brand b) {
	font-size: 10.5px;
}
.m-side :deep(.m-brand small) {
	font-size: 8px;
	color: var(--text-3);
}
.m-side :deep(.m-act) {
	display: flex;
	align-items: center;
	gap: 7px;
	padding: 4px 7px;
	font-size: 10px;
	color: var(--text-2);
	white-space: nowrap;
}
.m-side :deep(.m-act svg) {
	flex: none;
	color: var(--text-3);
}
.m-side :deep(.m-nav) {
	display: flex;
	align-items: center;
	gap: 7px;
	padding: 4.5px 7px;
	border-radius: 6px;
	font-size: 10.5px;
	color: var(--text-2);
	white-space: nowrap;
}
.m-side :deep(.m-nav.on) {
	background: var(--surface-3);
	color: var(--text);
	font-weight: 600;
}
.m-side :deep(.m-nav svg) {
	flex: none;
	color: var(--text-3);
}
.m-side :deep(.m-nav.on svg) {
	color: var(--text);
}
.m-side :deep(.m-recent) {
	font-size: 8px;
	color: var(--text-3);
	padding: 7px 7px 0;
}
.m-side :deep(.m-recent-item) {
	font-size: 9.5px;
	color: var(--text-2);
	padding: 4px 7px 0;
	white-space: nowrap;
	overflow: hidden;
	text-overflow: ellipsis;
}

/* suggestion-card icon tints (semantic, replaces inline emoji colors) */
.m-sugg .mi {
	display: inline-flex;
}
.m-sugg .mi-blue {
	color: var(--link);
}
.m-sugg .mi-green {
	color: var(--green);
}
.m-sugg .mi-amber {
	color: var(--amber);
}
.m-sugg .mi-violet {
	color: var(--text-3);
}
.m-row-cta {
	display: inline-flex;
	align-items: center;
	gap: 5px;
}
.m-row-cta svg {
	color: var(--text-3);
}

/* ---- welcome mock ---- */
.m-welcome {
	text-align: center;
	padding-top: 10px;
}
/* The mock's welcome mark must show what the real welcome screen shows: the
   brand gradient. It was `var(--text)` — near-black in light, near-white in dark
   — so the tour advertised a mark the product doesn't have. */
.m-welcome-mk {
	width: 30px;
	height: 30px;
	border-radius: 8px;
	background: var(--brand-grad);
	display: grid;
	place-items: center;
	margin: 0 auto 8px;
}
.m-welcome-hi {
	font-size: 12.5px;
	font-weight: 650;
	margin-bottom: 3px;
}
.m-welcome-sub {
	font-size: 9.5px;
	color: var(--text-3);
}
.m-sugg-grid {
	display: grid;
	grid-template-columns: 1fr 1fr;
	gap: 7px;
	margin: 12px auto 0;
	max-width: 290px;
}
.m-sugg {
	display: flex;
	gap: 8px;
	align-items: flex-start;
	border: 1px solid var(--border);
	border-radius: 9px;
	background: var(--surface);
	padding: 8px 9px;
}
.m-sugg b {
	font-size: 11px;
	font-style: normal;
	flex: none;
	line-height: 1.3;
}
.m-sugg i {
	display: block;
	font-size: 9.5px;
	font-weight: 600;
	font-style: normal;
	color: var(--text);
	margin-bottom: 2px;
}
.m-sugg u {
	display: block;
	font-size: 8.5px;
	color: var(--text-3);
	text-decoration: none;
	line-height: 1.3;
}

/* ---- chat mock ---- */
.cb {
	max-width: 74%;
	padding: 8px 11px;
	border-radius: 12px;
	font-size: 11.5px;
	line-height: 1.4;
	margin-bottom: 9px;
}
/* --cta/--cta INVERTS by theme (near-black light, near-white dark), so the
   foreground must come from its paired token. A hard-coded #fff here rendered
   white-on-near-white (1.18:1) in dark mode after #294 repointed the fill. */
.cb.u {
	margin-left: auto;
	background: var(--cta);
	color: var(--cta-fg);
	border-bottom-right-radius: 4px;
}
.cb.a {
	background: var(--surface-2);
	color: var(--text-2);
	border: 1px solid var(--border);
	border-bottom-left-radius: 4px;
}
.cb.tool {
	display: inline-flex;
	align-items: center;
	gap: 6px;
	font-size: 10.5px;
	color: var(--text-3);
	background: var(--surface-1);
	border: 1px solid var(--border);
	border-radius: 8px;
	padding: 4px 9px;
	margin-bottom: 9px;
	max-width: none;
}
.cb.tool .g {
	width: 6px;
	height: 6px;
	border-radius: 50%;
	background: var(--green);
	animation: jvDotPulse 1.1s ease-in-out infinite;
}
@keyframes jvDotPulse {
	0%,
	100% {
		opacity: 1;
	}
	50% {
		opacity: 0.35;
	}
}
.composer {
	position: absolute;
	left: 14px;
	right: 14px;
	bottom: 12px;
	height: 30px;
	border: 1px solid var(--border-2);
	border-radius: 9px;
	background: var(--surface);
	display: flex;
	align-items: center;
	padding: 0 10px;
	font-size: 10px;
	color: var(--text-3);
	white-space: nowrap;
	overflow: hidden;
}

/* ---- chat mock: looping animated conversation ----
   .chat-anim fades the whole exchange out at the loop boundary (script
   toggles chatFading). .chat-flow is pinned to the mock's fixed content
   box (top 0, bottom above the composer) and bottom-aligned, so as bubbles
   accumulate the oldest ones get pushed past the top edge, where
   overflow: hidden clips them - the same way a real scrolled-to-bottom chat
   behaves. Nothing here changes the mock's total height. */
.chat-anim {
	height: 100%;
	transition: opacity 0.45s ease;
	opacity: 1;
}
.chat-anim.fading {
	opacity: 0;
}
.chat-flow {
	position: absolute;
	top: 0;
	left: 0;
	right: 0;
	bottom: 50px;
	display: flex;
	flex-direction: column;
	justify-content: flex-end;
	overflow: hidden;
}
.cb-reply .tbl-lead {
	margin: 0 0 7px;
	font-weight: 600;
	color: var(--text);
}
.tbl {
	display: grid;
	gap: 6px;
}
.tbl-row b {
	display: block;
	font-size: 10px;
	font-weight: 600;
	color: var(--text);
}
.tbl-row span {
	display: block;
	font-size: 9px;
	color: var(--text-3);
	margin-top: 1px;
}
.confirm-t {
	margin-bottom: 8px;
}
.confirm-btns {
	display: flex;
	gap: 8px;
}
.confirm-btn {
	display: inline-flex;
	align-items: center;
	padding: 4px 10px;
	border-radius: 6px;
	font-size: 9.5px;
	font-weight: 600;
	border: 1px solid var(--border-2);
	color: var(--text-2);
	background: var(--surface);
}
.confirm-btn--ok {
	border-color: var(--cta);
	background: var(--cta);
	color: var(--cta-fg);
	transition: transform 0.15s ease, filter 0.15s ease;
}
.confirm-btn--ok.pressed {
	transform: scale(0.93);
	filter: brightness(0.88);
}
.cb-done {
	display: flex;
	align-items: center;
	gap: 6px;
}
.cb-done svg {
	color: var(--green);
	flex: none;
}

/* typing composer: a clip-path reveal reads as characters appearing
   without depending on font metrics (a ch-width reveal would, since ch is
   only exact for monospace); the caret just blinks alongside it rather
   than tracking the reveal edge pixel-for-pixel. */
.type-line {
	display: inline-block;
	max-width: 100%;
	overflow: hidden;
	white-space: nowrap;
	vertical-align: bottom;
	clip-path: inset(0 100% 0 0);
	animation-name: jvTypeReveal;
	animation-fill-mode: forwards;
}
.type-q1 {
	animation-duration: 3.2s;
	animation-timing-function: steps(46, end);
}
.type-q2 {
	animation-duration: 1.1s;
	animation-timing-function: steps(20, end);
}
@keyframes jvTypeReveal {
	to {
		clip-path: inset(0 0 0 0);
	}
}
.type-caret {
	display: inline-block;
	width: 1.5px;
	height: 10px;
	margin-left: 2px;
	background: var(--text-2);
	vertical-align: -1px;
	animation: jvCaretBlink 0.9s steps(1, end) infinite;
}
@keyframes jvCaretBlink {
	50% {
		opacity: 0;
	}
}

/* ---- rows mock (skills / macros / file box) ---- */
.m-row {
	display: flex;
	align-items: center;
	gap: 9px;
	padding: 8px 9px;
	border: 1px solid var(--border);
	border-radius: 8px;
	background: var(--surface);
	margin-bottom: 7px;
}
.m-row .pill {
	font-size: 9px;
	font-weight: 600;
	padding: 2px 7px;
	border-radius: 99px;
	background: var(--green-bg);
	color: var(--green);
	border: 1px solid var(--green-bd);
	flex: none;
}
.m-row .pill.amber {
	background: var(--amber-bg);
	color: var(--amber);
	border-color: var(--amber-bd);
}
.m-row .t {
	flex: 1;
	min-width: 0;
	font-size: 10px;
	font-weight: 600;
	color: var(--text);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.m-row .fname {
	font-family: ui-monospace, "SF Mono", Menlo, monospace;
	font-size: 9.5px;
	color: var(--text-2);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.m-row .meta {
	margin-left: auto;
	height: 6px;
	width: 52px;
	border-radius: 4px;
	background: var(--surface-2);
	flex: none;
}
.m-row-dashed {
	border-style: dashed;
	justify-content: center;
}
.m-row-cta {
	font-size: 10px;
	font-weight: 600;
	color: var(--text-2);
}

/* ---- agents mock ---- */
.m-grid {
	display: grid;
	grid-template-columns: 1fr 1fr;
	gap: 9px;
}
.m-card {
	border: 1px solid var(--border);
	border-radius: 9px;
	background: var(--surface);
	padding: 10px;
}
.m-card .ico {
	width: 22px;
	height: 22px;
	border-radius: 7px;
	background: var(--cta-bg);
	border: 1px solid var(--cta-bd);
	margin-bottom: 7px;
	display: grid;
	place-items: center;
	font-size: 11px;
}
.m-card .ico-plain {
	background: var(--surface-2);
	border-color: var(--border);
}
.m-card .nm {
	font-size: 10.5px;
	font-weight: 600;
	color: var(--text);
	margin-bottom: 2px;
}
.m-card .ds {
	font-size: 8.5px;
	color: var(--text-3);
	line-height: 1.35;
}
.m-card.dashed {
	border-style: dashed;
	display: flex;
	flex-direction: column;
	align-items: center;
	justify-content: center;
	text-align: center;
	gap: 4px;
}
.m-inst {
	display: inline-block;
	margin-top: 8px;
	font-size: 9px;
	font-weight: 600;
	padding: 3px 8px;
	border-radius: 6px;
	background: var(--text);
	color: var(--surface);
}
.m-inst.on {
	background: var(--green-bg);
	color: var(--green);
	border: 1px solid var(--green-bd);
}

/* ---- footer: dots + nav ---- */
.tour-foot {
	display: flex;
	align-items: center;
	justify-content: space-between;
	padding: 16px 44px 26px;
	border-top: 1px solid var(--border);
	background: var(--surface);
}
.dots {
	display: flex;
	gap: 7px;
}
.dots button {
	width: 8px;
	height: 8px;
	border-radius: 99px;
	border: none;
	background: var(--border-2);
	cursor: pointer;
	padding: 0;
	transition: width 0.2s, background 0.2s;
}
.dots button.on {
	width: 22px;
	background: var(--text);
}
.tour-nav {
	display: flex;
	gap: 10px;
	align-items: center;
}
.skip {
	font-size: 12.5px;
	color: var(--text-3);
	background: none;
	border: none;
	cursor: pointer;
	font-family: inherit;
}
.skip:hover {
	color: var(--text-2);
}

@media (max-width: 820px) {
	.tour {
		min-height: 0;
	}
	.slide {
		grid-template-columns: 1fr;
		gap: 20px;
		padding: 26px 24px 4px;
		min-height: 0;
	}
	.slide-copy {
		order: 2;
	}
	.mock {
		order: 1;
	}
	.tour-foot {
		padding: 14px 22px 20px;
	}
}
</style>
