<script setup>
// Renders the server-built "what will change" summary (F9) for a parked write's
// approval sheet - replacing the raw-JSON dump. The structured card comes from
// jarvis/chat/confirm_card.py (kind = create | update | bulk_update | verb | email |
// bulk_email | share | assign | skill | wiki | method | batch_create | import),
// already perm-filtered + size-capped + secret-masked server-side. Logic
// (verbSentence) is the SAME helper the desktop uses (@shared). All values render
// as escaped text; the raw dry-run JSON stays behind a Details expander.
//
// A kind rendered here ALSO needs an entry in CARD_KINDS
// (@shared/lib/actionSummary.js): pendingCardOf returns null for a kind not in that
// set and the sheet falls back to the raw preview, so a branch added here alone is
// dead code.
import { computed } from "vue";

import { verbSentence } from "@shared/lib/actionSummary.js";
import PlanOutline from "./PlanOutline.vue";

const props = defineProps({
	card: { type: Object, required: true },
	details: { type: String, default: "" },
});

const sentence = computed(() => (props.card.kind === "verb" ? verbSentence(props.card) : ""));

// The remainder line for a proposed child table. Duplicated from the desktop card
// rather than shared: the two TEMPLATES are deliberately separate (different class
// and token vocabularies), and only @shared lib logic crosses. unknown_columns MUST
// surface - the server counts keys it dropped rather than rendering them, and a
// count nobody sees makes a silent drop silent again.
function tableNote(t) {
	const parts = [];
	if (t.extra > 0) parts.push(`+${t.extra} more rows`);
	if (t.extra_columns > 0) parts.push(`+${t.extra_columns} more columns`);
	if (t.unknown_columns > 0)
		parts.push(
			`${t.unknown_columns} unrecognized field${t.unknown_columns === 1 ? "" : "s"} ignored`
		);
	return parts.join(" · ");
}
</script>

<template>
	<div class="jv-pc">
		<template v-if="card.kind === 'create'">
			<div class="jv-pc-head">
				Create {{ card.doctype }}<template v-if="card.name"> · {{ card.name }}</template>
			</div>
			<div v-for="(r, i) in card.rows" :key="i" class="jv-pc-kv">
				<span>{{ r.label }}</span
				><b>{{ r.value }}</b>
			</div>
			<div v-if="!card.rows.length && !(card.tables || []).length" class="jv-pc-empty">
				No fields set.
			</div>
			<div v-for="(t, ti) in card.tables || []" :key="'t' + ti" class="jv-pc-table">
				<div class="jv-pc-table-head">
					{{ t.label }} · {{ t.count }} row<template v-if="t.count !== 1">s</template>
				</div>
				<div class="jv-pc-table-scroll">
					<table>
						<thead>
							<tr>
								<th v-for="(c, ci) in t.columns" :key="'c' + ci">{{ c }}</th>
							</tr>
						</thead>
						<tbody>
							<tr v-for="(r, ri) in t.rows" :key="'r' + ri">
								<td v-for="(cell, di) in r.cells" :key="'d' + di">{{ cell }}</td>
							</tr>
						</tbody>
					</table>
				</div>
				<div v-if="tableNote(t)" class="jv-pc-more">{{ tableNote(t) }}</div>
			</div>
		</template>

		<template v-else-if="card.kind === 'update'">
			<div class="jv-pc-head">
				Update {{ card.doctype }}<template v-if="card.name"> · {{ card.name }}</template
				><template v-if="card.title"> · {{ card.title }}</template>
			</div>
			<div v-for="(d, i) in card.diff" :key="i" class="jv-pc-diff">
				<span class="jv-pc-lbl">{{ d.label }}</span>
				<span class="jv-pc-from">{{ d.from || "(empty)" }}</span>
				<span class="jv-pc-arrow">→</span>
				<span class="jv-pc-to">{{ d.to || "(empty)" }}</span>
			</div>
			<div v-if="!card.diff.length" class="jv-pc-empty">No field changes.</div>
		</template>

		<!-- ORDER IS LOAD-BEARING: records div -> targets <ul v-else-if> -> +N more.
		     v-else-if chains to the immediately preceding sibling carrying a v-if, so
		     putting the +N more div between them would chain the <ul> to IT - and with
		     extra === 0 (always, via the F16 batch cap) every bulk card would render
		     its targets twice, once as records and once as the old list. -->
		<template v-else-if="card.kind === 'verb'">
			<div class="jv-pc-verb">{{ sentence }}</div>
			<div v-if="(card.records || []).length" class="jv-pc-recs">
				<template v-for="(r, i) in card.records" :key="'vr' + i">
					<details v-if="r.rows.length" class="jv-pc-rec" :open="i === 0">
						<summary>
							<span class="jv-pc-chev" aria-hidden="true"></span>
							<span class="jv-pc-rid">{{ r.name }}</span>
							<span v-if="r.title" class="jv-pc-rtitle">{{ r.title }}</span>
						</summary>
						<div class="jv-pc-rbody">
							<div v-for="(f, j) in r.rows" :key="'vf' + j" class="jv-pc-kv">
								<span>{{ f.label }}</span
								><b>{{ f.value }}</b>
							</div>
						</div>
					</details>
					<div v-else class="jv-pc-rec jv-pc-rec-bare">
						<span class="jv-pc-rid">{{ r.name }}</span>
						<span v-if="r.title" class="jv-pc-rtitle">{{ r.title }}</span>
					</div>
				</template>
			</div>
			<ul v-else-if="card.count > 1 && card.targets.length" class="jv-pc-list">
				<li v-for="(t, i) in card.targets" :key="i">{{ t }}</li>
				<li v-if="card.extra > 0" class="jv-pc-more">+{{ card.extra }} more</li>
			</ul>
			<div v-if="(card.records || []).length && card.extra > 0" class="jv-pc-more">
				+{{ card.extra }} more
			</div>
		</template>

		<!-- email: cc/bcc/print format are v-if'd so an empty one adds no row. Without
		     them "you could not see who was copied" stayed true no matter what the
		     server sent. The body rides an expander (a _MAX_BODY budget now), open by
		     default: this is the one tool that cannot be recalled. -->
		<template v-else-if="card.kind === 'email'">
			<div class="jv-pc-kv">
				<span>To</span><b>{{ card.to }}</b>
			</div>
			<div v-if="card.cc" class="jv-pc-kv">
				<span>Cc</span><b>{{ card.cc }}</b>
			</div>
			<div v-if="card.bcc" class="jv-pc-kv">
				<span>Bcc</span><b>{{ card.bcc }}</b>
			</div>
			<div class="jv-pc-kv">
				<span>Subject</span><b>{{ card.subject }}</b>
			</div>
			<div v-if="card.print_format" class="jv-pc-kv">
				<span>Print format</span><b>{{ card.print_format }}</b>
			</div>
			<details v-if="card.body" class="jv-pc-expand" open>
				<summary>Message</summary>
				<pre class="jv-pc-body">{{ card.body }}</pre>
			</details>
		</template>

		<!-- bulk email: a mail-merge. Every message has its OWN recipient, subject and
		     body, so each gets its own expander - a count cannot stand in for 20
		     distinct irreversible emails. -->
		<template v-else-if="card.kind === 'bulk_email'">
			<div class="jv-pc-head">
				Send {{ card.count }} email<template v-if="card.count !== 1">s</template>
			</div>
			<p class="jv-pc-cap">
				Each message has its own recipient and body. Tap one to read it.
			</p>
			<div class="jv-pc-recs">
				<details
					v-for="(m, i) in card.messages"
					:key="'me' + i"
					class="jv-pc-rec"
					:open="i === 0"
				>
					<summary>
						<span class="jv-pc-chev" aria-hidden="true"></span>
						<span class="jv-pc-rid">{{ m.recipients }}</span>
						<span v-if="m.subject" class="jv-pc-rtitle">{{ m.subject }}</span>
					</summary>
					<div class="jv-pc-rbody">
						<div v-if="m.name" class="jv-pc-kv">
							<span>About</span><b>{{ m.name }}</b>
						</div>
						<div v-if="m.cc" class="jv-pc-kv">
							<span>Cc</span><b>{{ m.cc }}</b>
						</div>
						<div v-if="m.bcc" class="jv-pc-kv">
							<span>Bcc</span><b>{{ m.bcc }}</b>
						</div>
						<pre v-if="m.body" class="jv-pc-body">{{ m.body }}</pre>
					</div>
				</details>
			</div>
			<div v-if="card.extra > 0" class="jv-pc-more">
				+{{ card.extra }} more · full list in Details
			</div>
		</template>

		<!-- share: grantee + the permission flags. "Read for one person" and
		     "everyone + write + share" rendered identically before, and those grants are
		     the exact reason share_doc gates. Flags are filtered IN THE EXPRESSION:
		     v-for + v-if on one element is invalid in Vue 3 (v-if wins and cannot see
		     the loop variable), which only warns at compile and throws at render. -->
		<template v-else-if="card.kind === 'share'">
			<div class="jv-pc-head">
				Share {{ card.doctype
				}}<template v-if="card.count > 1"> · {{ card.count }} records</template>
			</div>
			<div class="jv-pc-kv">
				<span>Shared with</span><b>{{ card.grantee }}</b>
			</div>
			<div class="jv-pc-kv">
				<span>Grants</span>
				<b>
					<span
						v-for="f in card.flags.filter((x) => x.on)"
						:key="f.label"
						class="jv-pc-chip"
						>{{ f.label }}</span
					>
					<span v-if="!card.flags.some((x) => x.on)" class="jv-pc-empty">None</span>
				</b>
			</div>
			<div class="jv-pc-kv">
				<span>Notify by email</span><b>{{ card.notify ? "Yes" : "No" }}</b>
			</div>
			<div v-if="(card.records || []).length" class="jv-pc-recs">
				<template v-for="(r, i) in card.records" :key="'sr' + i">
					<details v-if="r.rows.length" class="jv-pc-rec" :open="i === 0">
						<summary>
							<span class="jv-pc-chev" aria-hidden="true"></span>
							<span class="jv-pc-rid">{{ r.name }}</span>
							<span v-if="r.title" class="jv-pc-rtitle">{{ r.title }}</span>
						</summary>
						<div class="jv-pc-rbody">
							<div v-for="(f, j) in r.rows" :key="'sf' + j" class="jv-pc-kv">
								<span>{{ f.label }}</span
								><b>{{ f.value }}</b>
							</div>
						</div>
					</details>
					<div v-else class="jv-pc-rec jv-pc-rec-bare">
						<span class="jv-pc-rid">{{ r.name }}</span>
						<span v-if="r.title" class="jv-pc-rtitle">{{ r.title }}</span>
					</div>
				</template>
			</div>
			<div v-if="card.extra > 0" class="jv-pc-more">+{{ card.extra }} more</div>
		</template>

		<!-- assign: the assignee and the description that gets EMAILED to them -->
		<template v-else-if="card.kind === 'assign'">
			<div class="jv-pc-head">
				Assign {{ card.doctype
				}}<template v-if="card.count > 1"> · {{ card.count }} records</template>
			</div>
			<div class="jv-pc-kv">
				<span>Assign to</span><b>{{ card.assignee }}</b>
			</div>
			<div v-if="card.priority" class="jv-pc-kv">
				<span>Priority</span><b>{{ card.priority }}</b>
			</div>
			<div v-if="card.date" class="jv-pc-kv">
				<span>Due date</span><b>{{ card.date }}</b>
			</div>
			<div class="jv-pc-kv">
				<span>Notify by email</span><b>{{ card.notify ? "Yes" : "No" }}</b>
			</div>
			<pre v-if="card.description" class="jv-pc-body">{{ card.description }}</pre>
			<div v-if="(card.records || []).length" class="jv-pc-recs">
				<template v-for="(r, i) in card.records" :key="'ar' + i">
					<details v-if="r.rows.length" class="jv-pc-rec" :open="i === 0">
						<summary>
							<span class="jv-pc-chev" aria-hidden="true"></span>
							<span class="jv-pc-rid">{{ r.name }}</span>
							<span v-if="r.title" class="jv-pc-rtitle">{{ r.title }}</span>
						</summary>
						<div class="jv-pc-rbody">
							<div v-for="(f, j) in r.rows" :key="'af' + j" class="jv-pc-kv">
								<span>{{ f.label }}</span
								><b>{{ f.value }}</b>
							</div>
						</div>
					</details>
					<div v-else class="jv-pc-rec jv-pc-rec-bare">
						<span class="jv-pc-rid">{{ r.name }}</span>
						<span v-if="r.title" class="jv-pc-rtitle">{{ r.title }}</span>
					</div>
				</template>
			</div>
			<div v-if="card.extra > 0" class="jv-pc-more">+{{ card.extra }} more</div>
		</template>

		<!-- skill: PERSISTENT AGENT INSTRUCTIONS. The card was the literal string
		     "create_custom_skill". Scope is the tool's EFFECTIVE value, not the request. -->
		<template v-else-if="card.kind === 'skill'">
			<div class="jv-pc-head">
				Create skill<template v-if="card.skill_name"> · {{ card.skill_name }}</template>
			</div>
			<div class="jv-pc-kv">
				<span>Scope</span><b>{{ card.scope }}</b>
			</div>
			<div class="jv-pc-kv">
				<span>User invocable</span><b>{{ card.user_invocable ? "Yes" : "No" }}</b>
			</div>
			<div v-if="card.description" class="jv-pc-kv">
				<span>Description</span><b>{{ card.description }}</b>
			</div>
			<details v-if="card.instructions" class="jv-pc-expand" open>
				<summary>Instructions · shape every future session</summary>
				<pre class="jv-pc-body">{{ card.instructions }}</pre>
			</details>
		</template>

		<!-- wiki: replace_body_md is a full rewrite and says so -->
		<template v-else-if="card.kind === 'wiki'">
			<div class="jv-pc-head">
				Update wiki<template v-if="card.slug"> · {{ card.slug }}</template>
			</div>
			<div v-if="card.title" class="jv-pc-kv">
				<span>Title</span><b>{{ card.title }}</b>
			</div>
			<div v-if="card.scope" class="jv-pc-kv">
				<span>Scope</span><b>{{ card.scope }}</b>
			</div>
			<div v-if="card.page_type" class="jv-pc-kv">
				<span>Page type</span><b>{{ card.page_type }}</b>
			</div>
			<div v-if="card.ref" class="jv-pc-kv">
				<span>About</span><b>{{ card.ref }}</b>
			</div>
			<div v-if="card.summary" class="jv-pc-kv">
				<span>Summary</span><b>{{ card.summary }}</b>
			</div>
			<div v-if="card.mode === 'replace'" class="jv-pc-warn">
				Replaces the entire page body
			</div>
			<div v-else-if="card.mode === 'append'" class="jv-pc-cap">
				Appends a section; nothing already recorded is removed.
			</div>
			<details v-if="card.body" class="jv-pc-expand" :open="card.mode === 'replace'">
				<summary>{{ card.mode === "replace" ? "New body" : "Added section" }}</summary>
				<pre class="jv-pc-body">{{ card.body }}</pre>
			</details>
		</template>

		<template v-else-if="card.kind === 'method'">
			<div class="jv-pc-kv">
				<span>Method</span><b>{{ card.method }}</b>
			</div>
			<div v-for="(v, k) in card.args" :key="k" class="jv-pc-kv">
				<span>{{ k }}</span
				><b>{{ v }}</b>
			</div>
		</template>

		<template v-else-if="card.kind === 'batch_create'">
			<div class="jv-pc-head">
				Create {{ card.count }} record<template v-if="card.count !== 1">s</template>
			</div>
			<div v-if="(card.records || []).length" class="jv-pc-recs">
				<details
					v-for="(r, i) in card.records"
					:key="'br' + i"
					class="jv-pc-rec"
					:open="i === 0"
				>
					<summary>
						<span class="jv-pc-chev" aria-hidden="true"></span>
						<span class="jv-pc-rid"
							>{{ r.doctype }} <b>{{ r.name }}</b></span
						>
					</summary>
					<div class="jv-pc-rbody">
						<div v-for="(f, j) in r.rows" :key="'bf' + j" class="jv-pc-kv">
							<span>{{ f.label }}</span
							><b>{{ f.value }}</b>
						</div>
						<div v-if="r.extra > 0" class="jv-pc-more">+{{ r.extra }} more fields</div>
						<div
							v-for="(t, ti) in r.tables || []"
							:key="'bt' + ti"
							class="jv-pc-table"
						>
							<div class="jv-pc-table-head">
								{{ t.label }} · {{ t.count }} row<template v-if="t.count !== 1"
									>s</template
								>
							</div>
							<div class="jv-pc-table-scroll">
								<table>
									<thead>
										<tr>
											<th v-for="(c, ci) in t.columns" :key="'bc' + ci">
												{{ c }}
											</th>
										</tr>
									</thead>
									<tbody>
										<tr v-for="(row, ri) in t.rows" :key="'brw' + ri">
											<td v-for="(cell, di) in row.cells" :key="'bd' + di">
												{{ cell }}
											</td>
										</tr>
									</tbody>
								</table>
							</div>
							<div v-if="tableNote(t)" class="jv-pc-more">{{ tableNote(t) }}</div>
						</div>
					</div>
				</details>
			</div>
			<ul v-else class="jv-pc-list">
				<li v-for="(r, i) in card.rows" :key="i">
					{{ r.doctype }} <b>{{ r.name }}</b>
				</li>
				<li v-if="card.extra > 0" class="jv-pc-more">+{{ card.extra }} more</li>
			</ul>
			<div v-if="(card.records || []).length && card.extra > 0" class="jv-pc-more">
				+{{ card.extra }} more
			</div>
		</template>

		<template v-else-if="card.kind === 'bulk_update'">
			<div class="jv-pc-head">
				Update {{ card.count }} {{ card.doctype }} record<template v-if="card.count !== 1"
					>s</template
				><span v-if="card.varying" class="jv-pc-sub"> · varying changes</span>
			</div>
			<p class="jv-pc-cap">Tap a record to review its changes.</p>
			<div class="jv-pc-recs">
				<details v-for="(r, i) in card.records" :key="i" class="jv-pc-rec" :open="i === 0">
					<summary>
						<span class="jv-pc-chev" aria-hidden="true"></span>
						<span class="jv-pc-rid">{{ r.name }}</span>
						<span v-if="r.title" class="jv-pc-rtitle">{{ r.title }}</span>
						<span v-if="r.fields?.length" class="jv-pc-rfields">{{
							r.fields.join(" · ")
						}}</span>
					</summary>
					<div class="jv-pc-rbody">
						<div v-for="(d, j) in r.diff" :key="j" class="jv-pc-diff">
							<span class="jv-pc-lbl">{{ d.label }}</span>
							<span class="jv-pc-from">{{ d.from || "(empty)" }}</span>
							<span class="jv-pc-arrow">→</span>
							<span class="jv-pc-to">{{ d.to || "(empty)" }}</span>
						</div>
						<div v-if="!r.diff.length" class="jv-pc-empty">No field changes.</div>
					</div>
				</details>
			</div>
			<div v-if="card.extra > 0" class="jv-pc-more">
				+{{ card.extra }} more · full list in Details
			</div>
		</template>

		<!-- import: run_import (bulk load via Frappe's Data Import). Rows vs records
		     shown distinctly - a flat file with a mapped child table can fan out into
		     more records than sheet rows (confirm_card.py::_import_card). Cells are
		     already secret-masked server-side ("[hidden]"); escaped text only, never
		     v-html. -->
		<template v-else-if="card.kind === 'import'">
			<div class="jv-pc-head">Import into {{ card.doctype }}</div>
			<div class="jv-pc-kv">
				<span>Import type</span><b>{{ card.import_type }}</b>
			</div>
			<div class="jv-pc-kv">
				<span>File</span><b>{{ card.file }}</b>
			</div>
			<div class="jv-pc-kv">
				<span>Rows → records</span><b>{{ card.total_rows }} → {{ card.total_records }}</b>
			</div>
			<div v-if="card.submit_after_import" class="jv-pc-warn">
				Will submit each record after import
			</div>
			<div v-if="(card.sample?.rows || []).length" class="jv-pc-table">
				<div class="jv-pc-table-head">Preview</div>
				<div class="jv-pc-table-scroll">
					<table>
						<thead>
							<tr>
								<th v-for="(c, ci) in card.sample.columns" :key="'ic' + ci">
									{{ c }}
								</th>
							</tr>
						</thead>
						<tbody>
							<tr v-for="(r, ri) in card.sample.rows" :key="'ir' + ri">
								<td v-for="(cell, di) in r.cells" :key="'id' + di">{{ cell }}</td>
							</tr>
						</tbody>
					</table>
				</div>
				<div v-if="card.sample.extra_cols > 0" class="jv-pc-more">
					+{{ card.sample.extra_cols }} more columns
				</div>
			</div>
			<div v-else class="jv-pc-empty">No preview rows.</div>
			<ul v-if="(card.advisory || []).length" class="jv-pc-list">
				<li v-for="(a, i) in card.advisory" :key="'ia' + i">{{ a }}</li>
			</ul>
			<div v-if="(card.columns?.unmapped || []).length" class="jv-pc-more">
				Unmapped columns ignored: {{ card.columns.unmapped.join(", ") }}
			</div>
		</template>

		<!-- Full-plan outline (P1, additive `card.plan`, no new CARD_KIND): OUTSIDE
		     the kind switch above so it renders beside whatever kind step 1 is. -->
		<PlanOutline v-if="card.plan?.steps?.length" :plan="card.plan" />

		<details v-if="details" class="jv-pc-details">
			<summary>Details</summary>
			<pre>{{ details }}</pre>
		</details>
	</div>
</template>

<style scoped>
.jv-pc {
	font-size: 13px;
	color: var(--ink7);
	line-height: 1.5;
}
.jv-pc-head {
	font-weight: 600;
	color: var(--ink9);
	margin-bottom: 6px;
	overflow-wrap: anywhere;
}
.jv-pc-verb {
	font-weight: 500;
	color: var(--ink9);
	overflow-wrap: anywhere;
}
.jv-pc-kv {
	display: flex;
	justify-content: space-between;
	gap: 12px;
	padding: 4px 0;
	border-bottom: 1px solid var(--border);
}
.jv-pc-kv:last-child {
	border-bottom: 0;
}
.jv-pc-kv span {
	color: var(--ink5);
}
.jv-pc-kv b {
	font-weight: 500;
	color: var(--ink8);
	text-align: right;
	overflow-wrap: anywhere;
}
.jv-pc-diff {
	display: grid;
	grid-template-columns: 1fr auto auto auto;
	gap: 8px;
	align-items: baseline;
	padding: 3px 0;
}
.jv-pc-lbl {
	color: var(--ink5);
}
.jv-pc-from {
	color: var(--ink5);
	text-decoration: line-through;
	overflow-wrap: anywhere;
}
.jv-pc-arrow {
	color: var(--ink5);
}
.jv-pc-to {
	color: var(--green, var(--ink9));
	font-weight: 500;
	overflow-wrap: anywhere;
}
.jv-pc-empty {
	color: var(--ink5);
	font-style: italic;
}
.jv-pc-list {
	margin: 5px 0 0;
	padding-left: 18px;
}
.jv-pc-list li {
	padding: 1px 0;
	overflow-wrap: anywhere;
}
.jv-pc-more {
	list-style: none;
	color: var(--ink5);
}
/* create: a proposed child table, scrolling inside its own box so the card never
   scrolls the page sideways */
.jv-pc-table {
	margin-top: 10px;
}
.jv-pc-table-head {
	font-size: 12px;
	color: var(--ink5);
	margin-bottom: 4px;
}
.jv-pc-table-scroll {
	overflow-x: auto;
	max-height: 240px;
	overflow-y: auto;
	-webkit-overflow-scrolling: touch;
	overscroll-behavior: contain;
}
.jv-pc-table table {
	border-collapse: collapse;
	width: 100%;
	font-size: 12px;
}
.jv-pc-table th,
.jv-pc-table td {
	text-align: left;
	padding: 4px 8px;
	white-space: nowrap;
}
.jv-pc-table th {
	color: var(--ink5);
	font-weight: 500;
}
.jv-pc-table td {
	color: var(--ink8);
	font-variant-numeric: tabular-nums;
}
/* scoped: .jv-pc-more is shared with the verb/batch_create <li>s */
.jv-pc-table .jv-pc-more {
	font-size: 12px;
	margin-top: 4px;
}
/* bulk update: collapsible per-record from→to list (touch-first) */
.jv-pc-sub {
	font-weight: 400;
	color: var(--ink5);
}
.jv-pc-cap {
	font-size: 12px;
	color: var(--ink5);
	margin: 0 0 8px;
}
.jv-pc-recs {
	display: flex;
	flex-direction: column;
	max-height: 320px;
	overflow-y: auto;
	overscroll-behavior: contain;
	-webkit-overflow-scrolling: touch;
}
.jv-pc-rec {
	border-top: 1px solid var(--border);
}
.jv-pc-rec:first-child {
	border-top: 0;
}
.jv-pc-rec > summary {
	list-style: none;
	cursor: pointer;
	display: flex;
	align-items: center;
	gap: 10px;
	min-height: 44px;
	padding: 8px 2px;
	user-select: none;
	-webkit-tap-highlight-color: transparent;
}
.jv-pc-rec > summary::-webkit-details-marker {
	display: none;
}
.jv-pc-rec > summary::marker {
	content: "";
}
.jv-pc-rec > summary:active {
	background: var(--card2);
}
.jv-pc-chev {
	flex: none;
	width: 8px;
	height: 8px;
	border-right: 1.7px solid var(--ink5);
	border-bottom: 1.7px solid var(--ink5);
	transform: rotate(-45deg);
	transition: transform 0.15s ease;
	margin-left: 2px;
}
.jv-pc-rec[open] > summary .jv-pc-chev {
	transform: rotate(45deg);
}
/* verb: a name-only record (missing or unreadable) gets no expander, so it needs
   the <summary> row's touch metrics by hand to line up with its expandable siblings. */
.jv-pc-rec-bare {
	display: flex;
	align-items: center;
	gap: 10px;
	min-height: 44px;
	padding: 8px 2px 8px 20px;
}
.jv-pc-rid {
	font-family: ui-monospace, Menlo, monospace;
	font-size: 12px;
	color: var(--ink9);
	font-weight: 500;
	flex: none;
}
.jv-pc-rtitle {
	font-size: 12px;
	color: var(--ink7);
	overflow-wrap: anywhere;
}
.jv-pc-rfields {
	font-size: 12px;
	color: var(--ink5);
	margin-left: auto;
	text-align: right;
	overflow-wrap: anywhere;
}
.jv-pc-rbody {
	padding: 2px 2px 10px 20px;
}
@media (max-width: 460px) {
	.jv-pc-rbody .jv-pc-diff {
		grid-template-columns: 1fr auto 1fr;
	}
	.jv-pc-rbody .jv-pc-lbl {
		grid-column: 1 / -1;
	}
}
@media (prefers-reduced-motion: reduce) {
	.jv-pc-chev {
		transition: none;
	}
}
.jv-pc-body {
	margin: 8px 0 0;
	padding: 10px;
	border-radius: 8px;
	background: var(--card2);
	color: var(--ink7);
	font-size: 12.5px;
	white-space: pre-wrap;
	overflow-wrap: anywhere;
	max-height: 220px;
	overflow-y: auto;
}
/* share: one chip per GRANTED permission (the effective value, not the request) */
.jv-pc-chip {
	display: inline-block;
	margin-left: 4px;
	padding: 2px 8px;
	border-radius: 999px;
	background: var(--card2);
	border: 1px solid var(--border);
	font-size: 11.5px;
	font-weight: 500;
	color: var(--ink8);
}
/* wiki: a full-body replace is a rewrite, not an edit - it must not read as routine */
.jv-pc-warn {
	margin-top: 8px;
	padding: 6px 10px;
	border-radius: 7px;
	background: var(--card2);
	border-left: 2px solid var(--red, var(--ink5));
	color: var(--ink9);
	font-weight: 500;
}
/* long-form bodies (skill instructions, wiki body, one email) - an 8k body must not
   dominate the sheet, so it collapses; the expander itself stays visible */
.jv-pc-expand {
	margin-top: 10px;
}
.jv-pc-expand summary {
	cursor: pointer;
	color: var(--ink5);
	font-size: 12px;
	user-select: none;
}
.jv-pc-expand .jv-pc-body {
	max-height: 260px;
}
.jv-pc-details {
	margin-top: 10px;
}
.jv-pc-details summary {
	cursor: pointer;
	color: var(--ink5);
	font-size: 12px;
}
.jv-pc-details pre {
	margin: 6px 0 0;
	padding: 10px;
	border-radius: 8px;
	background: var(--card2);
	font-size: 12px;
	white-space: pre-wrap;
	overflow-wrap: anywhere;
	max-height: 240px;
	overflow-y: auto;
}
</style>
