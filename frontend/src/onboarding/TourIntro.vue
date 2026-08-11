<template>
	<!-- Intro product tour (onboarding step 1, chromeless: no step rail).
		 5 slides, TOUR order: Welcome, Skills & Knowledge, Macros, File Box,
		 Dashboards (Welcome opens on what Jarvis does for the business - an ERP
		 teammate, not a generic chatbot - then each slide walks one product
		 surface a new user will actually use. The mock sidebar nav rendered
		 inside every slide mirrors the REAL app sidebar (see NAV_ORDER /
		 NAV_GROUPS): File Box, Approval Board, Dashboard, Skills, Agents, and a
		 "More" group holding Macros + Triggers - so the tour never advertises a
		 destination the user can't find. Every slide runs its own small
		 animation via the shared phase-clock driver (see createPhaseClock in
		 <script>): Welcome plays a looping chat conversation as the proof (ask,
		 run a report, rank customers, create a pick list, approve, done), Skills
		 & Knowledge builds the wiki the assistant keeps of your business, Macros
		 collapses
		 three routine steps into one run, File Box drops a file in and turns it
		 into structured fields, and Dashboards types a request and draws the
		 chart. NOTE: the approved preview at
		 docs/superpowers/specs/onboarding-preview-v6.html predates this cut
		 (Chat/Agents dropped, Skills reframed to Knowledge, Dashboards added) -
		 regenerate it before treating it as the source of truth. Self-contained,
		 only depends on the app palette vars OnboardingView already applies. -->
	<div class="tour">
		<div class="tour-stage">
			<!-- slide 1 · welcome (the opener: what Jarvis does for the
				 business, not a generic agent pitch - see the file header) -->
			<div v-if="cur === 0" class="slide">
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
						<i></i><i></i><i></i><span>{{ agentName }} · Stock &amp; dispatch</span>
					</div>
					<div class="mock-body">
						<div class="m-side" v-html="sideHtml('')"></div>
						<div class="m-main">
							<!-- looping animated conversation: chatStep (script) toggles which
								 bubbles exist via v-if; the motion (typing reveal, caret
								 blink, tool-dot pulse, loop fade) is CSS. .chat-flow is pinned
								 to the mock's fixed content box and bottom-aligned, so new
								 bubbles push older ones past the top edge where
								 overflow: hidden clips them, like a real scrolled-to-bottom
								 chat, and the mock never grows or jumps. This IS the proof the
								 Welcome copy promises: Jarvis working real ERP data end to
								 end. -->
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
									<template v-if="chatStep === 'type1' || chatStep === 'type2'"
										><span
											class="type-line"
											:class="chatStep === 'type1' ? 'type-q1' : 'type-q2'"
											>{{ chatStep === "type1" ? chatQ1 : chatQ2 }}</span
										><span class="type-caret" aria-hidden="true"></span
									></template>
									<template v-else>Ask a follow-up…</template>
								</div>
							</div>
						</div>
					</div>
				</div>
			</div>

			<!-- slide 2 · skills & knowledge (where the assistant keeps what it
				 knows about YOUR business: skills it can run, plus the wiki and
				 personalise notes - all tabs of the Skills area, which is why the
				 mock highlights Skills in the sidebar and shows the tab strip). -->
			<div v-else-if="cur === 1" class="slide">
				<div class="slide-copy">
					<span class="eyebrow">Skills &amp; Knowledge</span>
					<h2>It learns how your business runs.</h2>
					<p>
						{{ agentName }} ships knowing Frappe &amp; ERPNext, then keeps a private
						wiki of how <em>your</em> team works: your terms, rules and routines, so
						every answer fits your business, not a generic one.
					</p>
				</div>
				<div class="mock">
					<div class="mock-bar"><i></i><i></i><i></i><span>Skills · Wiki</span></div>
					<div class="mock-body">
						<div class="m-side" v-html="sideHtml('Skills')"></div>
						<div class="m-main">
							<!-- the Skills area's tab strip (Skills · Personalise · Wiki),
								 with Wiki active: the honest home of the knowledge base, since
								 it lives as a tab of /skills, not its own nav item.
								 knowledgeStep (script) writes the wiki page by page, then the
								 New page invite lights up - the assistant is still learning. -->
							<div class="m-tabs">
								<span class="m-tab">Skills</span>
								<span class="m-tab">Personalise</span>
								<span class="m-tab on">Wiki</span>
							</div>
							<div class="phase-fade" :class="{ fading: knowledgeFading }">
								<div class="m-row m-row-skill" v-if="knowledgeAtLeast('row1')">
									<span class="pill">org</span>
									<div class="t">How we raise a sales order</div>
									<div class="meta"></div>
								</div>
								<div class="m-row m-row-skill" v-if="knowledgeAtLeast('row2')">
									<span class="pill">org</span>
									<div class="t">Approval limits by amount</div>
									<div class="meta"></div>
								</div>
								<div class="m-row m-row-skill" v-if="knowledgeAtLeast('row3')">
									<span class="pill amber">you</span>
									<div class="t">Preferred vendors for packaging</div>
									<div class="meta"></div>
								</div>
							</div>
							<div
								class="m-row m-row-dashed"
								:class="{ active: knowledgeAtLeast('cta') }"
							>
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
									New page</span
								>
							</div>
						</div>
					</div>
				</div>
			</div>

			<!-- slide 3 · macros (lives in the sidebar's "More" group, so the mock
				 opens that group and highlights Macros there) -->
			<div v-else-if="cur === 2" class="slide">
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
							<!-- macrosStep (script) collapses three routine steps into one
								 run, then plays the run out (queued -> done): the "one click"
								 promise, shown rather than told. -->
							<div class="phase-fade" :class="{ fading: macrosFading }">
								<div class="m-row m-row-macro">
									<div
										class="chip-row"
										v-if="macrosStep === 'chips' || macrosStep === 'collapse'"
										:class="{ collapsing: macrosStep === 'collapse' }"
									>
										<span class="chip">GST</span
										><span class="chip">Reminders</span
										><span class="chip">Close</span>
									</div>
									<template v-else>
										<span class="pill amber" v-if="macrosStep === 'running'"
											>running</span
										>
										<span class="pill" v-else>done</span>
									</template>
									<div class="t">Month-end close</div>
									<div class="meta">
										<span
											class="meta-fill"
											:class="{
												on: macrosStep === 'running',
												done: macrosAtLeast('done'),
											}"
										></span>
									</div>
								</div>
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

			<!-- slide 4 · file box -->
			<div v-else-if="cur === 3" class="slide">
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
							<!-- fileboxStep (script) drops a file in, reads it, then turns
								 the placeholder meta bar into the real extracted fields:
								 the raw-to-clean transform the slide promises. -->
							<div class="phase-fade" :class="{ fading: fileboxFading }">
								<div
									class="m-row file-row"
									:class="{ dropping: fileboxStep === 'drop' }"
								>
									<span class="pill amber" v-if="fileboxStep === 'reading'"
										><span class="g"></span>reading</span
									>
									<span
										class="pill amber"
										v-else-if="fileboxStep === 'extracted'"
										>extracted</span
									>
									<span class="pill" v-else-if="fileboxAtLeast('filed')"
										>filed</span
									>
									<span class="fname">INV-ACME-0921.pdf</span>
									<div class="meta" v-if="!fileboxAtLeast('extracted')"></div>
									<div class="meta-info" v-else>₹42,500 · Acme Industries</div>
								</div>
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

			<!-- slide 5 · dashboards (final: the Onboard Jarvis CTA lives in the
				 footer's Next slot, so it sits bottom-right like every other slide's
				 advance button; Skip is hidden here. A secondary "Browse all
				 features" link opens the docs in a new tab - a same-tab navigation
				 would dump the user out of the signup wizard this tour is step 1
				 of). -->
			<div v-else class="slide">
				<div class="slide-copy">
					<span class="eyebrow">Dashboards</span>
					<h2>Ask for a chart. Watch it build.</h2>
					<p>
						Describe what you want to see in plain words and {{ agentName }} draws it
						on a live canvas. Keep the ones that matter and they’re saved to your
						dashboard.
					</p>
					<p class="final-call">
						<mark
							>Ready to see it on your data? Onboard {{ agentName }} and explore
							everything hands-on. It takes about two minutes.</mark
						>
					</p>
					<a
						class="more-link"
						:href="DOCS_URL"
						target="_blank"
						rel="noopener noreferrer"
					>
						Browse all features
						<svg
							width="13"
							height="13"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="2"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M7 17 17 7M8 7h9v9" />
						</svg>
					</a>
				</div>
				<div class="mock">
					<div class="mock-bar">
						<i></i><i></i><i></i><span>Dashboards · Canvas</span>
					</div>
					<div class="mock-body">
						<div class="m-side" v-html="sideHtml('Dashboard')"></div>
						<div class="m-main">
							<!-- dashboardStep (script): the request is typed into the composer,
								 a build indicator pulses, then the bar chart draws itself in on
								 the canvas - the "describe it, get a chart" promise shown, not
								 told. The chart is gated on the 'chart' step (v-if) and reveals
								 via the jvBarGrow entrance, so under prefers-reduced-motion
								 (clock stopped on its last step) the bars just render whole. -->
							<div class="phase-fade" :class="{ fading: dashboardFading }">
								<div class="canvas">
									<div class="canvas-head">
										<span class="canvas-title">Revenue by month</span>
										<span
											class="canvas-status"
											v-if="dashboardStep === 'building'"
											><span class="g"></span>building…</span
										>
										<span
											class="canvas-status"
											v-else-if="dashboardAtLeast('chart')"
											>Last 6 months</span
										>
									</div>
									<div class="chart" v-if="dashboardAtLeast('chart')">
										<span class="bar" style="--h: 42%"></span>
										<span class="bar" style="--h: 58%"></span>
										<span class="bar" style="--h: 49%"></span>
										<span class="bar" style="--h: 71%"></span>
										<span class="bar" style="--h: 64%"></span>
										<span class="bar" style="--h: 88%"></span>
									</div>
									<div class="chart-skeleton" v-else></div>
								</div>
								<div class="composer">
									<template v-if="dashboardStep === 'ask'"
										><span class="type-line type-d1"
											>Revenue by month, last 6 months</span
										><span class="type-caret" aria-hidden="true"></span
									></template>
									<template v-else>Describe a chart to build…</template>
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

// TODO(docs): replace with the real product docs / features URL. The final
// slide's "Browse all features" link opens this in a new tab so the curious
// can read everything without the tour having to show every surface.
const DOCS_URL = "https://docs.example.com/jarvis";

const SLIDE_COUNT = 5;
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

// ---- phase clock: shared animation driver for every slide ----
// A tiny phase clock steps through a named sequence with per-step
// durations; every visual (typing reveal, dot pulse, row presence,
// highlight) is driven off the current step name and rendered as CSS, so
// this is a state machine, not a frame-by-frame animator. At the loop
// boundary it fades the slide's dynamic content out, resets to the first
// step, then fades back in (`fading`); it never restarts mid-view because
// the reset happens while invisible. Only the clock for the visible slide
// runs (see the dispatcher below); every other clock sits stopped on its
// settled last step, which is also what prefers-reduced-motion shows,
// since it never starts a clock at all. This is the exact technique the
// Chat slide originated, extracted so every slide can reuse it.
function createPhaseClock(sequence, durationMs) {
	const INDEX = Object.fromEntries(sequence.map((name, i) => [name, i]));
	const step = ref(sequence[sequence.length - 1]);
	const fading = ref(false);
	let timer = null;
	let idx = 0;

	function clear() {
		if (timer) {
			clearTimeout(timer);
			timer = null;
		}
	}
	function atLeast(name) {
		return INDEX[step.value] >= INDEX[name];
	}
	function runStep() {
		const name = sequence[idx];
		step.value = name;
		timer = setTimeout(() => {
			idx += 1;
			if (idx >= sequence.length) {
				// loop boundary: fade the whole thing out, then reset to the start
				fading.value = true;
				timer = setTimeout(() => {
					idx = 0;
					fading.value = false;
					runStep();
				}, 500);
				return;
			}
			runStep();
		}, durationMs[name]);
	}
	function start() {
		clear();
		idx = 0;
		fading.value = false;
		runStep();
	}
	function stop() {
		clear();
		step.value = sequence[sequence.length - 1];
		fading.value = false;
	}
	return { step, fading, atLeast, start, stop };
}

function prefersReducedMotion() {
	return (
		typeof window !== "undefined" &&
		typeof window.matchMedia === "function" &&
		window.matchMedia("(prefers-reduced-motion: reduce)").matches
	);
}

// ---- welcome slide (cur === 0): looping animated conversation ----
// The Welcome mock IS the proof: Jarvis works a real ERP question end to end
// (ask about stock/dispatch, run a report, rank customers, create a pick list,
// approve, done). Typing reveal, caret blink, tool-dot pulse and bubble/card
// presence are all CSS keyed off chatStep.
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
const chatQ1 =
	"For the available stock, which customer orders can I dispatch now, who pays me on time, to keep up my cashflow in a good state? Analyse and list.";
const chatQ2 = "Create pick list for first order.";
const chatClock = createPhaseClock(CHAT_SEQUENCE, CHAT_DURATION_MS);
const chatStep = chatClock.step;
const chatFading = chatClock.fading;
const chatAtLeast = chatClock.atLeast;

// ---- skills & knowledge slide (cur === 1): the wiki writes itself ----
// Wiki pages appear one by one, then the New page invite lights up: the
// assistant keeps a growing, private record of how your business runs.
const KNOWLEDGE_SEQUENCE = ["row1", "row2", "row3", "cta", "hold"];
const KNOWLEDGE_DURATION_MS = {
	row1: 520,
	row2: 470,
	row3: 470,
	cta: 1500,
	hold: 1800,
};
const knowledgeClock = createPhaseClock(KNOWLEDGE_SEQUENCE, KNOWLEDGE_DURATION_MS);
const knowledgeFading = knowledgeClock.fading;
const knowledgeAtLeast = knowledgeClock.atLeast;

// ---- macros slide (cur === 2): three steps collapse into one run ----
// The chips converge into the row's pill, then the run plays out
// (queued -> done): the "one click" promise, shown rather than told.
const MACROS_SEQUENCE = ["chips", "collapse", "running", "done", "hold"];
const MACROS_DURATION_MS = {
	chips: 900,
	collapse: 650,
	running: 1500,
	done: 1600,
	hold: 1400,
};
const macrosClock = createPhaseClock(MACROS_SEQUENCE, MACROS_DURATION_MS);
const macrosStep = macrosClock.step;
const macrosFading = macrosClock.fading;
const macrosAtLeast = macrosClock.atLeast;

// ---- file box slide (cur === 3): raw file becomes clean fields ----
// A file drops in, gets read, then the placeholder meta bar turns into the
// real extracted fields: the raw-to-clean transform the slide promises.
const FILEBOX_SEQUENCE = ["drop", "reading", "extracted", "filed", "hold"];
const FILEBOX_DURATION_MS = {
	drop: 500,
	reading: 1500,
	extracted: 1800,
	filed: 1400,
	hold: 1600,
};
const fileboxClock = createPhaseClock(FILEBOX_SEQUENCE, FILEBOX_DURATION_MS);
const fileboxStep = fileboxClock.step;
const fileboxFading = fileboxClock.fading;
const fileboxAtLeast = fileboxClock.atLeast;

// ---- dashboards slide (cur === 4): a request becomes a chart ----
// The request types itself into the composer, a build indicator pulses, then
// the bar chart draws in row by row: "describe it, get a chart", shown not
// told.
const DASHBOARD_SEQUENCE = ["ask", "building", "chart", "hold"];
const DASHBOARD_DURATION_MS = {
	ask: 2200,
	building: 1100,
	chart: 2600,
	hold: 1600,
};
const dashboardClock = createPhaseClock(DASHBOARD_SEQUENCE, DASHBOARD_DURATION_MS);
const dashboardStep = dashboardClock.step;
const dashboardFading = dashboardClock.fading;
const dashboardAtLeast = dashboardClock.atLeast;

// Slides use v-if, so leaving a slide destroys its DOM - but each clock's
// timer above is JS state on this component, not the DOM, so it needs its
// own stop here too. Index-aligned with the v-if chain in the template:
// only the clock for the visible slide runs, every other one is stopped,
// so at most one animation loop is ever active at a time.
const CLOCKS = [chatClock, knowledgeClock, macrosClock, fileboxClock, dashboardClock];

function runOnly(slide) {
	CLOCKS.forEach((clock, i) => {
		if (i === slide && !prefersReducedMotion()) {
			clock.start();
		} else {
			clock.stop();
		}
	});
}

watch(cur, runOnly, { immediate: true });

// Restart the visible slide's clock when the tab comes back (round-1 review of
// #762). Every mock here gates its rows on how far its clock has advanced, so a
// browser that throttled our timers while the tab was hidden, which Chrome and
// Safari both do aggressively, would show a HALF-DRAWN mock on return: one skill
// row of four, a file with no extracted fields. The old static markup could not
// do that because it always rendered complete. `start()` re-enters at the top of
// the sequence, so the slide is whole immediately rather than resuming mid-way.
function onVisibility() {
	if (!document.hidden) runOnly(cur.value);
}
document.addEventListener("visibilitychange", onVisibility);

onUnmounted(() => {
	document.removeEventListener("visibilitychange", onVisibility);
	CLOCKS.forEach((clock) => clock.stop());
});

// ---- mock sidebar: mirrors the REAL app sidebar (brand + user, New Chat,
// Search Chat, feather-icon nav, Recent chats), rendered from data exactly
// like the preview's NAV_ICONS renderer. Static trusted strings only.
const FI = (d, size = 11) =>
	`<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
// Top-level nav, in the real app's default order (Sidebar.vue TOP_DEFS).
const NAV_ICONS = {
	"File Box":
		'<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
	"Approval Board":
		'<polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
	Dashboard:
		'<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>',
	Skills: '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
	Agents: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/>',
};
const NAV_ORDER = ["File Box", "Approval Board", "Dashboard", "Skills", "Agents"];
// The "More" group (Sidebar.vue MORE_DEFS): a real collapsible section holding
// Macros + Triggers. It sits collapsed by default and only opens (lighting its
// active child) when the visible slide's surface lives inside it - i.e. Macros.
const MORE_ICONS = {
	Macros: '<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>',
	Triggers:
		'<line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>',
};
const MORE_ORDER = ["Macros", "Triggers"];
const ICON_MORE =
	'<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>';

function sideHtml(active) {
	const inMore = MORE_ORDER.includes(active);
	const topNav = NAV_ORDER.map(
		(n) => `<div class="m-nav${n === active ? " on" : ""}">${FI(NAV_ICONS[n])}${n}</div>`,
	).join("");
	// More row lights up on a More-destination, matching the real onMoreDestination.
	const moreRow = `<div class="m-nav${inMore ? " on" : ""}">${FI(ICON_MORE)}More</div>`;
	const moreChildren = inMore
		? MORE_ORDER.map(
				(n) =>
					`<div class="m-nav m-sub${n === active ? " on" : ""}">${FI(
						MORE_ICONS[n],
					)}${n}</div>`,
			).join("")
		: "";
	return (
		`<div class="m-brand"><span class="d"><svg width="10" height="10" viewBox="0 0 24 24" fill="#fff"><path d="M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z"/></svg></span><span class="col"><b>${agentName}</b><small>Administrator</small></span></div>` +
		`<div class="m-act">${FI('<path d="M12 5v14M5 12h14"/>')}New Chat</div>` +
		`<div class="m-act">${FI(
			'<circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>',
		)}Search Chat</div>` +
		topNav +
		moreRow +
		moreChildren +
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
	/* belt-and-braces: every phase clock in <script setup> already never
	   starts under this preference, so none of this ever actually plays, but
	   it stays off here too in case that ever changes. */
	.phase-fade,
	.chat-anim {
		transition: none;
	}
	.type-caret {
		animation: none;
		opacity: 1;
	}
	.cb.tool .g,
	.pill .g,
	.canvas-status .g {
		animation: none;
	}
	.confirm-btn--ok {
		transition: none;
	}
	.m-row-skill,
	.meta-info {
		animation: none;
	}
	.m-row-dashed {
		transition: none;
	}
	.chip {
		transition: none;
	}
	.meta-fill {
		transition: none;
	}
	.file-row.dropping {
		animation: none;
	}
	.chart .bar {
		animation: none;
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
/* secondary docs link under the final CTA: opens the full feature docs in a
   new tab so the curious can read everything (a same-tab nav would drop the
   user out of the signup wizard this tour is step 1 of). */
.more-link {
	display: inline-flex;
	align-items: center;
	gap: 5px;
	margin-top: 14px;
	font-size: 13px;
	font-weight: 500;
	color: var(--text-2);
	text-decoration: none;
	transition: color 0.15s ease;
}
.more-link:hover {
	color: var(--text);
}
.more-link svg {
	color: var(--text-3);
	transition: color 0.15s ease;
}
.more-link:hover svg {
	color: var(--text);
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
	transition:
		background-color 0.15s ease,
		color 0.15s ease,
		border-color 0.15s ease;
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
	box-shadow:
		0 0 1px rgba(0, 0, 0, 0.2),
		0 12px 24px -6px rgba(0, 0, 0, 0.08);
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
/* "More" group children (Macros, Triggers) are indented under the More row */
.m-side :deep(.m-nav.m-sub) {
	padding-left: 20px;
	font-size: 10px;
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

.m-row-cta {
	display: inline-flex;
	align-items: center;
	gap: 5px;
}
.m-row-cta svg {
	color: var(--text-3);
}

/* ---- welcome / chat mock: the looping animated conversation that proves
   Jarvis works a real ERP question end to end (ask, run a report, rank, create
   a pick list, approve, done). .chat-flow is pinned to the mock's fixed content
   box and bottom-aligned, so new bubbles push older ones past the top edge
   where overflow: hidden clips them, like a real scrolled-to-bottom chat; the
   mock never grows or jumps. */
.cb {
	max-width: 74%;
	padding: 8px 11px;
	border-radius: 12px;
	font-size: 11.5px;
	line-height: 1.4;
	margin-bottom: 9px;
}
/* --cta INVERTS by theme (near-black light, near-white dark), so the foreground
   must come from its paired token, never a hard-coded #fff. */
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
	transition:
		transform 0.15s ease,
		filter 0.15s ease;
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

/* ---- shared pulse dot (File Box "reading", Dashboards "building") ---- */
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

/* ---- shared phase-clock fade: every animated slide wraps its dynamic
   content in this. Fades the content out at the loop boundary while the
   script resets to the first step underneath, so the reset is never
   visible. */
.phase-fade {
	transition: opacity 0.45s ease;
	opacity: 1;
}
.phase-fade.fading {
	opacity: 0;
}

/* ---- skills & knowledge mock: the Skills-area tab strip (Skills ·
   Personalise · Wiki), Wiki active - the honest home of the knowledge base. */
.m-tabs {
	display: flex;
	gap: 4px;
	margin-bottom: 10px;
}
.m-tab {
	font-size: 9.5px;
	font-weight: 600;
	color: var(--text-3);
	padding: 4px 9px;
	border-radius: 7px;
}
.m-tab.on {
	color: var(--text);
	background: var(--surface-2);
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
/* dashboards slide: the chart request typed into the composer */
.type-d1 {
	animation-duration: 1.8s;
	animation-timing-function: steps(31, end);
}
/* welcome/chat slide: the two typed messages (stock question, pick-list ask) */
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
	transition:
		border-color 0.3s ease,
		background 0.3s ease;
}
.m-row-cta {
	font-size: 10px;
	font-weight: 600;
	color: var(--text-2);
}
/* skills slide: the list building itself, row by row */
.m-row-skill {
	animation: jvRowIn 0.35s ease;
}
@keyframes jvRowIn {
	from {
		opacity: 0;
		transform: translateY(5px);
	}
	to {
		opacity: 1;
		transform: none;
	}
}
/* skills slide: the New skill invite lighting up once the list is built */
.m-row-dashed.active {
	border-color: var(--cta);
	background: var(--surface-2);
}
.m-row-dashed.active .m-row-cta {
	color: var(--text);
}

/* macros slide: three routine steps collapsing into one run */
.chip-row {
	display: flex;
	gap: 6px;
}
.chip {
	font-size: 8.5px;
	font-weight: 600;
	padding: 2px 7px;
	border-radius: 99px;
	background: var(--surface-2);
	color: var(--text-2);
	border: 1px solid var(--border);
	transition:
		transform 0.4s ease,
		opacity 0.4s ease;
}
.chip-row.collapsing .chip:nth-child(1) {
	transform: translateX(16px) scale(0.4);
	opacity: 0;
}
.chip-row.collapsing .chip:nth-child(2) {
	transform: scale(0.3);
	opacity: 0;
}
.chip-row.collapsing .chip:nth-child(3) {
	transform: translateX(-16px) scale(0.4);
	opacity: 0;
}
/* macros slide: the run's progress, filling while it's running and
   settling to full once it's done */
.meta-fill {
	display: block;
	height: 100%;
	width: 0%;
	border-radius: 4px;
	background: var(--amber);
	transition:
		width 1.3s linear,
		background 0.3s ease;
}
.meta-fill.on {
	width: 100%;
}
.meta-fill.done {
	width: 100%;
	background: var(--green);
}

/* file box slide: a file dropping in */
.file-row.dropping {
	animation: jvFileDrop 0.5s ease;
}
@keyframes jvFileDrop {
	from {
		opacity: 0;
		transform: translateY(-12px);
	}
	to {
		opacity: 1;
		transform: none;
	}
}
/* file box slide: the "reading" dot, same idiom as the chat tool chip */
.pill .g {
	display: inline-block;
	width: 5px;
	height: 5px;
	border-radius: 50%;
	background: currentColor;
	margin-right: 4px;
	vertical-align: middle;
	animation: jvDotPulse 1.1s ease-in-out infinite;
}
/* file box slide: the placeholder meta bar becoming the real extracted
   fields, the raw-to-clean transform the slide promises */
.meta-info {
	margin-left: auto;
	font-size: 9px;
	color: var(--text-2);
	white-space: nowrap;
	overflow: hidden;
	text-overflow: ellipsis;
	animation: jvRowIn 0.3s ease;
}

/* ---- dashboards mock: a request drawn as a bar chart on the canvas ---- */
.canvas {
	border: 1px solid var(--border);
	border-radius: 10px;
	background: var(--surface);
	padding: 11px 12px 12px;
}
.canvas-head {
	display: flex;
	align-items: center;
	justify-content: space-between;
	margin-bottom: 10px;
}
.canvas-title {
	font-size: 10.5px;
	font-weight: 600;
	color: var(--text);
}
.canvas-status {
	display: inline-flex;
	align-items: center;
	gap: 5px;
	font-size: 9px;
	color: var(--text-3);
}
.canvas-status .g {
	width: 5px;
	height: 5px;
	border-radius: 50%;
	background: var(--green);
	animation: jvDotPulse 1.1s ease-in-out infinite;
}
/* the chart is gated on the 'chart' step (v-if), so it enters via jvBarGrow
   each loop; the resting state is the full bar, so under prefers-reduced-motion
   (animation suppressed below) the bars simply render whole at full height. */
.chart {
	display: flex;
	align-items: flex-end;
	gap: 9px;
	height: 104px;
}
.chart .bar {
	flex: 1;
	height: var(--h);
	border-radius: 4px 4px 2px 2px;
	background: var(--brand-grad);
	transform-origin: bottom;
	animation: jvBarGrow 0.55s cubic-bezier(0.2, 0.7, 0.2, 1) both;
}
.chart .bar:nth-child(2) {
	animation-delay: 0.07s;
}
.chart .bar:nth-child(3) {
	animation-delay: 0.14s;
}
.chart .bar:nth-child(4) {
	animation-delay: 0.21s;
}
.chart .bar:nth-child(5) {
	animation-delay: 0.28s;
}
.chart .bar:nth-child(6) {
	animation-delay: 0.35s;
}
@keyframes jvBarGrow {
	from {
		transform: scaleY(0);
	}
	to {
		transform: scaleY(1);
	}
}
/* placeholder shown while the request is still being typed / built */
.chart-skeleton {
	height: 104px;
	border-radius: 8px;
	border: 1px dashed var(--border);
	background: var(--surface-1);
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
	transition:
		width 0.2s,
		background 0.2s;
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
