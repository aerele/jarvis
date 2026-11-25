<template>
	<div class="flex h-full flex-col overflow-hidden">
		<LayoutHeader>
			<template #left-header>
				<Breadcrumbs :items="[{ label: __('User activity') }]" />
			</template>
		</LayoutHeader>

		<div class="jv-scroll-overlay min-h-0 flex-1 overflow-y-auto">
			<div class="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 sm:py-8">
				<header class="mb-5">
					<h1 class="text-2xl font-semibold text-ink-gray-9">
						{{ __("User activity") }}
					</h1>
					<p v-if="scopeLine" class="mt-1 text-p-base text-ink-gray-6">
						{{ scopeLine }}
					</p>
				</header>

				<!-- Filters. One primary action: Apply. Activity and outcome narrow the
				     Events tab only (a summary that hid rows by outcome would hide the very
				     flags it exists to raise). They stay usable on the Summary, so a filter
				     can be set before opening Events; the line below them says what they do
				     and the two selects point at it (aria-describedby). -->
				<form
					class="mb-5 flex flex-col gap-3"
					:aria-label="__('Filters')"
					novalidate
					@submit.prevent="apply"
				>
					<div
						class="grid grid-cols-1 gap-3 sm:grid-cols-2 min-[769px]:grid-cols-[repeat(5,minmax(0,1fr))_auto] min-[769px]:items-end"
					>
						<FormControl
							type="date"
							:label="__('From')"
							:modelValue="draft.from"
							:aria-invalid="rangeError ? 'true' : undefined"
							:aria-describedby="rangeError ? 'ua-range-error' : undefined"
							@update:modelValue="(v) => (draft.from = v || '')"
						/>
						<FormControl
							type="date"
							:label="__('To')"
							:modelValue="draft.to"
							:aria-invalid="rangeError ? 'true' : undefined"
							:aria-describedby="rangeError ? 'ua-range-error' : undefined"
							@update:modelValue="(v) => (draft.to = v || '')"
						/>
						<div class="flex min-w-0 flex-col gap-1.5">
							<span id="ua-person-label" class="text-xs text-ink-gray-6">{{
								__("Person")
							}}</span>
							<!-- Not a role=group: the trigger carries the label itself, and a
							     labelled group would read it a second time. -->
							<div data-test="person-group" @focusin="userSearch.prime">
								<Autocomplete
									:options="userSearch.options.value"
									:loading="userSearch.loading.value"
									:modelValue="personValue"
									:placeholder="__('Anyone')"
									@update:query="userSearch.onQuery"
									@update:modelValue="pickPerson"
								>
									<!-- Our own trigger: the stock one is a bare <button>, which is
									     type=submit inside this form, so Enter or a click on it
									     submitted the filters. -->
									<template #target="{ togglePopover, isOpen }">
										<button
											ref="personTrigger"
											type="button"
											aria-haspopup="listbox"
											:aria-expanded="isOpen ? 'true' : 'false'"
											aria-labelledby="ua-person-label ua-person-value"
											class="flex h-7 w-full items-center justify-between gap-2 rounded border border-transparent bg-surface-gray-2 px-2 py-1 transition-colors hover:bg-surface-gray-3 focus:border-outline-gray-4 focus:outline-none focus:ring-2 focus:ring-outline-gray-3"
											:class="{ 'bg-surface-gray-3': isOpen }"
											data-test="person-trigger"
											@click="() => togglePopover()"
										>
											<span
												id="ua-person-value"
												class="truncate text-base leading-5"
												:class="
													draft.user
														? 'text-ink-gray-8'
														: 'text-ink-gray-6'
												"
												:title="draft.user ? personValue.label : undefined"
												>{{
													draft.user ? personValue.label : __("Anyone")
												}}</span
											>
											<FeatherIcon
												name="chevron-down"
												class="size-4 shrink-0 text-ink-gray-6"
												aria-hidden="true"
											/>
										</button>
									</template>
								</Autocomplete>
							</div>
						</div>
						<FormControl
							type="select"
							:label="__('Activity')"
							:options="activityOptions()"
							:aria-describedby="tab === 'summary' ? 'ua-events-only' : undefined"
							:modelValue="draft.activity"
							@update:modelValue="(v) => (draft.activity = v || '')"
						/>
						<FormControl
							type="select"
							:label="__('Outcome')"
							:options="outcomeOptions()"
							:aria-describedby="tab === 'summary' ? 'ua-events-only' : undefined"
							:modelValue="draft.outcome"
							@update:modelValue="(v) => (draft.outcome = v || '')"
						/>
						<Button
							type="submit"
							variant="solid"
							:label="__('Apply')"
							:loading="busy"
							class="sm:col-span-2 min-[769px]:col-span-1"
						/>
					</div>
					<p
						v-if="rangeError"
						id="ua-range-error"
						class="text-p-sm text-ink-red-4"
						role="alert"
					>
						{{ rangeError }}
					</p>
					<p
						v-if="tab === 'summary'"
						id="ua-events-only"
						class="text-p-xs text-ink-gray-6"
					>
						{{ __("Activity and outcome narrow the Events tab only.") }}
					</p>
				</form>

				<!-- Tabs: a real tablist (roving tabindex, arrow keys, Home / End). -->
				<div
					role="tablist"
					:aria-label="__('View')"
					class="mb-4 flex gap-1 border-b"
					@keydown="onTabKeydown"
				>
					<button
						v-for="t in TABS"
						:id="'ua-tab-' + t.key"
						:key="t.key"
						ref="tabButtons"
						type="button"
						role="tab"
						class="-mb-px h-8 rounded-t px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-outline-gray-3"
						:class="
							tab === t.key
								? 'border-b-2 border-outline-gray-5 font-medium text-ink-gray-9'
								: 'text-ink-gray-6 hover:text-ink-gray-8'
						"
						:aria-selected="tab === t.key ? 'true' : 'false'"
						:aria-controls="tab === t.key ? 'ua-panel' : undefined"
						:tabindex="tab === t.key ? 0 : -1"
						@click="select(t.key)"
					>
						{{ t.label }}
					</button>
				</div>

				<div v-if="bannerNotes.length" class="mb-4" data-test="notes-banner" role="status">
					<Banner :type="bannerType" :message="bannerNotes[0]">
						<p
							v-for="(n, i) in bannerNotes.slice(1)"
							:key="i"
							class="mt-1 text-p-xs text-ink-gray-7"
						>
							{{ n }}
						</p>
					</Banner>
				</div>

				<div
					id="ua-panel"
					ref="panel"
					role="tabpanel"
					:aria-labelledby="'ua-tab-' + tab"
					:aria-busy="active.status === 'loading' ? 'true' : 'false'"
					tabindex="-1"
					class="outline-none"
				>
					<!-- loading -->
					<div
						v-if="active.status === 'loading'"
						role="status"
						class="flex flex-col gap-2"
					>
						<span class="sr-only">{{ __("Loading activity") }}</span>
						<div
							v-for="i in 6"
							:key="i"
							class="h-10 animate-pulse rounded-md bg-surface-gray-2"
							aria-hidden="true"
						/>
					</div>

					<!-- error -->
					<div
						v-else-if="active.status === 'error'"
						class="flex flex-col items-center gap-3 py-12 text-center"
						role="alert"
					>
						<FeatherIcon name="alert-triangle" class="size-8 text-ink-gray-4" />
						<span class="text-base text-ink-gray-6">{{ active.error }}</span>
						<Button
							variant="subtle"
							:label="__('Retry')"
							iconLeft="refresh-cw"
							@click="reload"
						/>
					</div>

					<!-- Summary -->
					<template v-else-if="tab === 'summary' && active.status === 'ready'">
						<div
							v-if="!people.length"
							class="flex flex-col items-center gap-2 py-12 text-center"
							role="status"
						>
							<FeatherIcon name="activity" class="size-8 text-ink-gray-4" />
							<span
								v-if="firstRun"
								class="text-base text-ink-gray-6"
								data-test="first-run"
								>{{
									__(
										"Nothing has been recorded yet. Activity appears here as people use Jarvis."
									)
								}}</span
							>
							<template v-else>
								<span class="text-base text-ink-gray-6">{{
									__("No activity in this range")
								}}</span>
								<span class="text-p-sm text-ink-gray-6">{{
									recordedFrom ||
									__("Try a wider date range, or clear the person filter.")
								}}</span>
							</template>
						</div>
						<template v-else>
							<h2 class="sr-only">{{ __("Activity by person") }}</h2>
							<p class="mb-2 text-p-sm text-ink-gray-6" data-test="summary-count">
								{{ summaryCount }}
							</p>
							<div
								class="overflow-x-auto rounded-lg border border-outline-gray-1"
								:class="wideClass(SUMMARY_MIN)"
								data-test="summary-table"
							>
								<div
									role="table"
									:aria-label="__('Activity by person')"
									class="[.ua-wide_&]:min-w-[1024px]"
								>
									<div role="rowgroup" class="hidden [.ua-wide_&]:block">
										<div
											role="row"
											class="grid grid-cols-[minmax(10rem,1.6fr)_repeat(11,minmax(4rem,1fr))] gap-x-3 border-b bg-surface-gray-1 px-3 py-2 text-xs font-medium text-ink-gray-6"
										>
											<!-- Sticky: below ~1000px the table scrolls sideways and the
											     name must not scroll away from its numbers. -->
											<div role="columnheader">
												{{ __("Person") }}
											</div>
											<div
												v-for="c in columns"
												:key="c.key"
												role="columnheader"
												class="text-right"
											>
												{{ c.label }}
											</div>
										</div>
									</div>
									<div role="rowgroup">
										<div
											v-for="p in people"
											:key="p.user || '__none__'"
											role="row"
											data-test="person-row"
											class="border-b px-3 py-3 last:border-b-0"
										>
											<div
												class="grid grid-cols-1 gap-x-3 gap-y-1 [.ua-wide_&]:grid-cols-[minmax(10rem,1.6fr)_repeat(11,minmax(4rem,1fr))] [.ua-wide_&]:items-center"
											>
												<div role="rowheader" class="min-w-0">
													<button
														v-if="p.user"
														type="button"
														class="block max-w-full rounded text-left outline-none hover:underline focus-visible:ring-2 focus-visible:ring-outline-gray-3"
														:aria-label="
															__('View events for {0}', [
																p.name || p.user,
															])
														"
														data-test="person-link"
														@click="drill(p)"
													>
														<span
															class="block truncate text-sm font-medium text-ink-gray-8"
															:title="p.name || p.user"
															>{{ p.name || p.user }}</span
														>
													</button>
													<span
														v-else
														class="block truncate text-sm font-medium text-ink-gray-8"
														:title="p.name || __('No person')"
														>{{ p.name || __("No person") }}</span
													>
													<span
														v-if="p.user"
														class="block truncate text-xs text-ink-gray-6"
														:title="p.user"
														>{{ p.user }}</span
													>
												</div>
												<div
													v-for="c in columns"
													:key="c.key"
													role="cell"
													class="flex items-baseline justify-between gap-3 text-sm [.ua-wide_&]:justify-end"
													:class="
														p[c.key]
															? 'text-ink-gray-8'
															: 'text-ink-gray-6 [.ua-narrow_&]:hidden'
													"
												>
													<span
														class="text-xs text-ink-gray-6 [.ua-wide_&]:hidden"
														>{{ c.label }}</span
													>
													<span class="tabular-nums">{{
														formatCount(p[c.key])
													}}</span>
												</div>
											</div>
											<!-- A row's children must be cells: the chips sit in one. -->
											<div
												v-if="(p.flags || []).length"
												role="cell"
												class="mt-2"
											>
												<ul
													class="flex flex-wrap gap-1.5"
													:aria-label="__('Flags')"
												>
													<li v-for="f in p.flags" :key="f.key">
														<Badge
															variant="subtle"
															:theme="flagTheme(f.key)"
															:label="f.label"
															data-test="flag-chip"
														/>
													</li>
												</ul>
											</div>
										</div>
									</div>
								</div>
							</div>
						</template>

						<!-- The flag legend is shown with the empty state too: a first-time admin
						     learns what the page will raise before anything has happened. -->
						<section v-if="legend.length" class="mt-5" aria-labelledby="ua-legend">
							<h2 id="ua-legend" class="mb-2 text-sm font-medium text-ink-gray-8">
								{{ __("What the flags mean") }}
							</h2>
							<dl class="grid grid-cols-1 gap-x-6 gap-y-1.5 sm:grid-cols-2">
								<div v-for="l in legend" :key="l.key" class="text-p-sm">
									<dt class="inline font-medium text-ink-gray-8">
										{{ l.title }}:
									</dt>
									<dd class="ml-1 inline text-ink-gray-6">{{ l.text }}</dd>
								</div>
							</dl>
						</section>
					</template>

					<!-- Events -->
					<template v-else-if="tab === 'events' && active.status === 'ready'">
						<div class="mb-3 flex flex-wrap items-center gap-2">
							<h2
								class="text-base font-medium text-ink-gray-8"
								data-test="events-title"
							>
								{{ eventsTitle }}
							</h2>
							<Button
								v-if="applied.user"
								size="sm"
								variant="subtle"
								:label="__('Back to summary')"
								iconLeft="arrow-left"
								@click="backToSummary"
							/>
						</div>

						<div
							v-if="!ev.rows.length"
							class="flex flex-col items-center gap-2 py-12 text-center"
							role="status"
						>
							<FeatherIcon name="activity" class="size-8 text-ink-gray-4" />
							<span
								v-if="firstRun"
								class="text-base text-ink-gray-6"
								data-test="first-run"
								>{{
									__(
										"Nothing has been recorded yet. Activity appears here as people use Jarvis."
									)
								}}</span
							>
							<template v-else>
								<span class="text-base text-ink-gray-6">{{
									__("No activity in this range")
								}}</span>
								<span class="text-p-sm text-ink-gray-6">{{
									recordedFrom ||
									__("Try a wider date range, or clear the filters.")
								}}</span>
							</template>
						</div>
						<template v-else>
							<div
								class="overflow-x-auto rounded-lg border border-outline-gray-1"
								:class="wideClass(EVENTS_MIN)"
								data-test="events-table"
							>
								<div
									role="table"
									:aria-label="__('Events')"
									class="[.ua-wide_&]:min-w-[896px]"
								>
									<div role="rowgroup" class="hidden [.ua-wide_&]:block">
										<div
											role="row"
											class="grid grid-cols-[8rem_minmax(8rem,1.2fr)_minmax(10rem,1.8fr)_minmax(8rem,1.3fr)_4.5rem_6rem_5rem] gap-x-3 border-b bg-surface-gray-1 px-3 py-2 text-xs font-medium text-ink-gray-6"
										>
											<div role="columnheader">
												{{ __("When (site time)") }}
											</div>
											<div role="columnheader">{{ __("Person") }}</div>
											<div role="columnheader">{{ __("What") }}</div>
											<div role="columnheader">{{ __("Reference") }}</div>
											<div role="columnheader" class="text-right">
												{{ __("Records") }}
											</div>
											<div role="columnheader">{{ __("Outcome") }}</div>
											<div role="columnheader">{{ __("Channel") }}</div>
										</div>
									</div>
									<div role="rowgroup">
										<div
											v-for="(r, i) in ev.rows"
											:key="i"
											role="row"
											data-test="event-row"
											class="grid grid-cols-1 gap-x-3 gap-y-1 border-b px-3 py-3 last:border-b-0 [.ua-wide_&]:grid-cols-[8rem_minmax(8rem,1.2fr)_minmax(10rem,1.8fr)_minmax(8rem,1.3fr)_4.5rem_6rem_5rem] [.ua-wide_&]:items-center"
										>
											<!-- Below 769px a row stacks: every value carries its own label
											     (the column headers are hidden there), and a value that is
											     not there takes no line. -->
											<div role="cell" class="flex items-baseline gap-2">
												<span
													class="w-20 shrink-0 text-xs text-ink-gray-6 [.ua-wide_&]:hidden"
													data-test="cell-label"
													>{{ __("When") }}</span
												>
												<span class="text-xs text-ink-gray-6">{{
													formatWhen(r.at)
												}}</span>
											</div>
											<div
												role="cell"
												class="flex min-w-0 items-baseline gap-2"
											>
												<span
													class="w-20 shrink-0 text-xs text-ink-gray-6 [.ua-wide_&]:hidden"
													data-test="cell-label"
													>{{ __("Person") }}</span
												>
												<div class="min-w-0">
													<span
														class="block truncate text-sm text-ink-gray-8"
														:title="
															r.actor_name ||
															r.actor ||
															__('No person')
														"
														>{{
															r.actor_name ||
															r.actor ||
															__("No person")
														}}</span
													>
													<span
														v-if="
															r.actor &&
															r.actor_name &&
															r.actor_name !== r.actor
														"
														class="block truncate text-xs text-ink-gray-6"
														:title="r.actor"
														>{{ r.actor }}</span
													>
												</div>
											</div>
											<div
												role="cell"
												class="flex min-w-0 items-baseline gap-2"
											>
												<span
													class="w-20 shrink-0 text-xs text-ink-gray-6 [.ua-wide_&]:hidden"
													data-test="cell-label"
													>{{ __("What") }}</span
												>
												<div class="min-w-0">
													<span
														class="block truncate text-sm text-ink-gray-8"
														:title="
															r.label || activityLabel(r.activity)
														"
														>{{
															r.label || activityLabel(r.activity)
														}}</span
													>
													<span
														class="block truncate text-xs text-ink-gray-6"
														:title="activityLabel(r.activity)"
														>{{ activityLabel(r.activity) }}</span
													>
												</div>
											</div>
											<div
												role="cell"
												class="min-w-0 items-baseline gap-2 text-sm text-ink-gray-7 [.ua-narrow_&]:flex"
												:class="{
													'[.ua-narrow_&]:hidden': !(
														r.ref_doctype || r.ref_name
													),
												}"
											>
												<template v-if="r.ref_doctype || r.ref_name">
													<span
														class="w-20 shrink-0 text-xs text-ink-gray-6 [.ua-wide_&]:hidden"
														data-test="cell-label"
														>{{ __("Reference") }}</span
													>
													<span
														class="block min-w-0 truncate"
														:title="
															[r.ref_doctype, r.ref_name]
																.filter(Boolean)
																.join(' ')
														"
													>
														{{
															[r.ref_doctype, r.ref_name]
																.filter(Boolean)
																.join(" ")
														}}
													</span>
												</template>
											</div>
											<div
												role="cell"
												class="flex items-baseline gap-2 text-sm tabular-nums text-ink-gray-8 [.ua-wide_&]:justify-end"
											>
												<span
													class="w-20 shrink-0 text-xs text-ink-gray-6 [.ua-wide_&]:hidden"
													data-test="cell-label"
													>{{ __("Records") }}</span
												>
												<span>{{ formatCount(r.rows) }}</span>
											</div>
											<div
												role="cell"
												class="items-baseline gap-2 [.ua-narrow_&]:flex"
												:class="{ '[.ua-narrow_&]:hidden': !r.outcome }"
											>
												<template v-if="r.outcome">
													<span
														class="w-20 shrink-0 text-xs text-ink-gray-6 [.ua-wide_&]:hidden"
														data-test="cell-label"
														>{{ __("Outcome") }}</span
													>
													<Badge
														variant="subtle"
														:theme="outcomeTheme(r.outcome)"
														:label="outcomeLabel(r.outcome)"
													/>
												</template>
											</div>
											<div
												role="cell"
												class="items-baseline gap-2 text-xs text-ink-gray-6 [.ua-narrow_&]:flex"
												:class="{ '[.ua-narrow_&]:hidden': !r.channel }"
											>
												<template v-if="r.channel">
													<span
														class="w-20 shrink-0 text-xs text-ink-gray-6 [.ua-wide_&]:hidden"
														data-test="cell-label"
														>{{ __("Channel") }}</span
													>
													<span>{{ r.channel }}</span>
												</template>
											</div>
										</div>
									</div>
								</div>
							</div>

							<div class="mt-4 flex flex-col items-center gap-2">
								<p
									class="text-p-sm text-ink-gray-6"
									aria-live="polite"
									data-test="events-count"
								>
									{{ eventsCount }}
								</p>
								<p
									v-if="ev.moreError"
									class="text-p-sm text-ink-red-4"
									role="alert"
									data-test="more-error"
								>
									{{ ev.moreError }}
								</p>
								<Button
									v-if="ev.cursor"
									variant="subtle"
									:label="__('Load more')"
									:loading="ev.more === 'loading'"
									@click="loadMore"
								/>
							</div>
						</template>
					</template>
				</div>

				<footer
					class="mt-8 flex flex-wrap items-center gap-x-4 gap-y-2 border-t pt-4 text-sm"
				>
					<a
						:href="exportUrl"
						target="_blank"
						rel="noopener"
						class="inline-flex items-center gap-1.5 rounded text-ink-gray-7 outline-none hover:text-ink-gray-9 hover:underline focus-visible:ring-2 focus-visible:ring-outline-gray-3"
						data-test="export-link"
					>
						{{ __("Export to Excel in the Desk report") }}
						<FeatherIcon name="external-link" class="size-4" aria-hidden="true" />
						<span class="sr-only">{{ __("(opens in a new tab)") }}</span>
					</a>
				</footer>
			</div>
		</div>
	</div>
</template>

<script setup>
// The in-app twin of the Desk "Jarvis User Activity" report (plan 4.6): who did what
// through Jarvis, with red flags. One endpoint, two views.
//
// No client-side role guard: the endpoint is require_jarvis_admin() gated, so a non-admin
// who deep-links in sees the load error, not a silent redirect that hides why (the
// /billing precedent). The Settings entry is hidden from them, which is presentation only.
//
// What the page states about its numbers (scope, "kept for 90 days", totals still being
// prepared, logging warnings) is built once on the server (`notes`) and shared with the
// Desk report, so the two never disagree. Times are shown as stored, in the site's clock.
import { ref, reactive, computed, watch, nextTick, onMounted, onBeforeUnmount } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Autocomplete, Badge, Breadcrumbs, Button, FeatherIcon, FormControl } from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
import Banner from "@/components/Banner.vue";
import { useLinkSearch } from "@/composables/useLinkSearch";
import { getUserActivity } from "@/api/userActivity";
import { searchUsers } from "@/api/agents";
import { errMessage } from "@/lib/errors";
import { __ } from "@/lib/i18n";
import {
	activityOptions,
	outcomeOptions,
	summaryColumns,
	activityLabel,
	defaultRange,
	earliestEvent,
	flagTheme,
	formatCount,
	formatDay,
	formatWhen,
	legendLines,
	outcomeLabel,
	outcomeTheme,
	queryFromState,
	reportUrl,
	stateFromQuery,
	TAB_KEYS,
} from "./activityFormat";

// Labels are read when the page renders (not here at import), so the translator is ready.
const tabs = () => [
	{ key: "summary", label: __("Summary") },
	{ key: "events", label: __("Events") },
];
const TABS = computed(tabs);
const columns = computed(summaryColumns);

// What the inputs hold (draft) versus what the data on screen was fetched with (applied):
// the filters only take effect on Apply, and a person click or a tab switch always reads
// `applied`, so the numbers shown and the filters stated never disagree.
//
// The applied filters and the tab live in the URL (?tab=&from=&to=&user=&activity=&outcome=)
// so a view can be linked, survives a reload, and Back / Forward step through it. The page
// reads the query once on open, and again whenever the query changes to something other than
// what the page itself last wrote (that is a Back / Forward or a pasted link). A value that
// does not validate is ignored and the default stands in.
const route = useRoute();
const router = useRouter();
const DEFAULTS = defaultRange();
const initial = stateFromQuery(route.query, DEFAULTS);
const draft = reactive({ ...initial.filters, userLabel: "" });
const applied = ref({ ...draft });
const rangeError = ref("");

const tab = ref(initial.tab);
const panel = ref(null);

// ── table or cards: by the width of the CONTENT area, not the window ────────────────────
// With the sidebar open a 1100px window leaves the page well under what the tables need, so
// a viewport breakpoint would scroll them sideways. Each table renders as a table only when
// the panel is at least as wide as its minimum (grid tracks + gaps + padding + border) and as
// stacked, labelled cards otherwise. The wrapper gets `ua-wide` or `ua-narrow` and the
// classes inside key off it (`[.ua-wide_&]:`), so there is no sideways scroll at any width.
// Width 0 means "not measured" (no layout engine): the table is the default then.
const SUMMARY_MIN = 1028; // summary grid ~1020px + 2px border
const EVENTS_MIN = 900; // events grid ~888px + 2px border
const panelWidth = ref(0);
let panelObserver = null;
const measurePanel = () => {
	panelWidth.value = (panel.value && panel.value.clientWidth) || 0;
};
const wideClass = (min) =>
	!panelWidth.value || panelWidth.value >= min ? "ua-wide" : "ua-narrow";
onMounted(() => {
	measurePanel();
	if (panel.value && typeof ResizeObserver !== "undefined") {
		panelObserver = new ResizeObserver(measurePanel);
		panelObserver.observe(panel.value);
	}
});
onBeforeUnmount(() => panelObserver && panelObserver.disconnect());
const tabButtons = ref([]);

// One record of state per tab. `key` is the request the shown data answers; a tab whose
// key differs from the applied filters reloads when it is next shown.
const sum = reactive({ status: "idle", error: "", key: "", pending: "", data: null });
const ev = reactive({
	status: "idle",
	error: "",
	key: "",
	pending: "",
	rows: [],
	cursor: null,
	notes: [],
	thresholds: null,
	preparing: false,
	health: null,
	partial: false,
	firstEventAt: null,
	more: "idle",
	moreError: "",
});
const active = computed(() => (tab.value === "summary" ? sum : ev));

// ── person picker (the Access editor's, over the same admin-gated search) ──────────────
const userSearch = useLinkSearch((q) => searchUsers(q), {
	debounceMs: 200,
	mapper: (rows) =>
		(rows || []).map((r) => ({
			value: r.name,
			label: r.full_name || r.name,
			description: r.full_name && r.full_name !== r.name ? r.name : "",
		})),
});
const personValue = computed(() =>
	draft.user ? { value: draft.user, label: draft.userLabel || draft.user } : null
);
const personTrigger = ref(null);
function pickPerson(opt) {
	draft.user = opt && opt.value != null ? String(opt.value) : "";
	draft.userLabel = (opt && opt.label) || "";
	// The picker's search box had focus and is hidden by the choice, which would drop focus
	// to <body>: put it back on the trigger. Twice, because the popover finishes closing
	// after the next render.
	const back = () => personTrigger.value && personTrigger.value.focus();
	nextTick(back);
	setTimeout(() => {
		if (!document.activeElement || document.activeElement === document.body) back();
	}, 0);
}

// ── requests ────────────────────────────────────────────────────────────────────────────
function argsFor(view, f, cursor) {
	const args = { view, from: f.from, to: f.to, user: f.user };
	if (view === "events") {
		args.activity = f.activity;
		args.outcome = f.outcome;
		if (cursor) args.cursor = cursor;
	}
	return args;
}
const keyFor = (view, f) => JSON.stringify(argsFor(view, f));

let sumSeq = 0;
async function loadSummary() {
	const seq = ++sumSeq;
	const f = { ...applied.value };
	sum.status = "loading";
	sum.pending = keyFor("summary", f);
	sum.error = "";
	try {
		const res = await getUserActivity(argsFor("summary", f));
		if (seq !== sumSeq) return;
		sum.data = res || {};
		sum.key = keyFor("summary", f);
		sum.status = "ready";
	} catch (e) {
		if (seq !== sumSeq) return;
		sum.error = errMessage(e, __("Could not load user activity."));
		sum.status = "error";
	}
}

let evSeq = 0;
async function loadEvents() {
	const seq = ++evSeq;
	const f = { ...applied.value };
	ev.status = "loading";
	ev.pending = keyFor("events", f);
	ev.error = "";
	ev.more = "idle";
	ev.moreError = "";
	try {
		const res = (await getUserActivity(argsFor("events", f))) || {};
		if (seq !== evSeq) return;
		ev.rows = res.rows || [];
		ev.cursor = res.next_cursor || null;
		ev.notes = res.notes || [];
		ev.thresholds = res.thresholds || null;
		ev.preparing = !!res.preparing;
		// Only sent by a server that has them; the banner severity falls back on these when
		// the Summary has not been loaded (a deep link straight to Events).
		ev.health = res.logging_health || null;
		ev.partial = !!res.partial;
		ev.firstEventAt = res.first_event_at || null;
		ev.key = keyFor("events", f);
		ev.status = "ready";
	} catch (e) {
		if (seq !== evSeq) return;
		ev.error = errMessage(e, __("Could not load user activity."));
		ev.status = "error";
	}
}

async function loadMore() {
	if (!ev.cursor || ev.more === "loading") return;
	const seq = ++evSeq;
	const f = { ...applied.value };
	ev.more = "loading";
	ev.moreError = "";
	try {
		const res = (await getUserActivity(argsFor("events", f, ev.cursor))) || {};
		if (seq !== evSeq) return;
		ev.rows = ev.rows.concat(res.rows || []);
		ev.cursor = res.next_cursor || null;
		ev.more = "idle";
	} catch (e) {
		if (seq !== evSeq) return;
		ev.moreError = errMessage(e, __("Could not load more events."));
		ev.more = "error";
	}
}

function ensureLoaded() {
	const view = tab.value;
	const st = view === "summary" ? sum : ev;
	const want = keyFor(view, applied.value);
	// Already on its way for THESE filters. A request still in flight for older ones does
	// not count: Apply while another tab was showing leaves it behind, and its answer would
	// be dropped as stale, leaving this tab loading forever.
	if (st.status === "loading" && st.pending === want) return;
	if (st.status === "ready" && st.key === want) return;
	return view === "summary" ? loadSummary() : loadEvents();
}
function reload() {
	return tab.value === "summary" ? loadSummary() : loadEvents();
}

// ── the URL ─────────────────────────────────────────────────────────────────────────────
const stateQuery = () => queryFromState({ tab: tab.value, filters: applied.value });
const canonical = (query) => JSON.stringify(queryFromState(stateFromQuery(query, DEFAULTS)));

// A new history entry for the applied filters + tab, unless the URL already says so.
function syncQuery() {
	if (canonical(route.query) === JSON.stringify(stateQuery())) return;
	Promise.resolve(router.push({ query: stateQuery() })).catch(() => {});
}

// Back / Forward / a pasted link: the query changed to something this page did not write.
watch(
	() => route.query,
	(q) => {
		if (route.name && route.name !== "UserActivity") return;
		const st = stateFromQuery(q, DEFAULTS);
		if (JSON.stringify(queryFromState(st)) === JSON.stringify(stateQuery())) return;
		const same = st.filters.user === applied.value.user;
		applied.value = { ...st.filters, userLabel: same ? applied.value.userLabel : "" };
		Object.assign(draft, applied.value);
		rangeError.value = "";
		tab.value = st.tab;
		ensureLoaded();
	}
);

// ── filters ─────────────────────────────────────────────────────────────────────────────
// Apply is off while the shown tab is loading (a second Apply would just drop the first
// answer), and Enter in a field must not get around that.
const busy = computed(
	() => active.value.status === "loading" || (tab.value === "events" && ev.more === "loading")
);
function apply() {
	if (busy.value) return;
	if (draft.from && draft.to && draft.from > draft.to) {
		rangeError.value = __("From must be on or before To.");
		return;
	}
	rangeError.value = "";
	applied.value = { ...draft };
	syncQuery();
	// The other tab's data answers the old filters: forget it, so it reloads when shown.
	if (tab.value === "summary") ev.key = "";
	else sum.key = "";
	return reload();
}

// ── tabs ────────────────────────────────────────────────────────────────────────────────
async function select(key, { focusPanel = false } = {}) {
	tab.value = key;
	syncQuery();
	await nextTick();
	if (focusPanel && panel.value) panel.value.focus();
}
watch(tab, () => ensureLoaded(), { flush: "post" });

function onTabKeydown(e) {
	const keys = TAB_KEYS;
	const at = keys.indexOf(tab.value);
	let to = -1;
	if (e.key === "ArrowRight") to = (at + 1) % keys.length;
	else if (e.key === "ArrowLeft") to = (at - 1 + keys.length) % keys.length;
	else if (e.key === "Home") to = 0;
	else if (e.key === "End") to = keys.length - 1;
	if (to < 0) return;
	e.preventDefault();
	select(keys[to]).then(() => {
		const btn = (tabButtons.value || []).find((b) => b.id === "ua-tab-" + keys[to]);
		if (btn) btn.focus();
	});
}

// ── person drill-down: Events for one person, the applied dates kept ─────────────────────
// Only the person changes: edits typed into the other filters but not yet applied stay in
// the inputs (they are still waiting for Apply), and the fetch reads `applied`.
function drill(p) {
	if (!p.user) return;
	applied.value = { ...applied.value, user: p.user, userLabel: p.name || p.user };
	draft.user = p.user;
	draft.userLabel = p.name || p.user;
	ev.key = "";
	return select("events", { focusPanel: true });
}
function backToSummary() {
	applied.value = { ...applied.value, user: "", userLabel: "" };
	draft.user = "";
	draft.userLabel = "";
	sum.key = "";
	ev.key = "";
	return select("summary", { focusPanel: true });
}

// ── what the page says about the data ───────────────────────────────────────────────────
const people = computed(() => (sum.data && sum.data.people) || []);
const flaggedCount = computed(() => people.value.filter((p) => (p.flags || []).length).length);
const thresholds = computed(() => (sum.data && sum.data.thresholds) || ev.thresholds);
const legend = computed(() => legendLines(thresholds.value));

// notes[0] is the scope line, unless the totals are still being prepared (then the one
// note is that, and it is shown as the banner).
const notesOf = (st) => {
	const notes =
		(st.status === "ready" && (st === sum ? sum.data && sum.data.notes : ev.notes)) || [];
	const preparing = st === sum ? sum.data && sum.data.preparing : ev.preparing;
	return { scope: preparing ? "" : notes[0] || "", rest: preparing ? notes : notes.slice(1) };
};
// The scope line is the active tab's own: blank while it loads, never the other tab's.
const scopeLine = computed(() => notesOf(active.value).scope);
const bannerNotes = computed(() => notesOf(active.value).rest);
// A problem with the logging itself or totals that leave days out is a warning; "still
// being prepared" and "kept for 90 days" are information. The health flags come from the
// last Summary (the Events response carries only the notes), so the banner has the same
// severity on both tabs; a deep link straight to Events uses what that response carries.
const bannerType = computed(() => {
	const d = sum.data || { logging_health: ev.health, partial: ev.partial };
	const h = d.logging_health || {};
	return h.warn ||
		h.app_warn ||
		h.scheduler_disabled ||
		(h.rollup_lag_days || 0) > 1 ||
		d.partial
		? "warning"
		: "info";
});

// First-run / empty-state copy. `first_event_at` is { agent, app } (null when nothing of
// that kind was ever recorded); a server that does not send it leaves the plain hint.
const firstEvent = computed(() => (sum.data && sum.data.first_event_at) || ev.firstEventAt);
const preparingNow = computed(() =>
	tab.value === "summary" ? !!(sum.data && sum.data.preparing) : ev.preparing
);
const firstRun = computed(
	() => !!firstEvent.value && !earliestEvent(firstEvent.value) && !preparingNow.value
);
const recordedFrom = computed(() => {
	const day = earliestEvent(firstEvent.value);
	return day && applied.value.from && applied.value.from < day
		? __("Activity is recorded from {0}.", [formatDay(day)])
		: "";
});
const eventsCount = computed(() =>
	ev.rows.length === 1
		? __("Showing 1 event")
		: __("Showing {0} events", [formatCount(ev.rows.length)])
);
const eventsTitle = computed(() =>
	applied.value.user
		? __("Events for {0}", [applied.value.userLabel || applied.value.user])
		: __("Events")
);
const summaryCount = computed(() => {
	const n = people.value.length;
	const flagged = flaggedCount.value;
	if (flagged) {
		return n === 1
			? __("1 person, {0} flagged", [flagged])
			: __("{0} people, {1} flagged", [n, flagged]);
	}
	return n === 1 ? __("1 person") : __("{0} people", [n]);
});

// ── footer ──────────────────────────────────────────────────────────────────────────────
const exportUrl = computed(() => reportUrl({ view: tab.value, ...applied.value }));

ensureLoaded();
</script>
