<script setup>
// Renders the server-built "what will change" summary (F9) for a parked gated
// write's confirmation card - replacing the raw-JSON dump. The structured card
// comes from jarvis/chat/confirm_card.py (kind = create | update | bulk_update |
// verb | email | bulk_email | share | assign | skill | wiki | method |
// batch_create | import) and is already perm-filtered + size-capped server-side.
// All values render through escaped interpolation (no v-html); the raw dry-run
// preview stays available behind a collapsed Details expander.
//
// A kind rendered here ALSO needs an entry in CARD_KINDS (@/lib/actionSummary):
// pendingCardOf returns null for a kind not in that set and the SPA falls back to
// the raw preview, so a branch added here alone is dead code.
import { computed } from "vue";

import { verbSentence } from "@/lib/actionSummary";

const props = defineProps({
	card: { type: Object, required: true },
	// Raw preview text (pretty JSON) for the Details expander; "" hides it.
	details: { type: String, default: "" },
});

const sentence = computed(() => (props.card.kind === "verb" ? verbSentence(props.card) : ""));

// The remainder line for a proposed child table. A helper rather than chained
// <template>s: with only extra_columns set, an inline chain renders a dangling
// " · ". unknown_columns MUST surface - the server counts keys it dropped rather
// than rendering them (they are not real child fields, so the save discards
// them), and a count nobody sees makes a silent drop silent again.
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
	<div class="jv-pcard">
		<!-- create: the fields the write will set -->
		<template v-if="card.kind === 'create'">
			<div class="jv-pcard-head">
				Create {{ card.doctype }}<template v-if="card.name"> · {{ card.name }}</template>
			</div>
			<dl v-if="card.rows.length" class="jv-pcard-fields">
				<template v-for="(r, i) in card.rows" :key="i"
					><dt>{{ r.label }}</dt>
					<dd>{{ r.value }}</dd></template
				>
			</dl>
			<div v-else-if="!(card.tables || []).length" class="jv-pcard-empty">
				No fields set.
			</div>
			<!-- proposed child tables: "5 rows" would hide an invoice's line items -->
			<div v-for="(t, ti) in card.tables || []" :key="'t' + ti" class="jv-pcard-table">
				<div class="jv-pcard-table-head">
					{{ t.label }} · {{ t.count }} row<template v-if="t.count !== 1">s</template>
				</div>
				<div class="jv-pcard-table-scroll">
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
				<div v-if="tableNote(t)" class="jv-pcard-more">{{ tableNote(t) }}</div>
			</div>
		</template>

		<!-- update: from -> to diff -->
		<template v-else-if="card.kind === 'update'">
			<div class="jv-pcard-head">
				Update {{ card.doctype }}<template v-if="card.name"> · {{ card.name }}</template
				><template v-if="card.title"> · {{ card.title }}</template>
			</div>
			<div v-for="(d, i) in card.diff" :key="i" class="jv-pcard-diffrow">
				<span class="jv-pcard-lbl">{{ d.label }}</span>
				<span class="jv-pcard-from">{{ d.from || "(empty)" }}</span>
				<span class="jv-pcard-arrow">→</span>
				<span class="jv-pcard-to">{{ d.to || "(empty)" }}</span>
			</div>
			<div v-if="!card.diff.length" class="jv-pcard-empty">No field changes.</div>
		</template>

		<!-- verb: submit / cancel / delete / amend / apply_workflow_action -->
		<!-- ORDER IS LOAD-BEARING: records div -> targets <ul v-else-if> -> +N more.
		     v-else-if chains to the immediately preceding sibling carrying a v-if, so
		     putting the +N more div between them would chain the <ul> to IT - and with
		     extra === 0 (always, via the F16 batch cap) every bulk card would render
		     its targets twice, once as records and once as the old list. -->
		<template v-else-if="card.kind === 'verb'">
			<div class="jv-pcard-verb">{{ sentence }}</div>
			<div v-if="(card.records || []).length" class="jv-pcard-recs">
				<template v-for="(r, i) in card.records" :key="'vr' + i">
					<details v-if="r.rows.length" class="jv-rec" :open="i === 0">
						<summary>
							<span class="jv-chev" aria-hidden="true"></span>
							<span class="jv-rec-id">{{ r.name }}</span>
							<span v-if="r.title" class="jv-rec-title">{{ r.title }}</span>
						</summary>
						<dl class="jv-pcard-fields">
							<template v-for="(f, j) in r.rows" :key="'vf' + j"
								><dt>{{ f.label }}</dt>
								<dd>{{ f.value }}</dd></template
							>
						</dl>
					</details>
					<div v-else class="jv-rec jv-rec-bare">
						<span class="jv-rec-id">{{ r.name }}</span>
						<span v-if="r.title" class="jv-rec-title">{{ r.title }}</span>
					</div>
				</template>
			</div>
			<ul v-else-if="card.count > 1 && card.targets.length" class="jv-pcard-list">
				<li v-for="(t, i) in card.targets" :key="i">{{ t }}</li>
				<li v-if="card.extra > 0" class="jv-pcard-more">+{{ card.extra }} more</li>
			</ul>
			<div v-if="(card.records || []).length && card.extra > 0" class="jv-pcard-more">
				+{{ card.extra }} more
			</div>
		</template>

		<!-- email: cc/bcc/print format are v-if'd so an empty one adds no row. Without
		     them "you could not see who was copied" stayed true no matter what the
		     server sent. The body is a _MAX_BODY budget now, so it rides an expander -
		     open by default: this is the one tool that cannot be recalled. -->
		<template v-else-if="card.kind === 'email'">
			<div class="jv-pcard-kv">
				<span>To</span><b>{{ card.to }}</b>
			</div>
			<div v-if="card.cc" class="jv-pcard-kv">
				<span>Cc</span><b>{{ card.cc }}</b>
			</div>
			<div v-if="card.bcc" class="jv-pcard-kv">
				<span>Bcc</span><b>{{ card.bcc }}</b>
			</div>
			<div class="jv-pcard-kv">
				<span>Subject</span><b>{{ card.subject }}</b>
			</div>
			<div v-if="card.print_format" class="jv-pcard-kv">
				<span>Print format</span><b>{{ card.print_format }}</b>
			</div>
			<details v-if="card.body" class="jv-pcard-expand" open>
				<summary>Message</summary>
				<pre class="jv-pcard-body">{{ card.body }}</pre>
			</details>
		</template>

		<!-- bulk email: a mail-merge. Every message has its OWN recipient, subject and
		     body, so each gets its own expander - a count cannot stand in for 20
		     distinct irreversible emails. -->
		<template v-else-if="card.kind === 'bulk_email'">
			<div class="jv-pcard-head">
				Send {{ card.count }} email<template v-if="card.count !== 1">s</template>
			</div>
			<p class="jv-pcard-caption">
				Each message has its own recipient and body. Click one to read it.
			</p>
			<div class="jv-pcard-recs">
				<details
					v-for="(m, i) in card.messages"
					:key="'me' + i"
					class="jv-rec"
					:open="i === 0"
				>
					<summary>
						<span class="jv-chev" aria-hidden="true"></span>
						<span class="jv-rec-id">{{ m.recipients }}</span>
						<span v-if="m.subject" class="jv-rec-title">{{ m.subject }}</span>
					</summary>
					<div class="jv-rec-body">
						<div v-if="m.name" class="jv-pcard-kv">
							<span>About</span><b>{{ m.name }}</b>
						</div>
						<div v-if="m.cc" class="jv-pcard-kv">
							<span>Cc</span><b>{{ m.cc }}</b>
						</div>
						<div v-if="m.bcc" class="jv-pcard-kv">
							<span>Bcc</span><b>{{ m.bcc }}</b>
						</div>
						<pre v-if="m.body" class="jv-pcard-body">{{ m.body }}</pre>
					</div>
				</details>
			</div>
			<div v-if="card.extra > 0" class="jv-pcard-more">
				+{{ card.extra }} more · full list in Details
			</div>
		</template>

		<!-- share: grantee + the permission flags. "Read for one person" and
		     "everyone + write + share" rendered identically before, and those grants are
		     the exact reason share_doc gates. Flags are filtered IN THE EXPRESSION:
		     v-for + v-if on one element is invalid in Vue 3 (v-if wins and cannot see
		     the loop variable), which only warns at compile and throws at render. -->
		<template v-else-if="card.kind === 'share'">
			<div class="jv-pcard-head">
				Share {{ card.doctype
				}}<template v-if="card.count > 1"> · {{ card.count }} records</template>
			</div>
			<div class="jv-pcard-kv">
				<span>Shared with</span><b>{{ card.grantee }}</b>
			</div>
			<div class="jv-pcard-kv">
				<span>Grants</span>
				<b>
					<span
						v-for="f in card.flags.filter((x) => x.on)"
						:key="f.label"
						class="jv-chip"
						>{{ f.label }}</span
					>
					<span v-if="!card.flags.some((x) => x.on)" class="jv-pcard-empty">None</span>
				</b>
			</div>
			<div class="jv-pcard-kv">
				<span>Notify by email</span><b>{{ card.notify ? "Yes" : "No" }}</b>
			</div>
			<div v-if="(card.records || []).length" class="jv-pcard-recs">
				<template v-for="(r, i) in card.records" :key="'sr' + i">
					<details v-if="r.rows.length" class="jv-rec" :open="i === 0">
						<summary>
							<span class="jv-chev" aria-hidden="true"></span>
							<span class="jv-rec-id">{{ r.name }}</span>
							<span v-if="r.title" class="jv-rec-title">{{ r.title }}</span>
						</summary>
						<dl class="jv-pcard-fields">
							<template v-for="(f, j) in r.rows" :key="'sf' + j"
								><dt>{{ f.label }}</dt>
								<dd>{{ f.value }}</dd></template
							>
						</dl>
					</details>
					<div v-else class="jv-rec jv-rec-bare">
						<span class="jv-rec-id">{{ r.name }}</span>
						<span v-if="r.title" class="jv-rec-title">{{ r.title }}</span>
					</div>
				</template>
			</div>
			<div v-if="card.extra > 0" class="jv-pcard-more">+{{ card.extra }} more</div>
		</template>

		<!-- assign: the assignee and the description that gets EMAILED to them -->
		<template v-else-if="card.kind === 'assign'">
			<div class="jv-pcard-head">
				Assign {{ card.doctype
				}}<template v-if="card.count > 1"> · {{ card.count }} records</template>
			</div>
			<div class="jv-pcard-kv">
				<span>Assign to</span><b>{{ card.assignee }}</b>
			</div>
			<div v-if="card.priority" class="jv-pcard-kv">
				<span>Priority</span><b>{{ card.priority }}</b>
			</div>
			<div v-if="card.date" class="jv-pcard-kv">
				<span>Due date</span><b>{{ card.date }}</b>
			</div>
			<div class="jv-pcard-kv">
				<span>Notify by email</span><b>{{ card.notify ? "Yes" : "No" }}</b>
			</div>
			<pre v-if="card.description" class="jv-pcard-body">{{ card.description }}</pre>
			<div v-if="(card.records || []).length" class="jv-pcard-recs">
				<template v-for="(r, i) in card.records" :key="'ar' + i">
					<details v-if="r.rows.length" class="jv-rec" :open="i === 0">
						<summary>
							<span class="jv-chev" aria-hidden="true"></span>
							<span class="jv-rec-id">{{ r.name }}</span>
							<span v-if="r.title" class="jv-rec-title">{{ r.title }}</span>
						</summary>
						<dl class="jv-pcard-fields">
							<template v-for="(f, j) in r.rows" :key="'af' + j"
								><dt>{{ f.label }}</dt>
								<dd>{{ f.value }}</dd></template
							>
						</dl>
					</details>
					<div v-else class="jv-rec jv-rec-bare">
						<span class="jv-rec-id">{{ r.name }}</span>
						<span v-if="r.title" class="jv-rec-title">{{ r.title }}</span>
					</div>
				</template>
			</div>
			<div v-if="card.extra > 0" class="jv-pcard-more">+{{ card.extra }} more</div>
		</template>

		<!-- skill: PERSISTENT AGENT INSTRUCTIONS. The card was the literal string
		     "create_custom_skill". Scope is the tool's EFFECTIVE value, not the request. -->
		<template v-else-if="card.kind === 'skill'">
			<div class="jv-pcard-head">
				Create skill<template v-if="card.skill_name"> · {{ card.skill_name }}</template>
			</div>
			<div class="jv-pcard-kv">
				<span>Scope</span><b>{{ card.scope }}</b>
			</div>
			<div class="jv-pcard-kv">
				<span>User invocable</span><b>{{ card.user_invocable ? "Yes" : "No" }}</b>
			</div>
			<div v-if="card.description" class="jv-pcard-kv">
				<span>Description</span><b>{{ card.description }}</b>
			</div>
			<details v-if="card.instructions" class="jv-pcard-expand" open>
				<summary>Instructions · shape every future session</summary>
				<pre class="jv-pcard-body">{{ card.instructions }}</pre>
			</details>
		</template>

		<!-- wiki: replace_body_md is a full rewrite and says so -->
		<template v-else-if="card.kind === 'wiki'">
			<div class="jv-pcard-head">
				Update wiki<template v-if="card.slug"> · {{ card.slug }}</template>
			</div>
			<div v-if="card.title" class="jv-pcard-kv">
				<span>Title</span><b>{{ card.title }}</b>
			</div>
			<div v-if="card.scope" class="jv-pcard-kv">
				<span>Scope</span><b>{{ card.scope }}</b>
			</div>
			<div v-if="card.page_type" class="jv-pcard-kv">
				<span>Page type</span><b>{{ card.page_type }}</b>
			</div>
			<div v-if="card.ref" class="jv-pcard-kv">
				<span>About</span><b>{{ card.ref }}</b>
			</div>
			<div v-if="card.summary" class="jv-pcard-kv">
				<span>Summary</span><b>{{ card.summary }}</b>
			</div>
			<div v-if="card.mode === 'replace'" class="jv-pcard-warn">
				Replaces the entire page body
			</div>
			<div v-else-if="card.mode === 'append'" class="jv-pcard-caption">
				Appends a section; nothing already recorded is removed.
			</div>
			<details v-if="card.body" class="jv-pcard-expand" :open="card.mode === 'replace'">
				<summary>{{ card.mode === "replace" ? "New body" : "Added section" }}</summary>
				<pre class="jv-pcard-body">{{ card.body }}</pre>
			</details>
		</template>

		<!-- run_method -->
		<template v-else-if="card.kind === 'method'">
			<div class="jv-pcard-kv">
				<span>Method</span><b>{{ card.method }}</b>
			</div>
			<dl v-if="Object.keys(card.args).length" class="jv-pcard-fields">
				<template v-for="(v, k) in card.args" :key="k"
					><dt>{{ k }}</dt>
					<dd>{{ v }}</dd></template
				>
			</dl>
		</template>

		<!-- batch create -->
		<template v-else-if="card.kind === 'batch_create'">
			<div class="jv-pcard-head">
				Create {{ card.count }} record<template v-if="card.count !== 1">s</template>
			</div>
			<div v-if="(card.records || []).length" class="jv-pcard-recs">
				<details
					v-for="(r, i) in card.records"
					:key="'br' + i"
					class="jv-rec"
					:open="i === 0"
				>
					<summary>
						<span class="jv-chev" aria-hidden="true"></span>
						<span class="jv-rec-id"
							>{{ r.doctype }} <b>{{ r.name }}</b></span
						>
					</summary>
					<div class="jv-rec-body">
						<dl v-if="r.rows.length" class="jv-pcard-fields">
							<template v-for="(f, j) in r.rows" :key="'bf' + j"
								><dt>{{ f.label }}</dt>
								<dd>{{ f.value }}</dd></template
							>
						</dl>
						<div v-if="r.extra > 0" class="jv-pcard-more">
							+{{ r.extra }} more fields
						</div>
						<div
							v-for="(t, ti) in r.tables || []"
							:key="'bt' + ti"
							class="jv-pcard-table"
						>
							<div class="jv-pcard-table-head">
								{{ t.label }} · {{ t.count }} row<template v-if="t.count !== 1"
									>s</template
								>
							</div>
							<div class="jv-pcard-table-scroll">
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
							<div v-if="tableNote(t)" class="jv-pcard-more">{{ tableNote(t) }}</div>
						</div>
					</div>
				</details>
			</div>
			<ul v-else class="jv-pcard-list">
				<li v-for="(r, i) in card.rows" :key="i">
					{{ r.doctype }} <b>{{ r.name }}</b>
				</li>
				<li v-if="card.extra > 0" class="jv-pcard-more">+{{ card.extra }} more</li>
			</ul>
			<div v-if="(card.records || []).length && card.extra > 0" class="jv-pcard-more">
				+{{ card.extra }} more
			</div>
		</template>

		<!-- bulk update: one collapsible from→to preview per record -->
		<template v-else-if="card.kind === 'bulk_update'">
			<div class="jv-pcard-head">
				Update {{ card.count }} {{ card.doctype }} record<template v-if="card.count !== 1"
					>s</template
				><span v-if="card.varying" class="jv-pcard-sub"> · varying changes</span>
			</div>
			<p class="jv-pcard-caption">Click a record to review its changes.</p>
			<div class="jv-pcard-recs">
				<details v-for="(r, i) in card.records" :key="i" class="jv-rec" :open="i === 0">
					<summary>
						<span class="jv-chev" aria-hidden="true"></span>
						<span class="jv-rec-id">{{ r.name }}</span>
						<span v-if="r.title" class="jv-rec-title">{{ r.title }}</span>
						<span v-if="r.fields?.length" class="jv-rec-fields">{{
							r.fields.join(" · ")
						}}</span>
					</summary>
					<div class="jv-rec-body">
						<div v-for="(d, j) in r.diff" :key="j" class="jv-pcard-diffrow">
							<span class="jv-pcard-lbl">{{ d.label }}</span>
							<span class="jv-pcard-from">{{ d.from || "(empty)" }}</span>
							<span class="jv-pcard-arrow">→</span>
							<span class="jv-pcard-to">{{ d.to || "(empty)" }}</span>
						</div>
						<div v-if="!r.diff.length" class="jv-pcard-empty">No field changes.</div>
					</div>
				</details>
			</div>
			<div v-if="card.extra > 0" class="jv-pcard-more">
				+{{ card.extra }} more · full list in Details
			</div>
		</template>

		<!-- import: run_import (bulk load via Frappe's Data Import). Rows vs records
		     shown distinctly - a flat file with a mapped child table can fan out into
		     more records than sheet rows (confirm_card.py::_import_card). Cells are
		     already secret-masked server-side ("[hidden]"); escaped interpolation
		     only, never v-html. -->
		<template v-else-if="card.kind === 'import'">
			<div class="jv-pcard-head">Import into {{ card.doctype }}</div>
			<div class="jv-pcard-kv">
				<span>Import type</span><b>{{ card.import_type }}</b>
			</div>
			<div class="jv-pcard-kv">
				<span>File</span><b>{{ card.file }}</b>
			</div>
			<div class="jv-pcard-kv">
				<span>Rows → records</span><b>{{ card.total_rows }} → {{ card.total_records }}</b>
			</div>
			<div v-if="card.submit_after_import" class="jv-pcard-warn">
				Will submit each record after import
			</div>
			<div v-if="(card.sample?.rows || []).length" class="jv-pcard-table">
				<div class="jv-pcard-table-head">Preview</div>
				<div class="jv-pcard-table-scroll">
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
				<div v-if="card.sample.extra_cols > 0" class="jv-pcard-more">
					+{{ card.sample.extra_cols }} more columns
				</div>
			</div>
			<div v-else class="jv-pcard-empty">No preview rows.</div>
			<ul v-if="(card.advisory || []).length" class="jv-pcard-list">
				<li v-for="(a, i) in card.advisory" :key="'ia' + i">{{ a }}</li>
			</ul>
			<div v-if="(card.columns?.unmapped || []).length" class="jv-pcard-more">
				Unmapped columns ignored: {{ card.columns.unmapped.join(", ") }}
			</div>
		</template>

		<details v-if="details" class="jv-pcard-details">
			<summary>Details</summary>
			<pre>{{ details }}</pre>
		</details>
	</div>
</template>

<style scoped>
.jv-pcard {
	font-size: 12.5px;
	color: var(--text);
}
.jv-pcard-head {
	font-weight: 600;
	margin-bottom: 6px;
	overflow-wrap: anywhere;
}
.jv-pcard-verb {
	font-weight: 500;
	overflow-wrap: anywhere;
}
.jv-pcard-fields {
	display: grid;
	grid-template-columns: auto 1fr;
	gap: 3px 12px;
	margin: 0;
}
.jv-pcard-fields dt {
	color: var(--text-3);
}
.jv-pcard-fields dd {
	margin: 0;
	text-align: right;
	overflow-wrap: anywhere;
}
.jv-pcard-diffrow {
	display: grid;
	grid-template-columns: 1fr auto auto auto;
	gap: 8px;
	align-items: baseline;
	padding: 2px 0;
}
.jv-pcard-lbl {
	color: var(--text-3);
}
.jv-pcard-from {
	color: var(--text-3);
	text-decoration: line-through;
	overflow-wrap: anywhere;
}
.jv-pcard-arrow {
	color: var(--text-3);
}
.jv-pcard-to {
	color: var(--green, var(--text));
	font-weight: 500;
	overflow-wrap: anywhere;
}
.jv-pcard-empty {
	color: var(--text-3);
	font-style: italic;
}
.jv-pcard-kv {
	display: flex;
	justify-content: space-between;
	gap: 12px;
	padding: 2px 0;
}
.jv-pcard-kv span {
	color: var(--text-3);
}
.jv-pcard-kv b {
	font-weight: 500;
	text-align: right;
	overflow-wrap: anywhere;
}
.jv-pcard-list {
	margin: 4px 0 0;
	padding-left: 18px;
}
.jv-pcard-list li {
	padding: 1px 0;
	overflow-wrap: anywhere;
}
.jv-pcard-more {
	list-style: none;
	color: var(--text-3);
}
/* create: a proposed child table, scrolling inside its own box so the card never
   scrolls the page sideways */
.jv-pcard-table {
	margin-top: 10px;
}
.jv-pcard-table-head {
	font-size: 11.5px;
	color: var(--text-3);
	margin-bottom: 4px;
}
.jv-pcard-table-scroll {
	overflow-x: auto;
	max-height: 240px;
	overflow-y: auto;
}
.jv-pcard-table table {
	border-collapse: collapse;
	width: 100%;
	font-size: 12px;
}
.jv-pcard-table th,
.jv-pcard-table td {
	text-align: left;
	padding: 4px 8px;
	border-bottom: 1px solid var(--surface-2);
	white-space: nowrap;
}
.jv-pcard-table th {
	color: var(--text-3);
	font-weight: 500;
}
.jv-pcard-table td {
	font-variant-numeric: tabular-nums;
}
/* scoped: .jv-pcard-more is shared with the verb/batch_create <li>s */
.jv-pcard-table .jv-pcard-more {
	font-size: 11.5px;
	margin-top: 4px;
}
/* bulk update: collapsible per-record from→to list */
.jv-pcard-sub {
	font-weight: 400;
	color: var(--text-3);
}
.jv-pcard-caption {
	font-size: 11.5px;
	color: var(--text-3);
	margin: 0 0 8px;
}
.jv-pcard-recs {
	display: flex;
	flex-direction: column;
	max-height: 320px;
	overflow-y: auto;
	overscroll-behavior: contain;
	-webkit-overflow-scrolling: touch;
}
.jv-rec {
	border-top: 1px solid var(--surface-2);
}
.jv-rec:first-child {
	border-top: none;
}
.jv-rec > summary {
	list-style: none;
	cursor: pointer;
	display: flex;
	align-items: center;
	gap: 9px;
	min-height: 38px;
	padding: 6px 2px;
	border-radius: 6px;
	user-select: none;
	-webkit-tap-highlight-color: transparent;
}
.jv-rec > summary::-webkit-details-marker {
	display: none;
}
.jv-rec > summary::marker {
	content: "";
}
.jv-rec > summary:hover {
	background: var(--surface-1);
}
.jv-rec > summary:focus-visible {
	outline: 2px solid var(--blue, var(--text));
	outline-offset: -2px;
}
.jv-chev {
	flex: none;
	width: 7px;
	height: 7px;
	border-right: 1.6px solid var(--text-3);
	border-bottom: 1.6px solid var(--text-3);
	transform: rotate(-45deg);
	transition: transform 0.15s ease;
	margin-left: 2px;
}
.jv-rec[open] > summary .jv-chev {
	transform: rotate(45deg);
}
/* verb: a name-only record (missing or unreadable) gets no expander, so it needs
   the <summary> row's metrics by hand to line up with its expandable siblings. */
.jv-rec-bare {
	display: flex;
	align-items: center;
	gap: 9px;
	min-height: 38px;
	padding: 6px 2px 6px 18px;
}
.jv-rec-id {
	font-family: ui-monospace, "SF Mono", Menlo, monospace;
	font-size: 11.5px;
	color: var(--text);
	font-weight: 500;
	flex: none;
}
.jv-rec-title {
	font-size: 11.5px;
	color: var(--text);
	overflow-wrap: anywhere;
}
.jv-rec-fields {
	font-size: 11.5px;
	color: var(--text-3);
	margin-left: auto;
	text-align: right;
	overflow-wrap: anywhere;
}
.jv-rec-body {
	padding: 2px 2px 8px 19px;
}
@media (max-width: 460px) {
	.jv-rec-body .jv-pcard-diffrow {
		grid-template-columns: 1fr auto 1fr;
	}
	.jv-rec-body .jv-pcard-lbl {
		grid-column: 1 / -1;
	}
}
@media (prefers-reduced-motion: reduce) {
	.jv-chev {
		transition: none;
	}
}
.jv-pcard-body {
	margin: 6px 0 0;
	padding: 8px 10px;
	background: var(--surface-2);
	border-radius: 7px;
	white-space: pre-wrap;
	word-break: break-word;
	max-height: 200px;
	overflow-y: auto;
	font-size: 12px;
	line-height: 1.5;
}
/* share: one chip per GRANTED permission (filtered server-side value, not the request) */
.jv-chip {
	display: inline-block;
	margin-left: 4px;
	padding: 1px 7px;
	border-radius: 999px;
	background: var(--surface-2);
	border: 1px solid var(--border);
	font-size: 11px;
	font-weight: 500;
	color: var(--text);
}
/* wiki: a full-body replace is a rewrite, not an edit - it must not read as routine */
.jv-pcard-warn {
	margin-top: 6px;
	padding: 5px 9px;
	border-radius: 6px;
	background: var(--surface-2);
	border-left: 2px solid var(--red, var(--text-3));
	color: var(--text);
	font-weight: 500;
}
/* long-form bodies (skill instructions, wiki body, one email) - an 8k body must not
   dominate the card, so it collapses; the expander itself stays visible */
.jv-pcard-expand {
	margin-top: 8px;
}
.jv-pcard-expand summary {
	cursor: pointer;
	color: var(--text-3);
	font-size: 11.5px;
	user-select: none;
}
.jv-pcard-expand .jv-pcard-body {
	max-height: 260px;
}
.jv-pcard-details {
	margin-top: 8px;
}
.jv-pcard-details summary {
	cursor: pointer;
	color: var(--text-3);
	font-size: 11.5px;
	user-select: none;
}
.jv-pcard-details pre {
	margin: 6px 0 0;
	padding: 9px 11px;
	background: var(--surface-2);
	border: 1px solid var(--border);
	border-radius: 7px;
	font-family: ui-monospace, "SF Mono", Menlo, monospace;
	font-size: 11.5px;
	line-height: 1.5;
	white-space: pre-wrap;
	word-break: break-word;
	max-height: 260px;
	overflow-y: auto;
}
</style>
